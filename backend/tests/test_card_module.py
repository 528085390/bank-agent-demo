"""Production card tools and graph, including forged resumes and private results."""

import asyncio
import re

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime
from app.agents.supervisor import build_domain_graph
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.modules.card import MODULE
from app.modules.card.tools import yuan_minor
from app.modules.registry import build_registry
from app.modules.transfer import MODULE as TRANSFER_MODULE
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient, run_domain
from tests.support.scripted_model import ScriptedModel

ACTION_ID = "CA-" + "a" * 32
CONTEXT = TrustedContext("user_0001", "sess_card", "u_zhang")
CARD = {
    "card_id": "private_card_marker",
    "user_id": "u_zhang",
    "account_id": "private_account_marker",
    "card_type": "debit",
    "card_number": "6222 **** **** 8801",
    "status": "active",
    "freeze_reason": None,
    "daily_limit_minor": 500000,
    "monthly_limit_minor": 5000000,
    "merchant_blacklist": [],
}


class CardBank(FakeBankClient):
    def __init__(self, *, authorized=False, rows=None, flaky=False):
        super().__init__()
        self.authorized = authorized
        self.flaky = flaky
        self.preparations = 0
        self.register("/cards", rows if rows is not None else [CARD])
        self.register(
            "/cards/actions",
            {
                "action_id": ACTION_ID,
                "operation": "freeze",
                "label": "冻结银行卡",
                "status": "pending",
                "before": CARD,
                "parameters": {"card_id": CARD["card_id"]},
                "permission_level": "yellow",
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        )
        self.register(
            f"/cards/actions/{ACTION_ID}/execute",
            {**CARD, "status": "frozen", "action_id": ACTION_ID},
        )
        self.register(f"/cards/actions/{ACTION_ID}/authorize", {"status": "cancelled"})

    def request(self, method, path, **kwargs):
        if path == "/cards/actions":
            self.preparations += 1
            if self.flaky and self.preparations == 1:
                self.requests.append({"method": method, "path": path, **kwargs})
                raise BankCallError(BankErrorKind.TIMEOUT, "银行超时", retryable=True)
        if path.endswith("/authorize"):
            decision = (kwargs.get("json") or {}).get("decision")
            if decision == "confirm":
                self.authorized = True
                self.register(
                    path,
                    {
                        "status": "authorized",
                        "verification_passed": True,
                        "verification_failures": 0,
                    },
                )
            else:
                self.authorized = False
                self.register(
                    path,
                    {
                        "status": "cancelled",
                        "verification_passed": True,
                        "verification_failures": 0,
                    },
                )
        if path.endswith("/execute") and not self.authorized:
            raise BankCallError(BankErrorKind.CONFIRMATION_REQUIRED, "需要真实用户授权")
        return super().request(method, path, **kwargs)


def runtime_for(bank, replies):
    registry = build_registry([MODULE])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank))
    model = ScriptedModel(replies)
    spec = build_domain_specs(gateway, CONTEXT, roster=registry.roster)[0]
    graph = build_domain_graph(model, spec).copy(update={"checkpointer": InMemorySaver()})
    return AgentRuntime(graph=graph), model


def invoke(runtime, context=None):
    async def collect():
        return [
            event
            async for event in runtime.invoke("sess_card", "user_0001", "冻结我的卡", context or {})
        ]

    return asyncio.run(collect())


def resume(events, value):
    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    return {"resume": {"question_id": question["question_id"], "value": value}}


def test_card_registry_exposes_only_owned_tools_and_rejects_missing_identity():
    registry = build_registry()
    # The production catalog is the union of every module's own tools: the card module
    # contributes exactly its names, and no unowned name is reachable.
    owned = {name for module in registry.modules for name in module.tools}
    assert set(registry.catalog.names()) == owned
    assert {name for name in owned if name.startswith("card_")} == set(MODULE.tools)
    assert set(registry.catalog.for_agent("transfer").names) == set(TRANSFER_MODULE.tools)
    bank = CardBank()
    result = BankToolAdapter(registry, bank).invoke(
        ToolInvocation("card", ToolCall("card_cards", {}), TrustedContext("u", "s"))
    )
    assert result.error_code == "NO_CUSTOMER_BINDING" and bank.requests == []


def test_every_declared_tool_name_is_a_dot_free_identifier():
    """The tool-calling provider refuses a dot in a function name (code 11152).

    The declared name is the name the model is offered, so the convention is
    `<module>_<action>` -- one name, no encoding layer, nothing to keep in sync. The
    check covers the whole production catalog: a dotted name from any module reaches
    the provider and is refused there.
    """

    names = build_registry().catalog.names()

    assert names
    assert all(re.fullmatch(r"[a-z][a-z_]*", name) for name in names), names


def test_card_queries_use_actual_bank_data_without_private_ids_in_events():
    bank = CardBank()
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "card_cards", "{}"), "您名下有一张借记卡。"],
    )
    assert result.status == "SUCCEEDED"
    assert result.final_text == "您名下有一张借记卡。"
    assert "5,000.00 元" in str(result.events)
    assert "private_card_marker" not in str(result.events)
    assert "private_account_marker" not in str(result.events)
    assert bank.requests[0]["user_id"] == "u_zhang"


