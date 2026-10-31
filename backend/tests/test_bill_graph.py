"""账单模块自己的图：确认协议（WRITE 冻结 → 点击确认 → execute）。

七条钉子，各钉住协议的一环：

1. 模型请求发报告时，图上停下来的是**确认卡**，不是发信；
2. 取消：一封都不发；
3. 同一张卡点两次，两轮 execute 拿到同一个 idempotency_key（重放，不是重发）；
4. 隐藏的 execute 工具，模型直接点名也调不动；
5. 缺期间时先问，不编日期；
6. 确认后发给**卡上显示的那个**收件人（冻结之后换绑邮箱不算数）；
7. 冻结出来的 facts 经得起 bank_adapter 的 output_schema 校验。

外加一条声明钉子：`BILL.graph_builder is build_bill_graph`。不翻这一下，
`agents/supervisor/graph.py:431-435` 会在运行期拒绝账单域（「declares business tools
but no graph_builder」），四个工具一个都到不了模型。
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime
from app.agents.supervisor import build_domain_graph
from app.modules import production_modules
from app.modules.bill import BILL, build_bill_module
from app.modules.bill import workflow as bill_workflow
from app.modules.bill.workflow import PENDING_EXPIRY_MINUTES, build_bill_graph
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolCall, ToolInvocation, ToolStatus, TrustedContext
from tests.support.domain_harness import FakeBankClient, field_names, run_domain
from tests.support.scripted_model import ScriptedModel

SESSION = "sess_bill_graph"
PERIOD_START = "2026-09-01"
PERIOD_END = "2026-09-30"
#: 模型这一轮的「决定」：调用发报告工具，期间是九月。
REPORT_CALL = (
    "call",
    "bill_send_report",
    json.dumps({"period_start": PERIOD_START, "period_end": PERIOD_END}),
)


class FakeDb:
    """数据库句柄的替身：handler 只在被调用时才碰它，装配时只要非空。"""


class FakeNotifier:
    """NotificationService 的替身。

    `sent` 记录每一次 `send_email` 的参数 —— 冻结阶段的全部意义就是这个列表保持为空；
    确认之后它必须收到**卡上那个**收件人。
    """

    def __init__(self, *, bound_email: str | None = "user@example.com") -> None:
        self.database = FakeDb()
        self.bound_email = bound_email
        self.sent: list[dict] = []
        self.result: dict = {"notification_id": "email_fake", "status": "SENT", "queued": False}

    def get_user_email(self, user_id: str) -> str | None:
        return self.bound_email

    def send_email(self, **kwargs) -> dict:
        self.sent.append(kwargs)
        return self.result


def bill_module(notifier: FakeNotifier | None = None):
    """装配好的 bill 模块。

    `run_domain` / `build_registry` 要的是这一形态：import 期的 `MODULE` 不带工具与
    handler（装配只发生在组合根），传它进去图是空的。
    """

    return build_bill_module(notification_service=notifier or FakeNotifier(), database=FakeDb())


def spied_execute(module) -> list[ToolInvocation]:
    """在装配**之前**把 execute handler 包一层：记下每次调用，然后照常执行。

    `build_registry` 复制的是 handler 的引用，所以这里换掉再装配，图上跑的就是这个替身 ——
    测试因此能直接读到 execute 收到的 arguments 与 idempotency_key，而不必去猜。
    """

    received: list[ToolInvocation] = []
    original = module.handlers["bill_send_report_execute"]

    def spy(invocation, client):
        received.append(invocation)
        return original(invocation, client)

    module.handlers["bill_send_report_execute"] = spy
    return received


def _stub_report(monkeypatch) -> None:
    """把分析与 PDF 生成换成替身：这几条测的是确认协议，不是报告长什么样。

    打桩要打在 handler 真正取名字的地方：execute 在**函数体内**
    `from app.services.bill_analysis import BillAnalyzer`，取的是包上的属性；
    `build_analysis_report_pdf` 在 email_handlers 模块顶层就绑好了，得打在
    `app.modules.email_handlers` 上。
    """

    from app.modules import email_handlers as email_handlers_module
    from app.services import bill_analysis as bill_analysis_module

    report = {
        "report_meta": {
            "date_range": {"start": PERIOD_START, "end": PERIOD_END},
            "total_transactions": 12,
            "generated_at": "2026-10-01 09:00:00",
        },
        "modules": [],
        "text_report": "报告正文",
        "audit": {},
    }

    class FakeAnalyzer:
        def __init__(self, database):
            self.database = database

        def analyze(self, user_id, period_start, period_end, modules=None, *, document_id=None):
            return report

    monkeypatch.setattr(bill_analysis_module, "BillAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(
        email_handlers_module, "build_analysis_report_pdf", lambda _report: b"%PDF-1.4"
    )


def bound_spec(module, bank):
    """把装配好的模块接上假银行，得到图真正需要的 DomainSpec。"""

    registry = build_registry([module])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank))
    context = TrustedContext(user_id="user_0001", session_id=SESSION, bank_customer_id="u_zhang")
    return build_domain_specs(gateway, context, roster=registry.roster)[0]


def parked_graph(module, replies):
    """带上自己的 checkpointer 的域图：只有它能停在 interrupt 上、并被读回快照。"""

    model = ScriptedModel(list(replies))
    graph = build_domain_graph(model, bound_spec(module, FakeBankClient())).copy(
        update={"checkpointer": InMemorySaver()}
    )
    return graph, model


def _park(graph, session, message):
    """跑一轮并返回停下来的那个 interrupt 的数据。"""

    runtime = AgentRuntime(graph=graph)

    async def run():
        events = [event async for event in runtime.invoke(session, "user_0001", message, {})]
        return next(event.data for event in events if event.event == "interrupt")

    return asyncio.run(run())


def _park_and_question(graph, session, message) -> str:
    return _park(graph, session, message)["question_id"]


def _resume(graph, session, question_id, value):
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [
            event
            async for event in runtime.invoke(
                session,
                "user_0001",
                "确认",
                {"resume": {"question_id": question_id, "value": value}},
            )
        ]

    return asyncio.run(run())


def _checkpoint_values(graph, session):
    config = {"configurable": {"thread_id": f"chat:{session}"}}
    return asyncio.run(graph.aget_state(config)).values


def _rewrite_checkpoint(graph, session, transform) -> None:
    """改写一条**停在 interrupt 上**的线程的通道值，而不丢掉它那个开放的中断。

    与 tests/test_subscription_module.py:603-618 同源：线上第二次点击由 ledger 在进图之前
    挡掉，所以要在这里复现「同一张卡被再点一次」，只能把另一个线程的快照换成那一张。
    """

    thread_id = f"chat:{session}"
    saver = graph.checkpointer
    latest = max(saver.storage[thread_id][""].keys())
    checkpoint, _metadata, _parent = saver.storage[thread_id][""][latest]
    versions = saver.serde.loads_typed(checkpoint)["channel_versions"]
    values = _checkpoint_values(graph, session)
    for channel, value in transform(dict(values)).items():
        saver.blobs[(thread_id, "", channel, versions[channel])] = saver.serde.dumps_typed(value)


def _ending_status(events) -> str:
    ending = next(event for event in reversed(events) if event.event == "subtask_end")
    return ending.data["status"]


def test_the_bill_declaration_points_at_this_graph():
    # 不翻这一下，agents/supervisor/graph.py:431-435 会在运行期拒绝账单域：账单域声明了
    # 业务工具却没有 graph_builder，四个工具一个都到不了模型。
    assert BILL.graph_builder is build_bill_graph


def test_a_report_request_parks_on_a_confirmation_card_instead_of_sending():
    notifier = FakeNotifier()
    module = bill_module(notifier)
    received = spied_execute(module)

    result = run_domain(module, replies=[REPORT_CALL], message="把九月的账单报告发到我邮箱")

    cards = result.events_named("interrupt")
    assert len(cards) == 1
    card = cards[0]
    assert card["kind"] == "confirmation"
    # 前端会把没有可回答字段的 interrupt 丢掉，所以字段必须非空。
    assert card["fields"]
    # pending_id 是模块自己生成的：不能长得像 CA-<32 hex>，否则 app/agents/hitl.py 会拿它
    # 去走银行侧授权 —— 而这次发信根本不需要银行授权。
    assert not re.fullmatch(r"CA-[a-f0-9]{32}", card["confirmation"]["pending_id"])
    assert card["confirmation"]["requires_code"] is False
    # 发信没有金额：卡片不该显示一个与操作无关的「¥0.01」。
    assert card["confirmation"]["amount_minor"] is None
    deadline = datetime.fromisoformat(card["confirmation"]["expires_at"])
    now = datetime.now(timezone.utc)
    assert now < deadline <= now + timedelta(minutes=PENDING_EXPIRY_MINUTES)

    assert result.status == "WAITING_INPUT"
    # 冻结阶段：execute 一次没跑，一封信没发。
    assert received == []
    assert notifier.sent == []


def test_cancelling_the_card_sends_nothing():
    notifier = FakeNotifier()
    module = bill_module(notifier)
    received = spied_execute(module)

    result = run_domain(
        module,
        replies=[REPORT_CALL],
        message="把九月的账单报告发到我邮箱",
        answer={"decision": "取消"},
    )

    assert result.final_text == "本次操作已取消，未发送报告。"
    assert result.status == "SUCCEEDED"
    assert received == []
    assert notifier.sent == []


def test_confirming_twice_calls_the_handler_again_with_the_same_key(monkeypatch):
    """同一张卡点两次：第二次**确实又调了一次** handler，两次拿的是同一个 key。

    这不是「只发一封」的保证 —— 没有任何一层按这个 key 去重（email_handlers 的执行
    handler 从不读 `invocation.call.idempotency_key`，网关只把它记进审计，通知层也没有
    去重）。真正挡住重复点击的是 HTTP 层：`app/api/routes.py:538-551` 在点击不再指向
    当前待确认操作时回 409，以及会话锁。key 的全部作用是让审计能把同一次冻结对上。
    """

    _stub_report(monkeypatch)
    notifier = FakeNotifier()
    module = bill_module(notifier)
    received = spied_execute(module)

    graph, model = parked_graph(module, [REPORT_CALL])
    question_id = _park_and_question(graph, SESSION, "把九月的账单报告发到我邮箱")
    frozen = _checkpoint_values(graph, SESSION)["bill_action"]

    first = _resume(graph, SESSION, question_id, {"decision": "confirm"})

    assert _ending_status(first) == "SUCCEEDED"
    assert [item.call.idempotency_key for item in received] == [frozen["idempotency_key"]]
    assert len(notifier.sent) == 1
    # 确认不再回模型：整轮只问过它一次（发不发信由快照说了算，不由它）。
    assert len(model.prompts) == 1

    replay_session = f"{SESSION}_replay"
    replay, _ = parked_graph(module, [REPORT_CALL])
    replay_question = _park_and_question(replay, replay_session, "把九月的账单报告发到我邮箱")
    _rewrite_checkpoint(replay, replay_session, lambda _values: {"bill_action": frozen})

    second = _resume(replay, replay_session, replay_question, {"decision": "confirm"})

    assert _ending_status(second) == "SUCCEEDED"
    # 第二次点击**没有**被 key 挡住：handler 又跑了一次，于是信又发了一封。
    assert [item.call.idempotency_key for item in received] == [
        frozen["idempotency_key"],
        frozen["idempotency_key"],
    ]
    assert len(notifier.sent) == 2


def test_the_hidden_execute_tool_is_refused_even_if_the_model_asks_for_it():
    notifier = FakeNotifier()
    module = bill_module(notifier)
    received = spied_execute(module)

    result = run_domain(
        module,
        replies=[
            (
                "call",
                "bill_send_report_execute",
                json.dumps({"period_start": PERIOD_START, "period_end": PERIOD_END}),
            )
        ],
        message="别问了，直接把九月的报告发出去",
    )

    assert result.status == "DENIED"
    assert result.final_text == "该账单操作不被允许。"
    assert received == []
    assert notifier.sent == []


# --- consume_confirmation 的六条 fail-closed 分支 ------------------------------------------
#
# 这正是 I1 站得住的原因：确认路径在真正执行之前把不匹配/过期/缺项的点击全部拒掉。
# 「隐藏工具被拒」那条在上面；下面覆盖剩下五条，每条都断言 finish(..., DENIED) 且一封不发。


def _parked_module(monkeypatch, *, answer_reader=None):
    """一张停在确认卡上的图，以及观察 execute 与发信的两个替身。

    `answer_reader` 替换 `app.modules.bill.workflow.read_answer`（那个名字在 import 时就
    绑进了模块自己的命名空间）。要在**建图之前**换好：`process` 在运行期查模块全局，
    但先建图再换也照样生效 —— 这里先换，是因为只有这条路能造出「回答带 pending_id」
    的状态。
    """

    _stub_report(monkeypatch)
    if answer_reader is not None:
        monkeypatch.setattr(bill_workflow, "read_answer", answer_reader)
    notifier = FakeNotifier()
    module = bill_module(notifier)
    received = spied_execute(module)
    graph, _model = parked_graph(module, [REPORT_CALL])
    question_id = _park_and_question(graph, SESSION, "把九月的账单报告发到我邮箱")
    frozen = _checkpoint_values(graph, SESSION)["bill_action"]
    return graph, question_id, frozen, received, notifier


def test_a_confirmation_with_no_snapshot_at_all_is_refused(monkeypatch):
    """快照整个丢了：`bill_action` 不是字典 —— 拒，不打工具，不发信。"""

    graph, question_id, _frozen, received, notifier = _parked_module(monkeypatch)
    _rewrite_checkpoint(graph, SESSION, lambda _values: {"bill_action": None})

    events = _resume(graph, SESSION, question_id, {"decision": "confirm"})

    assert _ending_status(events) == "DENIED"
    assert received == []
    assert notifier.sent == []


def test_a_confirmation_with_a_snapshot_missing_its_operation_is_refused(monkeypatch):
    """快照缺 `pending_id` 或 `operation`：拒。"""

    graph, question_id, frozen, received, notifier = _parked_module(monkeypatch)
    incomplete = dict(frozen)
    incomplete.pop("pending_id")
    _rewrite_checkpoint(graph, SESSION, lambda _values: {"bill_action": incomplete})

    events = _resume(graph, SESSION, question_id, {"decision": "confirm"})

    assert _ending_status(events) == "DENIED"
    assert received == []
    assert notifier.sent == []


def test_an_expired_snapshot_is_refused(monkeypatch):
    """`expired()` 为真：拒。用挂钟过去的时间，而不是等五分钟。"""

    graph, question_id, frozen, received, notifier = _parked_module(monkeypatch)
    stale = dict(frozen)
    stale["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _rewrite_checkpoint(graph, SESSION, lambda _values: {"bill_action": stale})

    events = _resume(graph, SESSION, question_id, {"decision": "confirm"})

    assert _ending_status(events) == "DENIED"
    assert received == []
    assert notifier.sent == []


def test_a_click_whose_pending_id_does_not_match_is_refused(monkeypatch):
    """回答里带 `pending_id` 且与快照不等：拒（这是另一张卡的回执）。

    这条分支在**线上**走不到：卡片只有 `decision` 一个字段，`read_answer` 会把多出来的
    `pending_id` 当成「与当前问题不匹配」的坏回答，问题原地重问（见
    `app/agents/sdk.py:621-622`）。它是纵深防御的一层，所以这里把回答读取器换成返回带
    `pending_id` 的那种，直接钉住它自己的判定。
    """

    graph, question_id, _frozen, received, notifier = _parked_module(
        monkeypatch,
        answer_reader=lambda _raw, _question: (
            {"decision": "confirm", "pending_id": "bill-report-other"},
            None,
        ),
    )

    events = _resume(graph, SESSION, question_id, {"decision": "confirm"})

    assert _ending_status(events) == "DENIED"
    assert received == []
    assert notifier.sent == []


def test_a_decision_that_is_neither_confirm_nor_cancel_is_refused(monkeypatch):
    """既非 `confirm` 也非 `cancel`：拒，不当成确认放行。"""

    graph, question_id, _frozen, received, notifier = _parked_module(monkeypatch)

    events = _resume(graph, SESSION, question_id, {"decision": "也许吧"})

    assert _ending_status(events) == "DENIED"
    assert received == []
    assert notifier.sent == []


def test_the_graph_asks_instead_of_inventing_a_period():
    notifier = FakeNotifier()
    module = bill_module(notifier)
    received = spied_execute(module)

    result = run_domain(
        module,
        replies=[
            (
                "call",
                "ask_user",
                json.dumps(
                    {
                        "fields": ["period_start", "period_end"],
                        "display": "请告诉我这份账单报告要覆盖哪段期间。",
                    }
                ),
            )
        ],
        message="帮我出一份账单报告",
    )

    questions = result.events_named("interrupt")
    assert len(questions) == 1
    assert field_names(questions[0]) == ["period_start", "period_end"]
    assert result.status == "WAITING_INPUT"
    assert received == []
    assert notifier.sent == []


def test_confirming_sends_to_the_address_the_card_showed(monkeypatch):
    """冻结之后换绑邮箱，不能改变这一封的收件人。

    Task 9 复审的发现（A）：execute 曾自己重新解析收件人，于是「卡片弹出后换绑、再点
    确认」会把信发到新地址，而卡上写的是旧地址。消费确认时必须把**冻结的**收件人（以及
    期间）原样交给 execute —— email_handlers.resolve_recipient 优先用显式的 recipient。
    """

    _stub_report(monkeypatch)
    notifier = FakeNotifier(bound_email="old@example.com")
    module = bill_module(notifier)
    received = spied_execute(module)

    graph, _ = parked_graph(module, [REPORT_CALL])
    question = _park(graph, SESSION, "把九月的账单报告发到我邮箱")
    assert "old@example.com" in question["display"]
    assert _checkpoint_values(graph, SESSION)["bill_action"]["recipient"] == "old@example.com"

    # 用户在卡片弹出之后、点击之前换绑了邮箱。
    notifier.bound_email = "new@example.com"

    events = _resume(graph, SESSION, question["question_id"], {"decision": "confirm"})

    assert _ending_status(events) == "SUCCEEDED"
    assert [item.call.arguments["recipient"] for item in received] == ["old@example.com"]
    assert notifier.sent[0]["recipient"] == "old@example.com"


def test_the_prepare_facts_survive_the_adapter_schema_check():
    """Task 9 复审的发现（B）：REPORT_PREPARATION 从没经过 bank_adapter 的校验器。

    线上冻结走的是 `build_registry(production_modules(...))` 这条装配路径；这条测试就走
    同一条路，让适配器按 `bill_send_report` 的 output_schema 验一遍 facts。
    """

    registry = build_registry(
        production_modules(
            notification_service=FakeNotifier(), database=FakeDb(), writes=MagicMock()
        )
    )
    adapter = BankToolAdapter(registry, FakeBankClient())
    invocation = ToolInvocation(
        agent_id="bill",
        call=ToolCall(
            tool_name="bill_send_report",
            arguments={"period_start": PERIOD_START, "period_end": PERIOD_END},
            call_id="call_prepare_1",
            # WRITE 且没有 key 时适配器会直接判 IDEMPOTENCY_KEY_REQUIRED，连 handler 都不进。
            idempotency_key="idem-prepare-1",
        ),
        context=TrustedContext(user_id="user_0001", session_id=SESSION, bank_customer_id="u_zhang"),
    )

    result = adapter.invoke(invocation)

    assert result.status is ToolStatus.SUCCEEDED
    assert set(result.data["facts"]) == {
        "action_id", "period_start", "period_end", "recipient", "task_id"
    }


def test_the_domain_prompt_trusts_only_the_current_message_for_scope():
    """范围只能来自当前这条消息：历史里的任务号/日期在上一轮成功时不作数。

    否则用户换句话再说「我要分析流水」时，模型会照搬上一轮的任务号或期间，
    把同一份报告原样再发一次。
    """

    from app.modules.bill.workflow import _BILL_PROMPT

    assert "当前这一条用户消息" in _BILL_PROMPT
    assert "历史消息里出现过的任务号、日期、期间一律不算" in _BILL_PROMPT
    # 唯一的例外：重试失败的那一次。
    assert "重试失败的那一次" in _BILL_PROMPT
    assert "上一轮已经成功时不适用" in _BILL_PROMPT
