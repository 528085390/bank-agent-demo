"""The subscription module answers through its own graph with native read and write tools."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime
from app.agents.supervisor import build_domain_graph
from app.core.errors import AppError
from app.modules.base import ToolFailure
from app.modules.registry import build_registry
from app.modules.subscription import MODULE
from app.modules.subscription.tools import SPECS, prepare
from app.modules.subscription.workflow import (
    PENDING_EXPIRY_MINUTES,
    authorization_question,
    expired,
)
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import (
    ErrorCode,
    SideEffect,
    ToolCall,
    ToolInvocation,
    ToolStatus,
    TrustedContext,
)
from tests.support.domain_harness import FakeBankClient, run_domain
from tests.support.scripted_model import ScriptedModel

SUB_ONE = {
    "subscription_id": "sub_1",
    "user_id": "u_zhang",
    "from_account_id": "acc_1",
    "name": "视频会员",
    "merchant_name": "某视频",
    "amount_minor": 2500,
    "frequency": "monthly",
    "start_date": "2026-01-01",
    "next_due_date": "2026-11-01",
    "status": "active",
    "auto_renew": True,
    "last_charge_id": None,
}

WRITE_TOOLS = (
    "subscription_cancel",
    "subscription_pause",
    "subscription_resume",
    "subscription_charge",
)


def bound_spec(bank):
    """The module's own DomainSpec, built the way its graph is built."""

    registry = build_registry([MODULE])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank))
    context = TrustedContext(user_id="user_0001", session_id="sess_sub", bank_customer_id="u_zhang")
    return build_domain_specs(gateway, context, roster=registry.roster)[0]


def write_invocation(tool, arguments, key="idem-write-1"):
    """A ToolInvocation aimed straight at a write handler, so a unit test skips the graph."""

    return ToolInvocation(
        agent_id="subscription",
        call=ToolCall(tool_name=tool, arguments=arguments, idempotency_key=key),
        context=TrustedContext(
            user_id="user_0001", session_id="sess_sub", bank_customer_id="u_zhang"
        ),
    )


def parked_graph(bank, replies):
    """The module graph with its own saver, so a test can read the checkpoint back.

    The shared harness keeps its checkpointer private, and spec §3.4 is a claim about what the
    *checkpoint* holds, so this test builds the graph the way the card tests do.
    """

    model = ScriptedModel(replies)
    graph = build_domain_graph(model, bound_spec(bank)).copy(
        update={"checkpointer": InMemorySaver()}
    )
    return graph, model


def test_a_read_tool_answers_through_the_module_graph():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_list", "{}"), "你有一笔自动续费。"],
        message="我有哪些自动续费？",
    )
    assert [request["path"] for request in bank.requests] == ["/subscriptions"]
    assert result.final_text == "你有一笔自动续费。"

def test_an_invented_price_in_the_model_summary_is_blocked():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_list", "{}"), "你每月要付 99,999.00 元。"],
        message="我有哪些自动续费？",
    )
    assert result.status == "FAILED"
    assert "99,999" not in result.final_text
    assert "无法与工具返回核对" in result.final_text
    ending = next(event for event in result.events if event.event == "subtask_end")
    assert ending.data["grounding_blocked"]


def test_detail_answers_for_one_subscription():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_detail", '{"subscription_id":"sub_1"}'), "详情如上。"],
        message="视频会员的详情？",
    )
    assert result.status == "SUCCEEDED"
    assert result.final_text == "详情如上。"
    assert [request["path"] for request in bank.requests] == ["/subscriptions"]


def test_detail_with_an_unknown_id_is_not_found():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_detail", '{"subscription_id":"sub_404"}')],
    )
    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：没有找到对应的订阅。"


def test_detail_can_be_located_by_display_name_or_merchant_name():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])

    by_name = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_detail", '{"name":"视频会员"}'), "详情如上。"],
        message="视频会员的详情？",
    )
    by_merchant = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_detail", '{"merchant_name":"某视频"}'), "详情如上。"],
        message="某视频的详情？",
    )

    assert by_name.status == "SUCCEEDED"
    assert by_merchant.status == "SUCCEEDED"