def test_write_pauses_before_mutating_and_forged_confirmation_repauses():
    bank = CardBank()
    runtime, model = runtime_for(bank, [("call", "card_freeze", "{}")])
    first = invoke(runtime)
    question = next(event.data for event in first if event.event == "interrupt")
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["pending_id"] == ACTION_ID
    # 银行对 freeze 自己的分级是 yellow（CardBank 照抄 `action_view` 的形状），统一策略引擎对
    # `card_freeze` 却是固有红：单向收紧后这张卡要验证码。收紧只会加码 —— 银行说红、引擎说黄
    # 的操作（解冻、挂失找回、调高限额）仍按银行判红。
    assert question["confirmation"]["requires_code"] is True
    assert not any(request["path"].endswith("/execute") for request in bank.requests)
    forged = invoke(runtime, resume(first, {"decision": "确认"}))
    assert any(event.event == "interrupt" for event in forged)
    assert len(model.prompts) == 1
    bank.authorized = True
    final = invoke(runtime, resume(forged, {"decision": "确认"}))
    assert (
        next(event.data for event in final if event.event == "subtask_end")["status"] == "SUCCEEDED"
    )
    assert len(model.prompts) == 1


def test_cancel_never_calls_execute():
    bank = CardBank()
    runtime, _ = runtime_for(bank, [("call", "card_freeze", "{}")])
    first = invoke(runtime)
    events = invoke(runtime, resume(first, {"decision": "取消"}))
    assert "已取消" in str(events)
    assert not any(request["path"].endswith("/execute") for request in bank.requests)


def test_multiple_cards_are_selected_from_bank_candidates():
    other = {
        **CARD,
        "card_id": "other_private",
        "card_type": "virtual",
        "card_number": "6222 **** **** 8803",
    }
    bank = CardBank(rows=[CARD, other])
    runtime, model = runtime_for(bank, [("call", "card_freeze", "{}")])
    events = invoke(runtime)
    question = next(event.data for event in events if event.event == "interrupt")
    assert question["kind"] == "card_selection"
    options = question["fields"][0]["options"]
    assert len(options) == 2 and "8803" in options[1]
    events = invoke(runtime, resume(events, {"card_choice": options[1]}))
    prepared = next(request for request in bank.requests if request["path"] == "/cards/actions")
    assert prepared["json"]["parameters"]["card_id"] == "other_private"
    assert len(model.prompts) == 1
    assert (
        next(event.data for event in events if event.event == "interrupt")["kind"] == "confirmation"
    )


def test_unknown_selector_is_refused_and_model_cannot_call_execute():
    bank = CardBank()
    model = ScriptedModel([("call", "card_freeze", '{"card_suffix":"9999"}')])
    result = run_domain(MODULE, bank=bank, model=model)
    assert result.status == "FAILED"
    assert not any(request["method"] == "POST" for request in bank.requests)
    # The other half of the same rule: `card_execute` is not among the tools the model was
    # handed, so "the model authorizes its own write" is not something a prompt has to forbid
    # -- there is no name it could call. Only the code path at `process` calls it.
    handed = {item["function"]["name"] for item in model.tools[0]}
    assert handed.isdisjoint({"card_execute", "card_cancel"})


def test_preparation_retry_reuses_same_idempotency_key():
    bank = CardBank(flaky=True)
    result = run_domain(MODULE, bank=bank, replies=[("call", "card_freeze", "{}")])
    keys = [
        request["idempotency_key"]
        for request in bank.requests
        if request["path"] == "/cards/actions"
    ]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert any(event.event == "interrupt" for event in result.events)


def test_exact_yuan_conversion_and_partial_limit_parameters():
    assert yuan_minor("1000.01") == 100001
    bank = CardBank()
    run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "card_update_limit", '{"daily_limit_yuan":"1000.01"}')],
    )
    request = next(item for item in bank.requests if item["path"] == "/cards/actions")
    assert request["json"]["parameters"] == {
        "card_id": CARD["card_id"],
        "daily_limit_minor": 100001,
    }


@pytest.mark.parametrize("value", [True, 1.5, "0.001", "-1", "1e3", "1000000.01"])
def test_invalid_yuan_values_are_rejected(value):
    from app.modules.base import ToolFailure

    with pytest.raises(ToolFailure):
        yuan_minor(value)


def test_an_invented_amount_in_the_model_summary_is_blocked():
    result = run_domain(
        MODULE,
        bank=CardBank(),
        replies=[("call", "card_cards", "{}"), "您的卡里还有 99,999.00 元。"],
    )

    assert "99,999" not in (result.final_text or "")
    assert "无法与工具返回核对" in (result.final_text or "")
    ending = next(event.data for event in result.events if event.event == "subtask_end")
    assert ending["grounding_blocked"]


