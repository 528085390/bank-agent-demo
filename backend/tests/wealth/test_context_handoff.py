"""任务2：理财消费外层会话视图，并把结构化记忆交回去。

这一层证明四件事，全部离线（HTTP 传输替身 + ScriptModel），而且断言的是**模型实际收到的提示词**，
不是"DTO 里有这个字段"：

1. **历史真的进了提示词**：早期轮次、摘要、外层线索都出现在理财模型的那一份 prompt 里，而当前
   这句话只出现一次——它走 `TurnHandler` 的第三个参数，不再重复进历史。
2. **线索只是线索**：外层记下的"已答项"读回会话槽位时优先级最低（会话里的值覆盖它），读不出来
   的直接丢掉；余额、收益、风险、持仓仍然只能来自本轮的银行工具返回。
3. **会话键对齐**：跨轮状态住在检查点上，线程键就是会话编号（有会话时是 `conversation_id`，
   独立调用时是 `session_id`），跨会话的 pending 一律拒绝，也不会从记忆里恢复写权限。
4. **回写有界且非授权**：`context_update` 只带语义字段与来源引用，订单金额、冻结快照、pending 与
   幂等键都没有字段可以走。

`tests/test_domain_context_memory.py` 覆盖外层那一半（作用域、隔离、检查点写入），这一层只覆盖
理财怎么读它、怎么用它、以及怎么把自己记的东西交回去。
"""

from __future__ import annotations

import pytest

from app.context.handoff import CONTEXT_UPDATE_KEYS, Handoff
from app.context.ledger import LedgerMessage
from app.context.view import AssembledView
from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.contracts.context import (
    MEMORY_FIELDS,
    ConversationContext,
)
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.domain.rules import questionnaire_lines
from app.modules.wealth.llm.prompts import read_overhead
from app.modules.wealth.workflow.graph import _scoped_request
from app.tools.protocol import ErrorCode
from tests.wealth import support

CONVERSATION = "conv_wealth_1"
OTHER_CONVERSATION = "conv_wealth_2"

PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"
LATEST = "/wealth/risk-assessments/latest"


# ---------------------------------------------------------------------- 外层视图替身


class FakeAssembler:
    """按固定结果回答一次装配，并记下领域交上来的 prompt 开销。

    装配本身（预算、摘要、压缩）由 `tests/test_context_view.py` 与
    `tests/test_domain_context_memory.py` 覆盖；这里要证明的是理财**怎么用**装配结果，所以装配
    被换成一个确定的答案，而不是再测一遍预算算法。
    """

    def __init__(self, view: AssembledView) -> None:
        self.view = view
        self.overheads: list[str] = []
        self.calls = 0

    def assemble(
        self,
        *,
        conversation_id: str | None,
        current_sequence: int,
        prompt_overhead: str,
        transcript_fallback: str = "",
        aggressive: bool = False,
    ) -> AssembledView:
        del conversation_id, current_sequence, transcript_fallback, aggressive
        self.calls += 1
        self.overheads.append(prompt_overhead)
        return self.view


def ledger_rows(*pairs: tuple[str, str]) -> tuple[LedgerMessage, ...]:
    return tuple(
        LedgerMessage(
            sequence=index,
            role=role,
            content=content,
            message_kind="message",
            question_id=None,
            message_id=f"msg_{index}",
        )
        for index, (role, content) in enumerate(pairs, start=1)
    )


def assembled(
    rows: tuple[LedgerMessage, ...],
    *,
    summary: str | None = None,
    reason: str = "within_budget",
) -> AssembledView:
    return AssembledView(
        transcript="\n".join(f"{row.role}: {row.content}" for row in rows),
        summary_text=summary,
        estimated_tokens=len(summary or "") + sum(len(row.content) for row in rows),
        summary=None,
        retained_from_sequence=rows[0].sequence if rows else None,
        retained_to_sequence=rows[-1].sequence if rows else None,
        reason=reason,
        rows=rows,
    )


