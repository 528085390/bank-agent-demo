"""The outer conversation view reaches a module-owned graph, and nothing else does.

What this file proves is the half of the memory contract the base owns: the view a domain
handler can read during its call, the scope that bounds it, and the bounded write-back that
survives the call. What wealth *does* with the view is `tests/wealth/test_context_handoff.py`.

The domain here is invented on purpose. Driving the real wealth agent would prove both layers at
once and neither one clearly: a failure could be the handoff, the wealth prompt, or the bank
double. The spy below records exactly what the base published and nothing more.

The turns run through `ConversationTurns`, not through a hand-built state: admission, the
ledger, the `TurnContext` and the assembler are the production ones, because the whole point is
that the memory arrives over the real dispatch path. The module's own graph is the one
`tests/support/handler_graph.py` builds around the spy.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.agents.supervisor import _merged_domain_context
from app.agents.turn import TurnHandler, TurnResult, TurnStatus
from app.context.budget import Budget
from app.context.handoff import (
    MAX_CONTEXT_RECORDS,
    DomainView,
    Handoff,
    current_handoff,
    validate_context_update,
)
from app.integration.model_client import ModelReply
from app.modules.base import ModuleSpec
from app.modules.registry import build_registry
from app.storage.models import ConversationMessage
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.handler_graph import handler_module
from tests.support.scripted_model import RoutingModel, chunks
from tests.test_conversation_api import client_for as conversation_client
from tests.test_conversation_api import create, final_text, submit

# A budget a test can actually reach: the summary trigger has to be low enough that a handful of
# short messages crosses it, and the retained budget small enough that a strip really happens.
TIGHT = Budget(
    model_context_limit=4000,
    reserved_output_tokens=0,
    safety_margin=0,
    summary_trigger_ratio=0.5,
    recent_message_budget=400,
    summary_max_tokens=100,
)

ROUTE = ("call", "handoff", '{"agent_id":"wealth","instruction":"办事"}')
FINAL = "好"

# What a domain prompt's non-history part costs. The exact number does not matter here; what
# matters is that the domain supplies it, because only the domain knows its own prompt.
OVERHEAD = "系统提示：" + "理财" * 40

DECLARATION = DomainDeclaration(agent_id="wealth", display="理财 Agent", purpose="理财产品与持仓")


class MemorySpy:
    """A domain handler that records the view it was handed and publishes memory.

    `updates` is a per-call script: the n-th call publishes the n-th entry, so a test can make
    one turn write memory and assert what the next turn can read.
    """

    def __init__(self, updates: list[dict[str, Any] | None] | None = None) -> None:
        self._updates = list(updates or [])
        self.views: list[DomainView] = []
        self.messages: list[str] = []
        self.scope_inside_call: list[Handoff | None] = []
        self.scope_after_call: list[Handoff | None] = []

    def __call__(self, context: TrustedContext, model: Any, message: str) -> TurnResult:
        del context, model
        handoff = current_handoff()
        assert handoff is not None, "a domain call must run inside a conversation scope"
        self.messages.append(message)
        self.views.append(handoff.view(prompt_overhead=OVERHEAD))
        if self._updates:
            update = self._updates.pop(0)
            if update is not None:
                handoff.publish(update)
        # Read back inside the call: a scope that had already been reset would show up here.
        self.scope_inside_call.append(current_handoff())
        return TurnResult(status=TurnStatus.COMPLETED, summary=f"收到：{message}")

    def record_after(self) -> None:
        """Called once the whole conversation is done, to prove the scope did not outlive it."""

        self.scope_after_call.append(current_handoff())


class CrashSpy(MemorySpy):
    """Publishes memory and then fails, so its write-back can be shown to be dropped with it."""

    def __call__(self, context: TrustedContext, model: Any, message: str) -> TurnResult:
        del context, model, message
        handoff = current_handoff()
        assert handoff is not None
        handoff.publish({"goal": "崩溃前写下的目标"})
        raise RuntimeError("the domain blew up")


def spy_module(spy: Any) -> ModuleSpec:
    return handler_module("wealth", DECLARATION, spy)


def memory_agent(rt, spy: Any, *, budget: Budget = TIGHT, decisions: int = 16) -> AgentRuntime:
    """The production dispatch path, with the spy as the only module and a reachable budget.

    The model is a `RoutingModel` rather than a bare script because the assembler's summary call
    goes to the same model: with a plain script, the summary would consume a decision and the
    graph would fail for a reason that has nothing to do with memory.
    """

    registry = build_registry([spy_module(spy)])
    gateway = ToolGateway(registry.catalog, RecordingAdapter({}))
    agent = AgentRuntime(
        RoutingModel([item for _ in range(decisions) for item in (ROUTE, FINAL)]),
        graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
            model,
            gateway,
            context,
            assembler=assembler,
            turn=turn,
            roster=registry.roster,
        ),
        checkpointer=InMemorySaver(),
        assembler_factory=lambda user_id, turn, model: rt.build_context_assembler(
            user_id, turn, model, budget=budget
        ),
    )
    rt.agent = agent
    return agent


def talk(
    rt,
    session,
    *,
    messages: list[str],
    spy: Any,
    budget: Budget = TIGHT,
) -> tuple[dict, Any]:
    """One conversation, one message per entry, over the real HTTP turn lifecycle."""

    agent = memory_agent(rt, spy, budget=budget, decisions=2 * len(messages) + 2)
    client = conversation_client(rt, session)
    created = create(client, "记忆")
    for message in messages:
        assert final_text(submit(client, created["conversation_id"], message)) == "好"
    spy.record_after()
    return created, agent


# ---------------------------------------------------------------------- 记忆到达领域模型


def test_the_domain_handler_still_takes_three_arguments():
    """The callable shape a module handler is written against.

    `app/agents/turn.py` uses `from __future__ import annotations`, so the callable's arguments
    are the annotation strings, not the classes: comparing them as strings is what the module
    actually declares.
    """

    assert TurnHandler.__args__[:3] == ("TrustedContext", "ModelClient", str)
    assert TurnHandler.__args__[3] is TurnResult


def test_a_later_turn_sees_the_earlier_round_and_its_own_words(rt, session):
    """M01：首轮说了预算和期限，次轮"按刚才的条件"时领域模型必须真的看得到。"""

    spy = MemorySpy()
    _created, agent = talk(
        rt,
        session,
        messages=["我打算投 2000 元，三个月后用", "按刚才的条件推荐"],
        spy=spy,
    )

    assert len(spy.views) == 2
    first, second = spy.views
    assert first.current_user_text == "我打算投 2000 元，三个月后用"
    # 首轮没有更早的对话：历史是空的，不是"（暂无）"之类的占位文本。
    assert first.history_text == ""
    assert first.recent_messages == ()
    assert first.domain_context == {}

    assert second.current_user_text == "按刚才的条件推荐"
    assert "我打算投 2000 元，三个月后用" in second.history_text
    assert [message.role for message in second.recent_messages] == ["user", "assistant"]
    assert [message.sequence for message in second.recent_messages] == [1, 2]
    assert all(message.message_id for message in second.recent_messages)

    # 主模型的提示词里也有同一段上下文：M01 要求两边都看得到，不是只有领域侧。
    decisions = agent.model.decision_prompts
    assert any("我打算投 2000 元，三个月后用" in prompt for prompt in decisions[1:])


def test_the_message_being_answered_is_not_repeated_in_the_history(rt, session):
    """当前原话走第三个参数，就不能同时出现在历史里——同一句话会在一份提示词里出现两次。"""

    spy = MemorySpy()
    talk(rt, session, messages=["先看看产品", "再看一次"], spy=spy)

    second = spy.views[1]
    assert second.current_user_text == "再看一次"
    assert "再看一次" not in second.history_text
    assert "先看看产品" in second.history_text


def test_a_new_conversation_starts_with_no_history(rt, session):
    """M04：同一登录会话里的第二个聊天看不到第一个的历史。"""

    spy = MemorySpy()
    memory_agent(rt, spy, decisions=8)
    client = conversation_client(rt, session)
    first = create(client, "第一段")
    assert final_text(submit(client, first["conversation_id"], "我打算投 2000 元")) == "好"
    second = create(client, "第二段")
    assert final_text(submit(client, second["conversation_id"], "刚才说了什么？")) == "好"

    assert spy.views[0].conversation_id != spy.views[1].conversation_id
    assert spy.views[1].history_text == ""
    assert "2000" not in spy.views[1].history_text


def test_a_foreign_conversation_id_is_refused_before_any_memory_is_built(rt, session):
    """M05：归属检查在外层，伪造 conversation_id 读不到别人的历史，也进不了领域。"""

    spy = MemorySpy()
    memory_agent(rt, spy, decisions=4)
    client = conversation_client(rt, session)
    created = create(client, "自己的")
    assert final_text(submit(client, created["conversation_id"], "我打算投 2000 元")) == "好"

    forged = submit(client, f"conv_{uuid4().hex}", "她刚才说了什么？")

    assert forged.status_code >= 400
    # 只有第一次那条消息到达过领域模型：被拒绝的请求根本没有进图。
    assert len(spy.views) == 1


def test_a_long_conversation_reaches_the_domain_with_a_summary(rt, session):
    """M06：摘要覆盖较早的轮次，领域模型看到的是摘要加保留的近期轮次。"""

    spy = MemorySpy()
    talk(
        rt,
        session,
        messages=[f"第{index}个问题：" + "理财" * 100 for index in range(1, 11)],
        spy=spy,
    )

    last = spy.views[-1]
    assert last.summary_text == "历史摘要", "长会话必须已经写出摘要，否则这条断言毫无意义"
    assert last.estimated_tokens > 0
    # 摘要覆盖的那部分不再逐字出现在保留的近期轮次里，但摘要本身进了视图。
    assert len(last.recent_messages) < 20


# ---------------------------------------------------------------------- 作用域与隔离


def test_the_scope_lives_exactly_as_long_as_the_call(rt, session):
    """正常退出不能把作用域留给下一个请求。"""

    spy = MemorySpy()
    talk(rt, session, messages=["办件事"], spy=spy)

    assert spy.scope_inside_call[0] is not None
    assert spy.scope_after_call == [None]
    assert current_handoff() is None


def test_a_crashed_turn_leaves_no_scope_and_no_memory(rt, session):
    """崩溃的领域调用既不能留下作用域，也不能把半截记忆写进检查点。"""

    spy = CrashSpy()
    memory_agent(rt, spy, decisions=4)
    client = conversation_client(rt, session)
    created = create(client, "崩溃")
    assert final_text(submit(client, created["conversation_id"], "办件事")) == "好"

    assert current_handoff() is None
    with rt.database.sessions() as db:
        rows = db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == created["conversation_id"])
            .order_by(ConversationMessage.sequence)
        ).all()
    # 用户那条与主模型的回复：领域崩溃没有在会话里留下任何东西。
    assert [row.role for row in rows] == ["user", "assistant"]


class ConcurrentModel:
    """A stateless double, so two overlapping turns cannot consume each other's script.

    A `RoutingModel` answers from a list, and two concurrent turns sharing one list would
    interleave their replies -- the test would then be measuring the script, not the scope. This
    double decides from the prompt instead: route while no domain result has been collected, and
    answer once one has. The summary call is answered the same way `RoutingModel` answers it.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, messages: tuple[dict, ...]) -> ModelReply:
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if RoutingModel.SUMMARY_MARKER in prompt:
            return ModelReply(text='{"summary": "历史摘要"}')
        return ModelReply(text=FINAL if '"turn_status"' in prompt else ROUTE)

    def stream(self, messages: tuple[dict, ...], *, tools: Any = ()) -> Any:
        # The same decision, read as the one decision path reads it. The summary marker never
        # reaches here: the assembler summarizes through `complete`.
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        yield from chunks([FINAL if '"turn_status"' in prompt else ROUTE])

    def close(self) -> None:
        return None