def test_detail_with_an_unknown_name_is_not_found():
    """A selector the schema accepts has to take part in the filter, not just be tolerated.

    The mutant this pins keeps `name` in the tool's schema but drops it from the filter: with
    one subscription registered, a caller who names a subscription that does not exist still
    gets that one row back, as SUCCEEDED. That is a wrong answer -- the user asked about
    something that is not there -- so it has to be a NOT_FOUND, not a hit.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        # The trailing reply is never reached while the filter works: a NOT_FOUND ends the
        # turn. It is here so the mutant -- which answers SUCCEEDED -- reaches the assertion
        # below instead of running the script dry and failing for the wrong reason.
        replies=[("call", "subscription_detail", '{"name":"不存在的名称"}'), "详情如上。"],
        message="不存在的名称的详情？",
    )
    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：没有找到对应的订阅。"


def test_detail_without_a_selector_is_ambiguous_with_two_subscriptions():
    bank = FakeBankClient()
    bank.register(
        "/subscriptions",
        [SUB_ONE, {**SUB_ONE, "subscription_id": "sub_2", "name": "音乐会员"}],
    )

    result = run_domain(
        MODULE, bank=bank, replies=[("call", "subscription_detail", "{}")], message="详情？"
    )

    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：请明确选择一笔本人订阅。"


def test_a_duplicate_subscription_id_is_a_validation_failure():
    """`subscription_id` is the bank's primary key, so two rows mean a corrupt response."""

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE, {**SUB_ONE, "name": "重复记录"}])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_detail", '{"subscription_id":"sub_1"}')],
    )
    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：请明确选择一笔本人订阅。"


def test_a_non_array_subscription_response_is_a_protocol_error():
    """The bank answers a bare array; an envelope is a protocol failure, not an empty list."""

    bank = FakeBankClient()
    bank.register("/subscriptions", {"subscriptions": [SUB_ONE]})
    result = run_domain(MODULE, bank=bank, replies=[("call", "subscription_list", "{}")])
    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：订阅列表响应不正确。"


def test_an_empty_listing_answers_with_the_empty_list_message():
    """An empty array is not a failure: it gets the module's own empty-list sentence."""

    bank = FakeBankClient()
    bank.register("/subscriptions", [])
    model = ScriptedModel([("call", "subscription_list", "{}"), "没有订阅。"])
    result = run_domain(MODULE, bank=bank, model=model)
    assert result.status == "SUCCEEDED"
    assert "您目前没有订阅。" in str(model.messages[1])
    assert result.final_text == "没有订阅。"


def test_a_bank_failure_while_listing_is_not_an_empty_list():
    """A non-zero bank code is FAILED with its own code, never `您目前没有订阅。`.

    `subscriptions()` builds the empty-list sentence only from a real empty array, so the
    two outcomes have to stay distinguishable all the way out of the adapter.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [], code=1005, message="resource_not_found")
    adapter = BankToolAdapter(build_registry([MODULE]), bank)
    result = adapter.invoke(
        ToolInvocation(
            agent_id="subscription",
            call=ToolCall(tool_name="subscription_list", arguments={}),
            context=TrustedContext(
                user_id="user_0001", session_id="sess_sub", bank_customer_id="u_zhang"
            ),
        )
    )
    assert result.status is ToolStatus.FAILED
    assert result.error_code == ErrorCode.INVALID_STATE
    assert result.error == "该业务当前状态不允许此操作。"
    assert "您目前没有订阅。" not in (result.error or "")


def test_a_missing_customer_binding_fails_before_any_bank_call():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        customer=None,
        replies=[("call", "subscription_list", "{}")],
    )
    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：当前用户未绑定银行客户号。"
    assert bank.requests == []


def test_the_status_filter_narrows_the_rows_handed_back_to_the_model():
    paused = {
        **SUB_ONE,
        "subscription_id": "sub_2",
        "name": "音乐会员",
        "merchant_name": "某音乐",
        "status": "paused",
    }
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE, paused])
    model = ScriptedModel([("call", "subscription_list", '{"status":"paused"}'), "好。"])
    result = run_domain(MODULE, bank=bank, model=model)
    assert result.status == "SUCCEEDED"
    second = str(model.messages[1])
    assert "音乐会员" in second
    assert "视频会员" not in second


def test_a_write_tool_freezes_a_snapshot_and_asks_for_confirmation():
    """A write tool is snapshot-and-confirm: it reads the subscription, then raises a card.

    Without a click the write endpoint is unreachable, so the bank only ever sees the read.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_pause", '{"subscription_id": "sub_1"}')],
        message="帮我把这个订阅先停一停",
    )
    question = next(event.data for event in result.events if event.event == "interrupt")
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["pending_id"]
    assert question["confirmation"]["requires_code"] is False
    assert [request["method"] for request in bank.requests] == ["GET"]


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_every_write_tool_parks_on_a_confirmation_card_and_only_reads(tool):
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", tool, '{"subscription_id":"sub_1"}')],
        message="帮我改一下这个订阅",
    )
    question = next(event.data for event in result.events if event.event == "interrupt")
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["operation"] == tool.removeprefix("subscription_")
    assert question["confirmation"]["pending_id"].startswith("sub_")
    # Only the read ever reaches the bank; no write endpoint is declared or called.
    assert [request["method"] for request in bank.requests] == ["GET"]
    assert result.status == "WAITING_INPUT"


