"""ChannelEngine 的调度契约。

分两段：前半段用替身钉住编排的次序（白名单、水位线、防重、绑定、失败撤账），后半段用真实的
rt（独立 schema + alembic + seed）与真实的图钉住三件只有真跑才能证明的事：确认卡确实带着可点的
回传值上了卡、连点三十次只出一次账、以及排他锁在每条路径上都还了回去。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.agents.turn import TurnAction, TurnConfirmation, TurnResult, TurnStatus
from app.api.pending_actions import PendingActionResult
from app.channels.binding import ChannelBindingManager
from app.channels.code_cards import clear_code_cards, report_code_outcome, tracked_code_cards
from app.channels.contracts import CardActionClick, InboundMessage
from app.channels.dedup import ChannelDedup
from app.channels.engine import (
    ANSWER_PREFIX,
    BIND_FAILED_TEXT,
    BIND_OK_TEXT,
    CONFIRM_VIA_CARD_TEXT,
    DUPLICATE_CLICK_TEXT,
    FAILED_TEXT,
    INTERRUPTED_TEXT,
    MODEL_MISSING_TEXT,
    REFUSAL_TEXTS,
    REPLAY_TEXT,
    ROTATED_TEXT,
    UNAUTHORIZED_TEXT,
    ChannelEngine,
)
from app.channels.render.feishu import MAX_TEXT_CHARS
from app.channels.throttler import StreamThrottler
from app.conversation.service import Receipt
from app.conversation.turns import HeldLock
from app.core.config import Settings
from app.core.errors import AppError
from app.modules.registry import build_registry
from app.tools.gateway import ToolGateway
from app.tools.recording import RecordingAdapter
from tests.support.handler_graph import handler_module
from tests.support.scripted_model import ScriptedModel

#: seed() 在每个测试 schema 里写下的演示用户。
FIRST_USER = "user_0001"
OPEN_ID = "ou_alice"
PENDING_ID = "wp_engine_1"
PREVIEW_SUMMARY = "请确认这笔模拟申购：2,000.00 元，从 a_wealth_main 扣款。"
CARD_DECLARATION = DomainDeclaration(agent_id="wealth", display="理财 Agent", purpose="理财")


def preview(*, requires_code: bool = False) -> TurnConfirmation:
    return TurnConfirmation(
        pending_id=PENDING_ID,
        operation="purchase_product",
        title="确认申购",
        amount_minor=200000,
        product_name="演示灵活理财",
        account_display="a_wealth_main",
        expires_at="2030-01-01T00:00:00+00:00",
        requires_code=requires_code,
    )


class Clock:
    """可拨的单调时钟：绑定码 TTL 与防爆破锁定都读它。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Outbox:
    """替身外发通道：记下每一条 payload，并像真 dispatcher 一样返回一个消息 id。"""

    def __init__(self) -> None:
        self.payloads: list = []
        self.updates: list[str] = []

    async def __call__(self, payload, *, update_message_id=None):
        self.payloads.append(payload)
        if update_message_id is not None:
            self.updates.append(update_message_id)
        return f"om_{len(self.payloads)}"

    def texts(self) -> list[str]:
        """只要纯文本的那几条（引导、绑定结果、错误说明）。"""

        return [item for item in self.payloads if isinstance(item, str)]

    def cards(self) -> list[dict]:
        return [item for item in self.payloads if isinstance(item, dict)]

    def dump(self) -> str:
        return json.dumps(self.payloads, ensure_ascii=False, default=str)


def inbound(
    text: str,
    *,
    message_id: str | None = None,
    event_id: str | None = None,
    sender_id: str = OPEN_ID,
    create_time_ms: int | None = None,
) -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        sender_id=sender_id,
        sender_name="Alice",
        message_id=message_id or f"m-{uuid4().hex}",
        event_id=event_id or f"e-{uuid4().hex}",
        text=text,
        create_time_ms=(int(time.time() * 1000) if create_time_ms is None else create_time_ms),
    )


def click(*, decision: str = "confirm", action_id: str | None = None, event_id: str | None = None):
    return CardActionClick(
        channel="feishu",
        sender_id=OPEN_ID,
        action_id=action_id or f"act-{uuid4().hex}",
        question_id="q-1",
        pending_id=PENDING_ID,
        decision=decision,
        raw_value={"q": "q-1", "p": PENDING_ID, "d": decision},
        event_id=event_id if event_id is not None else f"ev-{uuid4().hex}",
        create_time_ms=int(time.time() * 1000),
    )


def stubbed_engine(
    *, allowed: bool = True, binding=None, auto_bind=None, dedup=None, throttler_factory=None
):
    """替身组合：一台上半段那些"次序"用例够用的引擎。

    `dedup` 可以换成一个**真的** ChannelDedup（撤键那类用例要看得见键真的被抹掉了），
    也可以换成节流器工厂——默认的节流器会拦掉 1.5 秒内的第二次刷新。
    """

    from unittest.mock import MagicMock

    rt = MagicMock()
    binding_mgr = MagicMock()
    binding_mgr.is_allowed.return_value = allowed
    binding_mgr.get_binding.return_value = binding
    binding_mgr.auto_bind_default_user.return_value = auto_bind
    # 默认没有悬着的问题：读挂起状态是新加的一步，替身必须能 await 它。
    rt.agent.recovery_hint = AsyncMock(
        return_value=SimpleNamespace(pending=None, recovery_required=False)
    )
    if dedup is None:
        dedup = MagicMock()
        dedup.is_historical.return_value = False
        dedup.is_duplicate_message_obj.return_value = False
        dedup.is_duplicate_click.return_value = False
    engine = ChannelEngine(
        rt=rt,
        settings=MagicMock(),
        binding_mgr=binding_mgr,
        dedup=dedup,
        throttler_factory=throttler_factory,
    )
    return engine, rt, binding_mgr, dedup


class ExplodingOutbox:
    """一条都发不出去的通道：飞书抖了，或者凭据过期了。"""

    def __init__(self) -> None:
        self.attempts = 0

    async def __call__(self, payload, *, update_message_id=None):
        del payload, update_message_id
        self.attempts += 1
        raise RuntimeError("feishu is down")


class IdlessOutbox(Outbox):
    """像那种"发得出去但拿不到 message id"的 dispatcher：返回值恒为 None。"""

    async def __call__(self, payload, *, update_message_id=None):
        self.payloads.append(payload)
        if update_message_id is not None:
            self.updates.append(update_message_id)
        return None


class HeldStub:
    """可观测的"锁还了没有"替身；真的 HeldLock 要一把真 asyncio.Lock，只在锁用例里用。"""

    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def ev(name: str, **data) -> SimpleNamespace:
    """一条 turns.stream 事件。真的 AgentEvent 字段更多，引擎只读 event 与 data。"""

    return SimpleNamespace(event=name, data=data)