def test_two_conversations_running_at_once_do_not_share_memory(rt, session):
    """M11：并发请求各自读自己的会话，作用域不串线。"""

    spy = MemorySpy()
    registry = build_registry([spy_module(spy)])
    gateway = ToolGateway(registry.catalog, RecordingAdapter({}))
    rt.agent = AgentRuntime(
        ConcurrentModel(),
        graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
            model,
            gateway,
            context,
            assembler=assembler,
            turn=turn,
            roster=registry.roster,
        ),
        checkpointer=InMemorySaver(),
        assembler_factory=lambda user_id, turn, model: rt.build_context_assembler(
            user_id, turn, model, budget=TIGHT
        ),
    )
    client = conversation_client(rt, session)
    first = create(client, "A")
    second = create(client, "B")

    async def run():
        return await asyncio.gather(
            asyncio.to_thread(submit, client, first["conversation_id"], "A 的预算 2000 元"),
            asyncio.to_thread(submit, client, second["conversation_id"], "B 的预算 9000 元"),
        )

    responses = asyncio.run(run())
    for response in responses:
        assert final_text(response) == "好"

    assert len(spy.views) == 2
    seen = {view.conversation_id: view.current_user_text for view in spy.views}
    assert seen[first["conversation_id"]] == "A 的预算 2000 元"
    assert seen[second["conversation_id"]] == "B 的预算 9000 元"
    assert all(view.history_text == "" for view in spy.views)
    assert all(view.domain_context == {} for view in spy.views)