def handoff_for(
    rows: tuple[LedgerMessage, ...],
    *,
    conversation_id: str = CONVERSATION,
    summary: str | None = None,
    reason: str = "within_budget",
    domain_context: dict | None = None,
    current_sequence: int | None = None,
    current_message_id: str | None = None,
) -> tuple[Handoff, FakeAssembler]:
    """一个外层作用域，`domain_context` 按外层检查点里的记录信封包装。

    外层存的是 `{"payload": {...}, "source": {...}}` 这样的记录（见 `app.agents.state`），
    理财读到的才是拍平后的字段。测试按真实形状构造，才测得到那条拍平逻辑。
    """

    assembler = FakeAssembler(assembled(rows, summary=summary, reason=reason))
    records = (
        [{"payload": dict(domain_context), "source": {"message_id": "msg_1", "sequence": 1}}]
        if domain_context
        else []
    )
    handoff = Handoff(
        conversation_id=conversation_id,
        current_message_id=current_message_id or (f"msg_{len(rows)}" if rows else "msg_1"),
        current_sequence=current_sequence if current_sequence is not None else len(rows),
        current_user_text=rows[-1].content if rows else "",
        agent_id="wealth",
        assembler=assembler,
        domain_records=records,
    )
    return handoff, assembler


def drive(turn, context, handoff: Handoff, model, message: str):
    """跑一轮领域图：外层作用域交给图，跨轮状态由检查点带着。"""

    return turn.lap(model, message, handoff=handoff, context=context)


@pytest.fixture
def turn(double, clock, context) -> support.GraphTurn:
    """整轮处理器的图替身：一个检查点就是一个会话，进程生命周期内复用。"""

    return support.GraphTurn(double, context=context, clock=clock)


def _stub_reads(double):
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on("GET", PRODUCT, data=support.product_payload())
    double.on("GET", ACCOUNTS, data=[support.account_payload()])
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    return double


# ---------------------------------------------------------------------- 1. 历史进提示词


def test_the_prompt_carries_the_earlier_round_the_summary_and_the_current_words(
    double, turn, context
):
    rows = ledger_rows(
        ("user", "我打算投 2000 元，三个月后用"),
        ("assistant", "好的，记下了。"),
        ("user", "按刚才的条件推荐"),
    )
    handoff, assembler = handoff_for(rows, summary="用户的目标是三个月后用 2000 元理财。")
    model = support.ScriptModel(support.finish("收到。"))

    lap = drive(turn, context, handoff, model, "按刚才的条件推荐")

    assert lap.status.value == "completed"
    prompt = model.prompts[0]
    assert "我打算投 2000 元，三个月后用" in prompt
    assert "用户的目标是三个月后用 2000 元理财。" in prompt
    # 当前这句话是"本次请求"，只出现一次：历史里已经把它按 sequence 去掉了。
    assert prompt.count("按刚才的条件推荐") == 1
    assert "历史上下文" in prompt

    # 领域交上去的开销就是它自己提示词里不含历史的那一半，不是空串。
    assert assembler.calls == 1
    assert assembler.overheads[0] == read_overhead(questionnaire_lines())
    assert "理财子智能体的对话工作流" in assembler.overheads[0]


def test_an_empty_history_is_rendered_as_an_explicit_sentence(double, turn, context):
    """没有更早的对话时写明"没有"，而不是留一片空白让模型以为被截断了。"""

    handoff, _assembler = handoff_for(ledger_rows(("user", "有哪些产品？")), current_sequence=1)
    model = support.ScriptModel(support.finish("收到。"))

    drive(turn, context, handoff, model, "有哪些产品？")

    assert "（没有更早的对话）" in model.prompts[0]
    assert "（没有更早的对话摘要）" in model.prompts[0]
    assert "（没有外层记下的线索）" in model.prompts[0]


# ---------------------------------------------------------------------- 2. 线索只是线索


