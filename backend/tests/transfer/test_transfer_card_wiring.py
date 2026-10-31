"""转账的 HTTP 全链路：确认卡、验证码、一次写、取消与选择分支。

模板是 tests/test_wealth_write_api.py —— 生产注册表 + 真的 graph_builder 模块 + 平台
`kind="confirmation"` 卡片 + `POST /pending-actions/{pending_id}/decide`。会话一律带标题建：
不带标题会触发后台自动命名，那会多问一次模型并把脚本吃掉一条。

**点击轮会再问模型一次**（Supervisor 收尾那句话），所以凡是要点击的用例，模型脚本里都要
多留一条收尾语（`extra=(CLICK_REPLY,)`）。
"""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.runtime import AgentRuntime
from app.main import create_app
from app.modules.registry import build_registry
from app.modules.transfer import MODULE as TRANSFER_MODULE
from app.modules.transfer.tools import TOOLS
from app.runtime import agent_graph_factory, build_tool_gateway
from app.tools.schema import function_schema
from tests.support.domain_harness import FakeBankClient
from tests.support.scripted_model import ScriptedModel
from tests.test_conversation_api import create, messages, submit
from tests.test_multi_agent_api import EVENT_FIELDS, frames

TITLE = "转账"
CLICK_REPLY = "已按您的确认提交。"
RED = "5000.00"
# 新口径（app.policy）：单笔 + 当日累计合计 > ¥1000 才红标要码，1000.00 恰好等于阈值仍免码。
SMALL = "999.99"

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

# 单份 1500 元：无论有没有数据库，单笔金额自己就越过 1000 元口径 → 红标要码。
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
    def __init__(self, *, accounts=None, contacts=None, refuse_transfer=None):
        super().__init__()
        self.refuse_transfer = refuse_transfer
        self.transfer_attempts = 0
        self.register("/accounts", ACCOUNTS if accounts is None else accounts)
        self.register("/contacts", CONTACTS if contacts is None else contacts)
        self.register("/transfers/scheduled", SCHEDULED)
        self.register("/transfers/scheduled/task_1/cancel", {"task_id": "task_1", "status": "cancelled"})
        self.register("/transfers", {"transaction_id": "t_1", "amount_minor": 500000, "status": "completed"})
        self.register("/transfers/aa", {"aa_request_id": "aa_1", "status": "open", "total_amount_minor": 30000})
        self.register("/transfers/aa/aa_1", AA_DETAIL)
        self.register("/transfers/aa/aa_1/pay", {"transaction_id": "t_aa_1", "status": "completed", "amount_minor": 150000})
        self.register("/transfers/aa/aa_1/cancel", {"aa_request_id": "aa_1", "status": "cancelled"})

    def request(self, method, path, *, user_id, params=None, json=None, idempotency_key=None):
        if method == "POST" and path == "/transfers":
            self.transfer_attempts += 1
            if self.refuse_transfer is not None and self.transfer_attempts == 1:
                raise self.refuse_transfer
        return super().request(
            method, path, user_id=user_id, params=params, json=json, idempotency_key=idempotency_key
        )


def _prepare_reply(amount=SMALL):
    payload = {"amount_yuan": amount, "to_name": "老王", "from_hint": "活期"}
    return ("call", "transfer_prepare", json.dumps(payload, ensure_ascii=False))


def _model(amount=SMALL, *, extra=(), tool="transfer_prepare", arguments=None):
    payload = (
        arguments
        if arguments is not None
        else {"amount_yuan": amount, "to_name": "老王", "from_hint": "活期"}
    )
    replies = [
        ("call", "handoff", '{"agent_id":"transfer","instruction":"办理转账"}'),
        ("call", tool, json.dumps(payload, ensure_ascii=False)),
    ]
    replies.extend(extra)
    return ScriptedModel(replies)


def _runtime(rt, session, bank, model, checkpoint=None):
    registry = build_registry()
    gateway = build_tool_gateway(registry, client=bank, audit=rt.audit)
    checkpoint = checkpoint or InMemorySaver()

    def rebuild():
        return AgentRuntime(
            model,
            graph_factory=agent_graph_factory(gateway, client=bank),
            checkpointer=checkpoint,
            customer_resolver=rt.identity.bank_customer_id,
        )

    rt.bank_client = bank
    rt.agent = rebuild()
    client = TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )
    return client, rebuild


def _card(client, conversation_id):
    return client.get(f"/api/conversations/{conversation_id}").json()["data"]["pending_interrupt"]


def _decide(client, conversation_id, pending_id, body, *, key=None):
    return client.post(
        f"/api/conversations/{conversation_id}/pending-actions/{pending_id}/decide",
        json=body,
        headers={"Idempotency-Key": key or f"click-{uuid4().hex}"},
    )