def eager_throttler() -> StreamThrottler:
    """把两道闸都拆掉的节流器：用例要看的是"每一段都发了"，不是"等不等得及"。"""

    return StreamThrottler(interval_ms=0, min_delta_chars=1, max_text_chars=MAX_TEXT_CHARS)


def streaming_engine(monkeypatch, *, stream, naming=None, held=None, dedup=None):
    """一台走流式分支的引擎：submit_prepared_turn 换成替身，渲染与节流都是真的。"""

    engine, rt, binding_mgr, dedup = stubbed_engine(
        binding=type("B", (), {"user_id": FIRST_USER})(),
        dedup=dedup,
        throttler_factory=eager_throttler,
    )
    binding_mgr.get_or_create_channel_session.return_value = {
        "user_id": FIRST_USER,
        "session_id": "sess_1",
    }
    binding_mgr.get_or_create_conversation.return_value = "conv_1"
    rt.turns.stream = stream
    held = held or HeldStub()

    async def submit(rt_, who, preparation):
        del rt_, who, preparation
        return PendingActionResult(
            status="streaming",
            outcome=SimpleNamespace(conversation_id="conv_1"),
            held_lock=held,
            naming=naming,
        )

    monkeypatch.setattr("app.channels.engine.submit_prepared_turn", submit)
    return engine, rt, dedup, held


# --- 消息进站的次序 -------------------------------------------------------------------


async def test_engine_unbound_user_message():
    engine, rt, _mgr, _dedup = stubbed_engine()
    reply_mock = Outbox()

    await engine.handle_message(
        inbound("Hello", message_id="m1", event_id="e1"), send_fn=reply_mock
    )

    # 一条引导，而且是纯文本（用户要能直接照抄那三个字）。
    assert len(reply_mock.payloads) == 1
    assert isinstance(reply_mock.payloads[0], str)
    assert "/bind" in reply_mock.payloads[0]
    # 没绑定的身份不该碰到对话：一次授信都不发起。
    rt.turns.acquire.assert_not_called()


async def test_whitelist_rejection_happens_before_anything_else():
    engine, rt, _mgr, dedup = stubbed_engine(allowed=False)
    outbox = Outbox()

    await engine.handle_message(inbound("转账 500 元"), send_fn=outbox)

    assert outbox.texts() == [UNAUTHORIZED_TEXT]
    # 白名单是第一道闸：水位线、防重、绑定、对话一个都不该被碰。
    dedup.is_historical.assert_not_called()
    dedup.is_duplicate_message_obj.assert_not_called()
    rt.turns.acquire.assert_not_called()


async def test_a_historical_event_is_acked_without_a_reply():
    engine, _rt, _mgr, dedup = stubbed_engine()
    dedup.is_historical.return_value = True
    outbox = Outbox()

    await engine.handle_message(inbound("重启前的堆积消息"), send_fn=outbox)

    assert outbox.payloads == []
    dedup.is_duplicate_message_obj.assert_not_called()


async def test_a_redelivered_event_is_acked_without_a_reply():
    engine, _rt, _mgr, dedup = stubbed_engine()
    dedup.is_duplicate_message_obj.return_value = True
    outbox = Outbox()

    await engine.handle_message(inbound("同一条消息的第二次投递"), send_fn=outbox)

    assert outbox.payloads == []


async def test_a_seconds_style_timestamp_does_not_swallow_the_message():
    """时间戳被传成 10 位数秒值时，宁可多答一轮，也不能让消息静默消失。"""

    engine, _rt, _mgr, dedup = stubbed_engine()
    dedup.is_historical.side_effect = ValueError("看起来是秒")
    outbox = Outbox()

    await engine.handle_message(inbound("Hello", create_time_ms=1_700_000_000), send_fn=outbox)

    # 没有被丢掉：走完了绑定的那一步，回了引导。
    assert len(outbox.payloads) == 1
    assert "/bind" in outbox.payloads[0]


# --- /bind 与绑定码红线 ---------------------------------------------------------------


async def test_the_bind_command_redeems_the_code_and_never_echoes_it(caplog):
    engine, _rt, binding_mgr, _dedup = stubbed_engine()
    outbox = Outbox()
    caplog.set_level("DEBUG")

    await engine.handle_message(inbound("/bind 123456"), send_fn=outbox)

    binding_mgr.redeem_code.assert_called_once()
    assert binding_mgr.redeem_code.call_args[0][0] == "123456"
    assert outbox.texts() == [BIND_OK_TEXT]
    # 绑定码是凭据：回执、卡片、日志里都不许出现它。
    assert "123456" not in outbox.dump()
    assert "123456" not in caplog.text


async def test_a_rejected_bind_code_is_not_echoed_either(caplog):
    engine, _rt, binding_mgr, _dedup = stubbed_engine()
    binding_mgr.redeem_code.return_value = None
    outbox = Outbox()
    caplog.set_level("DEBUG")

    await engine.handle_message(inbound("/bind 654321"), send_fn=outbox)

    assert outbox.texts() == [BIND_FAILED_TEXT]
    assert "654321" not in outbox.dump()
    assert "654321" not in caplog.text


async def test_a_bare_bind_command_answers_with_the_instructions():
    engine, _rt, _mgr, _dedup = stubbed_engine()
    outbox = Outbox()

    await engine.handle_message(inbound("/bind"), send_fn=outbox)

    assert len(outbox.payloads) == 1
    assert "/bind" in outbox.payloads[0]


# --- 失败要撤账 -----------------------------------------------------------------------


async def test_a_failed_turn_removes_the_message_keys(monkeypatch):
    engine, _rt, _mgr, dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())

    async def boom(rt, who, preparation):
        del rt, who, preparation
        raise AppError("PERSISTENCE_FAILED", "这次没能办成，请稍后再试一次。", 500)

    monkeypatch.setattr("app.channels.engine.submit_prepared_turn", boom)
    outbox = Outbox()

    await engine.handle_message(inbound("转账 500 元"), send_fn=outbox)

    # 处理失败必须撤掉去重键，否则飞书重投回来会被静默吞掉，用户的消息凭空消失。
    dedup.forget_message.assert_called_once()
    assert outbox.texts() == ["这次没能办成，请稍后再试一次。"]


async def test_an_unexpected_failure_is_reported_and_forgotten(monkeypatch):
    engine, _rt, _mgr, dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())

    async def boom(rt, who, preparation):
        del rt, who, preparation
        raise RuntimeError("boom")

    monkeypatch.setattr("app.channels.engine.submit_prepared_turn", boom)
    outbox = Outbox()

    await engine.handle_message(inbound("转账 500 元"), send_fn=outbox)

    dedup.forget_message.assert_called_once()
    assert outbox.texts() == [FAILED_TEXT]


