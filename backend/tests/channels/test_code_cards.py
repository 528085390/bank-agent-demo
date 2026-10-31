"""确认卡的回写：手机页面那边办完之后，聊天里那条卡片要跟着变。

这是「两边各说各话」造成的不一致：卡片由渠道引擎发、验证码由手机页面收，它们唯一的交点是
(conversation_id, pending_id)。不写回的话，用户手机上永远停在「等待您的确认」，而钱其实已经
出去了。这里钉住四件事：命中就原地更新同一条、只写回一次、没登记就一个字都不发、登记表有上限。
"""

from __future__ import annotations

import json

import pytest

from app.channels.code_cards import (
    MAX_CODE_CARDS,
    clear_code_cards,
    forget_code_card,
    remember_code_card,
    report_code_outcome,
    tracked_code_cards,
)
from app.channels.render.feishu import FeishuCardRenderer

TITLE = "银行智能助手"
CONVERSATION = "conv_code_card_1"
PENDING = "tp_code_card_1"


class Channel:
    """替身通道：记下发了什么、更新的是哪一条；`fail=True` 时像飞书抖了一样抛。"""

    def __init__(self) -> None:
        self.payloads: list = []
        self.updates: list[str] = []
        self.fail = False

    async def __call__(self, payload, *, update_message_id=None):
        if self.fail:
            raise RuntimeError("feishu is down")
        self.payloads.append(payload)
        if update_message_id is not None:
            self.updates.append(update_message_id)
        return "om_" + str(len(self.payloads))

    def dumped(self) -> str:
        return json.dumps(self.payloads[-1], ensure_ascii=False)


@pytest.fixture(autouse=True)
def clean_registry():
    """登记表是进程级单例：每个用例前后都清一遍，免得用例之间互相看见。"""

    clear_code_cards()
    yield
    clear_code_cards()


def remember(channel: Channel, *, message_id: str = "om_confirm", pending_id: str = PENDING) -> None:
    assert remember_code_card(
        conversation_id=CONVERSATION,
        pending_id=pending_id,
        send_fn=channel,
        message_id=message_id,
        renderer=FeishuCardRenderer(),
        title=TITLE,
    )


async def test_the_outcome_lands_on_the_same_message():
    channel = Channel()
    remember(channel)

    assert await report_code_outcome(CONVERSATION, PENDING, "这笔申购已经完成。") is True

    # 原地更新那条：手机页面办完，聊天里那张「等待您的确认」变成结论卡，而不是再发一条。
    assert channel.updates == ["om_confirm"]
    assert "这笔申购已经完成。" in channel.dumped()
    assert "已完成" in channel.dumped()
    assert "正在生成" not in channel.dumped()
    assert tracked_code_cards() == 0


async def test_a_failed_outcome_says_so():
    channel = Channel()
    remember(channel)

    assert await report_code_outcome(CONVERSATION, PENDING, "这笔申购没有完成。", ok=False) is True

    assert "这笔申购没有完成。" in channel.dumped()
    assert "未完成" in channel.dumped()


async def test_one_card_is_written_back_once():
    channel = Channel()
    remember(channel)

    assert await report_code_outcome(CONVERSATION, PENDING, "完成。") is True
    assert await report_code_outcome(CONVERSATION, PENDING, "又完成。") is False
    assert len(channel.payloads) == 1


async def test_an_unknown_card_sends_nothing():
    channel = Channel()

    assert await report_code_outcome("conv_other", PENDING, "完成。") is False
    assert channel.payloads == []


async def test_a_card_that_was_already_clicked_is_forgotten():
    channel = Channel()
    remember(channel)

    assert forget_code_card(CONVERSATION, PENDING) is True
    assert await report_code_outcome(CONVERSATION, PENDING, "完成。") is False
    assert channel.payloads == []


async def test_a_failed_update_is_given_up_on():
    channel = Channel()
    remember(channel)
    channel.fail = True

    assert await report_code_outcome(CONVERSATION, PENDING, "完成。") is False
    # 失败即放弃：卡片停在原样，页面那边照常给结论，不会因为写不回把办成的事报成失败。
    assert tracked_code_cards() == 0


async def test_no_message_id_means_no_registration():
    channel = Channel()

    assert (
        remember_code_card(
            conversation_id=CONVERSATION,
            pending_id=PENDING,
            send_fn=channel,
            message_id="",
            renderer=FeishuCardRenderer(),
            title=TITLE,
        )
        is False
    )
    assert tracked_code_cards() == 0


async def test_the_oldest_card_is_evicted_when_the_registry_is_full():
    channel = Channel()
    for index in range(MAX_CODE_CARDS + 1):
        remember(channel, message_id="om_" + str(index), pending_id="tp_" + str(index))

    assert tracked_code_cards() == MAX_CODE_CARDS
    assert await report_code_outcome(CONVERSATION, "tp_0", "完成。") is False
    assert await report_code_outcome(CONVERSATION, "tp_" + str(MAX_CODE_CARDS), "完成。") is True