def _writes(bank, path="/transfers"):
    return [item for item in bank.requests if item["method"] == "POST" and item["path"] == path]


def _turn_status(response):
    return next(data["turn_status"] for name, data in frames(response) if name == "subtask_end")


def _walk(client, conversation_id):
    """一投走到确认卡，返回（卡、这一投的响应）。模型脚本必须已经带上收尾语。"""

    response = submit(client, conversation_id, "从工资卡转给老王")
    assert response.status_code == 200, response.text
    card = _card(client, conversation_id)
    assert card["kind"] == "confirmation", card
    return card, response


def _click(client, conversation_id, card, decision="confirm", *, code=None, key=None):
    body = {"question_id": card["question_id"], "decision": decision}
    if code is not None:
        body["verification_code"] = code
    return _decide(
        client, conversation_id, card["confirmation"]["pending_id"], body, key=key
    )


# ------------------------------------------------------------------ 注册面


def test_the_production_catalog_exposes_ten_transfer_tools_and_no_turn():
    registry = build_registry()
    names = registry.catalog.for_agent("transfer").names

    assert set(names) == set(TRANSFER_MODULE.tools)
    assert len(names) == 10
    assert TRANSFER_MODULE.declaration.graph_builder is not None
    visible = {function_schema(item)["function"]["name"] for item in TOOLS.values()}
    assert "transfer_cancel" in visible  # 可见，但图会把它拦成确认卡
    assert {"transfer_aa", "transfer_aa_status", "transfer_aa_pay", "transfer_aa_cancel"} <= visible


# ------------------------------------------------------------------ 图到卡


def test_the_card_asks_for_a_code_only_at_the_red_threshold(rt, session):
    bank = TransferBank()
    client, _ = _runtime(rt, session, bank, _model(SMALL, extra=(CLICK_REPLY,)))
    conversation = create(client, title=TITLE)
    card, _ = _walk(client, conversation["conversation_id"])
    assert card["confirmation"]["requires_code"] is False
    assert _writes(bank) == []

    red_bank = TransferBank()
    red_client, _ = _runtime(rt, session, red_bank, _model(RED, extra=(CLICK_REPLY,)))
    red = create(red_client, title=TITLE)
    red_card, _ = _walk(red_client, red["conversation_id"])
    assert red_card["confirmation"]["requires_code"] is True
    assert red_card["confirmation"]["amount_minor"] == 500000


def test_the_stream_only_carries_the_documented_frame_keys(rt, session):
    bank = TransferBank()
    client, _ = _runtime(rt, session, bank, _model(SMALL, extra=(CLICK_REPLY,)))
    conversation = create(client, title=TITLE)

    _, response = _walk(client, conversation["conversation_id"])

    for name, data in frames(response):
        assert name in EVENT_FIELDS, name
        expected = EVENT_FIELDS[name]
        if name == "interrupt" and data.get("kind") == "confirmation":
            expected = expected | {"confirmation"}
        assert set(data) == expected, (name, sorted(data))


# ------------------------------------------------------------------ 点击消费


def test_a_small_click_writes_once_without_a_code(rt, session):
    bank = TransferBank()
    client, _ = _runtime(rt, session, bank, _model(SMALL, extra=(CLICK_REPLY,)))
    conversation = create(client, title=TITLE)
    card, _ = _walk(client, conversation["conversation_id"])

    response = _click(client, conversation["conversation_id"], card)

    assert response.status_code == 200, response.text
    assert _turn_status(response) == "completed"
    assert len(_writes(bank)) == 1
    body = _writes(bank)[0]["json"]
    assert body["confirm"] is True
    assert body["mfa_code"] is None
    assert _card(client, conversation["conversation_id"]) is None


def test_a_red_click_without_a_code_is_refused_before_the_bank(rt, session):
    bank = TransferBank()
    client, _ = _runtime(rt, session, bank, _model(RED, extra=(CLICK_REPLY,)))
    conversation = create(client, title=TITLE)
    card, _ = _walk(client, conversation["conversation_id"])

    refused = _click(client, conversation["conversation_id"], card)

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "VERIFICATION_CODE_REQUIRED"
    assert _writes(bank) == []
    still = _card(client, conversation["conversation_id"])
    assert still["confirmation"]["pending_id"] == card["confirmation"]["pending_id"]
    assert still["confirmation"]["requires_code"] is True

    done = _click(client, conversation["conversation_id"], card, code="123456")

    assert done.status_code == 200, done.text
    assert len(_writes(bank)) == 1
    assert _writes(bank)[0]["json"]["mfa_code"] == "123456"


def test_cancelling_the_card_writes_nothing(rt, session):
    bank = TransferBank()
    client, _ = _runtime(rt, session, bank, _model(SMALL, extra=(CLICK_REPLY,)))
    conversation = create(client, title=TITLE)
    card, _ = _walk(client, conversation["conversation_id"])

    response = _click(client, conversation["conversation_id"], card, decision="cancel")

    assert response.status_code == 200, response.text
    assert _writes(bank) == []
    assert _turn_status(response) == "completed"