# --- 卡片回调 -------------------------------------------------------------------------


def engine_with_fake_decision(monkeypatch, result):
    """一台绑好了用户的引擎，把授信状态机换成替身（它负责记账与锁，这里只验编排）。"""

    engine, rt, binding_mgr, dedup = stubbed_engine(
        binding=type("B", (), {"user_id": FIRST_USER})()
    )
    binding_mgr.get_or_create_channel_session.return_value = {
        "user_id": FIRST_USER,
        "session_id": "sess_1",
    }
    binding_mgr.get_or_create_conversation.return_value = "conv_1"
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("app.channels.engine.execute_pending_action_decision", fake)
    return engine, dedup, calls


def receipt_result() -> object:
    from app.api.pending_actions import PendingActionResult

    return PendingActionResult(
        status="receipt",
        outcome=Receipt(
            submission_id="sub_1",
            message_id="msg_1",
            run_id=None,
            conversation_id="conv_1",
            sequence=1,
            duplicate=True,
        ),
    )


async def test_the_click_gets_an_intermediate_card_before_the_decision_runs(monkeypatch):
    engine, _dedup, calls = engine_with_fake_decision(monkeypatch, receipt_result())
    outbox = Outbox()

    card = await engine.handle_card_action(click(), send_fn=outbox)

    # 飞书要求 3 秒内必有响应：同步返回的这张卡必须**点不动**，否则连击第二次就是第二次出账。
    buttons = [item for item in card["body"]["elements"] if item.get("tag") == "button"]
    assert [item["disabled"] for item in buttons] == [True]
    assert all("behaviors" not in item for item in buttons)

    await engine.wait_for_background()

    assert len(calls) == 1
    assert calls[0]["pending_id"] == PENDING_ID
    assert calls[0]["question_id"] == "q-1"
    assert calls[0]["decision"] == "confirm"
    # 飞书按钮的回传值带不回验证码，所以渠道这一侧永远是"没有验证码"。
    assert calls[0]["verification_code"] is None
    # 结论落在中间态那张卡上：一次点击在对话里只有一条消息，不会有第二条纯文本。
    assert outbox.texts() == []
    assert outbox.updates == ["om_1"]
    assert REPLAY_TEXT in json.dumps(outbox.payloads[-1], ensure_ascii=False)


async def test_a_duplicate_click_is_refused_before_any_decision(monkeypatch):
    engine, dedup, calls = engine_with_fake_decision(monkeypatch, receipt_result())
    dedup.is_duplicate_click.return_value = True
    outbox = Outbox()

    card = await engine.handle_card_action(click(), send_fn=outbox)
    await engine.wait_for_background()

    assert calls == []
    assert json.dumps(card, ensure_ascii=False).find(DUPLICATE_CLICK_TEXT) >= 0
    assert outbox.payloads == []


async def test_a_click_with_an_unknown_decision_is_refused(monkeypatch):
    engine, dedup, calls = engine_with_fake_decision(monkeypatch, receipt_result())
    outbox = Outbox()

    await engine.handle_card_action(click(decision="maybe"), send_fn=outbox)
    await engine.wait_for_background()

    assert calls == []
    dedup.is_duplicate_click.assert_not_called()


async def test_a_click_from_an_unbound_identity_gets_the_bind_hint(monkeypatch):
    engine, rt, binding_mgr, dedup = stubbed_engine(binding=None)
    _ = rt

    async def fake(**kwargs):
        raise AssertionError("unbound identity must not reach the decision state machine")

    monkeypatch.setattr("app.channels.engine.execute_pending_action_decision", fake)
    outbox = Outbox()

    card = await engine.handle_card_action(click(), send_fn=outbox)
    await engine.wait_for_background()

    assert "/bind" in json.dumps(card, ensure_ascii=False)
    dedup.forget_click.assert_called_once()


async def test_a_refused_click_updates_the_intermediate_card_instead_of_texting(monkeypatch):
    """失败结论走**卡片更新**，而且按渠道自己的话术说（M1 + M2）。"""

    engine, dedup, _calls = engine_with_fake_decision(
        monkeypatch, AppError("VERIFICATION_CODE_REQUIRED", "这笔操作需要验证码，请填写后重试", 422)
    )
    outbox = Outbox()

    await engine.handle_card_action(click(), send_fn=outbox)
    await engine.wait_for_background()

    # 用户重试必须能再进一次；键留着就等于这张卡从此点不动。
    dedup.forget_click.assert_called_once()
    # 第一条是那张中间态卡（引擎自己发，才拿得到消息 id），结论是它的原地更新：
    # 不能另起一条纯文本，否则中间态卡会永远停在"正在核验并执行…"。
    assert isinstance(outbox.payloads[0], dict)
    assert isinstance(outbox.payloads[-1], dict)
    assert outbox.updates == ["om_1"]
    assert outbox.texts() == []

    dumped = json.dumps(outbox.payloads[-1], ensure_ascii=False)
    # 飞书按钮带不回验证码，用户在这张卡上没地方"填写"——所以不能说"请填写后重试"，
    # 而要把他引到银行自己的页面上。
    assert "按钮带不了验证码" in dumped
    assert "请填写后重试" not in dumped


# --- 验证码红线 -----------------------------------------------------------------------


async def test_a_verification_code_in_the_payload_never_reaches_the_card_or_the_logs(caplog):
    """载荷里就算混进了验证码，卡片与日志里也找不到它。"""

    engine, _rt, _mgr, _dedup = stubbed_engine()
    caplog.set_level("DEBUG")
    payload = {
        "question_id": "q-1",
        "kind": "confirmation",
        "confirmation": {
            "pending_id": PENDING_ID,
            "operation": "purchase_product",
            "title": "确认申购",
            "amount_minor": 200000,
            "product_name": "演示灵活理财",
            "account_display": "a_wealth_main",
            "verification_code": "998877",
            "code": "998877",
            "confirm_label": "用 998877 确认",
        },
    }

    card = engine._interrupt_card(
        payload, who={"user_id": FIRST_USER, "session_id": "s"}, conversation_id="c"
    )

    assert card is not None
    dumped = json.dumps(card, ensure_ascii=False)
    assert "998877" not in dumped
    assert "998877" not in caplog.text


# --- 投递：首投失败、拿不到消息 id、命名失败 -------------------------------------------


