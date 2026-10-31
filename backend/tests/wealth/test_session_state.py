"""任务 3（W-6）：跨轮状态由调用方持有，随 `WealthAgent.handle` 进出，不再留在进程里。

三组断言：

1. **进出**：第一轮攒下的槽位与待办随返回值交给调用方；把它们递回去，第二轮就还能看见。
2. **不共享**：换一份空会话，同样的身份、同样的线程也什么都看不见；换一份空待办，旧问题不再被
   当成回答（I-21：过期由每个节点对递进来的待办自己判断，不再靠进程里的清理表）。
3. **不留表**：`WealthAgent` 的实例状态里没有 `_sessions`／`_pending`。
"""

from __future__ import annotations

from datetime import timedelta

from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.application.runtime import PENDING_TTL
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.workflow.state import SessionData, merge_session, session_of
from tests.wealth import support

ACCOUNTS = "/accounts"


def _agent(model, bank, clock):
    return build_wealth_agent(model, bank, clock)


def _two_accounts() -> list[dict]:
    return [
        support.account_payload(),
        support.account_payload(
            account_id="a_wealth_backup",
            account_type="savings",
            balance_minor=500000,
            available_minor=500000,
        ),
    ]


def test_the_session_is_passed_in_and_returned_out(bank, double, clock):
    """第一轮的槽位与待办由返回值交给调用方；递回去第二轮还看得见，换一份空的就看不见。"""

    double.on("GET", ACCOUNTS, data=_two_accounts())
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000}),
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
        support.call_read("wealth_accounts"),
        support.set_slots({"account_id": "a_wealth_main"}),
        support.finish("好的，用主账户买入 2000.00 元。"),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    session: SessionData = {}
    pending = None
    asked, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="我想买两千块理财"),
        session=session,
        pending=pending,
    )

    assert asked.status is WealthStatus.WAITING_INPUT
    assert asked.pending is not None
    # 第一轮攒下的槽位与待办都在返回值里，不在 agent 身上。
    assert session["slots"] == {"plan_amount_minor": 200000}
    assert pending is not None
    assert pending["pending_id"] == asked.pending.pending_id
    assert pending["missing"] == ("account_id",)

    answered, session, pending = agent.handle(
        context,
        WealthRequest(kind="answer", text="用主账户", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert answered.status is WealthStatus.COMPLETED
    assert session["slots"] == {"plan_amount_minor": 200000, "account_id": "a_wealth_main"}
    assert pending is None
    # 第二轮决策的提示词里已经有第一轮攒下的金额：它随会话回来了。
    assert "2000.00 元（200000 分）" in model.prompts[2]

    # 换一份空会话：同样的身份、同样的线程，第一轮的槽位不再出现。
    fresh_model = support.ScriptModel(support.finish("好的。"))
    fresh_agent = _agent(fresh_model, bank, clock)
    _, fresh_session, fresh_pending = fresh_agent.handle(
        context,
        WealthRequest(kind="message", text="我想买两千块理财"),
        session={},
        pending=None,
    )

    assert fresh_session.get("slots", {}) == {}
    assert fresh_pending is None
    assert "2000.00 元（200000 分）" not in fresh_model.prompts[0]


def test_an_expired_pending_is_dropped_per_node(bank, double, clock):
    """I-21：过期不再靠进程里的清理表，而是每个节点对递进来的待办自己判断。"""

    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"]),
        support.finish("那建议先看可灵活赎回的产品。"),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    asked, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="推荐点理财"),
        session={},
        pending=None,
    )
    assert asked.status is WealthStatus.WAITING_INPUT
    assert pending is not None

    clock.advance(PENDING_TTL + timedelta(seconds=1))

    # 把过期的待办递回去：它必须被丢弃，这条消息按新消息处理，而不是当成对这个问题的回答。
    response, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.COMPLETED
    assert response.summary == "那建议先看可灵活赎回的产品。"
    assert "用户回答：" not in model.prompts[1]
    assert pending is None
    assert session.get("pending_id") is None


def test_the_agent_keeps_no_session_tables(bank, double, clock):
    """会话与待办不在进程里：跑过两轮之后，实例状态仍然只剩模型、银行、时钟、工具表、图工厂和三笔按用户的账。

    断言必须发生在**至少一次 `handle` 之后**：老实现的那张会话表是第一次用到时才建的，
    在没跑过任何一轮之前看实例状态，懒建的表根本不会出现——那样的断言抓不到它。
    """

    double.on("GET", ACCOUNTS, data=_two_accounts())
    model = support.ScriptModel(
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
        support.call_read("wealth_accounts"),
        support.set_slots({"account_id": "a_wealth_main"}),
        support.finish("好的，用主账户买入。"),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    asked, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="我想买两千块理财"),
        session={},
        pending=None,
    )
    assert asked.status is WealthStatus.WAITING_INPUT

    answered, session, pending = agent.handle(
        context,
        WealthRequest(kind="answer", text="用主账户", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )
    assert answered.status is WealthStatus.COMPLETED

    assert set(vars(agent)) == {
        "_model",
        "_bank",
        "_clock",
        "_tools",
        "_graph_factory",
        "_locked_users",
        "_technical_failures",
        "_bypass_events",
    }


def test_session_of_and_merge_session_round_trip():
    """state.py 的映射函数：会话摊平进图状态，再原样收回。"""

    state = {
        "wealth_slots": {"plan_amount_minor": 200000},
        "wealth_shown": {"products": ["p_demo_cash"]},
        "wealth_intent_version": 3,
        "wealth_clarifications": 1,
        "wealth_pending_id": "wp_1",
        "wealth_last_holding": {"holding_id": "h_1"},
        "wealth_unresolved": {"pending_id": "wp_1"},
    }

    session = session_of(state)

    assert session["slots"] == {"plan_amount_minor": 200000}
    assert session["shown"] == {"products": ("p_demo_cash",)}
    assert session["intent_version"] == 3
    assert session["clarifications"] == 1
    assert session["pending_id"] == "wp_1"
    assert session["last_holding"] == {"holding_id": "h_1"}
    assert session["unresolved"] == {"pending_id": "wp_1"}

    # 空状态给的是空会话，不是崩溃。
    empty = session_of({})
    assert empty["slots"] == {}
    assert empty["shown"] == {}
    assert empty["intent_version"] == 0
    assert empty["pending_id"] is None

    target: dict = {}
    merge_session(target, session)
    assert target == state