def test_two_subscriptions_sharing_a_merchant_are_selected_from_bank_candidates():
    """A merchant that matches two rows asks which one, then reaches the confirmation card.

    The labels come from the bank rows, so the model cannot swap in a subscription that is
    not the caller's; the write itself still only happens after the confirmation click.
    """

    other = {
        **SUB_ONE,
        "subscription_id": "sub_2",
        "name": "音乐会员",
        "merchant_name": "某视频",
    }
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE, other])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_pause", '{"merchant_name":"某视频"}')],
        message="把某视频的订阅停一停",
        answer={"subscription_choice": "音乐会员（某视频）"},
    )
    questions = [event.data for event in result.events if event.event == "interrupt"]
    assert questions[0]["kind"] == "subscription_selection"
    assert questions[0]["fields"][0]["options"] == ["视频会员（某视频）", "音乐会员（某视频）"]
    assert questions[1]["kind"] == "confirmation"
    assert questions[1]["confirmation"]["operation"] == "pause"
    # The selection resume goes straight back into the write tool: the model is not consulted.
    assert len(result.prompts) == 1
    # The whole detour is read-only; the write endpoint waits for the confirmation click.
    assert [request["method"] for request in bank.requests] == ["GET", "GET"]


def test_a_merchant_that_names_one_subscription_goes_straight_to_the_confirmation_card():
    """A merchant that matches exactly one row needs no question: the id is filled in and frozen.

    This is the other half of the selection branch: the model names a merchant, the code lists the
    caller's rows, finds one, writes its `subscription_id` into the arguments and freezes the
    snapshot. The model is not consulted again.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_pause", '{"merchant_name":"某视频"}')],
        message="把某视频的订阅停一停",
    )
    questions = [event.data for event in result.events if event.event == "interrupt"]
    assert len(questions) == 1
    assert questions[0]["kind"] == "confirmation"
    assert questions[0]["confirmation"]["operation"] == "pause"
    assert questions[0]["confirmation"]["pending_id"]
    # One read to resolve the merchant, one to freeze the snapshot; no write is called.
    assert [request["method"] for request in bank.requests] == ["GET", "GET"]


def test_an_id_and_a_merchant_that_name_different_subscriptions_match_nothing():
    """An id paired with a merchant that belongs to another row fails closed.

    `_needs_selection` only checks for a missing id, so this pair skips the listing and goes
    straight to `prepare`; `_select_subscription` then ANDs both selectors, so no row matches
    both and the write is refused rather than acting on the id while ignoring the merchant the
    user actually named.
    """

    other = {
        **SUB_ONE,
        "subscription_id": "sub_2",
        "name": "音乐会员",
        "merchant_name": "某音乐",
    }
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE, other])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[
            (
                "call",
                "subscription_pause",
                '{"subscription_id":"sub_1","merchant_name":"某音乐"}',
            )
        ],
        message="停掉某音乐的订阅",
    )
    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：没有找到对应的订阅。"
    assert [request["method"] for request in bank.requests] == ["GET"]


def test_a_parked_write_checkpoints_the_live_thread_and_the_round_count():
    """Spec §3.4: a park must carry `domain_thread` and `tool_rounds`, or a resume restarts.

    Every other test in this file reads the interrupt event, so dropping the two lines that
    carry them leaves all of them green. This one reads the state the checkpoint is holding:
    the model queries first, then asks for the write, so the park happens on round two.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    graph, _model = parked_graph(
        bank,
        [
            ("call", "subscription_list", "{}"),
            ("call", "subscription_pause", '{"subscription_id":"sub_1"}'),
        ],
    )
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [
            event async for event in runtime.invoke("sess_park", "user_0001", "停掉这个订阅", {})
        ]

    events = asyncio.run(run())
    assert any(event.event == "interrupt" for event in events)

    async def read():
        return (await graph.aget_state({"configurable": {"thread_id": "chat:sess_park"}})).values

    state = asyncio.run(read())
    assert state.get("domain_thread")
    assert (state.get("tool_rounds") or 0) >= 1
    assert state["pending_action"]["operation"] == "pause"
    assert state["turn_phase"] == "waiting_for_user"