def test_memory_clues_are_injected_and_a_session_slot_wins_over_them(double, turn, context):
    """优先级：会话里已经有的值 > 外层线索。线索只补空缺，不改已经知道的东西。"""

    handoff, _assembler = handoff_for(
        ledger_rows(("user", "先随便聊聊")),
        domain_context={
            "goal": "给三个月后要用的 2000 元找产品",
            "answered": {"horizon_months": "9", "plan_amount_minor": "200000"},
            "shown": {"products": ["p_demo_cash", "p_demo_stable"]},
            "references": {"product_id": "p_demo_cash"},
        },
    )
    model = support.ScriptModel(
        support.set_slots({"horizon_months": 3}),
        support.finish("记下了。"),
    )

    drive(turn, context, handoff, model, "用钱时间是三个月")

    prompt = model.prompts[0]
    assert "上一次的任务目标：给三个月后要用的 2000 元找产品" in prompt
    assert "已经回答过的信息" in prompt
    assert "horizon_months=9" in prompt
    assert "上次按顺序展示过的 products：p_demo_cash、p_demo_stable" in prompt
    assert "上次涉及的业务引用（需要时重新核实）：product_id=p_demo_cash" in prompt

    # 第二轮：会话里已经有 horizon_months=3，外层的 9 不能再改它。
    second, _ = handoff_for(
        ledger_rows(("user", "用钱时间是三个月"), ("assistant", "记下了。")),
        domain_context={"answered": {"horizon_months": "9"}},
    )
    follow = support.ScriptModel(support.finish("好。"))
    drive(turn, context, second, follow, "那推荐一下")

    assert "horizon_months（用钱时间（月））：3" in follow.prompts[0]


def test_a_clue_that_cannot_be_read_is_dropped_rather_than_guessed(double, turn, context):
    """读不出来的线索丢掉，让模型重新问——不猜一个金额或期限。"""

    handoff, _assembler = handoff_for(
        ledger_rows(("user", "看看产品")),
        domain_context={
            "answered": {
                "horizon_months": "三个月",
                "plan_amount_minor": "大概两千",
                "nonsense_slot": "x",
            }
        },
    )
    model = support.ScriptModel(support.finish("收到。"))

    drive(turn, context, handoff, model, "看看产品")

    prompt = model.prompts[0]
    # 槽位清单里一个都没有恢复出来（含糊的金额与期限都不可用，未知槽位名不认识）。
    assert "本次会话已经知道的业务槽位（已经知道，不要再追问）：\n（还没有）" in prompt
    # 线索本身仍然原样展示给模型看：它是"用户说过什么"，不是可用槽位。
    assert "horizon_months=三个月" in prompt


def test_live_bank_facts_still_come_from_tools_not_from_memory(double, turn, context):
    """历史里有编号不等于本轮事实：查产品仍然要真的调用工具。"""

    _stub_reads(double)
    handoff, _assembler = handoff_for(
        ledger_rows(("user", "上次看的是演示灵活理财")),
        domain_context={"references": {"product_id": "p_demo_cash"}},
    )
    model = support.ScriptModel(
        support.call_read("wealth_product", {"product_id": "p_demo_cash"}),
        support.finish("期限 0 个月。", cited_ids=["p_demo_cash"]),
    )

    lap = drive(turn, context, handoff, model, "它期限多久？")

    assert lap.status.value == "completed"
    assert [call.path for call in double.calls] == [PRODUCT]


# ---------------------------------------------------------------------- 3. 会话键与隔离


def test_the_two_session_maps_use_the_same_scope_key(double, turn, context):
    """槽位与待办必须落在同一个会话键上：只写一边会让待办归属与槽位归属对不上。"""

    handoff, _assembler = handoff_for(ledger_rows(("user", "帮我推荐")), current_sequence=1)
    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"])
    )

    lap = drive(turn, context, handoff, model, "帮我推荐")

    assert lap.status.value == "waiting_input"
    # 业务待办与那只会话的问题都落在同一个检查点上（线程键就是会话编号）。
    assert lap.pending is not None
    assert lap.pending["missing"] == ["horizon_months"]
    assert (lap.question or {}).get("kind") == "input"
    assert (lap.question or {}).get("agent_id") == "wealth"
    assert "wealth_slots" in lap.state
    assert lap.slots == {}


