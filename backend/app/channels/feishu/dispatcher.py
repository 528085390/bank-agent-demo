"""飞书进站适配层：把飞书原始事件译成渠道契约，并把回复真的送回飞书。

三件事，各自有一条边界。

**一、这里只做"翻译 + 闸门"，不做业务。** 白名单、水位线、防重、绑定、授信都在别处；
这里负责把飞书那段 JSON 里散落的字段（`header.event_id`、`message.create_time`、
`sender.sender_id.open_id`……）摘成 `InboundMessage` / `CardActionClick`，然后把活交给
`ChannelEngine`。多写一句业务判断，就等于第二套策略。

**二、解析一律"宽进"，绝不把异常抛回飞书。** 回调里抛异常的唯一后果是飞书判定投递失败
并重投，同一段畸形事件会被反复送进来，日志刷成一片而用户什么也得不到。所以解析失败、
字段缺失、非白名单、历史堆积，全是"记一条日志 + 返回一个假值"，让回调安静结束。

**三、出站可以注入。** `send_fn` 为 None 时用内置的真实实现（内部才 import lark_oapi 的
API client）；测试里注入替身，于是整个模块不需要任何凭据、不联网也能测。

飞书字段形状的假设（留给 Task 9 联调核对，代码里已按"拿不到就降级"处理）：

* 消息事件：`event.sender.sender_id.open_id` / `event.sender.sender_name`（没有昵称时退到
  open_id）/ `event.message.message_id` / `message.message_type` / `message.content`
  （**JSON 字符串**，文本消息形如 `{"text":"你好"}`）/ `message.create_time`（epoch **毫秒**，
  字符串）/ `message.chat_id` / `header.event_id` / `header.create_time`。
* 卡片回调：`event.operator.open_id` / `event.action.value`（`{"q":..,"p":..,"d":..}`，也可能是
  一段 JSON 字符串）/ `event.context.open_chat_id` / `header.event_id` / `header.create_time`。
* 飞书没给 `create_time` 时按 0 处理（当作"刚发生"）：宁可多答一条，也不要因为缺一个字段
  把用户的消息静默丢掉。给了秒值（10 位数）则就地乘 1000，别让水位线把所有消息判成历史。

外发契约（与 `ChannelEngine` 一致）：`send_fn(payload) -> message_id | None`、
`send_fn(payload, update_message_id=<id>)`；`payload` 是 `str` 表示纯文本，`Mapping` 表示已经
渲染好的飞书卡片 JSON。

**四、外发撞上频控就原地退避重试；回执一定是 SDK 认得的那两个键。** 2200（卡片正在被更新）与
99991400（请求过频）是会自己好的错，规格 §3.4.2 要求最多 3 次、200/400/800ms 的退避，这一层
放在这里而不是 throttler——那边只回答"该不该发"，两处重试等于同一句内容被发两遍。卡片回调的
回执形状见 `_card_reply` / `_toast_reply`：lark 的 `P2CardActionTriggerResponse` 只认 `toast`
与 `card`（`card` 内是 `{type, data}`），裸卡片 dict 送回去飞书那边渲染不出来。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from app.channels.contracts import CardActionClick, InboundMessage

logger = logging.getLogger(__name__)

#: 渠道名，进防重键与契约字段；飞书事件只属于这一个渠道。
CHANNEL = "feishu"

#: 卡片按钮只认这两个决定（与 app.channels.engine.DECISIONS 同值）。
DECISIONS = ("confirm", "cancel")

#: 值得原地重试的飞书返回码：2200 是「卡片正在被同时更新」（流式刷新与点击回执撞在一起时最
#: 常见），99991400 是「请求过于频繁」。其余失败码（参数错、权限没开）重试只是把同一个错误再
#: 确认两遍，用户那边的等待却白白拖长。
RETRYABLE_CODES = frozenset({2200, 99991400})

#: 一次外发最多尝试几次（含第一次），以及每次重试前等多久（毫秒）。规格 §3.4.2。
MAX_SEND_ATTEMPTS = 3
RETRY_DELAYS_MS = (200, 400, 800)

#: dispatcher 自己那道防重拦下的重复点击。与引擎那句回执同义：这里拦在引擎之前，引擎的话
#: 到不了用户，所以得自己说一句。
DUPLICATE_CLICK_TEXT = "这次操作刚刚已经处理过了，没有重复执行。"

#: 回传值里的决定既不是 confirm 也不是 cancel：不能执行，也不能一声不吭（否则用户以为卡死了，
#: 只会再点一次）。
UNKNOWN_DECISION_TEXT = "这次点击没能识别成一次确认或取消，没有执行。请回到最新那条消息重新操作。"

#: 非文本消息的引导语。用户发的是图片/语音/文件时，引擎那边没有任何东西可读，
#: 与其静默丢弃（用户以为机器人坏了），不如明确告诉他该发什么。
TEXT_ONLY_GUIDE = "目前只支持文本消息。请直接发文字告诉我要办什么，例如：查一下我的余额。"

#: 小于这个数的正数毫秒时间戳一律当作"传成秒了"（与 app.channels.dedup 同一条判据）。
_MIN_PLAUSIBLE_EPOCH_MS = 100_000_000_000

#: 原地更新一条消息的接口路径；末尾那段是占位符，由 `BaseRequest.paths` 替换成真正的 message_id。
_UPDATE_MESSAGE_URI = "/open-apis/im/v1/messages/:message_id"

#: 这次事件该回给谁：`("chat_id", "oc_xxx")` 或 `("open_id", "ou_xxx")`。
#: 用 ContextVar 而不是参数，是因为 `send_fn` 的契约里没有收件人（`send_fn(payload,
#: update_message_id=...)`）；引擎在回调里起的后台任务会继承当时的 context，所以卡片那条
#: 中间态卡也回得到同一个会话。
_REPLY_TARGET: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "feishu_reply_target", default=None
)


class FeishuSendError(RuntimeError):
    """一次外发没成功。抛出去是为了让引擎把本轮判成失败并撤掉去重键。

    引擎对这两类失败处置相反：首投抛异常 = 用户一个字都没收到（撤键、让重投重来）；
    返回 None = 消息到了但拿不到 id（降级成"只发最终一条"）。所以这里绝不能把失败悄悄
    变成 None——那会让一条回复凭空消失，只留一条 warning。
    """


@dataclass(slots=True)
class _ParsedMessage:
    """解析结果：契约 + 解析过程中才知道的两项（消息类型、回给谁）。"""

    message: InboundMessage
    message_type: str
    reply_target: tuple[str, str]


@dataclass(slots=True)
class _ParsedClick:
    """解析结果：契约 + 回给谁。"""

    click: CardActionClick
    reply_target: tuple[str, str]


@dataclass(slots=True)
class _RefusedClick:
    """解析得出的、**必须让用户看见**的一次拒绝：这次点击不进引擎，但也不能没有回执。

    只有"回传值读得出、但内容不该被执行"这一档走到这里（目前是决定越界）。形状不对的事件
    仍然返回 None——那类事件连发起人是谁都不可信，给它生成回执等于替它说话。
    """

    sender_id: str
    reply_target: tuple[str, str]
    text: str


def _card_reply(card: Mapping[str, Any]) -> dict[str, Any]:
    """把一张卡片包成 `card.action.trigger` 的回执形状。

    lark-oapi 1.7.5 的 `P2CardActionTriggerResponse` 只认两个顶层键：`toast` 与 `card`，而
    `card` 内又是 `{type, data}`。ws 客户端把回执原样 JSON 序列化交回飞书，所以裸卡片 dict 到
    那边只是一个"顶层不认识"的对象：用户点了按钮，卡片一动不动。

    `type` 取 "raw"：data 是**已经渲染好的**卡片 JSON，飞书不要再套模板。
    """

    return {"card": {"type": "raw", "data": dict(card)}}


def _toast_reply(text: str, *, kind: str = "error") -> dict[str, Any]:
    """一条只活在用户眼前的信息回执（不落聊天记录、不替换卡片）。

    它是"没有卡片可给"时的答案：解析不出决定、dispatcher 自己拦下的重复点击。这类分支本来
    就没有可渲染的卡片，硬造一张替换掉被点的卡反而会让用户以为操作成功了。
    """

    return {"toast": {"type": kind, "content": text}}


def _clicked_message_id(event: Mapping[str, Any]) -> str | None:
    """这次回调"被点的那条消息"的 id：`event.context.open_message_id`。

    飞书 2.0 的卡片回调把被点消息放在 `context.open_message_id`（老形状里叫 `open_message`，
    是个带 `message_id` 的对象）。拿不到就没有原地更新的目标，调用方退回既有的"新开一条"。
    """

    body = _mapping(event.get("event")) or {}
    context = _mapping(body.get("context")) or {}
    direct = context.get("open_message_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    nested = _mapping(context.get("open_message"))
    if nested is not None:
        legacy = nested.get("message_id")
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip()
    return None


def _engine_dedup(engine: Any) -> Any:
    """引擎手里那份 `ChannelDedup`（公开属性优先，其次私有字段）；拿不到就是 None。"""

    shared = getattr(engine, "dedup", None)
    return shared if shared is not None else getattr(engine, "_dedup", None)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    """能当字典用就返回它，否则 None（宽进的第一道：别对形状做假设）。"""

    return value if isinstance(value, Mapping) else None


def _create_time_ms(value: Any) -> int:
    """飞书的时间戳（"epoch 毫秒"字符串）折成 int；拿不到就给 0。

    两种容错都在这里：**秒值就地乘 1000**（否则水位线会把每条消息都判成历史，机器人从此
    一声不吭），**空值/非数当 0**（0 在水位线那一侧被当作"刚发生"，宁可多答一条）。
    """

    if value is None or value == "":
        return 0
    try:
        raw = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("feishu_create_time_unreadable value=%r 不是整数毫秒，按 0 处理", value)
        return 0
    if raw <= 0:
        return 0
    if raw < _MIN_PLAUSIBLE_EPOCH_MS:
        return raw * 1000
    return raw


def _text_from_content(content: Any) -> str | None:
    """文本消息的正文：飞书给的是一段 JSON 字符串 `{"text":"..."}`。

    解析不出来或没有 text 字段一律返回 None（由调用方记 warning 后丢弃），绝不把那段原始
    JSON 当成用户的话送进对话——模型会把 `{"text":"查余额"}` 连花括号一起读进去。
    """

    body = content if isinstance(content, Mapping) else None
    if body is None:
        if not isinstance(content, str):
            return None
        try:
            body = json.loads(content)
        except (TypeError, ValueError):
            return None
        if not isinstance(body, Mapping):
            return None
    text = body.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text or None


def _action_value(raw: Any) -> Mapping[str, Any] | None:
    """卡片按钮的回传值：正常是对象，历史版本/手工配置里可能是一段 JSON 字符串。"""

    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


class FeishuDispatcher:
    """把飞书事件翻成渠道契约，并（可选）在此处再拦一道防重。

    `engine` 只需要两个方法（`handle_message` / `handle_card_action`），测试里换成替身即可；
    `dedup` 传的是**进程内唯一**的 `ChannelDedup`（在 lifespan 里建一次），这里只读它的
    水位线与（可选）防重。

    `duplicate_guard` 默认 **False**，因为 `ChannelDedup.is_duplicate_message_obj` 是
    "查即记账"（upsert）而不是纯查询：dispatcher 若也查一次，引擎那条路径看到的就永远是
    "见过"，每条首次到达的消息都会被静默 ACK、每次点击都回"已经处理过了"。生产里判重由
    引擎负责（失败时它会 forget，重投才能重来）。只有 dedup 归 dispatcher 独享（例如单测）
    时才把 `duplicate_guard` 打开，语义是"到这就已经是重复了，别再进引擎"。

    `duplicate_guard=True` 与"引擎共用同一份 dedup"是**互斥**的，构造时直接拒掉（ValueError）：
    那种组合不会报错，只会让每条首投都被静默 ACK——一个看不见的坑，比一个启动失败坏得多。

    `send_fn` 为 None 时用内置的真实发送（那时才 import lark_oapi 的 API client）；
    注入替身则完全不碰网络。`sleep_fn` 只在重试退避时被调用（默认 time.sleep），单列一个参数
    是为了让用例不必真等 200/400 毫秒。`loop` 由 `FeishuRunner.start()` 挂上来，lark 的回调
    线程用它把事件投回服务自己的事件循环。
    """

    def __init__(
        self,
        engine: Any,
        settings: Any,
        dedup: Any = None,
        *,
        send_fn: Any = None,
        duplicate_guard: bool = False,
        client_factory: Any = None,
        sleep_fn: Any = None,
    ) -> None:
        if duplicate_guard and dedup is not None and dedup is _engine_dedup(engine):
            raise ValueError(
                "duplicate_guard=True 不能与引擎共用同一份 ChannelDedup：is_duplicate_* 是"
                "「查即记账」，dispatcher 先查一次，引擎那边看到的就永远是「见过」，每条首投都会"
                "被静默 ACK。判重交给引擎，或者给 dispatcher 一份它独享的 dedup。"
            )
        self._engine = engine
        self._settings = settings
        self._dedup = dedup
        self._duplicate_guard = duplicate_guard
        self._client_factory = client_factory
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        # 注入的替身要原样交给引擎（消息那一路引擎拿到的 send_fn 就是调用方给的那个对象；
        # 卡片那一路会再包一层"默认更新被点消息"的壳，见 _sends_into_the_clicked_message）。
        self._send = send_fn if send_fn is not None else self.send
        self._client: Any = None
        self._client_lock = threading.Lock()
        # 服务中的事件循环，由 runner 在 start() 时挂上；runner 不回填时保持 None。
        self.loop: asyncio.AbstractEventLoop | None = None

    # --- 公开入口 ---------------------------------------------------------------------

    async def on_message_event(self, event: Any) -> bool:
        """一条 `im.message.receive_v1` 事件。

        返回值是"这条事件已经处理完了"（`True`）：非白名单、历史堆积、非文本引导、重复投递
        都算处理完（飞书别再重投）；只有**解析不出契约**时才返回 `False`。任何异常都在这里
        收口——抛回飞书只会换来同一段坏事件的无限重投。
        """

        try:
            parsed = self._parse_message(event)
            if parsed is None:
                return False

            message = parsed.message
            if not self._is_allowed(message.sender_id):
                return True
            if self._is_historical(message.create_time_ms):
                return True
            if parsed.message_type != "text":
                # 引擎那边没有可读的东西：直接回一句人能照做的引导，不进引擎。
                await self._reply(parsed.reply_target, TEXT_ONLY_GUIDE)
                return True
            if self._duplicate_guard and self._is_duplicate_message(message):
                logger.info("feishu_message_skipped_duplicate message_id=%s", message.message_id)
                return True

            token = _REPLY_TARGET.set(parsed.reply_target)
            try:
                await self._engine.handle_message(message, self._send)
            finally:
                _REPLY_TARGET.reset(token)
            return True
        except Exception:
            logger.exception("feishu_message_event_failed 处理消息事件时出了意外，已吞掉不抛回飞书")
            return False

    async def on_card_action_event(self, event: Any) -> dict[str, Any] | None:
        """一次 `card.action.trigger` 回调，返回**SDK 形状**的回执（没有就返回 None）。

        回执只有两种形状，理由见 `_card_reply` / `_toast_reply`：引擎算出来的卡片一律包成
        `{"card": {"type": "raw", "data": ...}}`（那是被点那张卡的新样子）；连一张卡都产不出来
        的分支（决定越界、dispatcher 自己拦下的重复点击）用 `toast`——它只是眼前一句提示，
        不会把"没有结果的卡片"留在聊天记录里。

        被点消息的 id 会被注入成 send_fn 的**默认更新目标**（`_sends_into_the_clicked_message`）：
        引擎第一次发送时传的是 `update_message_id=None`，注入之后这一发落在**被点的那张卡**
        上，卡片原地置灰成本轮中间态；不再在旁边新开一条，于是聊天里不会出现两张卡、被点的那
        张也不会永远停在"可点"的橙色态（规格 §3.4.1 要求原地置灰）。
        """

        try:
            parsed = self._parse_card_action(event)
            if parsed is None:
                return None
            if isinstance(parsed, _RefusedClick):
                if not self._is_allowed(parsed.sender_id):
                    return None
                return _toast_reply(parsed.text)

            click = parsed.click
            if not self._is_allowed(click.sender_id):
                return None
            if self._is_historical(click.create_time_ms):
                return None
            if self._duplicate_guard and self._is_duplicate_click(click):
                logger.info(
                    "feishu_card_skipped_duplicate question_id=%s decision=%s",
                    click.question_id,
                    click.decision,
                )
                # 只靠回执交付的分支：这里不返回一句话，用户点了就等于什么都没发生。
                return _toast_reply(DUPLICATE_CLICK_TEXT, kind="info")

            send_fn = self._sends_into_the_clicked_message(self._send, _clicked_message_id(event))
            token = _REPLY_TARGET.set(parsed.reply_target)
            try:
                response = await self._engine.handle_card_action(click, send_fn)
            finally:
                _REPLY_TARGET.reset(token)
            return _card_reply(response) if isinstance(response, Mapping) else None
        except Exception:
            logger.exception("feishu_card_event_failed 处理卡片回调时出了意外，已吞掉不抛回飞书")
            return None

    def _sends_into_the_clicked_message(self, send_fn: Any, message_id: str | None) -> Any:
        """把 send_fn 包一层：不带 update_message_id 的发送也落在**被点的那条消息**上。

        引擎不知道被点的是哪条消息（`CardActionClick` 是冻结契约，不带消息 id），而"点一下
        出现两张卡、被点那张僵在中间态"正是这条信息缺失的后果。所以补在这里：第一次
        `send_fn(payload, update_message_id=None)` 变成对这张卡的原地更新，后续的刷新本来就会
        带上同一个 id，整条路径因此只有一个消息 id。

        拿不到被点消息 id 时（回调里没有 `context.open_message_id`）退回既有行为并记一条
        warning：那一路会新开一条消息，被点的卡片只好留在原地。
        """

        if not message_id:
            logger.warning(
                "feishu_card_without_message_id 这次回调里没有 context.open_message_id，"
                "本轮只能新开一条消息（被点的那张卡不会原地更新）"
            )
            return send_fn

        async def send_into_the_clicked_message(payload: Any, update_message_id: str | None = None):
            sent = await send_fn(payload, update_message_id=update_message_id or message_id)
            # 更新接口本身不一定回 message_id，但我们每一次都是在更新同一条已知消息，所以这一路
            # 恒有 id 可用——不能让引擎因为"没拿到 id"退化成只发最终一条。
            return sent or message_id

        return send_into_the_clicked_message

    # --- 出站 -------------------------------------------------------------------------

    async def send(self, payload: Any, update_message_id: str | None = None) -> str | None:
        """真实外发：`str` 发文本消息，`Mapping` 发 interactive 卡片。

        SDK 调用是同步阻塞的，一律丢到工作线程（`asyncio.to_thread`），否则一次飞书 API
        往返就会卡住整个事件循环。`update_message_id` 有值就原地更新那条消息（流式刷新），
        否则新建一条，并把它自己的 message_id 返回给引擎。
        """

        target = _REPLY_TARGET.get()
        if target is None and not update_message_id:
            # 原地更新只要有 message_id 就够（PATCH 不带收件人），所以手机核验页面那种
            # 不在飞书事件里的调用也能回写卡片；新建消息才必须有收件人。
            raise FeishuSendError("这次发送不在任何飞书事件的处理上下文里，拿不到收件人")
        return await asyncio.to_thread(self._send_blocking, payload, update_message_id, target)

    def _send_blocking(
        self, payload: Any, update_message_id: str | None, target: tuple[str, str] | None
    ) -> str | None:
        """工作线程里的真实调用：只有这里才会 import lark_oapi 的 API client。

        撞上可重试的返回码（`RETRYABLE_CODES`）时在这里原地退避重试，最多
        `MAX_SEND_ATTEMPTS` 次、`RETRY_DELAYS_MS` 的间隔（规格 §3.4.2）。退避放在这一层而不是
        节流器：节流器只回答"该不该发"，两处重试会把同一句内容发两遍。
        """

        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        msg_type, content = _outbound_body(payload)
        client = self._api_client()
        if update_message_id:
            # 更新不能走 SDK 生成的 UpdateMessageRequest：它把 http_method 写死成 PUT，而飞书
            # 这个接口只吃 PATCH（照 SDK 发 PUT 会拿到 230001 invalid msg_type，实测）。
            call = partial(
                client.request,
                _update_message_request(str(update_message_id), msg_type, content),
            )
        else:
            if target is None:
                raise FeishuSendError("新建消息需要收件人，这次调用没有飞书事件上下文")
            receive_id_type, receive_id = target
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            call = partial(client.im.v1.message.create, request)

        response = self._send_with_retry(
            call, what=f"{'update' if update_message_id else 'create'} {msg_type}"
        )
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        return str(message_id) if message_id else None

    def _send_with_retry(self, call: Any, *, what: str) -> Any:
        """发一次；撞上瞬时错误就退避重试，别的失败码立刻失败。

        重试是有终点的：用完 `MAX_SEND_ATTEMPTS` 仍然不行就抛 `FeishuSendError`，由引擎那一侧
        按"这条回复没送到"处理（撤键、让飞书重投）。退避表按次取，最后一次尝试之后不再等。
        """

        attempt = 0
        while True:
            attempt += 1
            response = call()
            if response.success():
                return response
            code = getattr(response, "code", None)
            if code not in RETRYABLE_CODES or attempt >= MAX_SEND_ATTEMPTS:
                logger.error(
                    "feishu_send_failed %s attempt=%d/%d code=%s msg=%s",
                    what,
                    attempt,
                    MAX_SEND_ATTEMPTS,
                    code,
                    getattr(response, "msg", None),
                )
                raise FeishuSendError(
                    f"飞书消息接口返回失败 code={code} msg={getattr(response, 'msg', None)}"
                    f"（{what}，共尝试 {attempt} 次）"
                )
            delay_ms = RETRY_DELAYS_MS[min(attempt - 1, len(RETRY_DELAYS_MS) - 1)]
            logger.warning(
                "feishu_send_retry %s attempt=%d/%d code=%s 等 %dms 后重试",
                what,
                attempt,
                MAX_SEND_ATTEMPTS,
                code,
                delay_ms,
            )
            self._sleep(delay_ms / 1000.0)

    def _api_client(self) -> Any:
        """懒建 SDK 客户端：建一次就复用（重复 build 会重复拿 tenant token）。"""

        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = self._build_api_client()
        return self._client

    def _build_api_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self._settings)
        from lark_oapi import Client
        from lark_oapi.core.enum import LogLevel

        return (
            Client.builder()
            .app_id(str(getattr(self._settings, "feishu_app_id", "") or ""))
            .app_secret(str(getattr(self._settings, "feishu_app_secret", "") or ""))
            .log_level(LogLevel.INFO)
            .build()
        )

    async def _reply(self, target: tuple[str, str], text: str) -> None:
        """在当前事件上下文里回一句纯文本（引导语这类"不需要引擎"的答复）。"""

        token = _REPLY_TARGET.set(target)
        try:
            await self._send(text)
        finally:
            _REPLY_TARGET.reset(token)

    # --- 闸门 -------------------------------------------------------------------------

    def _is_allowed(self, open_id: str) -> bool:
        """白名单：`feishu_whitelist_enabled` 为假时谁都可以；为真时只认 `feishu_allow_from`。

        被拦的 open_id 要**打进日志**：运维加白名单时唯一需要的就是这一串，让用户自己去找
        自己的 open_id 是不现实的。
        """

        if not bool(getattr(self._settings, "feishu_whitelist_enabled", True)):
            return True
        allow_from = str(getattr(self._settings, "feishu_allow_from", "") or "")
        allowed = {part.strip() for part in allow_from.split(",") if part.strip()}
        if open_id and open_id in allowed:
            return True
        logger.warning(
            "feishu_identity_blocked open_id=%s 不在 feishu_allow_from 里，这条事件已忽略"
            "（要放行就把它加进 feishu_allow_from，多个用逗号分隔）",
            open_id,
        )
        return False

    def _is_historical(self, create_time_ms: int) -> bool:
        """早于"启动水位 − 容差"的事件：服务起来之前就躺在飞书队列里的离线堆积，直接丢。"""

        if self._dedup is None:
            return False
        try:
            historical = self._dedup.is_historical(create_time_ms)
        except ValueError:
            # 明显是单位传错（秒当毫秒）：这条事件的时间不可信，但丢消息的代价更大。
            logger.warning(
                "feishu_watermark_unusable create_time_ms=%s 通不过水位判定", create_time_ms
            )
            return False
        if historical:
            logger.info(
                "feishu_event_skipped_historical create_time_ms=%s 早于启动水位，丢弃",
                create_time_ms,
            )
        return historical

    def _is_duplicate_message(self, message: InboundMessage) -> bool:
        if self._dedup is None:
            return False
        # 命中就返回 True 且**不撤键**：引擎自己会在失败时 forget，这里撤掉等于把失败当成功。
        return bool(self._dedup.is_duplicate_message_obj(message))

    def _is_duplicate_click(self, click: CardActionClick) -> bool:
        if self._dedup is None:
            return False
        return bool(self._dedup.is_duplicate_click(click))

    # --- 解析 -------------------------------------------------------------------------

    def _parse_message(self, event: Any) -> _ParsedMessage | None:
        """飞书消息事件 -> `_ParsedMessage`；形状不对就记 warning 并返回 None。"""

        if not isinstance(event, Mapping):
            logger.warning(
                "feishu_message_malformed 事件不是 JSON 对象（%s）", type(event).__name__
            )
            return None
        header = _mapping(event.get("header")) or {}
        body = _mapping(event.get("event"))
        if not body:
            logger.warning("feishu_message_malformed 事件里没有 event 字段")
            return None
        message = _mapping(body.get("message"))
        if not message:
            logger.warning("feishu_message_malformed event 里没有 message 字段")
            return None
        sender = _mapping(body.get("sender")) or {}
        sender_ids = _mapping(sender.get("sender_id")) or {}
        open_id = str(sender_ids.get("open_id") or "").strip()
        if not open_id:
            logger.warning("feishu_message_malformed 拿不到 sender.sender_id.open_id")
            return None
        message_id = str(message.get("message_id") or "").strip()
        if not message_id:
            logger.warning("feishu_message_malformed 拿不到 message.message_id")
            return None
        event_id = str(header.get("event_id") or message.get("event_id") or "").strip()
        if not event_id:
            logger.warning(
                "feishu_message_malformed 拿不到 header.event_id message_id=%s", message_id
            )
            return None
        message_type = str(message.get("message_type") or "").strip().lower()
        if not message_type:
            logger.warning(
                "feishu_message_malformed 拿不到 message.message_type message_id=%s", message_id
            )
            return None

        text = ""
        if message_type == "text":
            text = _text_from_content(message.get("content")) or ""
            if not text:
                logger.warning(
                    "feishu_message_text_unreadable message_id=%s content=%r 不是可读的文本消息",
                    message_id,
                    message.get("content"),
                )
                return None

        create_time_ms = _create_time_ms(message.get("create_time")) or _create_time_ms(
            header.get("create_time")
        )
        sender_name = str(sender.get("sender_name") or sender.get("name") or open_id).strip()
        chat_id = str(message.get("chat_id") or "").strip()
        reply_target = ("chat_id", chat_id) if chat_id else ("open_id", open_id)
        parsed = _ParsedMessage(
            message=InboundMessage(
                channel=CHANNEL,
                sender_id=open_id,
                sender_name=sender_name or open_id,
                message_id=message_id,
                event_id=event_id,
                text=text,
                create_time_ms=create_time_ms,
            ),
            message_type=message_type,
            reply_target=reply_target,
        )
        return parsed

    def _parse_card_action(self, event: Any) -> _ParsedClick | _RefusedClick | None:
        """飞书卡片回调 -> `_ParsedClick`；形状不对返回 None，决定越界返回 `_RefusedClick`。

        两者都"不进引擎"，但后者要给用户一句话：卡上那两个按钮之外的取值（老版本卡片、手工
        配错的模板）点下去既不能执行也不该静默——用户只会以为卡死了，然后继续点。
        """

        if not isinstance(event, Mapping):
            logger.warning("feishu_card_malformed 事件不是 JSON 对象（%s）", type(event).__name__)
            return None
        header = _mapping(event.get("header")) or {}
        body = _mapping(event.get("event"))
        if not body:
            logger.warning("feishu_card_malformed 事件里没有 event 字段")
            return None
        operator = _mapping(body.get("operator")) or {}
        open_id = str(operator.get("open_id") or "").strip()
        if not open_id:
            logger.warning("feishu_card_malformed 拿不到 operator.open_id")
            return None
        action = _mapping(body.get("action"))
        if not action:
            logger.warning("feishu_card_malformed event 里没有 action 字段")
            return None
        value = _action_value(action.get("value"))
        if value is None:
            logger.warning("feishu_card_malformed action.value 不是可读的 JSON 对象")
            return None

        question_id = str(value.get("q") or "").strip()
        pending_id = str(value.get("p") or "").strip()
        decision = str(value.get("d") or "").strip().lower()
        context = _mapping(body.get("context")) or {}
        chat_id = str(context.get("open_chat_id") or "").strip()
        if not question_id:
            logger.warning(
                "feishu_card_malformed 回传值缺 q question_id=%r decision=%r",
                question_id,
                decision,
            )
            return None
        if decision not in DECISIONS:
            logger.warning(
                "feishu_card_unknown_decision 回传值里的决定不认识 question_id=%s decision=%r",
                question_id,
                decision,
            )
            return _RefusedClick(
                sender_id=open_id,
                reply_target=("chat_id", chat_id) if chat_id else ("open_id", open_id),
                text=UNKNOWN_DECISION_TEXT,
            )

        event_id = str(header.get("event_id") or body.get("event_id") or "").strip()
        if not event_id:
            logger.warning(
                "feishu_card_malformed 拿不到 header.event_id question_id=%s", question_id
            )
            return None
        create_time_ms = _create_time_ms(header.get("create_time")) or _create_time_ms(
            body.get("create_time")
        )
        # 按钮本身没有自己的 id：退到 question_id，日志与审计里至少能追到"哪张卡"。
        action_id = str(action.get("action_id") or value.get("action_id") or question_id).strip()
        reply_target = ("chat_id", chat_id) if chat_id else ("open_id", open_id)
        return _ParsedClick(
            click=CardActionClick(
                channel=CHANNEL,
                sender_id=open_id,
                action_id=action_id or question_id,
                question_id=question_id,
                pending_id=pending_id,
                decision=decision,
                raw_value=dict(value),
                event_id=event_id,
                create_time_ms=create_time_ms,
            ),
            reply_target=reply_target,
        )


def _outbound_body(payload: Any) -> tuple[str, str]:
    """把引擎给的 payload 折成飞书要的 (msg_type, content JSON 字符串)。"""

    if isinstance(payload, str):
        return "text", json.dumps({"text": payload}, ensure_ascii=False)
    if isinstance(payload, Mapping):
        return "interactive", json.dumps(payload, ensure_ascii=False)
    raise FeishuSendError(f"不认识的外发载荷类型 {type(payload).__name__}：只支持 str 与 Mapping")


def _update_message_request(message_id: str, msg_type: str, content: str) -> Any:
    """拼一次原地更新的请求：飞书只认 PATCH，而 SDK 生成的那个请求写死成了 PUT。

    `lark_oapi.api.im.v1.UpdateMessageRequest` 的 builder 里 `http_method` 硬编码
    `HttpMethod.PUT`，发给 `PATCH /open-apis/im/v1/messages/{message_id}` 才是飞书认可的
    更新口；照 SDK 发 PUT 会拿到 230001（invalid msg_type，实测）。所以这里退回 SDK 的通用
    请求口自己拼：取 tenant token、拼 URL、解析响应仍然交给 `client.request`，我们只把方法名
    和方法体改成飞书要的那个形状。
    """

    from lark_oapi.core.enum import AccessTokenType
    from lark_oapi.core.http import HttpMethod
    from lark_oapi.core.model import BaseRequest

    request = BaseRequest()
    request.http_method = HttpMethod.PATCH
    request.uri = _UPDATE_MESSAGE_URI
    request.token_types = {AccessTokenType.TENANT}
    request.paths = {"message_id": message_id}
    request.body = {"msg_type": msg_type, "content": content}
    return request