def test_a_parked_selection_checkpoints_the_live_thread_and_the_round_count():
    """Spec §3.4 for the selection park: it must carry `domain_thread` and `tool_rounds` too.

    The confirmation-park test cannot see this: the two extras are attached to every park on
    the way out of `activate`, so skipping them for the selection park alone leaves that test
    green. This one reads the checkpoint a selection park is holding, after a read round first.
    """

    other = {
        **SUB_ONE,
        "subscription_id": "sub_2",
        "name": "音乐会员",
        "merchant_name": "某视频",
    }
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE, other])
    graph, _model = parked_graph(
        bank,
        [
            ("call", "subscription_list", "{}"),
            ("call", "subscription_pause", '{"merchant_name":"某视频"}'),
        ],
    )
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [
            event
            async for event in runtime.invoke(
                "sess_select_park", "user_0001", "停掉某视频的订阅", {}
            )
        ]

    events = asyncio.run(run())
    assert any(event.event == "interrupt" for event in events)

    state = _checkpoint_values(graph, "sess_select_park")
    assert state.get("domain_thread")
    assert (state.get("tool_rounds") or 0) >= 1
    assert state["subscription_call"]["tool"] == "subscription_pause"
    assert state["subscription_choices"] == {
        "视频会员（某视频）": "sub_1",
        "音乐会员（某视频）": "sub_2",
    }
    assert state["pending_question"]["kind"] == "subscription_selection"
    assert state["turn_phase"] == "waiting_for_user"


def test_prepare_rejects_a_non_positive_amount_before_building_a_confirmation():
    bank = FakeBankClient()
    bank.register("/subscriptions", [{**SUB_ONE, "amount_minor": 0}])
    with pytest.raises(ToolFailure) as raised:
        prepare(write_invocation("subscription_pause", {"subscription_id": "sub_1"}), bank)
    assert raised.value.code == ErrorCode.VALIDATION
    assert [request["method"] for request in bank.requests] == ["GET"]


def test_a_zero_amount_subscription_is_refused_through_the_graph():
    """The direct `prepare` test pins the handler; this pins the failure a user would hit."""

    bank = FakeBankClient()
    bank.register("/subscriptions", [{**SUB_ONE, "amount_minor": 0}])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_pause", '{"subscription_id":"sub_1"}')],
        message="帮我把这个订阅先停一停",
    )
    assert result.status == "FAILED"
    assert result.final_text == "订阅操作未成功：订阅金额不正确，无法办理该操作。"
    assert not any(event.event == "interrupt" for event in result.events)
    assert [request["method"] for request in bank.requests] == ["GET"]


def test_the_frozen_snapshot_carries_the_fields_the_execute_step_needs():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    data = prepare(
        write_invocation("subscription_pause", {"subscription_id": "sub_1"}, key="idem-42"),
        bank,
    )
    action = data.facts["action"]
    assert set(action) == {
        "pending_id",
        "operation",
        "subscription_id",
        "product_name",
        "merchant_name",
        "amount_minor",
        "account_display",
        "frequency",
        "next_due_date",
        "expires_at",
        "idempotency_key",
    }
    assert action["pending_id"].startswith("sub_")
    assert action["operation"] == "pause"
    assert action["subscription_id"] == "sub_1"
    assert action["product_name"] == "视频会员"
    assert action["merchant_name"] == "某视频"
    assert action["amount_minor"] == 2500
    assert action["account_display"] == "acc_1"
    assert action["frequency"] == "monthly"
    assert action["next_due_date"] == "2026-11-01"
    assert action["idempotency_key"] == "idem-42"
    assert data.summary == "操作详情已准备，尚未修改订阅，等待用户确认。"
    # The deadline parses and lies in the future, so the snapshot is open right now.
    datetime.fromisoformat(action["expires_at"])
    assert not expired(action)


def test_expired_is_true_past_the_window_and_false_inside_it():
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    inside = {"expires_at": (now + timedelta(minutes=PENDING_EXPIRY_MINUTES - 1)).isoformat()}
    past = {"expires_at": (now - timedelta(seconds=1)).isoformat()}
    assert not expired(inside, now=now)
    assert expired(past, now=now)
    # A snapshot with no usable deadline is expired, never open-ended.
    assert expired({}, now=now)
    assert expired({"expires_at": "not-a-timestamp"}, now=now)