async def test_a_first_delivery_failure_fails_the_turn_and_lets_the_redelivery_retry(
    monkeypatch, caplog
):
    """首投抛异常 = 用户一个字都没收到：本轮算失败，去重键必须撤掉。"""

    runs: list[int] = []

    async def stream(user_id, session_id, plan, *, naming=None):
        del user_id, session_id, plan, naming
        runs.append(1)
        yield ev("final", text="已经帮您办好了。")

    engine, _rt, _dedup, held = streaming_engine(
        monkeypatch, stream=stream, dedup=ChannelDedup(boot_time=time.time() - 3600.0)
    )
    caplog.set_level("ERROR")
    msg = inbound("买 2000 元的演示灵活理财", message_id="m-undeliverable", event_id="e-undeliv")

    await engine.handle_message(msg, send_fn=ExplodingOutbox())

    # 一路都是"发不出去"：这不能只值一条 warning，它必须是本轮失败，而且有日志可查。
    assert held.released is True
    assert any("reached no message" in record.getMessage() for record in caplog.records)

    # 键撤掉了，所以飞书重投回来能真的重来：图再跑一次，用户这次拿到了答复。
    outbox = Outbox()
    await engine.handle_message(msg, send_fn=outbox)
    assert len(runs) == 2
    assert "已经帮您办好了。" in outbox.dump()


async def test_a_first_send_without_a_message_id_degrades_to_one_final_message(monkeypatch, caplog):
    """拿不到 message id 时不准一条条新发：本回合只发最终一条，不许刷屏。"""

    async def stream(user_id, session_id, plan, *, naming=None):
        del user_id, session_id, plan, naming
        yield ev("reply_delta", text="正在为您办理")
        yield ev("reply_delta", text="正在为您办理，请稍候片刻")
        yield ev("final", text="已经帮您办好了。")

    engine, _rt, _dedup, _held = streaming_engine(monkeypatch, stream=stream)
    outbox = IdlessOutbox()
    caplog.set_level("WARNING")

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    # 没有 id 就没法原地更新：那两段中途刷新必须被跳过，只剩"首段 + 最终"两条。
    assert outbox.updates == []
    assert len(outbox.payloads) == 2
    assert "已经帮您办好了。" in json.dumps(outbox.payloads[-1], ensure_ascii=False)
    # 降级必须说出来：静默降级等于把一个 dispatcher 的 bug 埋进"看起来正常"里。
    assert any("no message id" in record.getMessage() for record in caplog.records)


async def test_a_streamed_turn_updates_one_message_in_place(monkeypatch):
    """一轮流式答复只有一条消息：第一条新发，之后每条都是它的原地更新。"""

    async def stream(user_id, session_id, plan, *, naming=None):
        del user_id, session_id, plan, naming
        yield ev("reply_delta", text="正在为您办理")
        yield ev("reply_delta", text="正在为您办理，请稍候片刻")
        yield ev("final", text="已经帮您办好了。")

    engine, _rt, _dedup, _held = streaming_engine(monkeypatch, stream=stream)
    outbox = Outbox()

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    assert len(outbox.updates) >= 1
    # 全部指向第一条消息的 id：正是"原地更新复用同一条消息"。
    assert set(outbox.updates) == {"om_1"}


async def test_a_failing_naming_never_turns_the_answer_into_a_failure(monkeypatch, caplog):
    """命名是一次额外的模型调用：它失败不能说"没办成"，也不能撤掉成功那一轮的去重键。"""

    class BrokenNaming:
        def __init__(self) -> None:
            self.ran = False

        async def run(self) -> None:
            self.ran = True
            raise RuntimeError("the naming model timed out")

    naming = BrokenNaming()

    async def stream(user_id, session_id, plan, *, naming=None):
        del user_id, session_id, plan, naming
        yield ev("final", text="已经帮您办好了。")

    engine, _rt, dedup, _held = streaming_engine(monkeypatch, stream=stream, naming=naming)
    outbox = Outbox()
    caplog.set_level("WARNING")

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    assert naming.ran is True
    # 成功答复就是最后一句：后面不许再跟一句"这次没能办成"。
    assert outbox.texts() == []
    assert "已经帮您办好了。" in json.dumps(outbox.payloads[-1], ensure_ascii=False)
    dedup.forget_message.assert_not_called()
    assert any("naming failed" in record.getMessage() for record in caplog.records)


async def test_naming_runs_after_the_conversation_lock_is_released(monkeypatch):
    """命名排在 final 之后、锁释放之后（S-2.1）：它不许占着会话锁。"""

    engine, rt, binding_mgr, _dedup = stubbed_engine(
        binding=type("B", (), {"user_id": FIRST_USER})()
    )
    binding_mgr.get_or_create_channel_session.return_value = {
        "user_id": FIRST_USER,
        "session_id": "sess_1",
    }
    binding_mgr.get_or_create_conversation.return_value = "conv_1"
    lock = asyncio.Lock()
    await lock.acquire()
    rt.agent = SimpleNamespace(conversation_lock=lambda conversation_id: lock)
    seen: list[bool] = []

    class Naming:
        async def run(self) -> None:
            seen.append(rt.agent.conversation_lock("conv_1").locked())

    async def stream(user_id, session_id, plan, *, naming=None):
        del user_id, session_id, plan, naming
        yield ev("final", text="已经帮您办好了。")

    rt.turns.stream = stream

    async def submit(rt_, who, preparation):
        del rt_, who, preparation
        return PendingActionResult(
            status="streaming",
            outcome=SimpleNamespace(conversation_id="conv_1"),
            held_lock=HeldLock(lock),
            naming=Naming(),
        )

    monkeypatch.setattr("app.channels.engine.submit_prepared_turn", submit)

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=Outbox())

    assert seen == [False]


async def test_a_cancelled_stream_leaves_an_interrupted_card(monkeypatch):
    """流被取消时用户不能永远停在流式卡上，但收尾也不能把取消吞掉。"""

    async def stream(user_id, session_id, plan, *, naming=None):
        del user_id, session_id, plan, naming
        yield ev("reply_delta", text="正在生成的前半段")
        raise asyncio.CancelledError()

    engine, _rt, _dedup, held = streaming_engine(monkeypatch, stream=stream)
    outbox = Outbox()

    with pytest.raises(asyncio.CancelledError):
        await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    # 原始异常照样往上走（取消没被吞），锁还了，用户看到一张"已中断"的收尾。
    assert held.released is True
    assert outbox.updates == ["om_1"]
    assert INTERRUPTED_TEXT in json.dumps(outbox.payloads[-1], ensure_ascii=False)


# --- 点击：幂等键与验证码红线 -----------------------------------------------------------


async def test_the_click_key_is_stable_for_one_click_and_distinct_across_clicks(monkeypatch):
    """同一张卡上的两次点击不是同一次提交；同一次点击被重投还是同一把键。"""

    engine, _dedup, calls = engine_with_fake_decision(monkeypatch, receipt_result())
    outbox = Outbox()
    first = click(event_id="ev-1")

    await engine.handle_card_action(first, send_fn=outbox)
    await engine.handle_card_action(click(event_id="ev-2", action_id="act-same"), send_fn=outbox)
    # 飞书把同一个事件又投了一次：契约对象都一样。
    await engine.handle_card_action(first, send_fn=outbox)
    await engine.wait_for_background()

    keys = [item["idempotency_key"] for item in calls]
    assert len(keys) == 3
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]


