"""一条生产转账确认卡，能否活过一个全新的 checkpointer 实例。

总验收（docs/superpowers/plans/2026-10-13-architecture-optimization-plan.md:59）要求
「card/wealth/transfer 均经 graph_builder + 杀进程 Postgres resume」。真正验证跨实例 RDS
恢复的两个既有用例（tests/test_checkpointer_integration.py:103-133、:136-166）用的是合成图；
生产确认卡只在 InMemorySaver 下被覆盖（tests/transfer/test_transfer_graph.py:236-266、
tests/test_wealth_graph.py:172-209）。本文件补上这个洞。

覆盖「杀进程」的档 1 与档 2（档 3 是真 subprocess，明确不在范围内）：

1. 档 1：段 1 用 saver1 + runtime1 把一轮走到确认卡，然后关闭 saver1（退出 async 上下文）；
2. 档 2：段 2 换一个全新的 saver2 实例和一个全新的 AgentRuntime，并从零重建生产图
   （build_agent_graph(...).compile(checkpointer=saver2)）——这证伪「图对象或 AgentRuntime
   缓存了状态」，因为两者都是新的。

数据库不可用时整文件 skip；连接超时是真失败，不吞成静默通过。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime, TurnContext, thread_id_for_session
from app.core.errors import AppError
from app.modules.registry import build_registry
from app.modules.transfer import workflow as transfer_workflow
from app.runtime import build_tool_gateway
from app.storage.checkpointer import LockedAsyncPostgresSaver
from tests.conftest import resolve_test_database_url
from tests.support.domain_harness import FakeBankClient
from tests.support.scripted_model import ScriptedModel

pytestmark = pytest.mark.skipif(
    resolve_test_database_url() is None,
    reason="Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests",
)

SESSION = "s_subgraph_persistence"
USER = "u_1"
CUSTOMER = "u_zhang"
THREAD = thread_id_for_session(SESSION)

# 5000.00 越过转账的验证码阈值（见 tests/transfer/test_transfer_card_wiring.py:191-204）。
RED = "5000.00"
CODE = "123456"
CLICK_REPLY = "已按您的确认提交。"
CANCEL_REPLY = "已取消本次操作。"

ACCOUNTS = [
    {
        "account_id": "a_zhang_current",
        "user_id": "u_zhang",
        "account_type": "current",
        "balance_minor": 832000,
        "available_minor": 800000,
        "currency": "CNY",
        "interest_rate": 0.0,
        "status": "active",
    }
]
CONTACTS = [
    {
        "contact_id": "c_1",
        "user_id": "u_zhang",
        "name": "老王",
        "phone": "13800138000",
        "remark": None,
        "recipient_user_id": "u_partner",
        "recipient_account_id": "a_partner_current",
        "birthday": None,
    }
]


class TransferBank(FakeBankClient):
    """转账确认卡需要的银行面：两个只读查询，和那一次 POST /transfers。"""

    def __init__(self) -> None:
        super().__init__()
        self.register("/accounts", ACCOUNTS)
        self.register("/contacts", CONTACTS)
        self.register(
            "/transfers",
            {"transaction_id": "t_1", "amount_minor": 500000, "status": "completed"},
        )

    def writes(self) -> list[dict]:
        return [
            item
            for item in self.requests
            if item["method"] == "POST" and item["path"] == "/transfers"
        ]


def _handoff() -> tuple[str, str, str]:
    return ("call", "handoff", '{"agent_id":"transfer","instruction":"办理转账"}')


def _prepare(amount: str = RED) -> tuple[str, str, str]:
    payload = {"amount_yuan": amount, "to_name": "老王", "from_hint": "活期"}
    return ("call", "transfer_prepare", json.dumps(payload, ensure_ascii=False))


def _factory(bank: TransferBank):
    """生产装配：build_agent_graph 挂上模块图，runtime 负责 compile(checkpointer=...)。"""

    registry = build_registry()
    gateway = build_tool_gateway(registry, client=bank)

    def build(model, context, assembler=None, turn=None):
        return build_agent_graph(
            model, gateway, context, assembler=assembler, turn=turn, client=bank
        )

    return build


def _runtime(model, bank: TransferBank, saver) -> AgentRuntime:
    return AgentRuntime(
        model,
        graph_factory=_factory(bank),
        checkpointer=saver,
        customer_resolver=lambda user_id: CUSTOMER,
    )


async def _park(runtime: AgentRuntime, session: str = SESSION) -> list:
    """驱动一轮走到确认卡，返回这一投的完整事件流。"""

    return [event async for event in runtime.invoke(session, USER, "从工资卡转给老王 5000")]


def _confirmation(events: list) -> dict:
    cards = [
        event.data
        for event in events
        if event.event == "interrupt" and event.data.get("kind") == "confirmation"
    ]
    assert len(cards) == 1, [event.event for event in events]
    return cards[0]


def _final(events: list) -> dict:
    finals = [event.data for event in events if event.event == "final"]
    assert len(finals) == 1, [event.event for event in events]
    return finals[0]


async def _answer(
    runtime: AgentRuntime,
    *,
    thread_id: str,
    question_id: str,
    decision: str,
    code: str | None = None,
    message_id: str,
    content: str,
):
    """一次真实的点击：先过 admission 绑定问题，再用 TurnContext 走图。"""

    admission = await runtime.admission(
        USER,
        SESSION,
        thread_id,
        {"resume": {"question_id": question_id, "value": {"decision": decision}}},
    )
    turn = TurnContext(
        conversation_id="c_subgraph_persistence",
        thread_id=thread_id,
        message_id=message_id,
        content=content,
        sequence=2,
        resume=admission.resume,
        question_id=admission.question_id,
        verification_code=code,
    )
    events = [event async for event in runtime.invoke(SESSION, USER, content, turn=turn)]
    return admission, events


def test_a_paused_transfer_confirmation_card_survives_a_new_saver(schema_url, monkeypatch):
    calls: list[str | None] = []
    original = transfer_workflow.submit_transfer

    def counting_submit(client, context, intent, pending, *, verification_code, writes=None):
        calls.append(verification_code)
        return original(
            client,
            context,
            intent,
            pending,
            verification_code=verification_code,
            writes=writes,
        )

    monkeypatch.setattr(transfer_workflow, "submit_transfer", counting_submit)

    asyncio.run(_survives_a_new_saver(schema_url, calls))
    assert calls == [CODE]


async def _survives_a_new_saver(schema_url: str, calls: list) -> None:
    bank = TransferBank()

    # ---- 段 1：saver1 + runtime1，把一轮走到确认卡，然后关掉 saver1。
    async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver1:
        await saver1.setup()
        runtime1 = _runtime(ScriptedModel([_handoff(), _prepare()]), bank, saver1)
        events1 = await _park(runtime1)

    card = _confirmation(events1)
    assert card["confirmation"]["requires_code"] is True
    assert card["confirmation"]["pending_id"].startswith("tp_")
    question_id = card["question_id"]
    assert question_id.startswith("q_")
    assert bank.writes() == []

    # ---- 段 2：全新 saver2 实例 + 全新 runtime，图从零重建。
    async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver2:
        await saver2.setup()
        runtime2 = _runtime(ScriptedModel([CLICK_REPLY]), bank, saver2)

        # 父图的 aget_state 必须看见子图命名空间里的暂停：同一 question_id / pending_id。
        hint = await runtime2.recovery_hint(USER, SESSION, THREAD)
        assert hint.recovery_required is False
        assert hint.pending is not None
        assert hint.pending["question_id"] == question_id
        assert hint.pending["confirmation"]["pending_id"] == card["confirmation"]["pending_id"]

        admission, events2 = await _answer(
            runtime2,
            thread_id=THREAD,
            question_id=question_id,
            decision="confirm",
            code=CODE,
            message_id="m_confirm",
            content="确认本次操作",
        )

    assert admission.resuming is True
    assert admission.question_id == question_id
    assert [event.data["turn_status"] for event in events2 if event.event == "subtask_end"] == [
        "completed"
    ]
    assert "t_1" in _final(events2)["business_ids"]
    assert len(bank.writes()) == 1


def test_a_paused_confirmation_cannot_be_answered_by_a_stale_question_id(schema_url):
    asyncio.run(_stale_question_id(schema_url))


async def _stale_question_id(schema_url: str) -> None:
    bank = TransferBank()
    model = ScriptedModel([_handoff(), _prepare(), _handoff(), _prepare()])

    async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
        await saver.setup()
        runtime = _runtime(model, bank, saver)

        # 第一张卡：本地取消，银行零写，但问题就此作废。
        first = _confirmation(await _park(runtime))
        await _answer(
            runtime,
            thread_id=THREAD,
            question_id=first["question_id"],
            decision="cancel",
            message_id="m_cancel",
            content="取消本次操作",
        )
        assert bank.writes() == []

        # 第二张卡：新的问题与新的 pending_id。
        second = _confirmation(await _park(runtime))
        assert second["question_id"] != first["question_id"]
        assert second["confirmation"]["pending_id"] != first["confirmation"]["pending_id"]

        # 拿第一张（已作废）的 question_id 去答第二张：必须拒绝，且不产生任何写。
        with pytest.raises(AppError) as failure:
            await runtime.admission(
                USER,
                SESSION,
                THREAD,
                {
                    "resume": {
                        "question_id": first["question_id"],
                        "value": {"decision": "confirm"},
                    }
                },
            )

    assert failure.value.code == "INVALID_INTERRUPT_ANSWER"
    assert bank.writes() == []
