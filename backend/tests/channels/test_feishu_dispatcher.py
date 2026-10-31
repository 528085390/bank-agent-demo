"""飞书 dispatcher / runner 的可测契约：解析、白名单、水位线、防重、卡片回执与长连接生命周期。

全部用替身：不需要任何飞书凭据，不联网，也不碰数据库（settings 用 SimpleNamespace 拼）。
真飞书 API 的字段形状按「宽进」实现，这里的样例事件就是留给 Task 9 联调的假设。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from app.channels.dedup import ChannelDedup
from app.channels.feishu import dispatcher as dispatcher_module
from app.channels.feishu import runner as runner_module
from app.channels.feishu.dispatcher import (
    DUPLICATE_CLICK_TEXT,
    MAX_SEND_ATTEMPTS,
    RETRY_DELAYS_MS,
    TEXT_ONLY_GUIDE,
    UNKNOWN_DECISION_TEXT,
    FeishuDispatcher,
)
from app.channels.feishu.runner import FeishuRunner

OPEN_ID = "ou_alice"
OTHER_OPEN_ID = "ou_mallory"
CHAT_ID = "oc_chat_1"
MESSAGE_ID = "om_message_1"
EVENT_ID = "ev_message_1"
QUESTION_ID = "q-1"
PENDING_ID = "wp_1"
CREATE_TIME_MS = 1700000000456


# --- 替身与样例事件 ----------------------------------------------------------------


def feishu_settings(**overrides):
    """settings 只需要这些字段；用 SimpleNamespace 免掉 pydantic 与 .env。"""
    body = {
        "feishu_enabled": True,
        "feishu_app_id": "cli_demo",
        "feishu_app_secret": "app_secret",
        "feishu_encrypt_key": "",
        "feishu_verification_token": "",
        "feishu_allow_from": OPEN_ID,
        "feishu_whitelist_enabled": True,
        "feishu_default_bound_user": "user_0001",
        "feishu_auto_bind": True,
        "secret_key": "secret",
        "channel_mfa_base_url": "http://127.0.0.1:8000",
    }
    body.update(overrides)
    return SimpleNamespace(**body)


class Outbox:
    """替身外发通道：记下 payload，并像真飞书那样回一个消息 id。"""

    def __init__(self, message_id="om_sent_1"):
        self.payloads = []
        self.updates = []
        self.message_id = message_id

    async def __call__(self, payload, update_message_id=None):
        self.payloads.append(payload)
        if update_message_id is not None:
            self.updates.append(update_message_id)
        return self.message_id


class EngineStub:
    """只记录「被调用了几次、参数是什么」的引擎替身。"""

    def __init__(self):
        self.messages = []
        self.clicks = []
        self.send_fns = []
        self.card_response = {
            "schema": "2.0",
            "header": {"event_id": EVENT_ID},
            "body": {"elements": []},
        }

    async def handle_message(self, msg, send_fn):
        self.messages.append(msg)
        self.send_fns.append(send_fn)

    async def handle_card_action(self, click, send_fn):
        self.clicks.append(click)
        self.send_fns.append(send_fn)
        return self.card_response


def message_event(
    *,
    open_id=OPEN_ID,
    message_id=MESSAGE_ID,
    event_id=EVENT_ID,
    create_time=str(CREATE_TIME_MS),
    header_create_time="1700000000123",
    message_type="text",
    content=None,
    with_message=True,
    with_event=True,
):
    header = {"event_id": event_id, "event_type": "im.message.receive_v1", "token": "vt"}
    if header_create_time is not None:
        header["create_time"] = header_create_time
    event = {
        "sender": {
            "sender_id": {"open_id": open_id, "union_id": "on_union"},
            "sender_type": "user",
        },
    }
    if with_message:
        event["message"] = {
            "message_id": message_id,
            "create_time": create_time,
            "chat_id": CHAT_ID,
            "chat_type": "p2p",
            "message_type": message_type,
            "content": json.dumps({"text": "帮我查一下余额"}, ensure_ascii=False)
            if content is None
            else content,
        }
    payload = {"schema": "2.0", "header": header}
    if with_event:
        payload["event"] = event
    return payload


def card_action_event(
    *,
    open_id=OPEN_ID,
    decision="confirm",
    question_id=QUESTION_ID,
    pending_id=PENDING_ID,
    event_id="ev_click_1",
    create_time="1700000000789",
    value=None,
    with_action=True,
):
    if value is None:
        value = {"q": question_id, "p": pending_id, "d": decision}
    event = {
        "operator": {"open_id": open_id, "tenant_key": "tk"},
        "token": "c_card_token",
        "context": {"open_message_id": "om_card_1", "open_chat_id": CHAT_ID},
    }
    if with_action:
        event["action"] = {"value": value, "tag": "button", "timezone": "Asia/Shanghai"}
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "create_time": create_time,
            "token": "vt",
        },
        "event": event,
    }


def fresh_ms(dedup, *, seconds_before_boot=2):
    """落在水位线之内的事件时间（毫秒字符串）：防重用例专用。

    `CREATE_TIME_MS` 是 2023 年的固定值，对"刚启动"的水位线来说属于历史消息，
    所以凡是要真的进引擎的用例，时间都得由 ChannelDedup 实例现算。
    """
    return str(int((dedup.boot_time - seconds_before_boot) * 1000))


# --- 白名单 -------------------------------------------------------------------


async def test_non_whitelisted_sender_never_reaches_the_engine(caplog):
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=Outbox())

    with caplog.at_level(logging.WARNING, logger="app.channels.feishu.dispatcher"):
        handled = await dispatcher.on_message_event(message_event(open_id=OTHER_OPEN_ID))

    # 已处理（不要让飞书重投），但引擎一次都没跑。
    assert handled is True
    assert engine.messages == []
    # 便于把被拦的 open_id 抄进 feishu_allow_from。
    assert OTHER_OPEN_ID in caplog.text


async def test_whitelist_can_be_turned_off():
    engine = EngineStub()
    settings = feishu_settings(feishu_whitelist_enabled=False, feishu_allow_from="")
    dispatcher = FeishuDispatcher(engine, settings, send_fn=Outbox())

    assert await dispatcher.on_message_event(message_event(open_id=OTHER_OPEN_ID)) is True
    assert len(engine.messages) == 1


# --- 文本消息：字段逐一断言 ----------------------------------------------------------


async def test_text_message_becomes_the_inbound_contract():
    engine = EngineStub()
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=outbox)

    handled = await dispatcher.on_message_event(message_event())

    assert handled is True
    assert len(engine.messages) == 1
    msg = engine.messages[0]
    assert msg.channel == "feishu"
    assert msg.sender_id == OPEN_ID
    assert msg.sender_name == OPEN_ID  # 飞书消息事件里没有昵称，退到 open_id
    assert msg.message_id == MESSAGE_ID
    assert msg.event_id == EVENT_ID
    assert msg.text == "帮我查一下余额"
    # create_time 是字符串毫秒，这里必须已经是 int，且取的是 message 上的那个。
    assert msg.create_time_ms == CREATE_TIME_MS
    assert isinstance(msg.create_time_ms, int)
    # send_fn 被原样交给引擎（引擎会用它回话）。
    assert engine.send_fns[0] is outbox


async def test_create_time_in_seconds_is_normalised_to_ms():
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=Outbox())
    seconds = str(CREATE_TIME_MS // 1000)

    await dispatcher.on_message_event(message_event(create_time=seconds))

    assert engine.messages[0].create_time_ms == CREATE_TIME_MS // 1000 * 1000


async def test_missing_create_time_is_treated_as_fresh():
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=Outbox())

    handled = await dispatcher.on_message_event(
        message_event(create_time="", header_create_time=None)
    )

    assert handled is True
    assert engine.messages[0].create_time_ms == 0


# --- 水位线：历史消息丢弃 ------------------------------------------------------------


async def test_message_before_the_watermark_is_dropped():
    dedup = ChannelDedup(boot_time=time.time())
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), dedup, send_fn=Outbox())
    historical = str(int((dedup.boot_time - 120) * 1000))

    handled = await dispatcher.on_message_event(
        message_event(create_time=historical, header_create_time=None)
    )

    assert handled is True
    assert engine.messages == []  # 启动前的存量消息不进引擎


async def test_click_before_the_watermark_is_dropped():
    dedup = ChannelDedup(boot_time=time.time())
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), dedup, send_fn=Outbox())
    historical = str(int((dedup.boot_time - 120) * 1000))

    response = await dispatcher.on_card_action_event(card_action_event(create_time=historical))

    assert response is None
    assert engine.clicks == []


# --- 防重：同一 message 重投 --------------------------------------------------------


async def test_redelivered_message_is_not_processed_twice():
    dedup = ChannelDedup(boot_time=time.time())
    engine = EngineStub()
    # duplicate_guard 只在 dedup 归 dispatcher 独享时打开；生产里判重由引擎负责。
    dispatcher = FeishuDispatcher(
        engine, feishu_settings(), dedup, send_fn=Outbox(), duplicate_guard=True
    )

    event = message_event(create_time=fresh_ms(dedup), header_create_time=None)

    assert await dispatcher.on_message_event(event) is True
    assert await dispatcher.on_message_event(event) is True

    assert len(engine.messages) == 1  # 第二次重投没再进引擎


async def test_redelivery_passes_again_after_the_engine_forgets_the_keys():
    dedup = ChannelDedup(boot_time=time.time())
    engine = EngineStub()
    dispatcher = FeishuDispatcher(
        engine, feishu_settings(), dedup, send_fn=Outbox(), duplicate_guard=True
    )

    event = message_event(create_time=fresh_ms(dedup), header_create_time=None)

    await dispatcher.on_message_event(event)
    # 引擎这一轮失败了才撤键（Task 7 的约定）；撤键之后飞书重投必须能重来。
    dedup.forget_message(engine.messages[0])
    await dispatcher.on_message_event(event)

    assert len(engine.messages) == 2


async def test_redelivered_click_is_not_processed_twice():
    dedup = ChannelDedup(boot_time=time.time())
    engine = EngineStub()
    dispatcher = FeishuDispatcher(
        engine, feishu_settings(), dedup, send_fn=Outbox(), duplicate_guard=True
    )

    event = card_action_event(create_time=fresh_ms(dedup))

    first = await dispatcher.on_card_action_event(event)
    second = await dispatcher.on_card_action_event(event)

    assert set(first) == {"card"}
    # 第二次只活在回执里（这条路径不发任何消息），但必须让用户看见一句话。
    assert "toast" in second
    assert DUPLICATE_CLICK_TEXT in second["toast"]["content"]
    assert len(engine.clicks) == 1


# --- 非文本消息：回引导，不进引擎 ------------------------------------------------------


async def test_non_text_message_gets_a_guide_and_no_engine_call():
    engine = EngineStub()
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=outbox)

    handled = await dispatcher.on_message_event(
        message_event(message_type="image", content=json.dumps({"image_key": "img_1"}))
    )

    assert handled is True
    assert engine.messages == []
    assert outbox.payloads == [TEXT_ONLY_GUIDE]
    assert isinstance(outbox.payloads[0], str)  # 纯文本，用户要能直接读


# --- 卡片回调 --------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["confirm", "cancel"])
async def test_card_action_becomes_a_click_and_returns_the_callback_response(decision):
    engine = EngineStub()
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=outbox)

    response = await dispatcher.on_card_action_event(card_action_event(decision=decision))

    assert len(engine.clicks) == 1
    click = engine.clicks[0]
    assert click.channel == "feishu"
    assert click.sender_id == OPEN_ID
    assert click.question_id == QUESTION_ID
    assert click.pending_id == PENDING_ID
    assert click.decision == decision
    assert dict(click.raw_value) == {"q": QUESTION_ID, "p": PENDING_ID, "d": decision}
    assert click.event_id == "ev_click_1"
    assert click.create_time_ms == 1700000000789
    assert click.action_id == QUESTION_ID  # 没有 action.action_id 时退到 question_id
    # 引擎自己发的那张中间态卡的回执，就是飞书回调的应答——但必须包成 SDK 认得的那两个键。
    assert response == {"card": {"type": "raw", "data": engine.card_response}}


async def test_card_action_with_json_string_value_is_parsed():
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=Outbox())
    raw = json.dumps({"q": QUESTION_ID, "p": PENDING_ID, "d": "cancel"})

    await dispatcher.on_card_action_event(card_action_event(value=raw))

    assert engine.clicks[0].decision == "cancel"


async def test_card_action_from_a_non_whitelisted_operator_is_dropped():
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=Outbox())

    assert await dispatcher.on_card_action_event(card_action_event(open_id=OTHER_OPEN_ID)) is None
    assert engine.clicks == []


# --- 畸形事件：记日志，不抛回飞书 -----------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"header": {"event_id": EVENT_ID}},
        {"event": {}},
        {"event": {"sender": {"sender_id": {"open_id": OPEN_ID}}}},
        message_event(with_message=False),
        message_event(content="not json at all"),
        message_event(content=json.dumps({"image_key": "k"})),
        message_event(open_id=""),
        None,
    ],
)
async def test_malformed_message_event_never_raises(event):
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=Outbox())

    assert not await dispatcher.on_message_event(event)
    assert engine.messages == []


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"event": {}},
        card_action_event(with_action=False),
        card_action_event(value={}),  # 连 q 都没有：连一张能说话的卡都凑不出来
        card_action_event(open_id=""),
        None,
    ],
)
async def test_malformed_card_event_never_raises(event):
    engine = EngineStub()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=Outbox())

    assert not await dispatcher.on_card_action_event(event)
    assert engine.clicks == []


# --- 卡片回执：形状、被点的那张卡、以及只靠回执交付的分支 -------------------------------


class EchoOutbox:
    """像真飞书那样：更新哪条消息就回哪条消息的 id（更新接口自己不一定回 id）。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, payload, update_message_id=None):
        self.calls.append((payload, update_message_id))
        return update_message_id or "om_brand_new"