def test_a_repeat_click_is_one_submission_and_a_new_key_is_refused(rt, session):
    bank = TransferBank()
    client, _ = _runtime(rt, session, bank, _model(SMALL, extra=(CLICK_REPLY,)))
    conversation = create(client, title=TITLE)
    card, _ = _walk(client, conversation["conversation_id"])
    conversation_id = conversation["conversation_id"]

    first = _click(client, conversation_id, card, key="click-1")
    again = _click(client, conversation_id, card, key="click-1")
    fresh = _click(client, conversation_id, card, key="click-2")

    assert first.status_code == 200 and again.status_code == 200
    assert fresh.status_code == 409
    assert fresh.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert len(_writes(bank)) == 1


def test_a_bank_refusal_upgrades_the_card_and_the_next_click_succeeds(rt, session):
    from app.integration.bank_errors import BankCallError, BankErrorKind

    bank = TransferBank(
        refuse_transfer=BankCallError(BankErrorKind.CONFIRMATION_REQUIRED, "需要验证码")
    )
    checkpoint = InMemorySaver()
    client, rebuild = _runtime(
        rt, session, bank, _model(SMALL, extra=(CLICK_REPLY,)), checkpoint=checkpoint
    )
    conversation = create(client, title=TITLE)
    conversation_id = conversation["conversation_id"]
    card, _ = _walk(client, conversation_id)

    refused = _click(client, conversation_id, card)

    assert refused.status_code == 200, refused.text
    assert _turn_status(refused) == "waiting_confirmation"
    upgraded = _card(client, conversation_id)
    assert upgraded["confirmation"]["requires_code"] is True
    assert upgraded["confirmation"]["pending_id"] == card["confirmation"]["pending_id"]
    assert _writes(bank) == []

    # 带着码再点一次：图带着同一个检查点重建，验证码随新的 TurnContext 进来。
    rt.agent = rebuild()
    done = _click(client, conversation_id, upgraded, code="123456")

    assert done.status_code == 200, done.text
    assert len(_writes(bank)) == 1
    assert _writes(bank)[0]["json"]["mfa_code"] == "123456"


def test_the_code_never_reaches_the_stream_the_ledger_or_the_audit(rt, session):
    bank = TransferBank()
    model = _model(RED, extra=(CLICK_REPLY,))
    client, _ = _runtime(rt, session, bank, model)
    conversation = create(client, title=TITLE)
    conversation_id = conversation["conversation_id"]
    card, _ = _walk(client, conversation_id)

    done = _click(client, conversation_id, card, code="123456")

    assert done.status_code == 200, done.text
    assert "123456" not in done.text
    assert "123456" not in json.dumps(messages(client, conversation_id))
    assert "123456" not in json.dumps(model.prompts)
    assert "123456" not in client.get("/api/audit/run-events").text
    assert "123456" not in json.dumps(_card(client, conversation_id))


# ------------------------------------------------------------------ 取消定时转账


def test_cancel_scheduled_transfer_goes_through_a_card_and_the_gateway(rt, session):
    bank = TransferBank()
    client, _ = _runtime(
        rt,
        session,
        bank,
        _model(tool="transfer_cancel", arguments={"task_id": "task_1"}, extra=(CLICK_REPLY,)),
    )
    conversation = create(client, title=TITLE)
    conversation_id = conversation["conversation_id"]
    response = submit(client, conversation_id, "取消定时转账 task_1")
    assert response.status_code == 200, response.text

    card = _card(client, conversation_id)
    assert card["kind"] == "confirmation"
    assert card["confirmation"]["pending_id"].startswith("tc_")
    assert not any("cancel" in item["path"] for item in bank.requests)

    done = _click(client, conversation_id, card)

    assert done.status_code == 200, done.text
    cancels = _writes(bank, "/transfers/scheduled/task_1/cancel")
    assert len(cancels) == 1
    assert cancels[0]["idempotency_key"]
    assert "transfer_cancel" in client.get("/api/audit/run-events").text


# ------------------------------------------------------------------ AA 收款