def test_the_confirmation_preview_names_the_operation_target_and_amount():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_pause", '{"subscription_id":"sub_1"}')],
        message="帮我把这个订阅先停一停",
    )
    question = next(event.data for event in result.events if event.event == "interrupt")
    assert "暂停订阅" in question["display"]
    assert "视频会员" in question["display"]
    assert "25.00 元" in question["display"]
    assert question["confirmation"]["title"] == "暂停订阅"
    assert question["confirmation"]["amount_minor"] == 2500
    assert question["confirmation"]["product_name"] == "视频会员"
    assert question["confirmation"]["expires_at"]
    # The front end drops an interrupt whose question carries no field to answer with.
    assert question["fields"] == [
        {
            "name": "decision",
            "label": "操作决定",
            "type": "select",
            "required": True,
            "options": ["确认", "取消"],
        }
    ]


def test_the_charge_preview_asks_for_an_immediate_amount():
    action = {
        "pending_id": "sub_x",
        "operation": "charge",
        "product_name": "视频会员",
        "merchant_name": "某视频",
        "amount_minor": 2500,
        "account_display": "acc_1",
        "frequency": "monthly",
        "next_due_date": "2026-11-01",
        "expires_at": "2026-10-14T12:05:00+00:00",
    }
    question = authorization_question(action)
    assert "立即扣款" in question["display"]
    assert "25.00 元" in question["display"]
    assert question["confirmation"]["operation"] == "charge"
    assert question["confirmation"]["requires_code"] is False
    assert question["fields"]


def _park_and_question(graph, session, message):
    """Drive one graph to its confirmation card and return the open question id."""

    runtime = AgentRuntime(graph=graph)

    async def run():
        events = [event async for event in runtime.invoke(session, "user_0001", message, {})]
        return next(event.data["question_id"] for event in events if event.event == "interrupt")

    return asyncio.run(run())


def _resume(graph, session, question_id, value):
    """Answer the open question on the same checkpoint and collect what came back."""

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
    """The state a parked checkpoint holds, read straight back out of the saver."""

    config = {"configurable": {"thread_id": f"chat:{session}"}}
    return asyncio.run(graph.aget_state(config)).values


def _rewrite_checkpoint(graph, session, transform):
    """Rewrite the parked checkpoint in place, as an expired or foreign card would be.

    ``graph.aupdate_state`` publishes a fresh checkpoint and drops the pending
    interrupt, so the frozen channel value is replaced in the checkpointer's blob
    store instead: the open question and ``next`` are left exactly as they were.
    """

    thread_id = f"chat:{session}"
    saver = graph.checkpointer
    latest = max(saver.storage[thread_id][""].keys())
    checkpoint, _metadata, _parent = saver.storage[thread_id][""][latest]
    versions = saver.serde.loads_typed(checkpoint)["channel_versions"]
    values = _checkpoint_values(graph, session)
    for channel, value in transform(dict(values)).items():
        saver.blobs[(thread_id, "", channel, versions[channel])] = saver.serde.dumps_typed(value)


def _ending_status(events):
    ending = next(event for event in reversed(events) if event.event == "subtask_end")
    return ending.data["status"]


@pytest.mark.parametrize("decision", ["confirm", "确认"])
def test_confirm_executes_the_frozen_operation(decision):
    """Both the button's machine value and the card's label mean "go"."""

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    bank.register("/subscriptions/sub_1/pause", {**SUB_ONE, "status": "paused"})
    graph, model = parked_graph(
        bank, [("call", "subscription_pause", '{"subscription_id":"sub_1"}')]
    )
    session = "sess_confirm"
    question_id = _park_and_question(graph, session, "帮我把这个订阅先停一停")
    frozen = _checkpoint_values(graph, session)["pending_action"]
    events = _resume(graph, session, question_id, {"decision": decision})
    assert _ending_status(events) == "SUCCEEDED"
    posts = [request for request in bank.requests if request["method"] == "POST"]
    assert [request["path"] for request in posts] == ["/subscriptions/sub_1/pause"]
    # The write reuses the key frozen with the snapshot instead of minting a fresh one.
    assert posts[0]["idempotency_key"] == frozen["idempotency_key"]
    # Confirming does not hand the decision back to the model.
    assert len(model.prompts) == 1


