"""渠道调度核心：把一条进站消息或一次卡片点击，走成一次真实的网银对话。

它只做"编排"，不做业务：白名单、水位线、防重、绑定、渲染、节流都在各自的模块里，授信与
授记一律走 app.api.pending_actions——渠道层既不直调转账核心，也不复制 decide 的授信逻辑。
复制一份就等于第二套策略，两套迟早漂移，而漂移的那一天用户点一次确认就出两次账。

三条硬边界，代码里各有对应的位置：

1. **排他锁每条路径都要还。** 锁由 submit_prepared_turn /
   execute_pending_action_decision 拿到手，交回来时仍然持着（streaming 分支），调用方负责在
   body 结束时释放。所以消费 turns.stream 的每一段都在 try ... finally: held.release() 里；
   release() 本身幂等，重复调用无害，漏掉一次就等于把这个会话在该进程余下的生命周期里焊死。
2. **body 结束后才跑命名。** DeferredNaming.run() 必须排在 final 之后、锁释放之后（设计规格
   S-2.1）：命名是一次额外的模型调用，让它挡住发布或挡住锁，用户会盯着"正在生成…"等到超时。
3. **验证码不进卡片、不进日志、不进审计。** 卡片只由 FeishuCardRenderer 的白名单投影生成，
   要素里出现 verification_code 之类的键也上不了卡；引擎自己也不打印任何待确认载荷。飞书
   按钮的回传值带不回验证码，所以需要验证码的卡片只给一条签名短链，用户去银行自己的页面输入。

并发与幂等（详细结论见 app/channels/dedup.py 与 app/conversation/turns.py）：

* 同一个事件重投 -> ChannelDedup 的事件键 / 消息键命中，静默 ACK，不跑第二轮对话；
  即使防重被绕过（进程重启、键过期），幂等键由 message_id 派生，turns.prepare 会把第二次
  交付判成 receipt，仍然只有一次账本落笔、一次图调用。
* 同一张卡片连点 30 次 -> 防重键是 (question_id, decision)，只有第一次会起后台协程，其余 29
  次同步返回一张"已经处理过了"的卡片，一次授信都不碰。
* 处理失败 -> 主动把去重键撤掉（forget_message / forget_click）。防重只能回答"见过"，答不了
  "办成了没有"；键留着就等于把这条消息判了死刑，飞书重投回来会被静默吞掉。

外发约定（Task 8 的 dispatcher 按这个实现）：

    send_fn(payload) -> message_id | None
    send_fn(payload, update_message_id=<id>)

payload 是 str（纯文本引导、错误说明）或 Mapping（已渲染好的飞书卡片 JSON，直接送
im/v1/messages 的 interactive 类型）。第一次发送不带 update_message_id，返回的消息 id 用来
原地更新后续的流式刷新。两条兜底不能混为一谈：首投抛异常 = 这条回复一个字都没到用户手里，
本轮记成失败（撤键、让重投重来）；首投返回 None = 消息到了但拿不到 id，本回合降级成"只发
最终一条"，不会把一条流式答复刷成一屏卡片。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from app.api.pending_actions import (
    PendingActionPreparation,
    execute_pending_action_decision,
    submit_prepared_turn,
)
from app.channels.binding import ChannelBindingManager
from app.channels.code_cards import forget_code_card, remember_code_card
from app.channels.contracts import CardActionClick, InboundMessage
from app.channels.dedup import ChannelDedup
from app.channels.mfa import create_mfa_token
from app.channels.render.base import BaseRenderer, CardStatus
from app.channels.render.feishu import MAX_TEXT_CHARS, FeishuCardRenderer
from app.channels.throttler import StreamThrottler
from app.conversation.service import thread_id_for_conversation
from app.conversation.turns import DeferredNaming, HeldLock, TurnPlan
from app.core.errors import AppError

logger = logging.getLogger(__name__)

#: 卡片标题。流式刷新与最终结果共用同一个标题，用户才看得出"还是那一条"。
CARD_TITLE = "银行智能助手"

#: 还没绑定网银用户时的引导。/bind 这三个字是用户唯一能抄走的东西，所以每条引导都带它。
UNBOUND_TEXT = (
    "我还不知道您是哪位网银用户。请登录网银，在「飞书绑定」里生成一枚六位绑定码，"
    "然后在这里发送：/bind 123456（把 123456 换成您自己的码）"
)

#: 白名单拒绝。不解释细节，也不回显 open_id：拒绝本身不该变成一份可枚举的名单探测接口。
UNAUTHORIZED_TEXT = "抱歉，当前账号没有开通飞书办理渠道。请联系客户经理把你的飞书身份加入白名单。"

#: 绑定成功 / 绑定失败。/bind 的回执**永远不回显用户发来的那串码**：绑定码是凭据，
#: 复述一遍就等于把它留在聊天记录、日志与截图里（Global Constraints 的机密红线）。
BIND_OK_TEXT = "绑定成功。现在可以直接在这里办理业务了。"
BIND_FAILED_TEXT = (
    "绑定码无效、已过期或已被使用。请登录网银重新生成一枚，然后发送：/bind 您的绑定码"
)

#: 幂等重投：这条消息早就办过了，账本里那一条就是答案。
REPLAY_TEXT = "这条消息已经处理过了，没有重复执行。"

#: 卡片回调的中间态：飞书要求 3 秒内必有响应，这 3 秒里授信还没跑完。
INTERMEDIATE_TEXT = "已收到，正在核验并执行…"

#: 重复点击：这张卡（这同一个决定）刚刚已经处理过，不再执行第二次。
DUPLICATE_CLICK_TEXT = "这次操作刚刚已经处理过了，没有重复执行。"

#: 兜底失败说明。具体错误码只进日志，用户看到的是人能读懂的一句话。
FAILED_TEXT = "这次没能办成，请稍后再试一次。"
CARD_FAILED_TEXT = "这次操作没能完成。"

#: 流没跑完（被取消，或生成器没给 final 就结束）时的收尾卡：用户不能永远停在"正在生成…"，
#: 否则他既不知道这次到底有没有办成，也没有再发一次的理由。
INTERRUPTED_TEXT = "这次回答中断了，没有完整发出来。请重新发一次。"

#: 追问的回答前缀。与网页端同一口径（App.vue 发的是"补充信息：…"），命名的正文里也读得懂。
ANSWER_PREFIX = "补充信息："

#: runtime 在「悬着问题却没带 resume」时给的错误码：渠道拿它认「这句话是在跟确认卡说话」。
INTERRUPT_ANSWER_REFUSED = "INVALID_INTERRUPT_ANSWER"

#: 挂起的是一张确认卡时，文字答复不能当授权（金融红线在 pending_actions 里，这里连门都不
#: 让它进）。用户点按钮才算答复，所以只给一句"去点按钮"的引导。
CONFIRM_VIA_CARD_TEXT = (
    "这笔操作要用确认卡上的【确认】或【取消】按钮来答复。文字回复不能当作授权，请直接点按钮。"
)

#: 多字段追问的临时收集进度上限。一条会话占一个键，进程内即可：重启丢掉它，最坏不过是让
#: 用户把字段再答一遍，问题本身还在 checkpoint 里。
MAX_ASK_PROGRESS = 200


def _field_options(field: Mapping[str, Any]) -> list[str]:
    """选项型字段的可选值；不是选项型（或字段没给）就是空表。"""

    options = field.get("options")
    return [str(option) for option in options] if isinstance(options, list) else []


def _ask_next_field(label: str, options: list[str]) -> str:
    """还差一个字段时的追问：把字段名（和可选项）说清楚，用户才好一条消息答完。"""

    if options:
        return "还需要您补一句：" + label + "。可选：" + "、".join(options)
    return "还需要您补一句：" + label + "，直接回复内容就行。"


def _option_answer(text: str, options: list[str]) -> str:
    """把「回序号」换成选项原文；换不了（不是序号、序号越界）就原样交回去。

    选项原文是「活期账户（尾号 main）」这种长名字，手机上照抄一遍很容易错一个字就被打回。
    序号只是输入上的便利：换出来的是**选项原文本身**，所以给图的取值仍然是模块自己列出来的
    那个值，等于替用户省了打字，没有放松任何校验。
    """

    stripped = text.strip()
    if not options or not stripped:
        return text
    if stripped.isdigit():
        number = int(stripped)
        if 1 <= number <= len(options):
            return options[number - 1]
        return text
    for option in options:
        if stripped == option.strip():
            return option
    return text


def _question_body(display: str, fields: list[Mapping[str, Any]]) -> str:
    """追问卡片的正文：一句问题 + 每个字段。选项型把可选项编号列出来，用户回个数字就行。

    浏览器那边这些字段是输入框和下拉框，结构化控件本身就把「能填什么」摆在用户眼前；飞书卡片
    上只有正文，漏了这一步用户就只能猜，猜错了还会被业务模块当成越界取值打回——他自己根本
    不知道哪里错了。
    """

    lines = [display.strip()]
    for item in fields:
        label = str(item.get("label") or item.get("name") or "").strip()
        if not label:
            continue
        options = _field_options(item)
        lines.append("")
        if options:
            lines.append(label + "（回复序号或完整名称）：")
            lines.extend(str(number) + "、" + option for number, option in enumerate(options, 1))
        else:
            lines.append(label + "：直接回复内容就行。")
    if len(lines) == 1:
        return lines[0]
    lines.append("")
    lines.append("直接在下面回一条消息就行。")
    return chr(10).join(lines)


#: 卡片回传值只认这两个决定（与 contracts.Decision 同值）；越界取值一律拒绝。
DECISIONS = ("confirm", "cancel")

#: AppError.code -> 渠道自己的说法。**刻意不透传** `error.message`：
#:
#: API 层那些句子是写给网页和接口调用方的，落到飞书按钮上兑现不了。最典型的是 422 那句
#: 「这笔操作需要验证码，请填写后重试」——飞书按钮的回传值带不回验证码，用户在这张卡上根本
#: 没有"填写"的地方，照着这句话做只会反复点、反复被拒。文案本身也是随接口版本演进的，渠道
#: 话术要是跟着它漂，同一次失败在两版之间就对用户说了两套话。
#: 模型没配好时的一句话。渠道没有"设置"入口，只能把人指回网页——否则用户只会收到一句和
#: 他刚做的事毫不相干的报错。
MODEL_MISSING_TEXT = (
    "机器人还没配置模型。请在网页端的「模型设置」里填好 Base URL、API Key 和模型名，然后再发一次。"
)

REFUSAL_TEXTS: dict[str, str] = {
    "VERIFICATION_CODE_REQUIRED": (
        "这张卡需要验证码，按钮带不了验证码。请登录网银，在这张卡对应的确认页上输入验证码完成操作。"
    ),
    "VERIFICATION_FAILED": "验证码不正确或已经失效。请回到网银页面重新输入一次。",
    "INVALID_INTERRUPT_ANSWER": "这张卡已经过期了。请在最新那条消息里重新发起这次操作。",
    "IDEMPOTENCY_KEY_REQUIRED": "这次点击缺一个可识别的编号，没有执行。请重新点一次。",
    "IDEMPOTENCY_CONFLICT": "这次操作和上一笔对不上，为避免重复执行，请重新发起。",
    "CONVERSATION_NOT_WRITABLE": "这个会话已经归档，无法继续办理。请重新发一条消息开始新的会话。",
    "NEW_CONVERSATION_REQUIRED": "换一个新的目标要新开会话。请重新发一条消息。",
    "EXECUTION_RECOVERY_REQUIRED": "上一次操作没有完成、也无法安全恢复。请重新发一条消息。",
    "PERSISTENCE_FAILED": FAILED_TEXT,
    "CONTEXT_LIMIT_EXCEEDED": "这段对话太长，超出了处理上限。请重新发一条消息继续。",
    "MODEL_NOT_CONFIGURED": MODEL_MISSING_TEXT,
}

#: 这几个拒绝说明**这条会话本身**接不下去了（图停在半路、会话被归档、上下文超限）。
#: 网页那边用户点「新建会话」就能继续，渠道只有一个固定会话，所以必须自动换一条——
#: 否则用户发多少条都是同一句"上一次操作没有完成"，而他其实什么都没做错。
ROTATE_CODES = frozenset(
    {
        "EXECUTION_RECOVERY_REQUIRED",
        "CONVERSATION_NOT_WRITABLE",
        "CONTEXT_LIMIT_EXCEEDED",
    }
)

#: 换完会话必须请用户重发：新会话里没有上一句，不重发就断在半截。
ROTATED_TEXT = "这条会话接不下去了，我已经换了一条新会话。请把刚才那句话再发一次。"


def _refusal_text(error: AppError) -> str:
    """一次拒绝该对用户怎么说：按 code 查渠道话术，不认识的 code 用兜底那一句。

    未知 code 不退化成 `error.message`：那句可能是给接口调用方看的内部措辞，渠道这一侧
    判断不了它在飞书里说不说得通；兜底那句至少是完整的、用户能据以行动的。
    """

    return REFUSAL_TEXTS.get(error.code, FAILED_TEXT)


class _DeliveryFailed(RuntimeError):
    """首投失败：这条回复一个字都没到用户手里。

    它和"刷新丢了一次"是两回事：刷新失败只是显示落后一拍，下一段就补上了；首投失败是用户
    什么都没收到，而库里这条消息已经落笔、去重键还留在窗口里。处置也相反——刷新失败只能
    warning，首投失败必须让本轮算失败（撤键、让重投重来），否则飞书一抖，用户那条诉求就彻底
    消失，系统里只剩一条 warning。
    """


class _Outbox:
    """一次进站事件的外发队列：第一条新建消息，之后原地更新同一条。

    三类失败，三种处置（混为一谈就是"消息凭空消失"或"一条答复刷成一屏卡片"）：

    1. **首投失败**（第一次 send_fn 抛）-> 抛 `_DeliveryFailed`，由调用方落成本轮失败；
    2. **原地更新失败**（飞书 2200 冲突、网络抖）-> 只记 warning：正文是"显示层"，丢一次
       刷新还有下一次，而把异常抛回事件循环只会让用户那条已经落库的消息永远没人回；
    3. **首投拿不到 message id**（dispatcher 返回 None）-> 本回合降级为"只发最终一条"：
       没有 id 就没法原地更新，照旧一条条新发会把一条流式答复刷成刷屏。
    """

    __slots__ = ("_degraded", "_message_id", "_send_fn")

    def __init__(self, send_fn: Callable[..., Awaitable[Any]]) -> None:
        self._send_fn = send_fn
        self._message_id: str | None = None
        self._degraded = False

    @property
    def message_id(self) -> str | None:
        """第一条消息的 id；None 表示拿不到（本回合已经降级）。"""

        return self._message_id

    @property
    def degraded(self) -> bool:
        """首投没拿到 id：本回合只发最终一条，中间刷新一律跳过。"""

        return self._degraded

    async def post(self, payload: Any, *, final: bool = False) -> None:
        """发一条；`final=True` 表示这是本回合的最后一条（降级时也必须发出去）。"""

        if self._message_id is None and self._degraded:
            if not final:
                logger.info(
                    "channel refresh skipped: this turn has no message id to update in place"
                )
                return
            try:
                await self._send_fn(payload)
            except Exception as error:
                raise _DeliveryFailed("the closing channel message was not delivered") from error
            return
        if self._message_id is None:
            try:
                sent = await self._send_fn(payload)
            except Exception as error:
                raise _DeliveryFailed("the first channel message was not delivered") from error
            if isinstance(sent, str) and sent:
                self._message_id = sent
                return
            # 拿不到 id：之后每一次"原地更新"都无从谈起。降级成只发最终一条，而不是继续
            # 一条条新发——那正是把一条答复变成刷屏的那条路。
            self._degraded = True
            logger.warning(
                "channel send returned no message id (%s): this turn is degraded to a single "
                "final message",
                type(sent).__name__,
            )
            return
        try:
            await self._send_fn(payload, update_message_id=self._message_id)
        except Exception:
            logger.warning("could not update a channel message in place; continuing", exc_info=True)


async def _best_effort(outbox: _Outbox, payload: Any, *, final: bool = True) -> bool:
    """发一条"结论"类消息；发不出去只记 error 并返回 False。

    用在**失败路径**上：那次失败本身已经会被记下来（撤键 + error 日志），拿不到投递不能再
    抛一次——抛出去只会把"用户没收到结论"变成"事件循环里多一个没人接的异常"。
    """

    try:
        await outbox.post(payload, final=final)
    except _DeliveryFailed:
        logger.error(
            "the conclusion of a channel turn could not be delivered either", exc_info=True
        )
        return False
    return True


class ChannelEngine:
    """一条私聊的调度核心：消息进站、事件消费、卡片回调，全部收在这里。

    rt 是组合根（app.runtime.Runtime），settings 是同一份配置，binding_mgr 与 dedup 由**调用方
    注入**：ChannelDedup 必须是进程内唯一的那一个实例（它记着启动水位线与键表，new 两个等于
    两套防重各记各的，等于没有防重）。渲染器与节流器有默认实现，测试可以换。
    """

    def __init__(
        self,
        *,
        rt: Any,
        settings: Any,
        binding_mgr: ChannelBindingManager,
        dedup: ChannelDedup,
        renderer: BaseRenderer | None = None,
        throttler_factory: Callable[[], StreamThrottler] | None = None,
    ) -> None:
        self._rt = rt
        self._settings = settings
        self._binding_mgr = binding_mgr
        self._dedup = dedup
        self._renderer = renderer or FeishuCardRenderer()
        # 一个 turn 一个节流器（它记着"上次推出去的是哪段文字"），所以这里收的是工厂而不是实例。
        self._throttler_factory = throttler_factory or (
            lambda: StreamThrottler(max_text_chars=MAX_TEXT_CHARS)
        )
        # 卡片回调的后台协程。飞书要求 3 秒内先回一张中间态卡，真正的授信与流式生成跑在这里；
        # 手动记一份是为了让测试与优雅退出都能等到它们结束，而不是让任务悬在事件循环里。
        self._tasks: set[asyncio.Task] = set()
        # 多字段追问的逐字段收集进度（键：conversation_id）。一问多字段时一条消息装不下，
        # 而 resume 的 value 必须**恰好**覆盖问题的全部字段，缺一个就会被打回重问一轮。
        self._ask_progress: dict[str, dict[str, Any]] = {}

    # --- 公开接口 ---------------------------------------------------------------------

    async def handle_message(
        self, msg: InboundMessage, send_fn: Callable[..., Awaitable[Any]]
    ) -> None:
        """一条进站私聊消息：白名单 -> 水位线 -> 防重 -> 绑定 -> prepare -> stream。

        不抛异常（CancelledError 这类 BaseException 除外）：这是从飞书回调线程派进事件循环的
        落点，抛出去的异常没人接，用户只会看到"机器人没反应"。所以失败一律落成一张失败卡片
        加一条日志，并把去重键撤掉让重投能重来。
        """

        if not self._binding_mgr.is_allowed(msg.sender_id):
            logger.warning(
                "channel message rejected: %s sender %s is not on the whitelist",
                msg.channel,
                msg.sender_id,
            )
            await self._post_text(send_fn, UNAUTHORIZED_TEXT)
            return

        if self._is_historical(msg.create_time_ms):
            # 服务起来之前就躺在飞书队列里的离线堆积：静默 ACK，不回放。
            logger.info(
                "channel message skipped: %s event is older than the boot watermark", msg.channel
            )
            return

        if self._dedup.is_duplicate_message_obj(msg):
            # 同一个 event / message 被重投：静默 ACK。业务早就办过一轮了。
            logger.info("channel message skipped: %s event is a redelivery", msg.channel)
            return

        confirm_open = False
        try:
            if await self._answer_bind_command(msg, send_fn):
                return
            binding = self._binding_for(msg)
            if binding is None:
                await self._post_text(send_fn, UNBOUND_TEXT)
                return

            if not self._model_ready():
                # 模型没配好就在这儿拦住。放它进图，图会在模型调用处半途失败，把会话留在
                # "未完成"状态，之后每条消息都被判成 EXECUTION_RECOVERY_REQUIRED——用户
                # 最后看到的是"上一次操作没有完成"，跟他实际做的事毫无关系。
                self._forget_message(msg)
                logger.warning("channel turn refused: %s code=MODEL_NOT_CONFIGURED", msg.channel)
                await self._post_text(send_fn, MODEL_MISSING_TEXT)
                return

            who = await asyncio.to_thread(
                self._binding_mgr.get_or_create_channel_session, binding.user_id
            )
            conversation_id = await asyncio.to_thread(
                self._binding_mgr.get_or_create_conversation, binding, self._rt
            )
            # 悬着的问题要先看一眼：IM 里用户只会打字，而一条打字要么是那个问题的答案，要么
            # 是一张确认卡的回执——两者各走各的协议。一律当新消息塞进图里，runtime 会判
            # INVALID_INTERRUPT_ANSWER（图一次都不跑），用户就被一个他自己看不见的问题卡死。
            question = await self._open_question(who, conversation_id)
            text, context, confirm_open = await self._answer_for(
                question, conversation_id, msg, send_fn
            )
            if text is None:
                # 这一条已经就地答复完了（引导去点按钮，或者只收下了多字段里的一个字段）。
                return
            # 锁、准入、幂等与"该给流还是给回执"的次序全在 pending_actions 里，渠道不再抄一遍。
            result = await submit_prepared_turn(
                self._rt,
                who,
                PendingActionPreparation(
                    conversation_id=conversation_id,
                    message=text,
                    context=context,
                    key=self._message_key(msg),
                ),
            )
            if result.status == "receipt":
                # 幂等键已经花掉：这条消息进了账本、没有第二次图调用，用户看到一句说明就够。
                await self._post_text(send_fn, REPLAY_TEXT)
                return
            await self._relay_stream(
                who=who,
                plan=result.outcome,
                held=result.held_lock,
                naming=result.naming,
                send_fn=send_fn,
            )
        except _DeliveryFailed:
            # 这条回复一个字都没送出去：本轮算失败。键必须撤掉——防重只回答"见过"，答不了
            # "办成了没有"，键留着等于把这条消息判了死刑，飞书重投回来会被静默 ACK 吞掉。
            self._forget_message(msg)
            logger.error(
                "channel turn reached no message: %s message %s",
                msg.channel,
                msg.message_id,
                exc_info=True,
            )
            await _best_effort(_Outbox(send_fn), FAILED_TEXT)
        except AppError as error:
            self._forget_message(msg)
            logger.warning("channel turn refused: %s code=%s", msg.channel, error.code)
            if error.code == INTERRUPT_ANSWER_REFUSED and confirm_open:
                # 悬着的是确认卡：文字不能当授权（红线），但也不该甩一句「卡过期了」。
                # 只把用户按回按钮上，会话本身没坏，下一条消息照常。
                await self._post_text(send_fn, CONFIRM_VIA_CARD_TEXT)
                return
            if error.code in ROTATE_CODES and await self._rotate_conversation(msg):
                await self._post_text(send_fn, ROTATED_TEXT)
                return
            await self._post_text(send_fn, _refusal_text(error))
        except Exception:
            self._forget_message(msg)
            logger.exception("channel turn failed: %s message %s", msg.channel, msg.message_id)
            await self._post_text(send_fn, FAILED_TEXT)

    async def handle_card_action(
        self, click: CardActionClick, send_fn: Callable[..., Awaitable[Any]]
    ) -> dict[str, Any]:
        """一次卡片按钮点击：查重 -> 同步返回中间态卡片 -> 后台跑授信与流式生成。

        返回值就是那张中间态卡片（飞书要求 3 秒内响应，所以它必须同步产出，而且**不带任何
        behaviors**：这张卡还能再点一次，用户连击第二次就是第二次真实出账）。真正的执行在
        _decide 里，通过 execute_pending_action_decision 走同一套授信状态机。

        这张卡由引擎自己发**一条**（再把同一张卡作为回调应答返回）：一次性的回调应答拿不到
        消息 id，而后面无论是流式刷新还是失败结论都必须更新这张卡——拿不到 id 的代价是
        "点下去那张卡永远停在正在核验并执行…"，用户既不知道办没办成，也没有再试一次的入口。
        """

        if click.decision not in DECISIONS:
            # 契约不负责校验不可信输入，越界取值由引擎在这里拒掉。
            logger.warning("channel card action rejected: unknown decision")
            return self._text_card(CARD_FAILED_TEXT, CardStatus.FAILED)

        if self._is_historical(click.create_time_ms):
            logger.info(
                "channel card action skipped: %s event is older than the boot watermark",
                click.channel,
            )
            return self._text_card(DUPLICATE_CLICK_TEXT, CardStatus.CANCELLED)

        if self._dedup.is_duplicate_click(click):
            # 手机端 500 ms 内的连击，以及同一张卡重发之后的第二次点击，都在这里被挡住。
            logger.info("channel card action skipped: %s click is a repeat", click.channel)
            return self._text_card(DUPLICATE_CLICK_TEXT, CardStatus.CANCELLED)

        binding = self._binding_mgr.get_binding(click.channel, click.sender_id)
        if binding is None:
            # 点卡片的人还没绑定：同一个 open_id 在消息通道里也走不通，所以这里直接给引导。
            self._forget_click(click)
            return self._text_card(UNBOUND_TEXT, CardStatus.FAILED)

        who = await asyncio.to_thread(
            self._binding_mgr.get_or_create_channel_session, binding.user_id
        )
        conversation_id = await asyncio.to_thread(
            self._binding_mgr.get_or_create_conversation, binding, self._rt
        )
        card = self._renderer.render_intermediate_card(CARD_TITLE, INTERMEDIATE_TEXT)
        outbox = _Outbox(send_fn)
        try:
            await outbox.post(card)
        except _DeliveryFailed:
            # 3 秒应答不能因此消失：同步那张卡照旧返回，后台会用一条新消息说结论。
            logger.error(
                "channel card action could not post its intermediate card: %s",
                click.channel,
                exc_info=True,
            )
        self._spawn(
            self._decide(
                click=click,
                who=who,
                conversation_id=conversation_id,
                send_fn=send_fn,
                outbox=outbox,
            )
        )
        return card

    async def wait_for_background(self) -> None:
        """等到所有卡片回调的后台协程结束（测试与优雅退出用）。"""

        while self._tasks:
            pending = list(self._tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            for task in pending:
                self._tasks.discard(task)

    # --- 卡片回调的后台协程 -------------------------------------------------------------

    async def _decide(
        self,
        *,
        click: CardActionClick,
        who: Mapping[str, str],
        conversation_id: str,
        send_fn: Callable[..., Awaitable[Any]],
        outbox: _Outbox,
    ) -> None:
        """后台执行一次点击：授信与记账全在 execute_pending_action_decision 里。

        验证码恒为 None：飞书按钮的回传值带不回验证码，需要验证码的卡片只给签名短链，用户
        在银行自己的页面上输入。所以这里既没有地方读它，也没有任何把它写进日志的路径。

        `outbox` 是同步那张中间态卡的那条消息（见 handle_card_action）：结论也走它的**卡片
        更新**，一次点击在对话里只留一条消息，而不是在"正在核验并执行…"旁边另起一条纯文本。
        而且那张已经是卡片消息了，更新它也只能用卡片——把 interactive 换成 text 属于换消息
        类型，飞书的更新接口不吃。
        """

        # 这张卡已经被点了（确认或取消）：它不再等手机页面的结论，别再往它上面写东西。
        forget_code_card(conversation_id, click.pending_id)
        try:
            result = await execute_pending_action_decision(
                rt=self._rt,
                who=dict(who),
                conversation_id=conversation_id,
                pending_id=click.pending_id,
                question_id=click.question_id,
                decision=click.decision,
                verification_code=None,
                idempotency_key=self._click_key(click),
            )
            if result.status == "receipt":
                # 幂等键已经花掉：这次点击不产生第二次图调用，结论落在同一张卡上。
                await outbox.post(self._text_card(REPLAY_TEXT, CardStatus.CANCELLED), final=True)
                return
            await self._relay_stream(
                who=who,
                plan=result.outcome,
                held=result.held_lock,
                naming=result.naming,
                send_fn=send_fn,
                outbox=outbox,
            )
        except _DeliveryFailed:
            self._forget_click(click)
            logger.error("channel card action reached no message: %s", click.channel, exc_info=True)
            await _best_effort(
                _Outbox(send_fn), self._text_card(CARD_FAILED_TEXT, CardStatus.FAILED)
            )
        except AppError as error:
            # 这张卡没办成（点击不匹配、验证码缺失、银行拒绝……）：撤掉防重键，让用户重试。
            self._forget_click(click)
            logger.warning("channel card action refused: %s code=%s", click.channel, error.code)
            await _best_effort(outbox, self._text_card(_refusal_text(error), CardStatus.FAILED))
        except Exception:
            self._forget_click(click)
            logger.exception("channel card action failed: %s", click.channel)
            await _best_effort(outbox, self._text_card(CARD_FAILED_TEXT, CardStatus.FAILED))

    # --- turns.stream 的消费 ------------------------------------------------------------

    async def _relay_stream(
        self,
        *,
        who: Mapping[str, str],
        plan: TurnPlan,
        held: HeldLock | None,
        naming: DeferredNaming | None,
        send_fn: Callable[..., Awaitable[Any]],
        outbox: _Outbox | None = None,
    ) -> None:
        """消费一次性 turn 的事件流：reply_delta 节流刷新、interrupt 换成确认卡、final 收尾。

        锁在这段外面拿到手（streaming 分支），所以释放必须在这里、而且必须在 finally 里：
        生成器抛异常、被取消、或者客户端走掉，都会走同一条路径还锁。随后才跑命名——它是一次
        额外的模型调用，不能挡住发布，更不能挡住锁（S-2.1）。

        收尾也在这里：流被取消、或者走到头却没给 final 时，用户不能永远停在"正在生成…"，所以
        补一张"已中断"的卡。它排在 finally 里，但**盖不住**正在往外传的异常——真正失败时结论
        由 handle_message / _decide 说，这里不再抢着多说一句。
        """

        queue = outbox or _Outbox(send_fn)
        throttler = self._throttler_factory()
        text = ""
        final_seen = False
        # 已经给过一张"等你确认"的卡：那是这一轮的答复，不是中断。
        asked = False
        # 流是异常结束的吗：只有取消（和"自然走完却没 final"）才补收尾卡，真正的失败留给
        # 失败结论，否则用户会先看到一张"已中断"再看到一句"没办成"。
        failure: BaseException | None = None
        try:
            stream = self._rt.turns.stream(who["user_id"], who["session_id"], plan, naming=naming)
            async for event in stream:
                if event.event == "reply_delta":
                    text += str(event.data.get("text") or "")
                    if throttler.should_update(text, False):
                        await queue.post(self._renderer.render_text_card(CARD_TITLE, text))
                        throttler.record_update(text)
                elif event.event == "interrupt":
                    card = self._interrupt_card(
                        event.data, who=who, conversation_id=plan.conversation_id
                    )
                    if card is not None:
                        await queue.post(card)
                        asked = True
                        self._track_code_card(event.data, plan.conversation_id, queue, send_fn)
                elif event.event == "final":
                    final_text = str(event.data.get("text") or text)
                    text = final_text
                    await queue.post(
                        self._renderer.render_text_card(
                            CARD_TITLE, final_text, status=CardStatus.SUCCESS
                        ),
                        final=True,
                    )
                    throttler.record_update(final_text)
                    final_seen = True
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                if (
                    not final_seen
                    and not asked
                    and (failure is None or isinstance(failure, asyncio.CancelledError))
                ):
                    await queue.post(
                        self._renderer.render_text_card(
                            CARD_TITLE, INTERRUPTED_TEXT, status=CardStatus.CANCELLED
                        ),
                        final=True,
                    )
            except Exception:
                # 收尾是尽力而为：这里的失败绝不能盖掉正在往外传的那个异常。
                logger.warning("could not post the interruption card", exc_info=True)
            finally:
                if held is not None:
                    # 每条路径都要还；HeldLock.release 幂等，重复还无害，漏还就是把会话焊死。
                    held.release()
        if naming is not None:
            await self._run_naming(naming)

    async def _run_naming(self, naming: DeferredNaming) -> None:
        """跑这一轮唯一的那次命名。命名失败不能改变业务结论。

        它是一次额外的模型调用，排在 final 与锁释放之后（S-2.1）。失败最轻的后果是"这个会话
        没名字"，而把异常抛上去的后果严重得多：用户刚收到的成功答复后面会再跟一句「这次没能
        办成」，同时去重键被撤掉——成功的那一轮被允许再跑一次图。所以这里只记一条 warning。
        """

        try:
            await naming.run()
        except Exception:
            logger.warning("deferred naming failed; the turn's answer stands", exc_info=True)

    # --- 渲染 ---------------------------------------------------------------------------

    def _interrupt_card(
        self, data: Mapping[str, Any], *, who: Mapping[str, str], conversation_id: str
    ) -> dict[str, Any] | None:
        """一个 interrupt 事件 -> 一张卡片：确认框走交互卡片，其余按文本追问渲染。"""

        question_id = data.get("question_id")
        confirmation = data.get("confirmation")
        if data.get("kind") == "confirmation" and isinstance(confirmation, Mapping):
            pending_id = confirmation.get("pending_id")
            if not isinstance(question_id, str) or not isinstance(pending_id, str):
                logger.warning(
                    "channel interrupt skipped: confirmation carries no question/pending id"
                )
                return None
            pending = {
                "kind": data.get("kind"),
                "question_id": question_id,
                "confirmation": dict(confirmation),
            }
            return self._renderer.render_confirmation_card(
                pending,
                question_id,
                pending_id,
                mfa_url=self._mfa_url(
                    who=who,
                    conversation_id=conversation_id,
                    question_id=question_id,
                    pending_id=pending_id,
                    confirmation=confirmation,
                ),
            )
        display = data.get("display")
        fields = [item for item in (data.get("fields") or []) if isinstance(item, Mapping)]
        if isinstance(display, str) and display.strip():
            return self._text_card(_question_body(display, fields), CardStatus.PENDING)
        return None

    def _track_code_card(
        self,
        data: Mapping[str, Any],
        conversation_id: str,
        queue: _Outbox,
        send_fn: Callable[..., Awaitable[Any]],
    ) -> None:
        """需要验证码的确认卡记一笔，好让手机页面那边办完之后写回同一条消息。

        只认 requires_code 且真拿到了 message_id 的那一种：其余卡片的结果由这一轮自己的 final
        收尾，记进来反而会让页面提交时把一张无关的卡改成结论。登记只在进程内（见 code_cards），
        换进程就丢——那一次降级成「卡片停在原样、页面照常给结论」。
        """

        confirmation = data.get("confirmation")
        if not isinstance(confirmation, Mapping) or not confirmation.get("requires_code"):
            return
        pending_id = confirmation.get("pending_id")
        message_id = queue.message_id
        if not isinstance(pending_id, str) or not message_id:
            return
        remember_code_card(
            conversation_id=conversation_id,
            pending_id=pending_id,
            send_fn=send_fn,
            message_id=message_id,
            renderer=self._renderer,
            title=CARD_TITLE,
        )

    def _text_card(self, text: str, status: CardStatus | str) -> dict[str, Any]:
        return self._renderer.render_text_card(CARD_TITLE, text, status=str(status))

    def _mfa_url(
        self,
        *,
        who: Mapping[str, str],
        conversation_id: str,
        question_id: str,
        pending_id: str,
        confirmation: Mapping[str, Any],
    ) -> str | None:
        """需要验证码时的那条签名短链；配置不全会返回 None（渲染器那就只留取消按钮）。

        令牌里带的是 uid/cid/pid/qid 与一个 nonce，**没有任何验证码**：验证码是用户在银行页面
        上现输的，签在这里就等于把它写进一条会出现在聊天记录里的链接。
        """

        if not confirmation.get("requires_code"):
            return None
        base = getattr(self._settings, "channel_mfa_base_url", None)
        secret = getattr(self._settings, "secret_key", None)
        if not isinstance(base, str) or not base.strip():
            return None
        if not isinstance(secret, str) or not secret:
            return None
        token = create_mfa_token(
            who["user_id"], conversation_id, pending_id, question_id, secret=secret
        )
        return f"{base.rstrip('/')}/api/channels/mfa?t={token}"

    # --- 绑定 ---------------------------------------------------------------------------

    async def _answer_bind_command(
        self, msg: InboundMessage, send_fn: Callable[..., Awaitable[Any]]
    ) -> bool:
        """/bind <码> 的直接核销。返回 True 表示这条消息已经被当作绑定命令处理掉了。

        回执里绝不出现用户发来的那串码：绑定码是凭据，复述一遍就等于把它留在聊天记录里。
        """

        parts = (msg.text or "").strip().split(maxsplit=1)
        if not parts or parts[0].lower() != "/bind":
            return False
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:
            await self._post_text(send_fn, UNBOUND_TEXT)
            return True
        binding = await asyncio.to_thread(
            self._binding_mgr.redeem_code, code, msg.channel, msg.sender_id, msg.sender_name
        )
        if binding is None:
            logger.warning("channel bind refused: %s identity could not redeem a code", msg.channel)
            await self._post_text(send_fn, BIND_FAILED_TEXT)
            return True
        await self._post_text(send_fn, BIND_OK_TEXT)
        return True

    def _model_ready(self) -> bool:
        """这个进程配好模型了没有；拿不到这个信息时按"配好了"处理。

        宁可放一条消息进图（真失败会落成失败卡片），也不要因为替身 / 老版本 runtime 上没有
        这个属性，就把所有消息一律拒掉。
        """

        return bool(getattr(getattr(self._rt, "agent", None), "model_configured", True))

    async def _rotate_conversation(self, msg: InboundMessage) -> bool:
        """把绑定换到一条新会话上；换不了就返回 False（调用方回落成普通拒绝话术）。"""

        binding = self._binding_for(msg)
        if binding is None:
            return False
        try:
            await asyncio.to_thread(self._binding_mgr.open_new_conversation, binding, self._rt)
        except Exception:
            logger.exception("channel conversation rotation failed: %s", msg.channel)
            return False
        logger.info("channel conversation rotated: %s identity %s", msg.channel, msg.sender_id)
        return True

    async def _open_question(
        self, who: Mapping[str, Any], conversation_id: str
    ) -> Mapping[str, Any] | None:
        """这条会话当前悬着的那个问题；读不出来就当作没有悬着。

        读检查点只是判断"这条消息算不算一次回答"。判错了还有 turns.prepare 再判一次，所以
        读失败（检查点抖动、runtime 换了实现）不该把用户这条消息变成一次失败。
        """

        thread_id = thread_id_for_conversation(conversation_id)
        try:
            hint = await self._rt.agent.recovery_hint(who["user_id"], who["session_id"], thread_id)
        except Exception:
            logger.warning(
                "channel pending question unreadable; treating the message as a new turn",
                exc_info=True,
            )
            return None
        pending = getattr(hint, "pending", None)
        return pending if isinstance(pending, Mapping) else None

    async def _answer_for(
        self,
        question: Mapping[str, Any] | None,
        conversation_id: str,
        msg: InboundMessage,
        send_fn: Callable[..., Awaitable[Any]],
    ) -> tuple[str | None, dict[str, Any] | None, bool]:
        """一条打字消息该按什么身份交给图：新消息、某个问题的答案，还是先收着。

        三条分支各有各的理由：

        * 没有悬着的问题 -> 新消息，原样交出去。
        * 悬着确认卡 -> 照常交出去，但记下「这句话在跟确认卡说话」。文字不是授权（红线），
          所以真被拒回时只把用户按回按钮上，而不是甩一句「这张卡过期了」。
        * 悬着追问 -> 这条消息**就是**答案，按 resume 信封交回去；一问多字段时先逐字段收，
          收齐再一次性交（resume 的 value 必须恰好覆盖全部字段，少一个就被打回重问一轮）。
        """

        if question is None:
            self._ask_progress.pop(conversation_id, None)
            return msg.text, None, False

        if question.get("confirmation"):
            # 提前 return 会把幂等回执挡在后面：同一条重投回来的消息本该得到「处理过了」，
            # 却会被判成又答了一次确认卡。所以照常交出去，让 submit 先判回执，再由
            # INVALID_INTERRUPT_ANSWER 把这条消息认成「在跟确认卡说文字」。
            return msg.text, None, True

        fields = [item for item in (question.get("fields") or []) if isinstance(item, Mapping)]
        names = [str(item["name"]) for item in fields if item.get("name")]
        if not names:
            # 一个连字段都没说清的问题：照旧当新消息交出去，让 runtime 自己判。
            return msg.text, None, False

        question_id = question.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            return msg.text, None, False

        if len(names) == 1:
            self._ask_progress.pop(conversation_id, None)
            answer = _option_answer(msg.text, _field_options(fields[0]))
            return (
                ANSWER_PREFIX + answer,
                {"resume": {"question_id": question_id, "value": {names[0]: answer}}},
                False,
            )

        progress = self._ask_progress.get(conversation_id)
        if (
            not isinstance(progress, dict)
            or progress.get("question_id") != question_id
            or progress.get("names") != names
        ):
            if len(self._ask_progress) >= MAX_ASK_PROGRESS:
                # 满了就整体清一遍：这些都是还没答完的临时进度，丢掉只让用户重答一个字段。
                self._ask_progress.clear()
            progress = {"question_id": question_id, "names": names, "answers": {}, "index": 0}
            self._ask_progress[conversation_id] = progress

        index = int(progress.get("index") or 0)
        if index >= len(names):
            index = 0
        progress["answers"][names[index]] = _option_answer(msg.text, _field_options(fields[index]))
        progress["index"] = index + 1

        if progress["index"] < len(names):
            # 问的是「下一个」还没答的字段；用刚答过的那个 index 会让用户把同一个字段答两遍。
            asked = progress["index"]
            field = fields[asked]
            label = str(field.get("label") or names[asked])
            await self._post_text(send_fn, _ask_next_field(label, _field_options(field)))
            return None, None, False

        answers = dict(progress["answers"])
        self._ask_progress.pop(conversation_id, None)
        summary = "；".join(
            str(item.get("label") or item.get("name")) + "：" + answers.get(name, "")
            for item, name in zip(fields, names)
        )
        return (
            ANSWER_PREFIX + summary,
            {"resume": {"question_id": question_id, "value": answers}},
            False,
        )

    def _binding_for(self, msg: InboundMessage) -> Any:
        """这条消息该绑给谁：显式绑定优先，其次极简模式的默认用户，都没有就是没绑。"""

        binding = self._binding_mgr.get_binding(msg.channel, msg.sender_id)
        if binding is not None:
            return binding
        return self._binding_mgr.auto_bind_default_user(msg.channel, msg.sender_id, msg.sender_name)

    # --- 防重与配置 ---------------------------------------------------------------------

    def _is_historical(self, create_time_ms: int) -> bool:
        """启动水位判定；时间戳单位可疑（10 位数秒值）时不丢消息，只记一条警告。

        is_historical 对"看起来像秒"的输入会抛 ValueError——那是给调用方炸响的，但这里是
        飞书回调的落点，炸响等于整条消息凭空消失。宁可多答一轮（后面还有防重与幂等两道闸），
        也不要让用户发完话没有任何回应。
        """

        try:
            return bool(self._dedup.is_historical(create_time_ms))
        except ValueError:
            logger.warning(
                "channel event carried a create_time that is not epoch milliseconds; "
                "treating it as fresh rather than dropping the message"
            )
            return False

    def _message_key(self, msg: InboundMessage) -> str:
        """这条消息的幂等键：由 message_id 派生，所以重投过来还是同一把。"""

        return f"{msg.channel}:message:{msg.message_id}"

    def _click_key(self, click: CardActionClick) -> str:
        """这次点击的幂等键：按事件 id 派生；没有事件 id 时**绝不**退回 action_id。

        不用 question_id + decision：那是**防重**的键，回答的是"这张卡的这一个决定是不是刚
        点过"；幂等键回答的是"这次提交是不是同一次提交"，两者不能互相顶替——点另一张卡是另
        一件事，而同一个事件被飞书重投是真的同一次提交。

        也不能退回 action_id：同一张卡上按钮的回传 value 是固定的，action_id 会撞在一起，两
        次真实点击就成了"同一次提交"——第二次被幂等闸判成重投，连中间态卡都不给。事件 id 缺
        失是 dispatcher 的错（Task 8 必须填），这里退化成一次一个新键：宁可让重投多问一次
        后面的授信状态机（它会拒绝过期点击），也不要让两次点击共用一把键。
        """

        if click.event_id:
            return f"{click.channel}:click:{click.event_id}"
        logger.warning(
            "channel card action carried no event id; using a one-off click key "
            "(the dispatcher must fill event_id)"
        )
        return f"{click.channel}:click:nonce:{uuid4().hex}"

    def _forget_message(self, msg: InboundMessage) -> None:
        self._dedup.forget_message(msg)

    def _forget_click(self, click: CardActionClick) -> None:
        self._dedup.forget_click(click)

    async def _post_text(self, send_fn: Callable[..., Awaitable[Any]], text: str) -> None:
        """一句纯文本回执（引导、绑定结果、错误说明）。卡片才走 Mapping，文本走字符串。

        这类回执没有"本轮失败"可落（不撤键、不重跑），所以发不出去只记一条 error：它也不该
        以异常的形式从飞书回调里冒出来，那种异常没人接。
        """

        await _best_effort(_Outbox(send_fn), text, final=True)

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        """把后台协程挂到事件循环上，并留一份引用（否则它可能被垃圾回收掉）。"""

        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
