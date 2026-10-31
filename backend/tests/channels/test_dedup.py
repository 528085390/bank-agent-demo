"""渠道契约、启动水位线与防重缓存的单元测试。

纯单元测试：不碰数据库，也不用 rt / session / provider 这些 fixture（R2）——被钉住的
行为全是内存里的算术与字典，如果非要造库才能测，那说明实现里掺进了不该有的依赖。

水位线的毫秒/秒换算是这一层最容易犯又最难发现的错：create_time 是 epoch **毫秒**，
boot_time 是 epoch **秒**，少乘一次 1000 就会把每条新消息都判成历史，机器人从此一声
不吭。所以这里一律用真实量级的 epoch（2023-11-14T22:13:20Z），而不是 1000.0 那种玩具
纪元：实现会把 1973 年之前的毫秒值直接判成「传成秒了」，玩具纪元跑不过这条防线，也就
测不出这条防线到底拦住了什么。

键的拼法全部由 dedup 自己给（`action_key` / `message_keys`），调用方不给键——这是
Task 7（engine）不写出「per-click 随机键 → 永远不命中 → 重复出账」的唯一保证。
"""

from __future__ import annotations

import json
import threading
from dataclasses import FrozenInstanceError, fields

import pytest

from app.channels import dedup as dedup_module
from app.channels.contracts import CardActionClick, InboundMessage
from app.channels.dedup import ChannelDedup

#: 1_700_000_000 s = 2023-11-14T22:13:20Z，与飞书事件的量级一致。
BOOT_SEC = 1_700_000_000.0
BOOT_MS = 1_700_000_000_000


class FakeClock:
    """可手拨的单调时钟：TTL 用例得能在两步之间「跳过」60 秒，而不是真等 60 秒。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def inbound(**overrides) -> InboundMessage:
    """一条默认合法的私聊消息；用例只覆盖自己关心的字段。"""

    values: dict = {
        "channel": "feishu",
        "sender_id": "ou_123",
        "sender_name": "张三",
        "message_id": "om_1",
        "event_id": "ev_1",
        "text": "查下我的活期余额",
        "create_time_ms": BOOT_MS,
    }
    values.update(overrides)
    return InboundMessage(**values)


def card_click(**overrides) -> CardActionClick:
    """一次默认合法的卡片按钮点击（确认）。"""

    values: dict = {
        "channel": "feishu",
        "sender_id": "ou_123",
        "action_id": "act_1",
        "question_id": "q_1",
        "pending_id": "p_1",
        "decision": "confirm",
        "raw_value": {"q": "q_1", "p": "p_1", "d": "confirm"},
    }
    values.update(overrides)
    return CardActionClick(**values)


# --- 启动水位线 ------------------------------------------------------------------------


def test_watermark_discards_historical():
    """启动水位：早于「boot_time − 容差」的事件判为历史并丢弃，晚于水位的照常放行。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    # 比启动时刻早 15 s，落在 5 s 容差之外，是离线堆积。
    assert dedup.is_historical(BOOT_MS - 15_000) is True
    # 比启动时刻晚 5 s，是新消息。
    assert dedup.is_historical(BOOT_MS + 5_000) is False


def test_the_watermark_boundary_treats_the_equality_as_fresh():
    """正好等于「水位 − 容差」的事件**不算**历史。

    理由：规格要求「早于水位线才丢弃」，等号落在容差窗口内，不是「早于」。而且两侧代价
    不对称——误丢一条真消息，用户发完话得不到任何回应（业务事故）；误放一条略旧的消息，
    最多多答一轮，况且它紧接着还要过一遍消息去重。所以边界取闭区间：保消息。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    # 恰好是 boot_time − 5 s。
    assert dedup.is_historical(BOOT_MS - 5_000) is False
    # 少 1 毫秒就掉到窗口外。
    assert dedup.is_historical(BOOT_MS - 5_001) is True


def test_a_per_call_tolerance_overrides_the_instance_default():
    """单次调用可以收紧容差（例如补跑历史事件时按 0 容差判），不影响实例默认值。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    assert dedup.is_historical(BOOT_MS - 2_000, tolerance_sec=0.0) is True
    assert dedup.is_historical(BOOT_MS - 2_000) is False
    assert dedup.is_historical(BOOT_MS - 2_000, tolerance_sec=5.0) is False