def test_a_rejected_write_is_failed_with_its_own_code():
    """A bank rejection on the click path is FAILED with the bank's own code, not a success.

    The bank refuses the pause (1005 -> INVALID_STATE). The click has to come back FAILED, the
    classified code has to be the one surfaced, and the refusal still costs exactly one write
    attempt on the key the snapshot froze -- a regression that turned this into a success, or
    reported a different code, must not pass.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    bank.register("/subscriptions/sub_1/pause", {}, code=1005, message="resource_not_found")
    graph, _model = parked_graph(
        bank, [("call", "subscription_pause", '{"subscription_id":"sub_1"}')]
    )
    session = "sess_rejected"
    question_id = _park_and_question(graph, session, "帮我把这个订阅先停一停")
    frozen = _checkpoint_values(graph, session)["pending_action"]
    events = _resume(graph, session, question_id, {"decision": "confirm"})
    ending = next(event for event in reversed(events) if event.event == "subtask_end")
    assert ending.data["status"] == "FAILED"
    assert "订阅操作未成功" in ending.data["summary"]
    assert "已完成" not in ending.data["summary"]
    posts = [request for request in bank.requests if request["method"] == "POST"]
    assert [request["path"] for request in posts] == ["/subscriptions/sub_1/pause"]
    assert posts[0]["idempotency_key"] == frozen["idempotency_key"]

    # `subtask_end` carries the status and the module's summary, not the code. The code lives on
    # the ToolResult the hidden execute tool returns, so ask that tool the same question.
    adapter = BankToolAdapter(build_registry([MODULE]), bank)
    result = adapter.invoke(
        ToolInvocation(
            agent_id="subscription",
            call=ToolCall(
                tool_name="subscription_execute",
                arguments={"subscription_id": "sub_1", "operation": "pause"},
                idempotency_key=frozen["idempotency_key"],
            ),
            context=TrustedContext(
                user_id="user_0001", session_id=session, bank_customer_id="u_zhang"
            ),
        )
    )
    assert result.status is ToolStatus.FAILED
    assert result.error_code == ErrorCode.INVALID_STATE


@pytest.mark.parametrize("decision", ["cancel", "取消"])
def test_cancel_never_touches_the_bank(decision):
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "subscription_pause", '{"subscription_id":"sub_1"}')],
        message="帮我把这个订阅先停一停",
        answer={"decision": decision},
    )
    assert result.status == "SUCCEEDED"
    assert result.final_text == "本次操作已取消，未修改订阅。"
    assert [request["method"] for request in bank.requests] == ["GET"]


def test_an_expired_confirmation_is_denied_without_a_write(monkeypatch):
    """A snapshot past its window is refused, through the same public graph API as any resume.

    A negative window makes `prepare` freeze a deadline in the past, so the expiry branch runs
    without reaching into the checkpointer's internals.
    """

    monkeypatch.setattr("app.modules.subscription.workflow.PENDING_EXPIRY_MINUTES", -1)
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    graph, _model = parked_graph(
        bank, [("call", "subscription_pause", '{"subscription_id":"sub_1"}')]
    )
    session = "sess_expired"
    question_id = _park_and_question(graph, session, "停掉这个订阅")
    events = _resume(graph, session, question_id, {"decision": "confirm"})
    assert _ending_status(events) == "DENIED"
    assert not any(request["method"] == "POST" for request in bank.requests)


def test_a_confirmation_without_a_frozen_snapshot_is_denied():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    graph, _model = parked_graph(
        bank, [("call", "subscription_pause", '{"subscription_id":"sub_1"}')]
    )
    session = "sess_no_snapshot"
    question_id = _park_and_question(graph, session, "停掉这个订阅")
    _rewrite_checkpoint(graph, session, lambda _values: {"pending_action": None})
    events = _resume(graph, session, question_id, {"decision": "confirm"})
    assert _ending_status(events) == "DENIED"
    assert not any(request["method"] == "POST" for request in bank.requests)


def test_a_malformed_frozen_snapshot_is_denied():
    """A snapshot missing a write field fails closed instead of raising inside the node.

    The snapshot is otherwise valid -- a future deadline and a real operation -- so the
    pre-existing expiry check cannot be what denies it; only the missing-field check can.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    graph, _model = parked_graph(
        bank, [("call", "subscription_pause", '{"subscription_id":"sub_1"}')]
    )
    session = "sess_malformed"
    question_id = _park_and_question(graph, session, "停掉这个订阅")
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    _rewrite_checkpoint(
        graph,
        session,
        lambda _values: {
            "pending_action": {"pending_id": "sub_x", "operation": "pause", "expires_at": future}
        },
    )
    events = _resume(graph, session, question_id, {"decision": "confirm"})
    assert _ending_status(events) == "DENIED"
    assert not any(request["method"] == "POST" for request in bank.requests)