class SendingEngine(EngineStub):
    """进站消息就回一句话的引擎：把真的 send 走通到假飞书 API 上。"""

    async def handle_message(self, msg, send_fn):
        self.messages.append(msg)
        await send_fn("一句回复")


async def test_the_clicked_card_is_updated_in_place_and_nothing_new_is_posted():
    """点一下只更新被点的那条消息：不新开消息，聊天里才不会出现两张卡、被点那张僵在原地。

    引擎第一次 send 传的 update_message_id 是 None（它不知道被点的是哪条），dispatcher 把
    `context.open_message_id` 注入成默认目标——首投因此落在被点那张卡上（先置灰成本轮中间
    态），后续刷新沿用同一个 id，整条路径只出现一个消息 id。
    """

    engine = EngineStub()
    outbox = EchoOutbox()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=outbox)

    await dispatcher.on_card_action_event(card_action_event())
    send_fn = engine.send_fns[0]
    first = await send_fn("第一帧")
    await send_fn("第二帧")
    await send_fn("第三帧", update_message_id="om_other")

    # 首次不带 id 也落在被点消息上；引擎拿到的 id 就是那张卡，所以它不会降级成"只发最终一条"。
    assert first == "om_card_1"
    assert [call[1] for call in outbox.calls] == ["om_card_1", "om_card_1", "om_other"]
    assert "om_brand_new" not in {call[1] for call in outbox.calls}