def test_a_pending_from_another_conversation_is_never_answered(double, bank, clock, turn, context):
    """跨会话的 pending 一律拒绝：另一个聊天看不到、也回答不了这里的待办。"""

    first, _ = handoff_for(ledger_rows(("user", "帮我推荐")), current_sequence=1)
    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"])
    )
    lap = drive(turn, context, first, model, "帮我推荐")
    assert lap.status.value == "waiting_input"
    pending_id = lap.pending["pending_id"]

    # 另一个会话：这一句话是新消息，不是"回答"。
    second, _ = handoff_for(
        ledger_rows(("user", "帮我推荐")), conversation_id=OTHER_CONVERSATION, current_sequence=1
    )
    other_model = support.ScriptModel(support.finish("好的。"))
    drive(turn, context, second, other_model, "三个月")

    assert "你上一次向用户提了一个问题" not in other_model.prompts[0]

    # 直接拿另一个会话的待办编号来回答，也会被拒：这个编号在本会话里根本不存在。
    carrier = support.SessionCarrier()
    agent = build_wealth_agent(support.ScriptModel(support.finish("好的。")), bank, clock)
    refused = carrier.handle(
        agent,
        context,
        WealthRequest(kind="answer", text="三个月", pending_id=pending_id),
    )
    assert refused.status is WealthStatus.FAILED
    assert refused.error_code == ErrorCode.INVALID_STATE


def test_the_memory_of_one_conversation_is_not_visible_in_another(double, turn, context):
    """M04：同一登录会话里的两个聊天，历史与槽位互不串线。"""

    first, _ = handoff_for(
        ledger_rows(("user", "我打算投 2000 元")),
        domain_context={"answered": {"plan_amount_minor": "200000"}},
        current_sequence=1,
    )
    model = support.ScriptModel(support.finish("收到。"))
    drive(turn, context, first, model, "我打算投 2000 元")
    assert (
        "plan_amount_minor（计划投入金额（推荐筛选））：2000.00 元（200000 分）" in model.prompts[0]
    )

    second, _ = handoff_for(
        ledger_rows(("user", "你好")), conversation_id=OTHER_CONVERSATION, current_sequence=1
    )
    other = support.ScriptModel(support.finish("你好。"))
    drive(turn, context, second, other, "你好")

    # 第二个会话里既没有那段历史，也没有被恢复出来的槽位值（槽位名本身当然还在"缺什么"清单里）。
    assert "我打算投 2000 元" not in other.prompts[0]
    assert "2000.00 元" not in other.prompts[0]
    assert "本次会话已经知道的业务槽位（已经知道，不要再追问）：\n（还没有）" in other.prompts[0]


# ---------------------------------------------------------------------- 4. 回写非授权


def test_the_published_memory_carries_clues_and_never_an_order_amount(double, turn, context):
    """`context_update` 只有语义线索：单笔订单金额不进记忆，否则下一笔会"继承"上一笔。"""

    _stub_reads(double)
    handoff, _assembler = handoff_for(ledger_rows(("user", "我打算投 2000 元")), current_sequence=1)
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000, "horizon_months": 3}),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": "a_wealth_main", "amount_minor": 200000}
        ),
    )

    lap = drive(turn, context, handoff, model, "买 2000 元的演示灵活理财")

    # 预览变成一张等用户点击的确认卡片，但记忆照常交回去。
    assert lap.status.value == "waiting_confirmation"
    updates = lap.memory
    assert updates, "一次正常处理必须留下记忆"
    payload = updates[-1]["payload"]
    assert set(payload) <= MEMORY_FIELDS
    assert payload["answered"]["plan_amount_minor"] == "200000"
    assert payload["answered"]["horizon_months"] == "3"
    # 订单金额、冻结快照、pending 与幂等键都不在记忆里。
    assert "amount_minor" not in payload["answered"]
    assert "pending_id" not in payload
    assert "purchase_preview" not in payload
    assert "idempotency_key" not in payload
    # 来源引用是账本自己的那条消息。
    assert updates[-1]["source"]["message_id"] == "msg_1"