async def test_two_clicks_without_an_event_id_do_not_share_an_idempotency_key(monkeypatch):
    """event_id 缺失时不许退回 action_id：同一张卡的 action_id 是共用的。"""

    engine, _dedup, calls = engine_with_fake_decision(monkeypatch, receipt_result())
    outbox = Outbox()

    await engine.handle_card_action(click(event_id="", action_id="act-shared"), send_fn=outbox)
    await engine.handle_card_action(click(event_id="", action_id="act-shared"), send_fn=outbox)
    await engine.wait_for_background()

    assert len(calls) == 2
    assert calls[0]["idempotency_key"] != calls[1]["idempotency_key"]


async def test_a_verification_code_in_the_click_payload_never_reaches_the_decision(
    monkeypatch, caplog
):
    """按钮回传值里就算混进了验证码，渠道层也绝不把它交给授信状态机。"""

    engine, _dedup, calls = engine_with_fake_decision(monkeypatch, receipt_result())
    outbox = Outbox()
    caplog.set_level("DEBUG")
    forged = CardActionClick(
        channel="feishu",
        sender_id=OPEN_ID,
        action_id="act-forged",
        question_id="q-1",
        pending_id=PENDING_ID,
        decision="confirm",
        raw_value={"q": "q-1", "p": PENDING_ID, "d": "confirm", "verification_code": "998877"},
        event_id="ev-forged",
        create_time_ms=int(time.time() * 1000),
    )

    await engine.handle_card_action(forged, send_fn=outbox)
    await engine.wait_for_background()

    assert len(calls) == 1
    # 验证码绝不进渠道层：既不到授信状态机，也不上卡片、不进日志。
    assert calls[0]["verification_code"] is None
    assert "998877" not in outbox.dump()
    assert "998877" not in caplog.text


# --- 真实 rt：确认卡、连点、锁 ---------------------------------------------------------


class ConfirmingSpy:
    """脚本化的模块：先给一张预览，按钮事件由 act 回答。"""

    def __init__(self, action: TurnResult, preview_confirmation: TurnConfirmation) -> None:
        self._action = action
        self._preview = preview_confirmation
        self.actions: list[TurnAction] = []

    def __call__(self, context, model, message) -> TurnResult:
        del context, model, message
        return TurnResult(
            status=TurnStatus.WAITING_CONFIRMATION,
            summary=PREVIEW_SUMMARY,
            confirmation=self._preview,
        )

    def act(self, context, model, action: TurnAction) -> TurnResult:
        del context, model
        self.actions.append(action)
        return self._action


def real_engine(rt, *, card_preview: TurnConfirmation | None = None, replies=None):
    """一台连真实 rt 的引擎：图、绑定管理器与防重都换成了真的。"""

    spy = ConfirmingSpy(
        TurnResult(status=TurnStatus.COMPLETED, summary="模拟申购已完成。"),
        card_preview or preview(),
    )
    model = ScriptedModel(
        replies
        if replies is not None
        else [
            ("call", "handoff", '{"agent_id":"wealth","instruction":"买一笔"}'),
            "已按您的确认提交。",
        ]
    )
    registry = build_registry([handler_module("wealth", CARD_DECLARATION, spy)])
    gateway = ToolGateway(registry.catalog, RecordingAdapter({}), audit=rt.audit)
    checkpoint = InMemorySaver()

    rt.agent = AgentRuntime(
        model,
        graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
            model,
            gateway,
            context,
            assembler=assembler,
            turn=turn,
            roster=registry.roster,
        ),
        checkpointer=checkpoint,
        customer_resolver=rt.identity.bank_customer_id,
    )
    settings = Settings(
        database_url=rt.settings.database_url,
        feishu_allow_from=OPEN_ID,
        feishu_auto_bind=False,
    )
    mgr = ChannelBindingManager(rt.database.sessions, settings=settings, clock=Clock())
    dedup = ChannelDedup(boot_time=time.time() - 3600.0)
    engine = ChannelEngine(rt=rt, settings=settings, binding_mgr=mgr, dedup=dedup)
    return engine, mgr, dedup, spy, model


def bind(mgr: ChannelBindingManager) -> None:
    code = mgr.generate_binding_code(FIRST_USER)
    assert mgr.redeem_code(code, "feishu", OPEN_ID, "Alice") is not None


async def test_the_interrupt_becomes_a_clickable_confirmation_card(rt):
    engine, mgr, _dedup, _spy, _model = real_engine(rt)
    bind(mgr)
    outbox = Outbox()

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    cards = outbox.cards()
    assert cards, outbox.payloads
    confirmation = json.dumps(cards[-1], ensure_ascii=False)
    assert PENDING_ID in confirmation
    assert '"d": "confirm"' in confirmation or '"d":"confirm"' in confirmation
    # 卡上的按钮回传值就是 dispatcher 将来读回来的那一份：q / p / d。
    buttons = [
        element
        for element in cards[-1]["body"]["elements"]
        if element.get("tag") == "button" and "behaviors" in element
    ]
    values = [element["behaviors"][0]["value"] for element in buttons]
    assert {"q", "p", "d"} == set(values[0])
    assert values[0]["p"] == PENDING_ID


async def test_thirty_clicks_on_one_card_produce_exactly_one_decision(rt):
    engine, mgr, _dedup, spy, _model = real_engine(rt)
    bind(mgr)
    outbox = Outbox()

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)
    conversation_id = mgr.get_binding("feishu", OPEN_ID).conversation_id
    # 点不点得动全靠卡片上那一份回传值：question_id 与 pending_id 都在里面。
    button = next(
        element
        for element in outbox.cards()[-1]["body"]["elements"]
        if element.get("tag") == "button" and "behaviors" in element
    )
    value = button["behaviors"][0]["value"]
    question_id = value["q"]

    clicks = []
    for _ in range(30):
        item = CardActionClick(
            channel="feishu",
            sender_id=OPEN_ID,
            action_id=f"act-{uuid4().hex}",
            question_id=question_id,
            pending_id=PENDING_ID,
            decision="confirm",
            raw_value={"q": question_id, "p": PENDING_ID, "d": "confirm"},
            event_id=f"ev-{uuid4().hex}",
            create_time_ms=int(time.time() * 1000),
        )
        clicks.append(await engine.handle_card_action(item, send_fn=outbox))

    await engine.wait_for_background()

    # 三十次里只有第一次进了授信，其余 29 次同步返回一张"已经处理过了"的卡片。
    assert len(spy.actions) == 1
    assert spy.actions[0].kind == "confirm"
    assert spy.actions[0].pending_id == PENDING_ID
    notices = [
        card for card in clicks[1:] if DUPLICATE_CLICK_TEXT in json.dumps(card, ensure_ascii=False)
    ]
    assert len(notices) == 29
    # 结果卡上是模块的结论，而不是中间态。
    assert "模拟申购已完成。" in outbox.dump()
    # 排他锁还回去了：会话没有因为这一轮被焊死。
    assert rt.agent.conversation_lock(conversation_id).locked() is False