async def test_a_card_callback_without_a_message_id_falls_back_and_says_so(caplog):
    """拿不到被点消息 id 时退回既有行为（新开一条），并留下一条能定位的 warning。"""

    engine = EngineStub()
    outbox = EchoOutbox()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=outbox)
    event = card_action_event()
    event["event"]["context"] = {"open_chat_id": CHAT_ID}

    with caplog.at_level(logging.WARNING, logger="app.channels.feishu.dispatcher"):
        await dispatcher.on_card_action_event(event)
    await engine.send_fns[0]("新开一条")

    assert outbox.calls[0][1] is None  # 既有行为：这一次是新建消息
    assert "open_message_id" in caplog.text


async def test_a_click_with_an_unknown_decision_gets_a_visible_receipt():
    """决定越界：不能执行，也不能一声不吭——用户只会以为卡死了，然后继续点。"""

    engine = EngineStub()
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, feishu_settings(), send_fn=outbox)

    response = await dispatcher.on_card_action_event(
        card_action_event(value={"q": QUESTION_ID, "p": PENDING_ID, "d": "approve"})
    )

    assert "toast" in response
    assert UNKNOWN_DECISION_TEXT in response["toast"]["content"]
    assert engine.clicks == []
    assert outbox.payloads == []


async def test_the_duplicate_guard_refuses_to_share_the_engines_dedup():
    """duplicate_guard=True 与"引擎共用同一份 dedup"是互斥的：那种组合会静默吞掉每条首投。

    `is_duplicate_*` 是"查即记账"，dispatcher 先查一次，引擎那一侧看到的就永远是"见过"。
    这不会抛异常，只会让每条首投都被静默 ACK——所以在构造时直接拒掉。
    """

    dedup = ChannelDedup(boot_time=time.time())
    engine = EngineStub()
    engine.dedup = dedup
    with pytest.raises(ValueError):
        FeishuDispatcher(engine, feishu_settings(), dedup, send_fn=Outbox(), duplicate_guard=True)

    # 真引擎上那份 dedup 叫 _dedup：两种写法都要认，否则这个坑只在生产里存在。
    privately_named = EngineStub()
    privately_named._dedup = dedup
    with pytest.raises(ValueError):
        FeishuDispatcher(
            privately_named, feishu_settings(), dedup, send_fn=Outbox(), duplicate_guard=True
        )

    # 各拿一份就没问题：dispatcher 独享它自己的 dedup。
    own = ChannelDedup(boot_time=time.time())
    assert (
        FeishuDispatcher(engine, feishu_settings(), own, send_fn=Outbox(), duplicate_guard=True)
        is not None
    )