def test_a_per_call_tolerance_may_not_be_negative_either():
    """单次调用的容差也要把关：负数会把水位线推到启动之后，历史堆积反而全部放行。

    构造函数拦了这条，但 `is_historical` 的 `tolerance_sec` 参数是第二个入口——只在那里
    漏一个判断，绕过构造函数就又是一场重放风暴：重启前堆积的每一条都会当成新消息回一轮。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    with pytest.raises(ValueError) as raised:
        dedup.is_historical(BOOT_MS - 15_000, tolerance_sec=-1.0)

    assert "tolerance_sec" in str(raised.value)


def test_a_seconds_timestamp_is_rejected_as_a_unit_error():
    """传秒级时间戳进来必须当场报错，不能静默判成历史。

    10 位数的秒值比水位小一千倍，如果只比大小，它会被判成「离线堆积」，于是网关对每条
    消息都静默 ACK —— 用户发什么都得不到回应，而且日志里一个字都没有。宁可抛异常。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    with pytest.raises(ValueError) as raised:
        dedup.is_historical(1_700_000_000)

    assert "毫秒" in str(raised.value)
    assert "1700000000" in str(raised.value)


def test_the_unit_check_stops_at_the_1973_boundary():
    """100_000_000_000 ms（1973-03）是最后一个「不可能是真时间」的量级。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    with pytest.raises(ValueError):
        dedup.is_historical(100_000_000_000 - 1)

    # 再往上一毫秒就合法了，只是它实在太旧，老老实实算历史。
    assert dedup.is_historical(100_000_000_000) is True


def test_a_missing_timestamp_is_not_historical():
    """平台没给时间（0 或负数）时放行：宁可多回一条，也不要静默丢消息。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    assert dedup.is_historical(0) is False
    assert dedup.is_historical(-1) is False


# --- 按键防重（底层接口） --------------------------------------------------------------


def test_message_and_action_dedup():
    """同一 key 第二次出现返回 True，且消息域与卡片域各记各的。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message("msg_1") is False
    assert dedup.is_duplicate_message("msg_1") is True
    assert dedup.is_duplicate_action("act_1") is False
    assert dedup.is_duplicate_action("act_1") is True


def test_a_dedup_entry_expires_after_its_ttl():
    """TTL 到期后同一个 key 重新算「第一次见」：飞书重试窗口过了就该允许再入队。"""

    clock = FakeClock()
    dedup = ChannelDedup(boot_time=BOOT_SEC, message_ttl_sec=60.0, clock=clock)

    assert dedup.is_duplicate_message("m1") is False

    clock.advance(59.9)
    assert dedup.is_duplicate_message("m1") is True

    # 累计 60.1 s，越过 60 s 防重窗口。
    clock.advance(0.2)
    assert dedup.is_duplicate_message("m1") is False


def test_expiry_boundary_is_half_open_at_exactly_the_ttl():
    """窗口是 [插入时刻, 插入时刻 + ttl)：正好踩在 ttl 上已经过期，不用再多等一纳秒。"""

    clock = FakeClock()
    dedup = ChannelDedup(boot_time=BOOT_SEC, action_ttl_sec=30.0, clock=clock)

    assert dedup.is_duplicate_action("a1") is False

    clock.advance(29.999)
    assert dedup.is_duplicate_action("a1") is True

    clock.advance(0.001)
    assert dedup.is_duplicate_action("a1") is False


def test_the_two_caches_expire_on_their_own_schedules():
    """默认 TTL 各按规格：消息 60 s，卡片按钮 30 s——狂击窗口比重投窗口短。"""

    clock = FakeClock()
    dedup = ChannelDedup(boot_time=BOOT_SEC, clock=clock)

    assert dedup.is_duplicate_message("same-key") is False
    assert dedup.is_duplicate_action("same-key") is False

    clock.advance(30.0)
    # 卡片域已经放行，消息域还在窗口里。
    assert dedup.is_duplicate_action("same-key") is False
    assert dedup.is_duplicate_message("same-key") is True

    clock.advance(30.0)
    assert dedup.is_duplicate_message("same-key") is False


def test_the_capacity_limit_evicts_the_oldest_key():
    """容量上限：常驻进程里无界增长就是事故，满仓时先扔最久没被命中的那个。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC, max_entries=2)

    assert dedup.is_duplicate_message("m1") is False
    assert dedup.is_duplicate_message("m2") is False
    # 第三个键挤掉最旧的一个（m1）。
    assert dedup.is_duplicate_message("m3") is False

    assert dedup.is_duplicate_message("m2") is True
    assert dedup.is_duplicate_message("m3") is True
    # 被挤掉的 m1 重新按第一次出现处理。
    assert dedup.is_duplicate_message("m1") is False