async def test_a_redelivered_message_never_runs_a_second_turn(rt):
    engine, mgr, dedup, _spy, model = real_engine(rt)
    bind(mgr)
    outbox = Outbox()
    msg = inbound("买 2000 元的演示灵活理财", message_id="m-redelivered", event_id="e-redelivered")

    await engine.handle_message(msg, send_fn=outbox)
    sent = len(outbox.payloads)
    streams_after_first = model.streams

    # 同一条消息再投一次：防重命中，一个字都不回，也不再跑图。
    await engine.handle_message(msg, send_fn=outbox)
    assert len(outbox.payloads) == sent
    assert model.streams == streams_after_first

    # 就算防重被绕过（进程重启、键过期），幂等键也会把第二次交付判成 receipt。
    dedup.forget_message(msg)
    await engine.handle_message(msg, send_fn=outbox)
    assert outbox.texts()[-1] == REPLAY_TEXT
    assert model.streams == streams_after_first


async def test_a_failing_turn_releases_the_lock_and_withdraws_the_keys(rt):
    engine, mgr, _dedup, _spy, model = real_engine(rt, replies=[("fail", RuntimeError("boom"))])
    bind(mgr)
    outbox = Outbox()
    msg = inbound("买 2000 元的演示灵活理财", message_id="m-failing", event_id="e-failing")

    await engine.handle_message(msg, send_fn=outbox)

    assert outbox.texts()[-1] == FAILED_TEXT
    conversation_id = mgr.get_binding("feishu", OPEN_ID).conversation_id
    assert rt.agent.conversation_lock(conversation_id).locked() is False

    # 失败就是失败：去重键撤掉了，所以重投回来不会被静默 ACK 吞掉——它会走到幂等那一层，
    # 拿到"这条消息已经处理过了"。若引擎在失败时把键留在窗口里，这里一个字都不会多。
    delivered = len(outbox.payloads)
    await engine.handle_message(msg, send_fn=outbox)
    assert len(outbox.payloads) == delivered + 1
    assert outbox.texts()[-1] == REPLAY_TEXT
    assert model.streams == 1


async def test_a_card_that_requires_a_code_carries_a_signed_link_instead_of_a_confirm_button(rt):
    engine, mgr, _dedup, _spy, _model = real_engine(rt, card_preview=preview(requires_code=True))
    bind(mgr)
    outbox = Outbox()

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    confirmation = outbox.cards()[-1]
    dumped = json.dumps(confirmation, ensure_ascii=False)
    assert "/api/channels/mfa?t=" in dumped
    # 需要验证码时不给回调确认入口：那种点击必然被拒（422），用户会以为是自己按错了。
    assert '"d": "confirm"' not in dumped
    assert '"d":"confirm"' not in dumped


# --- 会话接不下去时换一条 ---------------------------------------------------------------


def broken_submit(monkeypatch, code: str, message: str = "接不下去了", status: int = 409):
    """让 prepare 那一步抛一个指定 code 的拒绝：这些 code 是 runtime 那边真实会抛出来的。"""

    async def boom(rt, who, preparation):
        del rt, who, preparation
        raise AppError(code, message, status)

    monkeypatch.setattr("app.channels.engine.submit_prepared_turn", boom)


async def test_a_missing_model_is_reported_instead_of_poisoning_a_conversation(monkeypatch):
    """模型没配好时不许把消息放进图：那条消息会半途失败，把会话留在未完成状态。"""

    engine, rt, binding_mgr, dedup = stubbed_engine(
        binding=type("B", (), {"user_id": FIRST_USER})()
    )
    rt.agent.model_configured = False
    outbox = Outbox()

    await engine.handle_message(inbound("你好"), send_fn=outbox)

    assert outbox.texts() == [MODEL_MISSING_TEXT]
    # 会话一次都不该被创建：这条消息根本没进图，不需要一个专属会话。
    binding_mgr.get_or_create_conversation.assert_not_called()
    # 键撤掉，配好模型后飞书的重投还能重来。
    dedup.forget_message.assert_called_once()


async def test_a_recovery_required_refusal_moves_the_binding_to_a_new_conversation(monkeypatch):
    """EXECUTION_RECOVERY_REQUIRED 不是"用户做错了"，而是这条会话再也接不下去。"""

    engine, _rt, binding_mgr, _dedup = stubbed_engine(
        binding=type("B", (), {"user_id": FIRST_USER})()
    )
    broken_submit(monkeypatch, "EXECUTION_RECOVERY_REQUIRED")
    outbox = Outbox()

    await engine.handle_message(inbound("你好"), send_fn=outbox)

    binding_mgr.open_new_conversation.assert_called_once()
    assert outbox.texts() == [ROTATED_TEXT]
    # 换会话之后必须请用户重发：新会话里没有上一句。
    assert "再发一次" in ROTATED_TEXT


async def test_other_refusals_keep_the_conversation(monkeypatch):
    """别的拒绝是"这一次不行"，会话还能用：不许为了除错把用户的会话换掉。"""

    engine, _rt, binding_mgr, _dedup = stubbed_engine(
        binding=type("B", (), {"user_id": FIRST_USER})()
    )
    broken_submit(monkeypatch, "PERSISTENCE_FAILED")
    outbox = Outbox()

    await engine.handle_message(inbound("转账 500 元"), send_fn=outbox)

    binding_mgr.open_new_conversation.assert_not_called()
    assert outbox.texts() == [REFUSAL_TEXTS["PERSISTENCE_FAILED"]]


async def test_a_failed_rotation_falls_back_to_the_plain_refusal(monkeypatch):
    """换会话本身失败（库抖了）：仍要说一句人话，不能因为除错再抛一次。"""

    engine, _rt, binding_mgr, _dedup = stubbed_engine(
        binding=type("B", (), {"user_id": FIRST_USER})()
    )
    broken_submit(monkeypatch, "CONVERSATION_NOT_WRITABLE")
    binding_mgr.open_new_conversation.side_effect = RuntimeError("db is down")
    outbox = Outbox()

    await engine.handle_message(inbound("你好"), send_fn=outbox)

    assert outbox.texts() == [REFUSAL_TEXTS["CONVERSATION_NOT_WRITABLE"]]