# --- 2200 冲突重试（规格 §3.4.2） ---------------------------------------------------


class FlakyFeishuApi:
    """假飞书 API：按脚本回 2200 或成功，并记下被调了几次。"""

    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = 0
        message = SimpleNamespace(create=self._respond, update=self._respond)
        self.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message)))

    def _respond(self, request):
        del request
        self.calls += 1
        code = self.codes[min(self.calls - 1, len(self.codes) - 1)]
        if code == 0:
            return SimpleNamespace(
                success=lambda: True,
                code=0,
                msg="ok",
                data=SimpleNamespace(message_id="om_updated"),
            )
        return SimpleNamespace(
            success=lambda: False, code=code, msg="card is being updated", data=None
        )


def flaky_dispatcher(api, sleeps):
    """一个把 sleep 换成本地记录的 dispatcher：退避要被看见，但不能真等。"""

    return FeishuDispatcher(
        SendingEngine(),
        feishu_settings(),
        client_factory=lambda settings: api.client,
        sleep_fn=sleeps.append,
    )


async def test_a_2200_conflict_is_retried_and_then_succeeds():
    """第一次 2200、第二次成功：退避 200ms 后重发一次，用户那条回复照常送达。"""

    api = FlakyFeishuApi([2200, 0])
    sleeps = []

    assert await flaky_dispatcher(api, sleeps).on_message_event(message_event()) is True

    assert api.calls == 2
    assert sleeps == [RETRY_DELAYS_MS[0] / 1000.0]