def test_a_resume_that_names_a_stale_question_is_a_protocol_error():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    graph, _model = parked_graph(
        bank, [("call", "subscription_pause", '{"subscription_id":"sub_1"}')]
    )
    session = "sess_stale"
    _park_and_question(graph, session, "停掉这个订阅")
    with pytest.raises(AppError) as failure:
        _resume(graph, session, "q_not_the_one", {"decision": "confirm"})
    assert failure.value.code == "INVALID_INTERRUPT_ANSWER"
    assert not any(request["method"] == "POST" for request in bank.requests)


def test_a_selection_answer_outside_the_candidates_is_denied_without_a_write():
    """A crafted label must not become a subscription id the user never chose.

    The question's `options` are a UI hint, not a guard: `_read_answer` checks the key set, not
    membership, so the refusal has to live in `process`. This resumes the parked selection with
    a label that was never offered and asserts the denial happens before any bank call.
    """

    other = {
        **SUB_ONE,
        "subscription_id": "sub_2",
        "name": "音乐会员",
        "merchant_name": "某视频",
    }
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE, other])
    graph, _model = parked_graph(
        bank,
        [("call", "subscription_pause", '{"merchant_name":"某视频"}')],
    )
    session = "sess_bogus_choice"
    question_id = _park_and_question(graph, session, "停掉某视频的订阅")
    events = _resume(graph, session, question_id, {"subscription_choice": "不存在的订阅（某视频）"})
    assert _ending_status(events) == "DENIED"
    assert not any(request["method"] == "POST" for request in bank.requests)


def test_a_selection_without_its_original_call_is_denied():
    """A stale selection checkpoint fails closed instead of raising inside `process`.

    A crafted resume cannot set `subscription_call` -- it is checkpoint state, not answer data --
    so this reaches the guard the way the malformed-snapshot tests do: by rewriting the parked
    checkpoint. Without the guard, `call["arguments"]` would raise instead of denying.
    """

    other = {
        **SUB_ONE,
        "subscription_id": "sub_2",
        "name": "音乐会员",
        "merchant_name": "某视频",
    }
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE, other])
    graph, _model = parked_graph(
        bank,
        [("call", "subscription_pause", '{"merchant_name":"某视频"}')],
    )
    session = "sess_selection_stale"
    question_id = _park_and_question(graph, session, "停掉某视频的订阅")
    _rewrite_checkpoint(graph, session, lambda _values: {"subscription_call": None})
    events = _resume(graph, session, question_id, {"subscription_choice": "视频会员（某视频）"})
    assert _ending_status(events) == "DENIED"
    assert not any(request["method"] == "POST" for request in bank.requests)


def test_a_model_naming_the_hidden_execute_tool_is_denied_without_a_bank_call():
    """The denial `run_call` documents has to be reachable through the public graph.

    `known` is built from the tools the model was offered, so `parse_domain_decision` rejects
    `subscription_execute` with a `DecisionError` before `run_call` can see the call. `activate`
    has to turn that rejection into the DENIED ending its guard promises, not let the error
    escape the turn.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[
            ("call", "subscription_execute", '{"subscription_id":"sub_1","operation":"pause"}')
        ],
        message="直接帮我暂停这个订阅",
    )
    assert result.status == "DENIED"
    assert result.final_text == "该订阅操作不被允许。"
    assert bank.requests == []


def test_a_decision_that_is_not_a_hidden_tool_is_failed_not_denied():
    """A parse rejection that is not a hidden-tool name is FAILED, not DENIED.

    `ask_user` is a tool the model was offered, so `parse_domain_decision` rejects the call
    because it named no fields -- a malformed turn, not a forbidden operation. Reporting it as
    DENIED would blame the user for the model's slip.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    result = run_domain(
        MODULE,
        bank=bank,
        replies=[("call", "ask_user", "{}")],
    )
    assert result.status == "FAILED"
    assert result.final_text == "模型没有给出可用的订阅信息，请重新说明。"
    assert bank.requests == []