def test_a_restarted_turn_cannot_turn_memory_back_into_a_write(double, turn, clock, context):
    """临时 pending 丢了就是丢了：记忆只说明"之前预览过"，不构成任何写授权。"""

    _stub_reads(double)
    first, _ = handoff_for(ledger_rows(("user", "买 2000 元的演示灵活理财")), current_sequence=1)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": "a_wealth_main", "amount_minor": 200000}
        ),
    )
    drive(turn, context, first, model, "买 2000 元的演示灵活理财")
    preview_calls = len(double.calls)

    # 重启：同一个会话，但检查点是新的一张，待办不再跟着回来。
    restarted = support.GraphTurn(double, context=context, clock=clock)
    second, _ = handoff_for(
        ledger_rows(
            ("user", "买 2000 元的演示灵活理财"),
            ("assistant", "请确认本次申购信息。"),
            ("user", "确认"),
        ),
        domain_context={
            "goal": "买 2000 元的演示灵活理财",
            "answered": {"product_id": "p_demo_cash", "account_id": "a_wealth_main"},
        },
    )
    follow = support.ScriptModel(support.finish("请重新确认一次，之前的预览已经失效。"))

    lap = drive(restarted, context, second, follow, "确认")

    assert lap.status.value == "completed"
    # 一次银行调用都没有发生：没有确认、没有资金写、也没有任何"恢复授权"的路径。
    assert len(double.calls) == preview_calls
    assert lap.pending is None


def test_the_conversation_contract_refuses_authorization_fields():
    """记忆里出现授权字段是契约错误，不是"被忽略的字段"。"""

    with pytest.raises(ValueError, match="不允许的字段"):
        ConversationContext(conversation_id=CONVERSATION, domain_context={"notes": "x"})
    with pytest.raises(ValueError, match="不得携带授权字段"):
        ConversationContext(conversation_id=CONVERSATION, domain_context={"mfa_code": "123456"})
    with pytest.raises(ValueError, match="不得携带授权字段"):
        ConversationContext(conversation_id=CONVERSATION, domain_context={"pending_id": "wp_1"})
    with pytest.raises(ValueError):
        ConversationContext(conversation_id="")


def test_the_module_whitelist_mirrors_the_outer_one():
    """两侧各写一份白名单（两个包不能互相导入），所以必须有一条断言防止它们漂移。"""

    assert MEMORY_FIELDS == CONTEXT_UPDATE_KEYS


# ---------------------------------------------------------------------- 5. 独立调用


def test_the_standalone_path_needs_no_conversation(double, turn, context):
    """没有外层作用域时退回 `(user_id, session_id)`：CLI 那条路保持可用。"""

    model = support.ScriptModel(support.finish("收到。"))

    lap = turn.lap(model, "有哪些产品？", session=context.session_id)

    assert lap.status.value == "completed"
    assert "（没有更早的对话）" in model.prompts[0]

    # 没有会话时线程键是 (user_id, session_id)：同一登录会话的下一轮还看得见上一轮的槽位。
    follow = support.ScriptModel(
        support.set_slots({"horizon_months": 3}),
        support.finish("好。"),
    )
    again = turn.lap(follow, "用钱时间是三个月", session=context.session_id)

    assert again.slots == {"horizon_months": 3}


def test_a_handoff_goal_scopes_the_request_the_wealth_model_sees() -> None:
    """跨领域的一句话被拆成多个节点时，理财只应看到自己的那一段。"""

    state = {
        "goal": "查询用户名下可用账户的余额，请办理并返回结果",
        "messages": [{"role": "user", "content": "查一下我名下可用账户余额以及常用收款人"}],
    }

    assert _scoped_request(state) == "查询用户名下可用账户的余额，请办理并返回结果"


def test_an_independent_wealth_call_still_uses_the_user_message() -> None:
    """CLI 与领域单测没有总调度，入口仍是用户原话。"""

    state = {"messages": [{"role": "user", "content": "我活期账户里还有多少钱？"}]}

    assert _scoped_request(state) == "我活期账户里还有多少钱？"