async def test_three_2200s_stop_retrying_and_fail_softly(caplog):
    """连续 3 次 2200：用完次数就认输，只记 error，绝不把异常抛回飞书回调。"""

    api = FlakyFeishuApi([2200, 2200, 2200])
    sleeps = []

    with caplog.at_level(logging.ERROR, logger="app.channels.feishu.dispatcher"):
        handled = await flaky_dispatcher(api, sleeps).on_message_event(message_event())

    assert api.calls == MAX_SEND_ATTEMPTS
    assert sleeps == [RETRY_DELAYS_MS[0] / 1000.0, RETRY_DELAYS_MS[1] / 1000.0]
    assert handled is False
    assert "feishu_send_failed" in caplog.text


async def test_a_deterministic_failure_is_not_retried():
    """参数错、权限没开这类码重试只是把同一个错误再确认两遍：一次就失败。"""

    api = FlakyFeishuApi([99991672])
    sleeps = []

    await flaky_dispatcher(api, sleeps).on_message_event(message_event())

    assert api.calls == 1
    assert sleeps == []


# --- 原地更新：必须是 PATCH（SDK 生成的 update 发的是 PUT，飞书回 230001） ---------------


class PatchRecordingApi:
    """假飞书 API：更新走 client.request，把每次请求原样留下。"""

    def __init__(self, outcomes=None):
        self.requests = []
        self.outcomes = list(outcomes or [])
        message = SimpleNamespace(create=self._create, update=self._put_never_used)
        self.client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=message)),
            request=self._request,
        )

    def _create(self, request):
        del request
        return SimpleNamespace(
            success=lambda: True, code=0, msg="ok", data=SimpleNamespace(message_id="om_new")
        )

    def _put_never_used(self, request):
        del request
        raise AssertionError("更新要走 PATCH：SDK 生成的 update 发 PUT，飞书回 230001")

    def _request(self, request):
        self.requests.append(request)
        if not self.outcomes:
            return SimpleNamespace(success=lambda: True, code=0, msg="ok", data=None)
        code = self.outcomes.pop(0)
        if code == 0:
            return SimpleNamespace(success=lambda: True, code=0, msg="ok", data=None)
        return SimpleNamespace(
            success=lambda: False, code=code, msg="card is being updated", data=None
        )


