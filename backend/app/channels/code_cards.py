"""需要验证码的确认卡：手机页面那边办完之后，把结论写回聊天里那一条卡片。

这条链路本来不是一条线：确认卡由渠道进程发（engine -> dispatcher -> im/v1/messages，拿到
message_id），而用户输验证码走的是另一个端点（GET/POST /api/channels/mfa）。两边互不相识，
于是页面提交完不写回卡片，用户手机上就永远停在「请确认」，而钱其实已经出去了——这是最坏的
一种不一致：他会再点一次、再提交一次，然后不知道到底扣了几次。

所以这里只做一件事：把「这一笔待确认操作对应聊天里的哪一条消息」记进一张进程内的表，页面
提交成功之后按 (conversation_id, pending_id) 找回那条消息，原地 PATCH 成结论卡。

三条边界：

1. **单次消费。** 一次提交只写回一次，取走即删：同一笔操作不会因为重复提交反复改卡。
2. **拿不到就降级。** 进程重启、卡片早被删、登记被淘汰——一律返回 False，页面那边照常给
   结论，聊天里那张卡片保持原样。宁可旧卡片不动，也不能因为「写不回」把已经办成的事报成失败。
3. **只放卡片句柄，不放业务载荷。** 表里存的是 send_fn 与 message_id，验证码一个字都不进来
   （它只出现在银行自己的核身页面上，连令牌里都没有）。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.channels.render.base import BaseRenderer, CardStatus

logger = logging.getLogger(__name__)

#: 最多记这么多张待回写的卡片。超出按先来先丢：同一个进程里同时挂着 200 张待核验的确认卡
#: 已经属于异常用法，而淘汰的代价只是「那张卡不再更新」，页面上的结论仍然是对的。
MAX_CODE_CARDS = 200

SendFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class _CodeCard:
    """一张等着被回写的确认卡：怎么发、发到哪一条、长什么样。"""

    send_fn: SendFn
    message_id: str
    renderer: BaseRenderer
    title: str


#: (conversation_id, pending_id) -> 那张卡片。只在事件循环线程里读写，所以不加锁；
#: OrderedDict 保序，淘汰时丢最老的那一条。
_CARDS: OrderedDict[tuple[str, str], _CodeCard] = OrderedDict()


def _key(conversation_id: str, pending_id: str) -> tuple[str, str]:
    return (str(conversation_id), str(pending_id))


def remember_code_card(
    *,
    conversation_id: str,
    pending_id: str,
    send_fn: SendFn,
    message_id: str,
    renderer: BaseRenderer,
    title: str,
) -> bool:
    """记下这张需要验证码的确认卡；拿不到 message_id 就不记（无从原地更新）。"""

    if not message_id:
        return False
    key = _key(conversation_id, pending_id)
    _CARDS[key] = _CodeCard(
        send_fn=send_fn, message_id=str(message_id), renderer=renderer, title=title
    )
    _CARDS.move_to_end(key)
    while len(_CARDS) > MAX_CODE_CARDS:
        _CARDS.popitem(last=False)
    return True


def forget_code_card(conversation_id: str, pending_id: str) -> bool:
    """不再等这张卡的结果了（例如用户直接在卡片上点了取消）。"""

    return _CARDS.pop(_key(conversation_id, pending_id), None) is not None


def tracked_code_cards() -> int:
    """当前还等着回写的卡片数（诊断与测试用）。"""

    return len(_CARDS)


def clear_code_cards() -> None:
    """清空整张表（测试用；进程重启本来也是空的）。"""

    _CARDS.clear()


async def report_code_outcome(
    conversation_id: str,
    pending_id: str,
    text: str,
    *,
    ok: bool = True,
) -> bool:
    """把结论写回那张确认卡：命中就原地 PATCH 成一条结论卡，未命中返回 False。

    正文来自这一轮的 final；`ok=False` 时卡片是失败样式。写不回去只记 warning——提交本身已经
    成功或被拒，这里再抛一次只会让手机页面变成 500。
    """

    card = _CARDS.pop(_key(conversation_id, pending_id), None)
    if card is None:
        logger.info("no channel card is waiting for the outcome of this action")
        return False
    status = CardStatus.SUCCESS if ok else CardStatus.FAILED
    payload = card.renderer.render_text_card(card.title, text, status=str(status))
    try:
        await card.send_fn(payload, update_message_id=card.message_id)
    except Exception:
        logger.warning("could not write the action outcome back to its card", exc_info=True)
        return False
    return True
