"""转账自有图：挂平台确认卡、点击消费、验证码、取消与选择分支。

走真实图（生产 `MODULE`），不用 Mock 图；模型是 ScriptedModel，银行是 FakeBankClient 的转账版。
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime, TurnContext
from app.agents.supervisor import build_domain_graph
from app.agents.turn import TERMINAL_TURN_STATUSES
from app.integration.bank_client import BankResponse
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.modules.registry import build_registry
from app.modules.transfer import MODULE
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from tests.support.domain_harness import FakeBankClient
from tests.support.scripted_model import ScriptedModel

CONTEXT = TrustedContext("user_0001", "sess_transfer", "u_zhang")

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

SCHEDULED = [
    {
        "task_id": "task_1",
        "user_id": "u_zhang",
        "task_type": "scheduled_transfer",
        "execute_at": "2026-10-14T09:00:00+08:00",
        "status": "pending",
        "amount_minor": 150000,
        "result": None,
        "error_code": None,
    }
]

AA_DETAIL = {
    "aa_request_id": "aa_1",
    "user_id": "u_li",
    "from_account_id": "a_li_current",
    "total_amount_minor": 300000,
    "participant_count": 2,
    "remark": "演示聚餐",
    "status": "open",
    "created_at": "2026-10-30T09:00:00+08:00",
    "expires_at": "2026-11-06T09:00:00+08:00",
    "participants": [
        {
            "participant_index": 0,
            "name": "张三",
            "phone": None,
            "account_id": "a_zhang_current",
            "amount_minor": 150000,
            "status": "pending",
            "transaction_id": None,
            "paid_at": None,
        },
        {
            "participant_index": 1,
            "name": "李四",
            "phone": None,
            "account_id": "a_li_current",
            "amount_minor": 150000,
            "status": "pending",
            "transaction_id": None,
            "paid_at": None,
        },
    ],
}


class TransferBank(FakeBankClient):
    """记录每一次调用；可以按需让第一次 `POST /transfers` 被银行拒绝。"""

    def __init__(self, *, accounts=None, contacts=None, refuse_transfer=None):
        super().__init__()
        self.refuse_transfer = refuse_transfer
        self.transfer_attempts = 0
        self.scheduled_posts = []
        self.aa_posts = []
        self.register("/accounts", ACCOUNTS if accounts is None else accounts)
        self.register("/contacts", CONTACTS if contacts is None else contacts)
        self.register("/transfers/scheduled", SCHEDULED)
        self.register("/transfers/scheduled/task_1/cancel", {"task_id": "task_1", "status": "cancelled"})
        self.register("/transfers", {"transaction_id": "t_1", "amount_minor": 500000, "status": "completed"})
        self.register("/transfers/aa/aa_1", AA_DETAIL)
        self.register("/transfers/aa/aa_1/cancel", {"aa_request_id": "aa_1", "status": "cancelled"})

    def request(self, method, path, *, user_id, params=None, json=None, idempotency_key=None):
        if method == "POST" and path == "/transfers/scheduled":
            self.scheduled_posts.append(
                {
                    "method": method,
                    "path": path,
                    "user_id": user_id,
                    "json": dict(json or {}),
                    "idempotency_key": idempotency_key,
                }
            )
            return BankResponse(
                code=0,
                message="success",
                data={
                    "task_id": "task_scheduled_1",
                    "status": "pending",
                    "execute_at": (json or {}).get("execute_at"),
                },
            )
        if method == "POST" and path in ("/transfers/aa", "/transfers/aa/aa_1/pay"):
            self.aa_posts.append(
                {
                    "method": method,
                    "path": path,
                    "user_id": user_id,
                    "json": dict(json or {}),
                    "idempotency_key": idempotency_key,
                }
            )
            data = (
                {"aa_request_id": "aa_1", "status": "open"}
                if path == "/transfers/aa"
                else {"transaction_id": "t_aa_1", "status": "completed", "amount_minor": 150000}
            )
            return BankResponse(code=0, message="success", data=data)
        if method == "POST" and path == "/transfers":
            self.transfer_attempts += 1
            if self.refuse_transfer is not None and self.transfer_attempts == 1:
                raise self.refuse_transfer
        return super().request(
            method, path, user_id=user_id, params=params, json=json, idempotency_key=idempotency_key
        )


def runtime_for(bank, replies, *, turn=None, checkpointer=None):
    registry = build_registry([MODULE])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank))
    model = ScriptedModel(list(replies))
    spec = build_domain_specs(gateway, CONTEXT, roster=registry.roster)[0]
    graph = build_domain_graph(model, spec, client=bank, turn=turn).copy(
        update={"checkpointer": checkpointer or InMemorySaver()}
    )
    return AgentRuntime(graph=graph), model


def invoke(runtime, context=None, *, content="从工资卡转给老王"):
    async def collect():
        return [
            event
            async for event in runtime.invoke("sess_transfer", "user_0001", content, context or {})
        ]

    return asyncio.run(collect())


def resume(events, value):
    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    return {"resume": {"question_id": question["question_id"], "value": dict(value)}}


def interrupt(events):
    return next(event.data for event in reversed(events) if event.event == "interrupt")


def subtask(events):
    return next(event.data for event in events if event.event == "subtask_end")


def writes(bank):
    return [request for request in bank.requests if request["method"] == "POST" and request["path"] == "/transfers"]


def prepare_call(amount_yuan, *, to_name="老王", **extra):
    arguments = {"amount_yuan": amount_yuan, "to_name": to_name, **extra}
    import json

    return ("call", "transfer_prepare", json.dumps(arguments, ensure_ascii=False))


def schedule_call(amount_yuan, *, to_name="老王", execute_date="2030-01-01", **extra):
    import json

    arguments = {
        "amount_yuan": amount_yuan,
        "to_name": to_name,
        "execute_date": execute_date,
        **extra,
    }
    return ("call", "transfer_schedule", json.dumps(arguments, ensure_ascii=False))


def aa_call(amount_yuan="300.00", **extra):
    import json

    arguments = {"amount_yuan": amount_yuan, "participant_count": 3, **extra}
    return ("call", "transfer_aa", json.dumps(arguments, ensure_ascii=False))


# ------------------------------------------------------------------ 挂卡


def test_an_explicit_transfer_phrase_skips_the_model_and_goes_to_a_card():
    """「给X转N元」已经完整：不要再让模型把 X 当成需要补全的收款人。"""

    bank = TransferBank()
    runtime, model = runtime_for(bank, [])
    events = invoke(runtime, content="给老王转 100 元")

    question = interrupt(events)

    assert question["kind"] == "confirmation"
    assert question["confirmation"]["amount_minor"] == 10000
    assert model.prompts == []
    assert writes(bank) == []


def test_a_small_transfer_pauses_on_a_card_without_a_code():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [prepare_call("999.99", from_hint="活期")])

    events = invoke(runtime)
    question = interrupt(events)

    assert question["kind"] == "confirmation"
    assert question["confirmation"]["requires_code"] is False
    assert question["confirmation"]["pending_id"].startswith("tp_")
    assert question["confirmation"]["amount_minor"] == 99999
    assert question["confirmation"]["product_name"] == "老王"
    assert subtask(events)["turn_status"] == "waiting_confirmation"
    assert writes(bank) == []


def test_a_red_transfer_pauses_on_a_card_that_asks_for_a_code():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [prepare_call("5000.00", from_hint="活期")])

    question = interrupt(invoke(runtime))

    assert question["confirmation"]["requires_code"] is True
    assert "需要验证码" in question["display"]


def test_the_model_is_asked_once_and_the_read_tools_only_read():
    bank = TransferBank()
    runtime, model = runtime_for(bank, [prepare_call("100.00", from_hint="活期")])

    invoke(runtime)

    assert len(model.prompts) == 1
    assert [request["method"] for request in bank.requests] == ["GET", "GET"]


# ------------------------------------------------------------------ 点击消费


def test_confirming_a_small_transfer_writes_once_without_a_code():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [prepare_call("999.99", from_hint="活期")])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert len(writes(bank)) == 1
    body = writes(bank)[0]["json"]
    assert body["confirm"] is True
    assert body["mfa_code"] is None
    assert body["amount_minor"] == 99999
    assert subtask(done)["turn_status"] == "completed"


def test_a_completed_transfer_receipt_names_the_transaction():
    """用户拿到回执时要能看到交易号，而不是只有一句“已完成”。"""

    bank = TransferBank()
    runtime, _ = runtime_for(bank, [prepare_call("999.99", from_hint="活期")])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert subtask(done)["summary"] == "转账已完成：999.99 元。交易号 t_1。"


def test_confirming_a_red_transfer_takes_the_code_from_the_turn():
    bank = TransferBank()
    turn = TurnContext(
        conversation_id="c_1",
        thread_id="t_1",
        message_id="m_1",
        content="转 5000 给老王",
        sequence=1,
        verification_code="123456",
    )
    runtime, _ = runtime_for(bank, [prepare_call("5000.00", from_hint="活期")], turn=turn)
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert len(writes(bank)) == 1
    assert writes(bank)[0]["json"]["mfa_code"] == "123456"
    assert subtask(done)["turn_status"] == "completed"


def test_a_scheduled_request_gets_a_scheduled_card_and_creates_a_task():
    bank = TransferBank()
    runtime, _ = runtime_for(
        bank,
        [schedule_call("100.00", from_hint="活期", execute_time="14:30")],
    )
    events = invoke(runtime)

    question = interrupt(events)
    confirmation = question["confirmation"]
    assert confirmation["operation"] == "schedule_transfer"
    assert confirmation["title"] == "确认定时转账"
    assert "2030-01-01 14:30" in confirmation["product_name"]

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert len(bank.scheduled_posts) == 1
    body = bank.scheduled_posts[0]["json"]
    assert body["amount_minor"] == 10000
    assert body["execute_at"] == "2030-01-01T14:30:00+08:00"
    assert body["confirm"] is True
    assert bank.scheduled_posts[0]["idempotency_key"]
    assert writes(bank) == []
    assert subtask(done)["turn_status"] == "completed"
    assert "定时转账已创建" in subtask(done)["summary"]


def test_cancelling_the_card_writes_nothing():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [prepare_call("999.99", from_hint="活期")])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "cancel"}))

    assert writes(bank) == []
    assert "已取消" in subtask(done)["summary"]
    assert subtask(done)["turn_status"] == "completed"


def test_an_unknown_decision_is_refused_without_writing():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [prepare_call("999.99", from_hint="活期")])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "改成3000"}))

    assert writes(bank) == []
    assert subtask(done)["turn_status"] == "unsupported"


def test_a_bank_refusal_re_hangs_a_card_that_now_asks_for_a_code():
    bank = TransferBank(refuse_transfer=BankCallError(BankErrorKind.CONFIRMATION_REQUIRED, "需要验证码"))
    checkpoint = InMemorySaver()
    runtime, _ = runtime_for(bank, [prepare_call("999.99", from_hint="活期")], checkpointer=checkpoint)
    first = invoke(runtime)
    pending_id = interrupt(first)["confirmation"]["pending_id"]

    refused = invoke(runtime, resume(first, {"decision": "confirm"}))
    rehung = interrupt(refused)

    assert rehung["kind"] == "confirmation"
    assert rehung["confirmation"]["requires_code"] is True
    assert rehung["confirmation"]["pending_id"] == pending_id
    assert subtask(refused)["turn_status"] == "waiting_confirmation"
    assert writes(bank) == []

    # 用户带着码再点一次：图带着同一个检查点重建，验证码随新的 TurnContext 进来。
    turn = TurnContext(
        conversation_id="c_1",
        thread_id="t_1",
        message_id="m_2",
        content="确认转账",
        sequence=2,
        verification_code="123456",
    )
    rebuilt, _ = runtime_for(bank, [], turn=turn, checkpointer=checkpoint)
    done = invoke(rebuilt, resume(refused, {"decision": "confirm"}))

    assert len(writes(bank)) == 1
    assert writes(bank)[0]["json"]["mfa_code"] == "123456"
    assert subtask(done)["turn_status"] == "completed"


# ------------------------------------------------------------------ 选择分支


def test_two_same_named_contacts_are_resolved_by_a_selection_question():
    bank = TransferBank(
        contacts=[CONTACTS[0], {**CONTACTS[0], "contact_id": "c_9", "recipient_account_id": "a_partner_save"}]
    )
    runtime, _ = runtime_for(bank, [prepare_call("100.00", from_hint="活期")])

    events = invoke(runtime)
    question = interrupt(events)

    assert question["kind"] == "transfer_selection"
    assert question["fields"][0]["name"] == "selection"
    assert len(question["fields"][0]["options"]) == 2
    assert writes(bank) == []

    chosen = question["fields"][0]["options"][1]
    confirmed = invoke(runtime, resume(events, {"selection": chosen}))

    card = interrupt(confirmed)
    assert card["kind"] == "confirmation"
    assert card["confirmation"]["product_name"] == "老王"


# 浏览器级验收发现的两件事，在这里钉住：真实模型对可选参数会送 null（或整个省略），
# 金额会当 number 送。两者都不该把整轮打回「参数格式不正确」。


def test_null_optional_parameters_still_reach_the_card():
    bank = TransferBank()
    reply = (
        '{"amount_yuan": "100.00", "to_name": "老王", "from_hint": null, '
        '"from_account_id": null, "to_account_id": null, "remark": null}'
    )
    runtime, _ = runtime_for(bank, [("call", "transfer_prepare", reply)])

    question = interrupt(invoke(runtime))

    assert question["kind"] == "confirmation"
    assert question["confirmation"]["amount_minor"] == 10000


def test_string_null_optional_parameters_still_reach_the_card():
    """弱模型把「没提供」写成字符串 "null"，它必须和 null 一样被当成没提供。"""

    bank = TransferBank()
    reply = (
        '{"amount_yuan": "100.00", "to_name": "老王", "from_hint": "null", '
        '"from_account_id": "null", "to_account_id": "null", "remark": "null"}'
    )
    runtime, _ = runtime_for(bank, [("call", "transfer_prepare", reply)])

    question = interrupt(invoke(runtime))

    assert question["kind"] == "confirmation"
    assert question["confirmation"]["amount_minor"] == 10000


def test_a_numeric_amount_still_reaches_the_card():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [("call", "transfer_prepare", '{"amount_yuan": 100, "to_name": "老王", "from_hint": "活期"}')])

    question = interrupt(invoke(runtime))

    assert question["kind"] == "confirmation"
    assert question["confirmation"]["amount_minor"] == 10000


# ------------------------------------------------------------------ 取消定时转账


def test_cancel_goes_through_a_card_before_the_gateway_write():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [("call", "transfer_cancel", '{"task_id":"task_1"}')])

    events = invoke(runtime)
    question = interrupt(events)

    assert question["kind"] == "confirmation"
    assert question["confirmation"]["requires_code"] is False
    assert question["confirmation"]["pending_id"].startswith("tc_")
    assert not any("cancel" in request["path"] for request in bank.requests)

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    cancels = [request for request in bank.requests if request["path"].endswith("/cancel")]
    assert len(cancels) == 1
    assert cancels[0]["idempotency_key"]
    assert subtask(done)["turn_status"] == "completed"


def test_cancelling_the_cancel_card_writes_nothing():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [("call", "transfer_cancel", '{"task_id":"task_1"}')])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "cancel"}))

    assert not any("cancel" in request["path"] for request in bank.requests)
    assert "已取消" in subtask(done)["summary"]


# ------------------------------------------------------------------ AA 收款


def test_an_aa_collect_request_hangs_a_card_and_creates_the_request_on_confirm():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [aa_call("300.00")])
    events = invoke(runtime)

    question = interrupt(events)
    confirmation = question["confirmation"]
    assert confirmation["operation"] == "aa_collect"
    assert confirmation["title"] == "确认发起 AA 收款"
    assert confirmation["pending_id"].startswith("taa_")
    assert confirmation["amount_minor"] == 30000
    assert confirmation["requires_code"] is False
    assert confirmation["product_name"] == "3 人分摊 · 参与者1、参与者2、参与者3"
    assert bank.aa_posts == []
    assert writes(bank) == []

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert len(bank.aa_posts) == 1
    body = bank.aa_posts[0]["json"]
    assert body["total_amount_minor"] == 30000
    assert len(body["participants"]) == 3
    assert body["confirm"] is True
    assert bank.aa_posts[0]["idempotency_key"]
    # 发起 AA 只生成收款请求，绝不能同时出现一笔即时转账。
    assert writes(bank) == []
    assert subtask(done)["turn_status"] == "completed"
    assert subtask(done)["summary"] == "AA 收款已发起：总额 300.00 元，3 人分摊。编号 aa_1，7 天内有效。"


def test_cancelling_the_aa_card_starts_nothing():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [aa_call("300.00")])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "cancel"}))

    assert bank.aa_posts == []
    assert writes(bank) == []
    assert subtask(done)["summary"] == "已取消，本次 AA 操作没有发起。"
    assert subtask(done)["turn_status"] == "completed"


def test_an_aa_share_payment_asks_for_the_code_and_takes_it_from_the_turn():
    bank = TransferBank()
    turn = TurnContext(
        conversation_id="c_1",
        thread_id="t_1",
        message_id="m_1",
        content="付我那一份",
        sequence=1,
        verification_code="123456",
    )
    runtime, _ = runtime_for(
        bank, [("call", "transfer_aa_pay", '{"aa_request_id":"aa_1"}')], turn=turn
    )
    events = invoke(runtime)

    question = interrupt(events)
    confirmation = question["confirmation"]
    assert confirmation["operation"] == "aa_payment"
    assert confirmation["pending_id"].startswith("tap_")
    # 单份 1500 元已过 1000 元口径：红标卡要验证码（这里没有数据库，单笔金额自己越线）。
    assert confirmation["requires_code"] is True
    assert confirmation["amount_minor"] == 150000

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    pays = [post for post in bank.aa_posts if post["path"].endswith("/pay")]
    assert len(pays) == 1
    body = pays[0]["json"]
    assert body["participant_index"] == 0
    assert body["confirm"] is True
    assert body["mfa_code"] == "123456"
    assert pays[0]["idempotency_key"]
    assert writes(bank) == []
    assert subtask(done)["turn_status"] == "completed"
    assert subtask(done)["summary"] == "AA 分摊已支付：1,500.00 元。交易号 t_aa_1。"


def test_cancelling_an_aa_request_goes_through_a_card_before_the_write():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [("call", "transfer_aa_cancel", '{"aa_request_id":"aa_1"}')])

    events = invoke(runtime)
    question = interrupt(events)

    assert question["confirmation"]["operation"] == "aa_cancel"
    assert question["confirmation"]["pending_id"].startswith("tac_")
    assert question["confirmation"]["amount_minor"] == 300000
    # 挂卡前只读了一次详情；银行还没有收到任何写。
    assert [request["method"] for request in bank.requests if "aa_1" in request["path"]] == ["GET"]
    assert bank.aa_posts == []

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    cancels = [request for request in bank.requests if request["path"] == "/transfers/aa/aa_1/cancel"]
    assert len(cancels) == 1
    assert cancels[0]["idempotency_key"]
    assert subtask(done)["turn_status"] == "completed"
    assert "已取消 AA 收款 aa_1" in subtask(done)["summary"]


def test_an_aa_phrase_is_not_swallowed_by_the_direct_fast_path():
    """「AA 收款」这句话里既有收款人也有金额，绝不能被即时转账直通当成一笔转账。"""

    bank = TransferBank()
    runtime, model = runtime_for(bank, [aa_call("300.00")])

    events = invoke(runtime, content="给同事AA收款300元")

    assert model.prompts != []
    question = interrupt(events)
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["operation"] == "aa_collect"


# ------------------------------------------------------------------ 其它


def test_a_reservation_request_is_not_executed_as_an_immediate_transfer():
    bank = TransferBank()
    runtime, _ = runtime_for(bank, ["当前不支持预约转账。"])

    events = invoke(runtime)

    assert writes(bank) == []
    assert "不支持预约" in subtask(events)["summary"]
    assert not any(event.event == "interrupt" for event in events)


def test_a_prepare_failure_ends_the_turn_without_a_card():
    bank = TransferBank(accounts=[])
    runtime, _ = runtime_for(bank, [prepare_call("100.00", from_hint="活期")])

    events = invoke(runtime)

    assert not any(event.event == "interrupt" for event in events)
    assert subtask(events)["turn_status"] == "failed"
    assert writes(bank) == []


@pytest.mark.parametrize("decision", ["confirm", "cancel", "改成3000"])
def test_every_ending_carries_a_terminal_turn_status(decision):
    bank = TransferBank()
    runtime, _ = runtime_for(bank, [prepare_call("999.99", from_hint="活期")])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": decision}))

    assert subtask(done)["turn_status"] in TERMINAL_TURN_STATUSES


def test_a_string_null_status_still_queries_the_scheduled_list():
    """枚举参数上的 "null" 过不了 transfer 自己的那道校验，必须和省略一样被归一化掉。"""

    bank = TransferBank()
    runtime, _ = runtime_for(
        bank, [("call", "transfer_scheduled", '{"status": "null"}'), "您有一笔定时转账。"]
    )

    events = invoke(runtime)

    calls = [request for request in bank.requests if request["path"] == "/transfers/scheduled"]
    assert calls and calls[0]["params"] == {}
    assert subtask(events)["turn_status"] == "completed"