def test_an_update_is_issued_as_patch_past_the_sdk_builder():
    """一次原地更新 = PATCH /open-apis/im/v1/messages/{id}，body 是 {msg_type, content}。"""

    pytest.importorskip("lark_oapi")
    from lark_oapi.core.enum import AccessTokenType

    request = dispatcher_module._update_message_request(
        "om_card_1", "interactive", '{"schema":"2.0"}'
    )

    assert request.http_method.name == "PATCH"
    assert request.uri == "/open-apis/im/v1/messages/:message_id"
    assert request.paths == {"message_id": "om_card_1"}
    assert request.body == {"msg_type": "interactive", "content": '{"schema":"2.0"}'}
    assert request.token_types == {AccessTokenType.TENANT}


def test_sending_into_an_existing_message_goes_through_client_request():
    """更新不发新消息、也不碰 SDK 生成的 update：返回 None（更新接口本来就不回 id）。"""

    api = PatchRecordingApi()
    dispatcher = FeishuDispatcher(
        EngineStub(), feishu_settings(), client_factory=lambda settings: api.client
    )

    sent = dispatcher._send_blocking({"schema": "2.0"}, "om_card_1", ("chat_id", CHAT_ID))

    assert sent is None
    assert len(api.requests) == 1
    request = api.requests[0]
    assert request.http_method.name == "PATCH"
    assert request.paths == {"message_id": "om_card_1"}
    assert request.body["msg_type"] == "interactive"
    assert json.loads(request.body["content"]) == {"schema": "2.0"}