async def test_a_poisoned_conversation_is_rotated_and_the_next_message_gets_an_answer(rt):
    """真实一条龙：第一轮失败把会话留在未完成状态 -> 换会话 -> 用户重发就有答复。

    这是真机飞书里出现过的那条路：模型没配好时第一句"你好"落库、图跑到一半炸掉，
    之后每一条消息都被判成 EXECUTION_RECOVERY_REQUIRED，用户只看到"上一次操作没有完成"。
    """

    engine, mgr, _dedup, _spy, model = real_engine(
        rt, replies=[("fail", RuntimeError("boom")), "您好，我是您的银行助手。"]
    )
    bind(mgr)
    outbox = Outbox()

    await engine.handle_message(inbound("你好", message_id="m-poison-1"), send_fn=outbox)
    assert outbox.texts()[-1] == FAILED_TEXT
    poisoned = mgr.get_binding("feishu", OPEN_ID).conversation_id

    # 第二轮：这条会话已经接不下去，引擎换一条新会话并请用户重发。
    await engine.handle_message(inbound("你好", message_id="m-poison-2"), send_fn=outbox)
    assert outbox.texts()[-1] == ROTATED_TEXT
    rotated = mgr.get_binding("feishu", OPEN_ID).conversation_id
    assert rotated != poisoned
    # 换会话不该偷偷多跑一轮图：被拒的那条消息一个字都没生成。
    assert model.streams == 1

    # 第三轮：新会话是干净的，用户重发的那句话真拿到了答复（答复走卡片，不是纯文本）。
    await engine.handle_message(inbound("你好", message_id="m-poison-3"), send_fn=outbox)
    final = json.dumps(outbox.cards()[-1], ensure_ascii=False)
    assert "您好，我是您的银行助手。" in final
    assert "正在生成" not in final
    assert mgr.get_binding("feishu", OPEN_ID).conversation_id == rotated


# --- 追问与确认卡：打字消息按什么身份进图 ---------------------------------------------

QUESTION_ID = "q_open_1"


def open_question(fields, *, confirmation=None, question_id=QUESTION_ID):
    """一条挂起的追问，形状与 runtime.recovery_hint 的 pending 一致。"""

    return SimpleNamespace(
        pending={
            "kind": "input",
            "question_id": question_id,
            "agent_id": "transfer",
            "display": "有多张账户符合条件，请选择付款账户。",
            "fields": list(fields),
            **({"confirmation": confirmation} if confirmation else {}),
        },
        recovery_required=False,
    )


def record_submit(monkeypatch):
    """把 submit_prepared_turn 换成只记账的替身：返回回执，让引擎停在"这条消息办过了"。"""

    seen: dict = {}

    async def submit(rt, who, preparation):
        del rt, who
        seen["preparation"] = preparation
        return PendingActionResult(
            status="receipt",
            outcome=Receipt(
                submission_id="sub_1",
                message_id="msg_1",
                run_id=None,
                conversation_id="conv_1",
                sequence=1,
                duplicate=True,
            ),
            held_lock=None,
            naming=None,
        )

    monkeypatch.setattr("app.channels.engine.submit_prepared_turn", submit)
    return seen


async def test_a_text_answer_to_an_open_question_is_sent_as_a_resume(monkeypatch):
    engine, rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())
    rt.agent.recovery_hint = AsyncMock(
        return_value=open_question(
            [{"name": "selection", "label": "付款账户", "type": "select", "required": True}]
        )
    )
    seen = record_submit(monkeypatch)
    outbox = Outbox()

    await engine.handle_message(inbound("a_wealth_main", message_id="m_q1"), send_fn=outbox)

    preparation = seen["preparation"]
    # 答案必须装在 resume 信封里并指名问题：不指名就等于"当前没有等待回答的问题"。
    assert preparation.context == {
        "resume": {"question_id": QUESTION_ID, "value": {"selection": "a_wealth_main"}}
    }
    # 账本里那句话要是人读得懂的补充信息，而不是一句孤零零的账户号。
    assert preparation.message == ANSWER_PREFIX + "a_wealth_main"


def refuse_submit(monkeypatch, code="INVALID_INTERRUPT_ANSWER"):
    """submit 替身：记账后按 runtime 的口径拒回（悬着问题却没带 resume）。"""

    seen: dict = {}

    async def submit(rt, who, preparation):
        del rt, who
        seen["preparation"] = preparation
        raise AppError(code, "这张卡已经过期了。请在最新那条消息里重新发起这次操作。", 409)

    monkeypatch.setattr("app.channels.engine.submit_prepared_turn", submit)
    return seen


async def test_a_confirmation_box_is_not_answered_by_chat_text(monkeypatch):
    engine, rt, mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())
    rt.agent.recovery_hint = AsyncMock(
        return_value=open_question(
            [{"name": "decision", "label": "决定", "type": "text", "required": True}],
            confirmation={"pending_id": "pa_1", "operation": "purchase_product"},
        )
    )
    seen = refuse_submit(monkeypatch)
    outbox = Outbox()

    await engine.handle_message(inbound("确认", message_id="m_c1"), send_fn=outbox)

    # 文字答复不带 resume 信封：它进不了授权链路，只是被拒回。
    assert seen["preparation"].context is None
    # 拒回时给的是"去点按钮"，不是"这张卡过期了，重新发起一次"——后者把人骗去重来一遍。
    assert outbox.texts() == [CONFIRM_VIA_CARD_TEXT]
    # 会话没坏：不换会话，也不把用户踢进别的流程。
    mgr.open_new_conversation.assert_not_called()


async def test_another_refusal_with_an_open_question_still_says_what_went_wrong(monkeypatch):
    engine, rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())
    rt.agent.recovery_hint = AsyncMock(
        return_value=open_question(
            [{"name": "decision", "label": "决定", "type": "text", "required": True}],
            confirmation={"pending_id": "pa_1", "operation": "purchase_product"},
        )
    )
    refuse_submit(monkeypatch, code="PERSISTENCE_FAILED")
    outbox = Outbox()

    await engine.handle_message(inbound("确认", message_id="m_c2"), send_fn=outbox)

    # 只有"在跟确认卡说文字"这一种拒回才换成按钮引导，别的失败照实说。
    assert outbox.texts() == [REFUSAL_TEXTS["PERSISTENCE_FAILED"]]