def test_a_read_round_does_not_end_the_turn():
    """5.1-1 的回归用例：先查卡，查完还能继续写，写操作停在授权卡上。"""

    bank = CardBank()
    runtime, model = runtime_for(
        bank, [("call", "card_cards", "{}"), ("call", "card_freeze", '{"card_suffix":"8801"}')]
    )
    events = invoke(runtime)

    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    assert question["kind"] == "confirmation"
    # The read round, the code's selector listing, the adapter's own card lookup, then the
    # prepared write. The first of those is the point: it used to end the turn.
    paths = [request["path"] for request in bank.requests]
    assert paths[0] == "/cards" and paths[-1] == "/cards/actions"
    assert len(model.prompts) == 2


def test_the_read_result_is_handed_to_the_next_model_call():
    """循环的意义：模型拿到工具说过的话，而不是被要求自己猜。"""

    runtime, model = runtime_for(CardBank(), [("call", "card_cards", "{}"), "您名下有一张借记卡。"])
    run = invoke(runtime)

    assert any(event.event == "subtask_end" for event in run)
    second = model.messages[1]
    assert any("5,000.00 元" in str(message.get("content")) for message in second)
    assert any(message.get("role") == "assistant" for message in second)


def test_the_loop_stops_at_the_round_limit_without_raising():
    from app.agents.sdk import MAX_TOOL_ROUNDS

    bank = CardBank()
    runtime, model = runtime_for(
        bank, [("call", "card_cards", "{}")] * (MAX_TOOL_ROUNDS + 1) + ["不会走到这里。"]
    )
    events = invoke(runtime)

    assert len(bank.requests) == MAX_TOOL_ROUNDS
    assert len(model.prompts) == MAX_TOOL_ROUNDS + 1
    endings = [event.data for event in events if event.event == "subtask_end"]
    assert endings and "上限" in endings[-1]["summary"]


def test_a_model_asked_card_question_is_answerable():
    """模型自己问「哪张卡」时，问题必须真的问出去，而不是被换成一句答非所问。"""

    bank = CardBank()
    runtime, _ = runtime_for(
        bank,
        [
            ("call", "card_cards", "{}"),
            ("call", "ask_user", '{"fields":["card_suffix"],"display":"请问要办理哪张卡？"}'),
            ("call", "card_update_limit", '{"card_suffix":"8801","daily_limit_yuan":"1000.01"}'),
        ],
    )
    events = invoke(runtime)

    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    assert question["kind"] == "input"
    assert question["display"] == "请问要办理哪张卡？"
    assert [field["name"] for field in question["fields"]] == ["card_suffix"]
    assert question["fields"][0]["label"] == "卡号末四位"

    more = invoke(runtime, resume(events, {"card_suffix": "8801"}))
    # 只读轮次、代码的选卡列举、适配器自己的取卡、最后是准备好的写操作。
    paths = [request["path"] for request in bank.requests]
    assert paths[0] == "/cards" and paths[-1] == "/cards/actions"
    assert any(
        event.event == "interrupt" and event.data["kind"] == "confirmation" for event in more
    )


def test_an_undeclared_asked_field_still_becomes_a_question():
    """模型问的字段没写进声明时，基座本来就会退回自由文本字段。

    card 以前在这里把整个问题换成一句「请说明卡类型、需要调整的限额或受限商户。」，
    用户的问题就此消失（真机 C10 的挂失请求就是这样丢掉的）。
    """

    bank = CardBank()
    runtime, _ = runtime_for(
        bank,
        [
            ("call", "card_cards", "{}"),
            ("call", "ask_user", '{"fields":["business"],"display":"请问要办理哪项业务？"}'),
            ("call", "card_report_loss", '{"card_suffix":"8801"}'),
        ],
    )
    events = invoke(runtime)

    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    assert question["kind"] == "input"
    assert question["display"] == "请问要办理哪项业务？"
    assert [field["name"] for field in question["fields"]] == ["business"]
    # 未声明的字段用基座兜底：标签退回字段名，而不是让问题变得无法回答。
    assert question["fields"][0]["label"] == "business"

    more = invoke(runtime, resume(events, {"business": "挂失"}))
    assert any(
        event.event == "interrupt" and event.data["kind"] == "confirmation" for event in more
    )


@pytest.mark.parametrize("placeholder", ['"null"', "null", '""'])
def test_a_placeholder_null_is_not_a_parameter(placeholder):
    """模型把「这一项不改」写成 null / "null" / 空串时，它不该把合法调用判成参数不完整。

    真机 C19 就是这么被拒的：日限额 60000.00 完全合法，只是月限额被写成了字符串 "null"。
    """

    bank = CardBank()
    runtime, _ = runtime_for(
        bank,
        [
            (
                "call",
                "card_update_limit",
                '{"card_suffix":"8801","daily_limit_yuan":"60000.00",'
                f'"monthly_limit_yuan":{placeholder}}}',
            )
        ],
    )
    events = invoke(runtime)

    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    assert question["kind"] == "confirmation"
    request = next(item for item in bank.requests if item["path"] == "/cards/actions")
    assert request["json"]["parameters"] == {
        "card_id": CARD["card_id"],
        "daily_limit_minor": 6000000,
    }