# ---------------------------------------------------------------------- 记忆回写


def test_memory_the_domain_publishes_comes_back_on_the_next_turn(rt, session):
    """M07：外层已持久化的语言记忆可恢复，领域下一轮就能读到自己的目标与已答项。"""

    spy = MemorySpy(
        updates=[
            {"goal": "给三个月后要用的 2000 元找产品", "answered": {"horizon_months": "3"}},
            None,
        ]
    )
    talk(rt, session, messages=["我打算投 2000 元，三个月后用", "按刚才的条件推荐"], spy=spy)

    assert spy.views[0].domain_context == {}
    assert spy.views[1].domain_context["goal"] == "给三个月后要用的 2000 元找产品"
    assert spy.views[1].domain_context["answered"] == {"horizon_months": "3"}


def test_a_memory_record_is_stamped_with_the_message_it_came_from(rt, session):
    """结构化引用要带来源：没有 message_id／sequence 的记录无法审计。"""

    spy = MemorySpy(updates=[{"references": {"product_id": "p_demo_cash"}}, None])
    created, _agent = talk(rt, session, messages=["看看演示灵活理财", "还是它"], spy=spy)

    with rt.database.sessions() as db:
        rows = db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == created["conversation_id"])
            .order_by(ConversationMessage.sequence)
        ).all()

    assert spy.views[1].domain_context["references"] == {"product_id": "p_demo_cash"}
    source = spy.views[1].context_sources["references"]
    assert source["sequence"] == rows[0].sequence
    assert source["message_id"] == rows[0].message_id