def test_a_hit_refreshes_recency_so_the_hot_key_survives_eviction():
    """命中会刷新「最近使用」，反复重投的热键不会被新键挤掉。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC, max_entries=2)

    assert dedup.is_duplicate_message("hot") is False
    assert dedup.is_duplicate_message("cold") is False
    assert dedup.is_duplicate_message("hot") is True  # 命中后 hot 排到最新
    assert dedup.is_duplicate_message("new") is False  # 挤掉 cold 而不是 hot

    assert dedup.is_duplicate_message("hot") is True
    assert dedup.is_duplicate_message("cold") is False


def test_message_and_action_keys_do_not_share_a_window():
    """两个域用同一把 key 也互不影响：消息重投与按钮狂击是两种危害，不能互相抵消。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message("shared") is False
    assert dedup.is_duplicate_action("shared") is False
    assert dedup.is_duplicate_message("shared") is True
    assert dedup.is_duplicate_action("shared") is True


def test_repeated_calls_are_idempotent():
    """只有第一次是 False；水位判定是无副作用的纯查询，问多少遍答案都一样。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=5.0)

    assert dedup.is_duplicate_message("m1") is False
    assert [dedup.is_duplicate_message("m1") for _ in range(3)] == [True, True, True]
    assert dedup.is_duplicate_action("a1") is False
    assert [dedup.is_duplicate_action("a1") for _ in range(3)] == [True, True, True]

    assert {dedup.is_historical(BOOT_MS - 15_000) for _ in range(3)} == {True}
    assert {dedup.is_historical(BOOT_MS + 5_000) for _ in range(3)} == {False}


# --- 构造函数参数校验 ------------------------------------------------------------------


def test_the_defaults_are_the_documented_constants():
    """不传任何可选参数时，窗口就是模块常量：消息 60 s、按钮 30 s、上限 2000、容差 5 s。

    这几个数直接决定「狂击 30 秒防重」和「重投 60 秒窗口」是否成立，所以钉在常量上，
    而不是钉在某次实现里顺手写下的字面量上。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.message_ttl_sec == dedup_module.DEFAULT_MESSAGE_TTL_SEC == 60.0
    assert dedup.action_ttl_sec == dedup_module.DEFAULT_ACTION_TTL_SEC == 30.0
    assert dedup.max_entries == dedup_module.DEFAULT_MAX_ENTRIES == 2000
    assert dedup.tolerance_sec == dedup_module.DEFAULT_TOLERANCE_SEC == 5.0


def test_the_default_windows_actually_beat_for_that_long():
    """默认参数不只是数字好看：59.9 s 还算重复，60.1 s 就放行。"""

    clock = FakeClock()
    dedup = ChannelDedup(boot_time=BOOT_SEC, clock=clock)

    assert dedup.is_duplicate_message_obj(inbound()) is False
    clock.advance(59.9)
    assert dedup.is_duplicate_message_obj(inbound()) is True
    clock.advance(0.2)
    assert dedup.is_duplicate_message_obj(inbound()) is False


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"message_ttl_sec": 0}, "message_ttl_sec"),
        ({"message_ttl_sec": -60.0}, "message_ttl_sec"),
        ({"action_ttl_sec": 0}, "action_ttl_sec"),
        ({"action_ttl_sec": -30.0}, "action_ttl_sec"),
        ({"max_entries": 0}, "max_entries"),
        ({"max_entries": -1}, "max_entries"),
    ],
)
def test_a_window_setting_that_disables_dedup_is_refused(kwargs, fragment):
    """ttl=0 / 容量=0 会静默关掉防重——这种「配错了但看起来正常」必须当场炸掉。"""

    with pytest.raises(ValueError) as raised:
        ChannelDedup(boot_time=BOOT_SEC, **kwargs)

    assert fragment in str(raised.value)