async def test_a_multi_field_question_is_collected_one_field_at_a_time(monkeypatch):
    engine, rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())
    rt.agent.recovery_hint = AsyncMock(
        return_value=open_question(
            [
                {"name": "payee", "label": "收款人", "type": "text", "required": True},
                {"name": "amount", "label": "金额", "type": "text", "required": True},
            ]
        )
    )
    seen = record_submit(monkeypatch)
    outbox = Outbox()

    await engine.handle_message(inbound("张三", message_id="m_f1"), send_fn=outbox)

    # 第一个字段先收着：一条消息填不满两个字段，而 resume 的 value 少一个就会被判不匹配。
    assert "preparation" not in seen
    assert len(outbox.texts()) == 1
    assert "金额" in outbox.texts()[0]

    await engine.handle_message(inbound("100 元", message_id="m_f2"), send_fn=outbox)

    preparation = seen["preparation"]
    assert preparation.context == {
        "resume": {"question_id": QUESTION_ID, "value": {"payee": "张三", "amount": "100 元"}}
    }
    assert preparation.message == ANSWER_PREFIX + "收款人：张三；金额：100 元"


async def test_a_new_question_discards_the_answers_of_the_old_one(monkeypatch):
    engine, rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())
    two_fields = [
        {"name": "payee", "label": "收款人", "type": "text", "required": True},
        {"name": "amount", "label": "金额", "type": "text", "required": True},
    ]
    rt.agent.recovery_hint = AsyncMock(return_value=open_question(two_fields))
    seen = record_submit(monkeypatch)
    outbox = Outbox()

    await engine.handle_message(inbound("张三", message_id="m_d1"), send_fn=outbox)

    # 模块重问了另一个问题（比如必填项被拒后重问）：旧的半拉答案是别人的，不能拼进来。
    rt.agent.recovery_hint = AsyncMock(
        return_value=open_question(two_fields, question_id="q_open_2")
    )
    await engine.handle_message(inbound("李四", message_id="m_d2"), send_fn=outbox)
    await engine.handle_message(inbound("200 元", message_id="m_d3"), send_fn=outbox)

    preparation = seen["preparation"]
    assert preparation.context["resume"]["question_id"] == "q_open_2"
    assert preparation.context["resume"]["value"] == {"payee": "李四", "amount": "200 元"}


ACCOUNT_OPTIONS = ["活期账户（尾号 ckup）", "活期账户（尾号 main）"]


def one_select_question():
    return open_question(
        [
            {
                "name": "selection",
                "label": "付款账户",
                "type": "select",
                "required": True,
                "options": list(ACCOUNT_OPTIONS),
            }
        ]
    )


async def test_a_numbered_reply_picks_the_option_it_names(monkeypatch):
    engine, rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())
    rt.agent.recovery_hint = AsyncMock(return_value=one_select_question())
    seen = record_submit(monkeypatch)
    outbox = Outbox()

    await engine.handle_message(inbound("2", message_id="m_o1"), send_fn=outbox)

    # 序号只是省打字：交给图的取值仍然是模块自己列出来的那个选项原文。
    preparation = seen["preparation"]
    assert preparation.context["resume"]["value"] == {"selection": ACCOUNT_OPTIONS[1]}
    assert preparation.message == ANSWER_PREFIX + ACCOUNT_OPTIONS[1]


async def test_an_out_of_range_number_is_left_alone(monkeypatch):
    engine, rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())
    rt.agent.recovery_hint = AsyncMock(return_value=one_select_question())
    seen = record_submit(monkeypatch)
    outbox = Outbox()

    await engine.handle_message(inbound("7", message_id="m_o2"), send_fn=outbox)

    # 猜不出来的当普通文本交回去：让业务模块自己说"不在候选列表里"，别在引擎里编一个值。
    assert seen["preparation"].context["resume"]["value"] == {"selection": "7"}


async def test_a_question_card_lists_the_options_with_their_numbers():
    engine, _rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())

    card = engine._interrupt_card(
        {
            "kind": "transfer_selection",
            "question_id": QUESTION_ID,
            "agent_id": "transfer",
            "display": "有多张账户符合条件，请选择付款账户。",
            "fields": [
                {
                    "name": "selection",
                    "label": "付款账户",
                    "type": "select",
                    "required": True,
                    "options": list(ACCOUNT_OPTIONS),
                }
            ],
        },
        who={"user_id": FIRST_USER, "session_id": "sess_1"},
        conversation_id="conv_1",
    )

    # 卡片正文必须自带可选项：飞书上没有下拉框，漏了这一步用户只能盯着"请选择"干瞪眼。
    body = json.dumps(card, ensure_ascii=False)
    assert "1、" + ACCOUNT_OPTIONS[0] in body
    assert "2、" + ACCOUNT_OPTIONS[1] in body
    assert "回复序号或完整名称" in body


async def test_a_question_card_without_options_still_asks_for_free_text():
    engine, _rt, _mgr, _dedup = stubbed_engine(binding=type("B", (), {"user_id": FIRST_USER})())

    card = engine._interrupt_card(
        {
            "kind": "input",
            "question_id": QUESTION_ID,
            "agent_id": "transfer",
            "display": "请问收款人叫什么？",
            "fields": [{"name": "payee", "label": "收款人", "type": "text", "required": True}],
        },
        who={"user_id": FIRST_USER, "session_id": "sess_1"},
        conversation_id="conv_1",
    )

    body = json.dumps(card, ensure_ascii=False)
    assert "收款人：直接回复内容就行。" in body
    assert "请问收款人叫什么？" in body


# --- 手机页面办完之后，那张卡要跟着变 ----------------------------------------------------
# 卡片由引擎发、验证码由手机页面收，两边唯一的交点是 (conversation_id, pending_id)。不写回的
# 话，用户手机上永远停在「等待您的确认」，而钱其实已经出去了。


async def test_the_phone_page_writes_the_outcome_back_to_the_card(rt):
    clear_code_cards()
    engine, mgr, _dedup, _spy, _model = real_engine(rt, card_preview=preview(requires_code=True))
    bind(mgr)
    outbox = Outbox()
    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    # 需要验证码的卡发出去那一刻就登记了：手机页面办完才有地方回写。
    assert tracked_code_cards() == 1
    conversation_id = mgr.get_binding("feishu", OPEN_ID).conversation_id
    assert await report_code_outcome(conversation_id, PENDING_ID, "这笔申购已经完成。") is True

    # 原地更新同一条，而不是再发一条结论：一条消息走到「已完成」。
    assert len(outbox.updates) == 1
    dumped = outbox.dump()
    assert "这笔申购已经完成。" in dumped
    assert "已完成" in dumped
    assert tracked_code_cards() == 0


async def test_a_card_without_a_code_is_not_kept_for_write_back(rt):
    clear_code_cards()
    engine, mgr, _dedup, _spy, _model = real_engine(rt)
    bind(mgr)
    outbox = Outbox()

    await engine.handle_message(inbound("买 2000 元的演示灵活理财"), send_fn=outbox)

    # 点按钮就能办完的卡不登记：那种卡由点击自己收尾，留着只会在进程里攒垃圾。
    assert tracked_code_cards() == 0