def test_memory_that_carries_authority_is_refused(rt, session):
    """记忆不是授权：确认、验证码、幂等键没有字段可走。"""

    spy = MemorySpy(
        updates=[
            {
                "goal": "允许的目标",
                "confirmed": True,
                "verification_code": "246810",
                "idempotent_key": "idem-1",
            },
            None,
        ]
    )
    talk(rt, session, messages=["办件事", "再办一次"], spy=spy)

    # 整条更新被拒：白名单外的字段不是被忽略，而是让这条记忆不被接受。
    assert spy.views[1].domain_context == {}


def test_a_malformed_update_is_dropped_without_failing_the_turn(rt, session):
    """记忆是优化：一条写坏的线索不能让一次已经正确的回答失败。"""

    spy = MemorySpy(updates=[{"goal": 12345}, None])
    talk(rt, session, messages=["办件事", "再办一次"], spy=spy)

    assert spy.views[1].domain_context == {}


def test_only_the_newest_records_are_kept_and_other_agents_survive():
    """记忆有界，而且一个领域的回合不会清掉另一个领域的记录。

    Called directly: through the graph the bound is invisible, because the merged view only
    ever shows the newest value per field.
    """

    previous = [
        {"payload": {"goal": f"第 {index} 轮"}, "source": {"sequence": index}}
        for index in range(MAX_CONTEXT_RECORDS + 3)
    ]
    state = {"domain_context": {"wealth": {"records": previous}, "transfer": {"records": []}}}

    merged = _merged_domain_context(
        state,
        "wealth",
        ({"payload": {"goal": "最新一轮"}, "source": {"sequence": 99}},),
    )

    assert merged is not None
    assert len(merged["wealth"]["records"]) == MAX_CONTEXT_RECORDS
    assert merged["wealth"]["records"][-1]["payload"] == {"goal": "最新一轮"}
    assert merged["transfer"] == {"records": []}