def test_an_aa_collect_card_creates_the_request_on_confirm(rt, session):
    bank = TransferBank()
    client, _ = _runtime(
        rt,
        session,
        bank,
        _model(
            tool="transfer_aa",
            arguments={"amount_yuan": "300.00", "participant_count": 3},
            extra=(CLICK_REPLY,),
        ),
    )
    conversation = create(client, title=TITLE)
    conversation_id = conversation["conversation_id"]

    response = submit(client, conversation_id, "给同事AA收款300元")
    assert response.status_code == 200, response.text

    card = _card(client, conversation_id)
    assert card["kind"] == "confirmation", card
    assert card["confirmation"]["operation"] == "aa_collect"
    assert card["confirmation"]["pending_id"].startswith("taa_")
    assert card["confirmation"]["requires_code"] is False
    assert _writes(bank, "/transfers/aa") == []

    done = _click(client, conversation_id, card)

    assert done.status_code == 200, done.text
    assert _turn_status(done) == "completed"
    assert "AA 收款已发起" in done.text
    creates = _writes(bank, "/transfers/aa")
    assert len(creates) == 1
    assert len(creates[0]["json"]["participants"]) == 3
    assert creates[0]["json"]["confirm"] is True
    assert creates[0]["idempotency_key"]
    # 发起 AA 是收款请求，绝不能顺手出现一笔即时转账。
    assert _writes(bank, "/transfers") == []


def test_an_aa_share_red_card_refuses_a_click_without_a_code(rt, session):
    bank = TransferBank()
    client, _ = _runtime(
        rt,
        session,
        bank,
        _model(
            tool="transfer_aa_pay",
            arguments={"aa_request_id": "aa_1"},
            extra=(CLICK_REPLY,),
        ),
    )
    conversation = create(client, title=TITLE)
    conversation_id = conversation["conversation_id"]

    response = submit(client, conversation_id, "付我那一份")
    assert response.status_code == 200, response.text

    card = _card(client, conversation_id)
    assert card["confirmation"]["operation"] == "aa_payment"
    assert card["confirmation"]["requires_code"] is True
    assert card["confirmation"]["amount_minor"] == 150000

    refused = _click(client, conversation_id, card)

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "VERIFICATION_CODE_REQUIRED"
    assert _writes(bank, "/transfers/aa/aa_1/pay") == []

    done = _click(client, conversation_id, card, code="123456")

    assert done.status_code == 200, done.text
    assert _turn_status(done) == "completed"
    pays = _writes(bank, "/transfers/aa/aa_1/pay")
    assert len(pays) == 1
    assert pays[0]["json"]["participant_index"] == 0
    assert pays[0]["json"]["mfa_code"] == "123456"
    # 付份额只走 AA 支付路径，不是一笔普通转账。
    assert _writes(bank, "/transfers") == []


# ------------------------------------------------------------------ 选择与其它


def test_two_same_named_contacts_are_chosen_before_the_card(rt, session):
    bank = TransferBank(
        contacts=[
            CONTACTS[0],
            {**CONTACTS[0], "contact_id": "c_9", "recipient_account_id": "a_partner_save"},
        ]
    )
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"办理转账"}'),
            _prepare_reply(SMALL),
            _prepare_reply(SMALL),
            CLICK_REPLY,
        ]
    )
    client, _ = _runtime(rt, session, bank, model)
    conversation = create(client, title=TITLE)
    conversation_id = conversation["conversation_id"]

    response = submit(client, conversation_id, "从工资卡转给老王")
    assert response.status_code == 200, response.text
    selection = _card(client, conversation_id)
    assert selection["kind"] == "transfer_selection", selection
    options = selection["fields"][0]["options"]
    assert len(options) == 2

    chosen = submit(
        client,
        conversation_id,
        options[1],
        context={
            "resume": {
                "question_id": selection["question_id"],
                "value": {"selection": options[1]},
            }
        },
    )

    assert chosen.status_code == 200, chosen.text
    card = _card(client, conversation_id)
    assert card["kind"] == "confirmation"
    assert card["confirmation"]["product_name"] == "老王"


def test_a_reservation_request_is_not_executed_as_an_immediate_transfer(rt, session):
    bank = TransferBank()
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"办理转账"}'),
            "当前不支持预约转账。",
            "当前不支持预约转账。",
        ]
    )
    client, _ = _runtime(rt, session, bank, model)
    conversation = create(client, title=TITLE)

    response = submit(client, conversation["conversation_id"], "明天 9 点转 5000 给老王")

    assert response.status_code == 200, response.text
    assert _writes(bank) == []
    assert "不支持预约" in response.text
    assert _card(client, conversation["conversation_id"]) is None


def test_a_plain_message_cannot_answer_an_open_card(rt, session):
    bank = TransferBank()
    model = _model(SMALL, extra=(CLICK_REPLY,))
    client, _ = _runtime(rt, session, bank, model)
    conversation = create(client, title=TITLE)
    conversation_id = conversation["conversation_id"]
    card, _ = _walk(client, conversation_id)
    prompts_before = len(model.prompts)

    response = submit(client, conversation_id, "确认")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert len(model.prompts) == prompts_before
    assert _writes(bank) == []
    still = _card(client, conversation_id)
    assert still["question_id"] == card["question_id"]
    assert still["attempt"] == 1