def test_a_negative_tolerance_is_refused_but_zero_is_allowed():
    """容差 -1 会把水位线推到启动之后（等于「全都算新消息」），0 则是合法的最严格判定。"""

    with pytest.raises(ValueError) as raised:
        ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=-1.0)

    assert "tolerance_sec" in str(raised.value)
    assert ChannelDedup(boot_time=BOOT_SEC, tolerance_sec=0.0).tolerance_sec == 0.0


# --- 消息防重（双键，event_id + message_id） -------------------------------------------


def test_message_keys_carry_both_identifiers():
    """两个键一起给：重投时 event_id 会变、message_id 不变，反过来也会发生。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.message_keys(inbound(event_id="ev_1", message_id="om_1")) == (
        "feishu:event:ev_1",
        "feishu:message:om_1",
    )


def test_a_redelivery_with_a_new_event_id_is_caught_by_the_message_key():
    """飞书重投换了 event_id：message 键还认得这条消息。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message_obj(inbound(event_id="ev_1", message_id="om_1")) is False
    assert dedup.is_duplicate_message_obj(inbound(event_id="ev_2", message_id="om_1")) is True


def test_a_redelivery_with_a_new_message_id_is_caught_by_the_event_key():
    """同一个事件带出两条不同 message_id：event 键挡住第二次，不能当两条消息处理。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message_obj(inbound(event_id="ev_1", message_id="om_1")) is False
    assert dedup.is_duplicate_message_obj(inbound(event_id="ev_1", message_id="om_2")) is True


def test_two_genuinely_different_messages_both_pass():
    """两个键都没见过就是新消息：防重不能把正常对话也吃掉。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message_obj(inbound(event_id="ev_1", message_id="om_1")) is False
    assert dedup.is_duplicate_message_obj(inbound(event_id="ev_2", message_id="om_2")) is False
    assert dedup.is_duplicate_message_obj(inbound(event_id="ev_1", message_id="om_1")) is True


def test_the_same_message_id_in_another_channel_is_a_different_message():
    """键里带 channel：第二个渠道接进来时，id 撞车不能互相吞消息。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message_obj(inbound(message_id="om_1", event_id="ev_1")) is False
    assert (
        dedup.is_duplicate_message_obj(inbound(channel="wecom", message_id="om_1", event_id="ev_1"))
        is False
    )


# --- 卡片按钮防重（键由 dedup 生成，pending_id 故意不进键） -----------------------------


def test_action_key_is_built_from_the_question_and_the_decision():
    """键格式就是规格 §3.2 的 {channel}:action:{question_id}:{decision}，由本类拼。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert (
        dedup.action_key(card_click(question_id="q_42", decision="confirm"))
        == "feishu:action:q_42:confirm"
    )
    assert (
        dedup.action_key(card_click(question_id="q_42", decision="cancel"))
        == "feishu:action:q_42:cancel"
    )


def test_resending_the_card_does_not_open_a_second_click_window():
    """卡片重发换了 pending_id、也换了 action_id，用户第二次点仍必须被挡住。

    pending_id 故意不进键：它每次发卡都可能变，进了键就等于「重发一次卡片 = 防重清零」，
    而狂点 30 秒恰恰是跨卡片的——用户根本没意识到中间刷过一次卡。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    first = card_click(action_id="act_1", pending_id="p_1", question_id="q_1")
    resent = card_click(action_id="act_2", pending_id="p_2", question_id="q_1")

    assert dedup.is_duplicate_click(first) is False
    assert dedup.is_duplicate_click(resent) is True


def test_confirm_and_cancel_are_two_different_actions():
    """确认与取消是两件事，各自只允许发生一次。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_click(card_click(decision="confirm")) is False
    assert dedup.is_duplicate_click(card_click(decision="cancel")) is False
    assert dedup.is_duplicate_click(card_click(decision="confirm")) is True
    assert dedup.is_duplicate_click(card_click(decision="cancel")) is True