def test_nothing_is_written_when_there_is_nothing_to_remember():
    """没有可记的东西就不写这个通道：一次写空值会把别的领域的记录一起抹掉。"""

    assert _merged_domain_context({"domain_context": {}}, "wealth", ()) is None
    assert _merged_domain_context(None, "wealth", ()) is None


def test_the_standalone_session_path_still_runs_without_a_conversation(rt):
    """无作用域的独立调用保持可用：领域拿到 no_conversation 的视图，不报错。"""

    spy = MemorySpy()
    registry = build_registry([spy_module(spy)])
    gateway = ToolGateway(registry.catalog, RecordingAdapter({}))
    graph = build_agent_graph(
        RoutingModel([ROUTE, FINAL]),
        gateway,
        TrustedContext(user_id="u_1", session_id="s_1"),
        roster=registry.roster,
    ).compile(checkpointer=InMemorySaver())
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [event async for event in runtime.invoke("s_1", "u_1", "办件事", {})]

    events = asyncio.run(run())

    assert spy.views[0].conversation_id is None
    assert spy.views[0].reason == "no_conversation"
    assert spy.views[0].summary_text is None
    assert spy.views[0].current_user_text == "办件事"
    # 会话路径只有检查点记录，而当前这句话已经从历史里去掉：它走第三个参数。
    assert "办件事" not in spy.views[0].history_text
    assert [event.data["text"] for event in events if event.event == "final"] == ["好"]
    assert current_handoff() is None


def test_a_reused_key_replays_without_a_second_domain_call(rt, session):
    """M08：同一条消息重放不重复派发业务，也不重复建立记忆。

    The replay is a JSON receipt rather than a stream -- the one contract a client cannot guess
    -- so the second response is read as a receipt and not parsed for frames.
    """

    spy = MemorySpy()
    memory_agent(rt, spy, decisions=4)
    client = conversation_client(rt, session)
    created = create(client, "重放")
    key = f"test-{uuid4().hex}"
    first = submit(client, created["conversation_id"], "办件事", key=key)
    second = submit(client, created["conversation_id"], "办件事", key=key)

    assert final_text(first) == "好"
    assert "application/json" in second.headers["Content-Type"]
    assert second.json()["data"]["duplicate"] is True
    assert len(spy.views) == 1


# ---------------------------------------------------------------------- 白名单规则本身


def test_the_update_whitelist_raises_on_an_unknown_field():
    """白名单外的键抛错，而不是悄悄丢弃——否则一条停止工作的记忆没有任何信号。"""

    with pytest.raises(ValueError, match="不允许的字段"):
        validate_context_update({"goal": "好", "mfa_code": "123456"})


def test_the_update_whitelist_normalizes_what_it_keeps():
    assert validate_context_update({"goal": "  好  "}) == {"goal": "好"}
    assert validate_context_update({"goal": "x" * 1000})["goal"] == "x" * 400
    assert validate_context_update({"goal": "   "}) == {}
    assert validate_context_update({}) == {}
    assert validate_context_update({"answered": {"q1": "1"}}) == {"answered": {"q1": "1"}}
    assert validate_context_update({"references": {"product_id": "p_1"}}) == {
        "references": {"product_id": "p_1"}
    }
    assert validate_context_update({"shown": {"products": ["p_1", "p_2"]}}) == {
        "shown": {"products": ["p_1", "p_2"]}
    }
    # 非字符串的值没有字段可表达：它被丢掉，而不是被转成字符串。
    assert validate_context_update({"answered": {"q1": 1}}) == {}


def test_the_update_whitelist_raises_on_a_malformed_value():
    """形状错误也抛错：一个"看起来像"的授权字段不能因为值写错就被放行。"""

    with pytest.raises(ValueError):
        validate_context_update({"answered": "不是对象"})
    with pytest.raises(ValueError):
        validate_context_update({"shown": {"products": "p_1"}})