def test_a_confirmation_carrying_a_foreign_pending_id_is_denied_without_a_write():
    """The `pending_id` guard is defence in depth, pinned by making it reachable.

    No public route hands `process` an extra key: `_read_answer` re-asks unless the answer's
    keys are exactly the question's fields, and the confirmation card asks only for `decision`.
    The branch exists so a stale card can never replay against a newer snapshot, so the parked
    checkpoint is rewritten to ask for a `pending_id` too -- the same technique the expired and
    malformed-snapshot tests use -- and the answer then carries a mismatched one.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    bank.register("/subscriptions/sub_1/pause", {**SUB_ONE, "status": "paused"})
    graph, _model = parked_graph(
        bank, [("call", "subscription_pause", '{"subscription_id":"sub_1"}')]
    )
    session = "sess_pending_id"
    question_id = _park_and_question(graph, session, "帮我把这个订阅先停一停")

    def add_pending_id_field(values):
        question = values["pending_question"]
        return {
            "pending_question": {
                **question,
                "fields": [
                    *question["fields"],
                    {
                        "name": "pending_id",
                        "label": "待办标识",
                        "type": "text",
                        "required": False,
                    },
                ],
            }
        }

    _rewrite_checkpoint(graph, session, add_pending_id_field)
    events = _resume(
        graph,
        session,
        question_id,
        {"decision": "confirm", "pending_id": "stale-id"},
    )
    assert _ending_status(events) == "DENIED"
    ending = next(event for event in reversed(events) if event.event == "subtask_end")
    assert ending.data["summary"] == "这笔操作的确认信息已失效，请重新说明。"
    assert not any(request["method"] == "POST" for request in bank.requests)


def test_the_module_graph_has_exactly_one_wait_node():
    """Base `_one_question` only accepts one open interrupt, so the graph parks in one place.

    A second `wait` node would let two cards open at once, and a button's resume would bind to
    whichever `interrupt` the runtime read. Pinning the module-owned node set is what stops a
    future edge from quietly adding another parking spot.
    """

    graph, _model = parked_graph(FakeBankClient(), [])
    nodes = set(graph.get_graph().nodes)
    assert nodes == {"__start__", "decide", "wait", "process", "__end__"}


def test_the_module_declares_two_read_tools_and_five_write_tools():
    names = {item.name for item in SPECS}
    assert names == {
        "subscription_list",
        "subscription_detail",
        "subscription_cancel",
        "subscription_pause",
        "subscription_resume",
        "subscription_charge",
        "subscription_execute",
    }
    assert {item.name for item in SPECS if item.side_effect == SideEffect.READ} == {
        "subscription_list",
        "subscription_detail",
    }
    # The execute tool is a write too, but the graph never offers it to the model.
    assert {item.name for item in SPECS if item.side_effect == SideEffect.WRITE} == set(
        WRITE_TOOLS
    ) | {"subscription_execute"}


def test_a_write_tool_never_steers_the_model_to_a_selector_it_ignores():
    """The four write tools match on `subscription_id` and `merchant_name` only.

    `matched_subscriptions` ignores `name`, so a write tool that reuses the detail tool's
    `subscription_id` description -- which says to prefer `name` -- sends a model that follows
    it straight into a TOOL_DENIED. The menu handed to the model is what the model reads, so
    pin the write tools' description there, and keep the detail tool's three selectors.
    """

    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    model = ScriptedModel([("call", "subscription_list", "{}"), "好。"])
    run_domain(MODULE, bank=bank, model=model)
    menu = {item["function"]["name"]: item["function"] for item in model.tools[0]}
    for name in WRITE_TOOLS:
        properties = menu[name]["parameters"]["properties"]
        assert "name" not in properties, name
        assert "name" not in properties["subscription_id"]["description"], name
    detail = menu["subscription_detail"]["parameters"]["properties"]["subscription_id"]
    assert "name" in detail["description"]


def test_the_bound_catalog_carries_the_execute_tool_the_model_never_sees():
    """`SPECS` alone would not catch a graph built from a different catalog.

    The bound menu is what the Gateway will accept; the graph filters `subscription_execute`
    out of the schemas it hands the model, which is what the next test pins.
    """

    assert {name for name, _ in bound_spec(FakeBankClient()).tool_menu()} == {
        "subscription_list",
        "subscription_detail",
        *WRITE_TOOLS,
        "subscription_execute",
    }


def test_the_graph_hands_the_model_exactly_the_six_tools():
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_ONE])
    model = ScriptedModel([("call", "subscription_list", "{}"), "好。"])
    run_domain(MODULE, bank=bank, model=model)
    handed = {item["function"]["name"] for item in model.tools[0]}
    assert handed == {"subscription_list", "subscription_detail", *WRITE_TOOLS, "ask_user"}
    assert "subscription_execute" not in handed