async def test_an_update_outside_a_feishu_event_still_lands():
    """手机核验页面提交后要回写卡片，但那一刻没有飞书事件上下文。

    更新只需要 message_id，本来就不需要收件人；只有新建消息才需要。
    """

    api = PatchRecordingApi()
    dispatcher = FeishuDispatcher(
        EngineStub(), feishu_settings(), client_factory=lambda settings: api.client
    )

    sent = await dispatcher.send({"schema": "2.0"}, update_message_id="om_card_1")

    assert sent is None
    assert len(api.requests) == 1
    assert api.requests[0].http_method.name == "PATCH"


async def test_a_new_message_outside_a_feishu_event_is_refused():
    """新建一条消息必须有收件人，没有上下文就明确报错，不要静默丢。"""

    api = PatchRecordingApi()
    dispatcher = FeishuDispatcher(
        EngineStub(), feishu_settings(), client_factory=lambda settings: api.client
    )

    with pytest.raises(dispatcher_module.FeishuSendError):
        await dispatcher.send("你好")

    assert api.requests == []

def test_a_busy_card_is_retried_on_the_update_path_too():
    """2200 在更新路径上一样退避重试（正是「卡片正在被同时更新」那个码）。"""

    api = PatchRecordingApi([2200, 0])
    sleeps = []
    dispatcher = FeishuDispatcher(
        EngineStub(),
        feishu_settings(),
        client_factory=lambda settings: api.client,
        sleep_fn=sleeps.append,
    )

    dispatcher._send_blocking("正在生成…", "om_card_1", ("chat_id", CHAT_ID))

    assert len(api.requests) == 2
    assert sleeps == [RETRY_DELAYS_MS[0] / 1000.0]


# --- import 隔离 ------------------------------------------------------------------


def test_no_lark_import_at_module_scope():
    for module in (dispatcher_module, runner_module):
        tree = ast.parse(inspect.getsource(module))
        top_level_imports = [
            node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert not any("lark" in ast.dump(node) for node in top_level_imports), module.__name__


# --- runner：长连接生命周期 -----------------------------------------------------------


class FakeWsClient:
    """假的长连接客户端：start() 阻塞到 stop()，和真 lark ws Client 一样。"""

    def __init__(self):
        self.started = threading.Event()
        self.stop_requested = threading.Event()

    def start(self):
        self.started.set()
        self.stop_requested.wait(timeout=10)

    def stop(self):
        self.stop_requested.set()


async def test_runner_start_returns_false_and_logs_when_lark_is_missing(monkeypatch, caplog):
    def boom(dispatcher, settings):
        raise ModuleNotFoundError("No module named 'lark_oapi'")

    monkeypatch.setattr(runner_module, "_build_ws_client", boom)
    runner = FeishuRunner(EngineStub(), feishu_settings())

    with caplog.at_level(logging.ERROR):
        started = await runner.start()

    # 应用照常起：只记 error，不抛。
    assert started is False
    assert runner.is_running is False
    assert "lark-oapi" in caplog.text.lower()


async def test_runner_refuses_without_credentials(caplog):
    runner = FeishuRunner(EngineStub(), feishu_settings(feishu_app_id="", feishu_app_secret=""))

    with caplog.at_level(logging.ERROR):
        assert await runner.start() is False

    assert runner.is_running is False
    assert "feishu_app_id" in caplog.text


async def test_runner_runs_the_client_off_the_event_loop_and_stops_idempotently():
    client = FakeWsClient()
    runner = FeishuRunner(
        EngineStub(), feishu_settings(), client_factory=lambda dispatcher, settings: client
    )

    assert await runner.start() is True
    assert runner.is_running is True
    # start 立刻返回、连接在工作线程里：事件循环没被卡住。
    assert await asyncio.wait_for(asyncio.to_thread(client.started.wait, 5), 10) is True

    await runner.stop()
    assert runner.is_running is False
    assert client.stop_requested.is_set()

    await runner.stop()  # 幂等：再停一次不抛
    assert runner.is_running is False


async def test_runner_describe_is_probe_friendly():
    runner = FeishuRunner(EngineStub(), feishu_settings())

    text = runner.describe()

    assert "feishu" in text.lower()
    assert "cli_demo" in text