def test_two_different_questions_do_not_block_each_other():
    """两张不同卡片的确认互不相干：键里带 question_id。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_click(card_click(question_id="q_1")) is False
    assert dedup.is_duplicate_click(card_click(question_id="q_2")) is False


def test_a_click_in_another_channel_is_not_the_same_click():
    """键里带 channel，第二个渠道的点击不会被第一个渠道的点击吞掉。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_click(card_click()) is False
    assert dedup.is_duplicate_click(card_click(channel="wecom")) is False


def test_click_dedup_expires_after_the_action_ttl():
    """30 s 之后同一次点击可以再进来（卡片长挂时用户改了主意仍然点得动）。"""

    clock = FakeClock()
    dedup = ChannelDedup(boot_time=BOOT_SEC, clock=clock)

    assert dedup.is_duplicate_click(card_click()) is False
    clock.advance(30.0)
    assert dedup.is_duplicate_click(card_click()) is False


# --- 撤销（业务失败后让重投能重来） ----------------------------------------------------


def test_forget_message_drops_both_keys_and_lets_the_retry_through():
    """记了但没处理成功，就得撤掉——否则飞书重投被静默吞掉，用户消息永久丢失。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)
    message = inbound(event_id="ev_1", message_id="om_1")

    assert dedup.is_duplicate_message_obj(message) is False
    assert dedup.is_duplicate_message_obj(message) is True

    dedup.forget_message(message)

    # 回到「没见过」，重投能真正重跑一轮，而不是被静默 ACK。
    assert dedup.is_duplicate_message_obj(message) is False
    assert dedup.is_duplicate_message_obj(message) is True


def test_forget_message_drops_both_keys_not_just_one():
    """撤的是两把键：只按 event 键或只按 message 键去问，也都不该再命中。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)
    message = inbound(event_id="ev_1", message_id="om_1")

    assert dedup.is_duplicate_message_obj(message) is False
    dedup.forget_message(message)

    assert dedup.is_duplicate_message("feishu:event:ev_1") is False
    assert dedup.is_duplicate_message("feishu:message:om_1") is False


def test_forget_clears_the_key_in_both_windows():
    """forget 是底层的「抹掉这把键」，消息域与动作域同一把锁内一起清。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message("shared") is False
    assert dedup.is_duplicate_action("shared") is False

    dedup.forget("shared")

    assert dedup.is_duplicate_message("shared") is False
    assert dedup.is_duplicate_action("shared") is False


def test_forgetting_an_unknown_key_is_harmless():
    """撤一个从没见过的键不能抛异常，也不能顺手清掉别的键。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_message("keep_me") is False
    dedup.forget("never_seen")
    dedup.forget_message(inbound(event_id="ev_unknown", message_id="om_unknown"))

    assert dedup.is_duplicate_message("keep_me") is True


def test_forget_click_lets_the_same_click_through_again():
    """点击也是「记了但没处理成功」：撤掉之后同一次点击能重新进来，用户重试不被自己挡住。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)
    click = card_click()

    assert dedup.is_duplicate_click(click) is False
    assert dedup.is_duplicate_click(click) is True

    dedup.forget_click(click)

    assert dedup.is_duplicate_click(click) is False


def test_forget_click_undoes_the_action_key_not_one_click_id():
    """撤的是 `action_key`：卡片重发换了 pending_id / action_id 之后照样撤得掉。

    如果撤销要求调用方自己拼键，Task 7 迟早会拼错（用 action_id 或 pending_id），
    那次失败的点击就再也撤不掉——用户点一次没反应，再点还是被当成重复。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC)

    assert dedup.is_duplicate_click(card_click(action_id="act_1", pending_id="p_1")) is False

    dedup.forget_click(card_click(action_id="act_2", pending_id="p_2"))

    assert dedup.is_duplicate_click(card_click(action_id="act_3", pending_id="p_3")) is False
    assert dedup.is_duplicate_click(card_click(action_id="act_3", pending_id="p_3")) is True


def test_forget_message_clears_its_keys_in_both_domains():
    """撤的时候两个域一起清：同一把键若在动作域也记过，不能留下来继续吞重投。

    正常路径下消息键与动作键不会撞（前缀不同），但如果某处误用把消息键记进了动作域，
    「撤销」就该是撤销到底，而不是按域的写法各表一枝、留下一半。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC)
    message = inbound(event_id="ev_1", message_id="om_1")
    event_key, message_key = dedup.message_keys(message)

    assert dedup.is_duplicate_message_obj(message) is False
    assert dedup.is_duplicate_action(event_key) is False

    dedup.forget_message(message)

    assert dedup.is_duplicate_message(event_key) is False
    assert dedup.is_duplicate_action(event_key) is False
    assert dedup.is_duplicate_message(message_key) is False


def test_forget_click_clears_its_key_in_both_domains():
    """同理：`forget_click` 撤的是那个字符串键，两个域都清干净。"""

    dedup = ChannelDedup(boot_time=BOOT_SEC)
    click = card_click(question_id="q_9", decision="confirm")
    key = dedup.action_key(click)

    assert dedup.is_duplicate_click(click) is False
    assert dedup.is_duplicate_message(key) is False

    dedup.forget_click(click)

    assert dedup.is_duplicate_action(key) is False
    assert dedup.is_duplicate_message(key) is False


# --- 并发 ------------------------------------------------------------------------------


def test_concurrent_callers_see_exactly_one_first_sighting():
    """8 个线程各点 200 次同一个按钮：只能有一次「没见过」。

    飞书的回调落在 ws.Client 的工作线程里，而「先查后写」不是原子操作——两次并发狂击
    都可能在对方写入前查到「没见过」，那就是重复出账的窗口。
    """

    dedup = ChannelDedup(boot_time=BOOT_SEC)
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        seen = [dedup.is_duplicate_action("feishu:action:q_hot:confirm") for _ in range(200)]
        with lock:
            results.extend(seen)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8 * 200
    assert results.count(False) == 1


# --- 淘汰顺序 --------------------------------------------------------------------------


def test_expired_keys_are_swept_before_any_live_key_is_evicted():
    """腾位置时先把过期的清掉，最旧的真键轮不到被挤出去。"""

    clock = FakeClock()
    dedup = ChannelDedup(boot_time=BOOT_SEC, max_entries=4, message_ttl_sec=60.0, clock=clock)

    # 两个键先入队，然后过了整个窗口，它们已经过期但还占着容量。
    for key in ("stale_1", "stale_2"):
        assert dedup.is_duplicate_message(key) is False

    clock.advance(61.0)
    for key in ("live_1", "live_2", "live_3"):
        assert dedup.is_duplicate_message(key) is False

    # 窗口里此时是 2 个过期键 + 3 个活键，容量 4 —— 塞第 4 个活键时先清过期。
    assert dedup.is_duplicate_message("live_4") is False

    for key in ("live_1", "live_2", "live_3", "live_4"):
        assert dedup.is_duplicate_message(key) is True

    # 过期键按「第一次见」重新入队；容量这时才真的挤掉最旧的真键（live_1 比其余三个旧）。
    assert dedup.is_duplicate_message("stale_1") is False
    assert dedup.is_duplicate_message("stale_1") is True
    assert dedup.is_duplicate_message("live_1") is False


# --- 契约：字段顺序 --------------------------------------------------------------------


def test_the_channel_contracts_keep_the_agreed_field_order():
    """字段名与顺序是后续 Task 的接口（engine / dispatcher 都按名字构造，顺序也会进日志）。

    CardActionClick 末尾的 event_id / create_time_ms 是后补的事件字段：卡片回调也是事件
    推送，没有这两个字段 Task 7 既没法对它做水位判断，也没法按事件去重。
    """

    assert [field.name for field in fields(InboundMessage)] == [
        "channel",
        "sender_id",
        "sender_name",
        "message_id",
        "event_id",
        "text",
        "create_time_ms",
    ]
    assert [field.name for field in fields(CardActionClick)] == [
        "channel",
        "sender_id",
        "action_id",
        "question_id",
        "pending_id",
        "decision",
        "raw_value",
        "event_id",
        "create_time_ms",
    ]


def test_a_card_click_carries_the_event_fields_needed_downstream():
    """不填时退化成「只按 question_id + decision 防重」，水位数也用不上——所以默认值是空。"""

    bare = CardActionClick(
        channel="feishu",
        sender_id="ou_123",
        action_id="act_1",
        question_id="q_1",
        pending_id="p_1",
        decision="confirm",
        raw_value={},
    )

    assert bare.event_id == ""
    assert bare.create_time_ms == 0

    stamped = card_click(event_id="ev_9", create_time_ms=BOOT_MS)
    assert (stamped.event_id, stamped.create_time_ms) == ("ev_9", BOOT_MS)


def test_channel_contracts_are_immutable_and_json_friendly():
    """契约一旦产出来就不许被下游就地改；to_dict 产出能直接进日志或卡片 JSON 的 plain dict。"""

    message = inbound()

    with pytest.raises(FrozenInstanceError):
        message.text = "改一下"  # type: ignore[misc]

    assert message.to_dict() == {
        "channel": "feishu",
        "sender_id": "ou_123",
        "sender_name": "张三",
        "message_id": "om_1",
        "event_id": "ev_1",
        "text": "查下我的活期余额",
        "create_time_ms": BOOT_MS,
    }

    click = card_click()

    with pytest.raises(FrozenInstanceError):
        click.decision = "cancel"  # type: ignore[misc]

    assert click.to_dict()["raw_value"] == {"q": "q_1", "p": "p_1", "d": "confirm"}
    # 两份契约的字段集合互不掺和。
    assert set(message.to_dict()) != set(click.to_dict())


# --- 契约：frozen 是浅的，所以 raw_value 必须自己冻住 --------------------------------


def test_raw_value_cannot_be_mutated_through_the_dict_the_caller_passed_in():
    """构造之后改外面那个字典（含嵌套）不能影响契约——frozen 只挡字段重绑。"""

    source = {"q": "q_1", "p": "p_1", "d": "confirm", "extra": {"tags": ["a", "b"]}}
    click = card_click(raw_value=source)

    source["q"] = "被外面改了"
    source["extra"]["tags"].append("c")
    source["new_key"] = 1

    assert click.raw_value["q"] == "q_1"
    assert list(click.raw_value["extra"]["tags"]) == ["a", "b"]
    assert "new_key" not in click.raw_value


def test_raw_value_is_read_only_after_construction():
    """拿到契约之后就地改 raw_value 必须失败，而不是悄悄改掉「当时飞书送来的东西」。"""

    click = card_click()

    with pytest.raises(TypeError):
        click.raw_value["q"] = "改一下"  # type: ignore[index]

    assert click.raw_value["q"] == "q_1"


def test_to_dict_returns_a_copy_that_cannot_pollute_the_contract():
    """to_dict 的返回值是给日志和回放用的：调用方随便改，原对象一个字节都不变。"""

    click = card_click(raw_value={"q": "q_1", "nested": {"tags": ["a"]}})

    first = click.to_dict()
    first["raw_value"]["q"] = "改掉了"
    first["raw_value"]["nested"]["tags"].append("b")

    assert click.to_dict()["raw_value"] == {"q": "q_1", "nested": {"tags": ["a"]}}
    # 每次都是新对象：两次 to_dict 不共享同一份可变结构。
    assert click.to_dict() is not click.to_dict()
    assert click.to_dict()["raw_value"] is not click.to_dict()["raw_value"]

    message = inbound()
    snapshot = message.to_dict()
    snapshot["text"] = "改掉了"
    assert message.to_dict()["text"] == "查下我的活期余额"


def test_to_dict_output_is_json_serialisable():
    """日志与离线探针直接 json.dumps(to_dict())，冻结之后这条也得照样成立。"""

    click = card_click(raw_value={"q": "q_1", "nested": {"tags": ["a", "b"]}})

    assert json.loads(json.dumps(click.to_dict()))["raw_value"] == {
        "q": "q_1",
        "nested": {"tags": ["a", "b"]},
    }
