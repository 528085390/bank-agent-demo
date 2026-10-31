"""`ChannelBindingManager` 的契约：绑定码、核销语义、会话换发与自动绑定。

每个用例都跑在 `rt` 造出来的独立 schema 上：绑定行、登录会话与 conversation 都落在
真实迁移建出来的表里，所以这里钉住的是数据库里真正的行为，而不是替身。

时钟一律注入。绑定码 TTL 与防爆破锁定读 `clock`（单调秒），可以拨表而不真等 10 分钟；
`LoginSession` 的过期必须用墙钟判（`require_session` 就是墙钟），那一条直接造一行
`expires_at` 已经过去的记录。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

import app.channels.binding as binding_module
from app.channels.binding import (
    DEFAULT_CODE_TTL_SECONDS,
    MAX_REDEEM_FAILURES,
    REDEEM_LOCKOUT_SECONDS,
    ChannelBindingManager,
)
from app.core.config import Settings
from app.core.errors import AppError
from app.storage.models import ChannelBinding, Conversation, LoginSession
from tests.test_conversation_api import second_session

# `seed()` 在每个测试 schema 里写入的演示用户。
FIRST_USER = "user_0001"


def login_session(rt, session_id: str) -> LoginSession:
    """按主键取一条 `session` 行，顺手断言它确实落库了。"""

    with rt.database.sessions() as db:
        row = db.scalar(select(LoginSession).where(LoginSession.session_id == session_id))
    assert row is not None
    return row# 数据库里没有这个用户：外键会拒绝，接口必须自己先说清楚。
UNKNOWN_USER = "user_9999"


class Clock:
    """可拨的单调时钟：绑定码 TTL 与防爆破锁定都读它。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def manager_for(rt, *, clock: Clock | None = None, **overrides) -> ChannelBindingManager:
    """一个连着本测试 schema 的管理器；`overrides` 只覆盖 Settings 里被点名的项。"""

    settings = Settings(database_url=rt.settings.database_url, **overrides)
    return ChannelBindingManager(
        db_factory=rt.database.sessions, settings=settings, clock=clock or Clock()
    )


def wrong_codes(code: str, count: int = MAX_REDEEM_FAILURES) -> list[str]:
    """与 `code` 不同的若干个 6 位码，用来制造失败尝试。"""

    candidates = [f"{value:06d}" for value in range(1, count + 8) if f"{value:06d}" != code]
    return candidates[:count]


def binding_count(rt) -> int:
    with rt.database.sessions() as db:
        return int(db.scalar(select(func.count()).select_from(ChannelBinding)) or 0)


def conversation_count(rt, user_id: str) -> int:
    with rt.database.sessions() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.user_id == user_id)
            )
            or 0
        )


def stored_binding(rt, platform_user_id: str) -> ChannelBinding:
    with rt.database.sessions() as db:
        row = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == platform_user_id)
        )
    assert row is not None
    return row


def wait_for_blocked_insert(rt, timeout: float = 10.0) -> bool:
    """等到另一个连接确实卡在 `channel_binding` 的唯一索引上。

    这条用例要的不是「大概会撞」，而是「一定撞」：输家的 INSERT 已经排到唯一键上，主线程
    才提交赢家那一行。判据取自 Postgres 自己（pg_stat_activity 里等锁的 INSERT），比
    sleep 一个拍脑袋的时长可靠。
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with rt.database.sessions() as db:
            waiting = db.scalar(
                text(
                    "select count(*) from pg_stat_activity "
                    "where state = 'active' and wait_event_type = 'Lock' "
                    "and query like 'INSERT INTO channel_binding%'"
                )
            )
        if waiting:
            return True
        time.sleep(0.01)
    return False


# --- 绑定码：格式、覆盖、过期 ---------------------------------------------------------


def test_a_generated_code_is_six_digits_and_never_the_placeholder(rt):
    """6 位数字；两个用户各拿一个且互不相同；全零不参与发放（它是用户手输的占位串）。"""

    second = second_session(rt)
    mgr = manager_for(rt)

    first_code = mgr.generate_binding_code(FIRST_USER)
    other_code = mgr.generate_binding_code(second["user_id"])

    for code in (first_code, other_code):
        assert len(code) == 6
        assert code.isascii() and code.isdigit()
        assert code != "000000"
    assert first_code != other_code


def test_generating_a_code_for_an_unknown_user_is_refused(rt):
    """给不存在的用户发码，等于发一张核销时必然撞外键的票：当场拒绝。"""

    with pytest.raises(AppError) as raised:
        manager_for(rt).generate_binding_code(UNKNOWN_USER)

    assert raised.value.code == "NOT_FOUND"


def test_regenerating_replaces_the_earlier_code_for_the_same_user(rt):
    """同一用户重复生成覆盖旧码：旧码立刻作废，新码照常可用。"""

    mgr = manager_for(rt)
    first = mgr.generate_binding_code(FIRST_USER)
    second = first
    for _ in range(10):  # 抽到同一个数字的概率极低，但别让用例靠概率
        second = mgr.generate_binding_code(FIRST_USER)
        if second != first:
            break
    assert second != first

    assert mgr.redeem_code(first, "feishu", "ou_stale") is None
    assert mgr.get_binding("feishu", "ou_stale") is None

    binding = mgr.redeem_code(second, "feishu", "ou_stale", "第二次生成的码")
    assert binding is not None
    assert binding.user_id == FIRST_USER


def test_a_wrong_code_binds_nothing(rt):
    """猜错的码不落任何记录，也不影响主人手里那个真码。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)

    assert mgr.redeem_code(wrong_codes(code)[0], "feishu", "ou_wrong") is None
    assert binding_count(rt) == 0

    bound = mgr.redeem_code(code, "feishu", "ou_right")
    assert bound is not None
    assert bound.user_id == FIRST_USER


def test_malformed_input_is_ignored_and_does_not_spend_attempts(rt):
    """形状不对的输入不算一次尝试：否则谁都能把某个身份锁死。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)

    for junk in ("", "   ", "12345", "1234567", "abcdef", "12 456"):
        assert mgr.redeem_code(junk, "feishu", "ou_junk") is None
    assert mgr.get_binding("feishu", "ou_junk") is None

    # 六个畸形输入之后仍未达阈值：真码照样核销得掉。
    assert mgr.redeem_code(code, "feishu", "ou_junk") is not None


def test_a_non_string_code_is_refused_rather_than_crashing(rt):
    """畸形输入包含类型畸形：`/bind 123456` 若被解析成 int，这里必须返回 None 而不是炸。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)

    for junk in (123456, None, b"123456", 12.5, ["123456"]):
        assert mgr.redeem_code(junk, "feishu", "ou_not_a_string") is None
    assert mgr.get_binding("feishu", "ou_not_a_string") is None
    assert binding_count(rt) == 0

    # 类型畸形同样不算一次失败：真码照样核销得掉。
    assert mgr.redeem_code(code, "feishu", "ou_not_a_string") is not None


def test_the_code_may_arrive_with_surrounding_whitespace(rt):
    """用户在飞书里打的是 `/bind 123456`，解析层剥壳时留白不该让人白跑一趟。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)

    padded = mgr.redeem_code(f"  {code}  ", "feishu", "ou_padded")
    assert padded is not None
    assert padded.user_id == FIRST_USER


def test_a_code_expires_after_its_ttl(rt):
    """码在 10 分钟之后就只是一串数字：不核销、不落库。"""

    clock = Clock()
    mgr = manager_for(rt, clock=clock)
    code = mgr.generate_binding_code(FIRST_USER)

    clock.advance(DEFAULT_CODE_TTL_SECONDS + 1)

    assert mgr.redeem_code(code, "feishu", "ou_late") is None
    assert mgr.get_binding("feishu", "ou_late") is None


def test_a_zero_ttl_code_is_dead_on_arrival(rt):
    """`ttl_seconds=0` 的码出生即过期——它让过期路径不必真等十分钟。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER, ttl_seconds=0)

    assert mgr.redeem_code(code, "feishu", "ou_now") is None
    assert mgr.get_binding("feishu", "ou_now") is None


def test_a_redeemed_code_cannot_be_used_again(rt):
    """核销即失效：同一个码换一个平台身份再核销一次，什么都不会发生。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)

    assert mgr.redeem_code(code, "feishu", "ou_first", "第一个身份") is not None
    assert mgr.redeem_code(code, "feishu", "ou_second", "第二个身份") is None
    assert mgr.get_binding("feishu", "ou_second") is None


# --- 核销语义：同一枚码只能核销一次（并发） --------------------------------------------


def test_a_code_can_only_be_redeemed_once_under_concurrency(rt):
    """一枚一次性码被两个线程、两个身份同时核销：恰好一个成功，库里恰好一行。

    这正是「查码」与「删码」之间那段锁外事务留下的窗口：两个都通过查询、都落了库，一枚
    码就绑了两个身份。占用必须在锁内完成，落库被拒才把码放回去。
    """

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)
    start = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    identities = ("ou_race_a", "ou_race_b")

    def redeem(platform_user_id: str) -> None:
        start.wait(timeout=10)
        try:
            outcomes[platform_user_id] = mgr.redeem_code(
                code, "feishu", platform_user_id, platform_user_id
            )
        except BaseException as exc:  # 线程里的异常要回到断言里，不能只打在 stderr
            outcomes[platform_user_id] = exc

    threads = [threading.Thread(target=redeem, args=(identity,)) for identity in identities]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == 2, outcomes
    for outcome in outcomes.values():
        assert not isinstance(outcome, BaseException), outcome
    winners = [identity for identity, outcome in outcomes.items() if outcome is not None]
    assert len(winners) == 1, outcomes
    winner = outcomes[winners[0]]
    assert winner.user_id == FIRST_USER
    assert stored_binding(rt, winners[0]).id == winner.id
    assert binding_count(rt) == 1
    for loser in (identity for identity in identities if identity not in winners):
        assert mgr.get_binding("feishu", loser) is None

    # 核销即失效：赢家用掉的那枚码没有因为输家的路径而复活。
    assert mgr.redeem_code(code, "feishu", "ou_race_third") is None
    assert binding_count(rt) == 1


def test_a_refused_redeem_hands_the_code_back_under_concurrency(rt):
    """并发下被拒的那一侧不烧码：身份被对方先占了，输家还能拿自己的码绑另一个身份。"""

    second = second_session(rt)
    mgr = manager_for(rt)
    codes = {
        FIRST_USER: mgr.generate_binding_code(FIRST_USER),
        second["user_id"]: mgr.generate_binding_code(second["user_id"]),
    }
    start = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def grab(user_id: str) -> None:
        start.wait(timeout=10)
        try:
            outcomes[user_id] = mgr.redeem_code(
                codes[user_id], "feishu", "ou_contested", "抢同一个身份"
            )
        except BaseException as exc:
            outcomes[user_id] = exc

    threads = [threading.Thread(target=grab, args=(user_id,)) for user_id in codes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == 2, outcomes
    winner_id = next(user_id for user_id, outcome in outcomes.items() if outcome is not None)
    loser_id = next(user_id for user_id in codes if user_id != winner_id)
    assert stored_binding(rt, "ou_contested").user_id == winner_id
    assert binding_count(rt) == 1

    # 输家被拒的是「抢别人的身份」，不是他的码：换个没人占的身份照样绑得上。
    rebound = mgr.redeem_code(codes[loser_id], "feishu", "ou_loser_own", "被拒者的另一个身份")
    assert rebound is not None
    assert rebound.user_id == loser_id


# --- 核销语义：同一身份、不同用户 ------------------------------------------------------


def test_redeeming_the_same_identity_again_refreshes_the_same_row(rt):
    """同一个用户重绑同一个平台身份是刷新（改平台昵称），不是插一行新的。"""

    mgr = manager_for(rt)
    first = mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_same", "旧名字")
    assert first is not None

    again = mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_same", "新名字")

    assert again is not None
    assert again.id == first.id
    assert again.user_id == FIRST_USER
    assert again.platform_username == "新名字"
    assert binding_count(rt) == 1


def test_another_users_code_cannot_take_a_bound_identity(rt):
    """被抢绑的语义是**拒绝**，不是覆盖：第二个用户拿不到别人的平台身份，且他的码不被烧掉。"""

    mgr = manager_for(rt)
    owner = mgr.redeem_code(
        mgr.generate_binding_code(FIRST_USER), "feishu", "ou_shared", "先到的人"
    )
    assert owner is not None

    second = second_session(rt)
    other_code = mgr.generate_binding_code(second["user_id"])

    assert mgr.redeem_code(other_code, "feishu", "ou_shared", "后来的人") is None
    kept = mgr.get_binding("feishu", "ou_shared")
    assert kept is not None
    assert kept.user_id == FIRST_USER

    # 拒绝不是核销：第二个用户的码还在，他仍能绑自己的另一个平台身份。
    second_binding = mgr.redeem_code(other_code, "feishu", "ou_second_identity", "后来的人")
    assert second_binding is not None
    assert second_binding.user_id == second["user_id"]


def test_get_binding_reads_one_row_or_none(rt):
    """`get_binding` 是"这个人绑了吗"的唯一读口：(渠道, 平台身份) 才是一个键。"""

    mgr = manager_for(rt)
    assert mgr.get_binding("feishu", "ou_nobody") is None

    mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_here", "有名字")

    found = mgr.get_binding("feishu", "ou_here")
    assert found is not None
    assert found.user_id == FIRST_USER
    assert mgr.get_binding("another_channel", "ou_here") is None


def test_redeeming_again_bumps_updated_at_even_when_the_name_is_unchanged(rt):
    """重核销就是「这一行被用过」：昵称没变也要推进 updated_at，否则库里看不出刷新。"""

    mgr = manager_for(rt)
    first = mgr.redeem_code(
        mgr.generate_binding_code(FIRST_USER), "feishu", "ou_touch", "同一个名字"
    )
    assert first is not None
    before = stored_binding(rt, "ou_touch").updated_at

    time.sleep(0.05)  # updated_at 是微秒精度的墙钟：别让两次写落进同一个刻度
    again = mgr.redeem_code(
        mgr.generate_binding_code(FIRST_USER), "feishu", "ou_touch", "同一个名字"
    )

    assert again is not None
    assert again.id == first.id
    assert again.platform_username == "同一个名字"
    assert stored_binding(rt, "ou_touch").updated_at > before


# --- 防爆破 ---------------------------------------------------------------------------


def test_repeated_wrong_codes_lock_the_identity_out(rt):
    """同一平台身份连续猜错就到阈值：正确码也不再核销；锁定按身份隔离，到期自行解除。"""

    clock = Clock()
    mgr = manager_for(rt, clock=clock)
    code = mgr.generate_binding_code(FIRST_USER)

    for wrong in wrong_codes(code):
        assert mgr.redeem_code(wrong, "feishu", "ou_brute") is None

    # 阈值之后，连真码都进不来。
    assert mgr.redeem_code(code, "feishu", "ou_brute") is None
    assert mgr.get_binding("feishu", "ou_brute") is None

    # 锁是按身份下的，别人不受牵连。
    innocent = mgr.redeem_code(code, "feishu", "ou_innocent")
    assert innocent is not None
    assert innocent.user_id == FIRST_USER

    clock.advance(REDEEM_LOCKOUT_SECONDS + 1)
    fresh = mgr.generate_binding_code(FIRST_USER)
    assert mgr.redeem_code(fresh, "feishu", "ou_brute") is not None


def test_a_successful_redeem_clears_the_failure_count(rt):
    """失败计数在成功核销时清零：否则一个偶尔手滑的用户攒够次数就再也进不来。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)
    slipping = wrong_codes(code)

    for wrong in slipping[: MAX_REDEEM_FAILURES - 1]:
        assert mgr.redeem_code(wrong, "feishu", "ou_slip") is None
    assert mgr.redeem_code(code, "feishu", "ou_slip") is not None

    for wrong in slipping[: MAX_REDEEM_FAILURES - 1]:
        assert mgr.redeem_code(wrong, "feishu", "ou_slip") is None
    assert mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_slip") is not None


def test_a_full_failure_table_never_evicts_a_live_lockout(rt, monkeypatch):
    """失败表满了只许淘汰已经解锁的项：把一条仍在锁定中的记录挤掉，等于悄悄解除锁定。"""

    monkeypatch.setattr(binding_module, "MAX_TRACKED_IDENTITIES", 2)
    clock = Clock()
    mgr = manager_for(rt, clock=clock)
    code = mgr.generate_binding_code(FIRST_USER)
    slipping = wrong_codes(code)

    for identity in ("ou_locked_a", "ou_locked_b"):
        for wrong in slipping:
            assert mgr.redeem_code(wrong, "feishu", identity) is None
    assert len(mgr._attempts) == 2

    # 表满且两项都在锁定期内：这一次不计数，也不许挤掉别人的锁。
    assert mgr.redeem_code(slipping[0], "feishu", "ou_third") is None
    assert len(mgr._attempts) == 2
    assert mgr.redeem_code(code, "feishu", "ou_locked_a") is None

    # 容量满不该连合法核销一起挡掉：真码给一个干净身份照样能用。
    assert mgr.redeem_code(code, "feishu", "ou_clean") is not None

    # 锁定过期之后，淘汰才重新开始动这张表。
    clock.advance(REDEEM_LOCKOUT_SECONDS + 1)
    assert mgr.redeem_code(slipping[0], "feishu", "ou_third") is None
    assert len(mgr._attempts) <= 2


def test_a_binding_code_never_reaches_the_logs(rt, caplog):
    """绑定码是机密：生成、核销、猜错的全过程都不许在日志里留下明文。"""

    mgr = manager_for(rt)
    with caplog.at_level(logging.DEBUG):
        code = mgr.generate_binding_code(FIRST_USER)
        mgr.redeem_code(code, "feishu", "ou_logged", "会出现在日志里的人")
        for wrong in wrong_codes(code):
            mgr.redeem_code(wrong, "feishu", "ou_logged")

    assert code not in caplog.text


def test_a_binding_code_lives_in_memory_only_as_a_digest(rt):
    """日志红线之外再断一条：进程内存里也只留摘要，`_codes` 的可读视图里没有明文。"""

    mgr = manager_for(rt)
    code = mgr.generate_binding_code(FIRST_USER)

    assert code not in repr(mgr._codes)
    assert code not in repr(mgr.__dict__)
    assert code not in repr(next(iter(mgr._codes.values())))
    # 留下的是摘要：HMAC-SHA256 的十六进制，不是那六个数字。
    assert len(next(iter(mgr._codes.values())).digest) == 64


# --- 会话换发：who 必须与 identity/service.py 同一套语义 --------------------------------


def test_a_live_login_session_is_reused(rt):
    """Web 端刚登进来的会话还没过期，渠道就用它——不另造一套 session 格式。"""

    web = rt.identity.create_demo_session()
    mgr = manager_for(rt)

    who = mgr.get_or_create_channel_session(FIRST_USER)
    assert who == {"user_id": FIRST_USER, "session_id": web["session_id"]}

    assert mgr.get_or_create_channel_session(FIRST_USER) == who


def test_an_expired_login_session_is_replaced(rt):
    """几天前绑定的用户，库里那行早已过期：自动换发一条新的 24 小时会话，绝不中断业务。"""

    expired_id = "sess_expired_for_channel"
    now = datetime.now(timezone.utc)
    with rt.database.sessions.begin() as db:
        db.add(
            LoginSession(
                session_id=expired_id,
                user_id=FIRST_USER,
                created_at=now - timedelta(hours=48),
                expires_at=now - timedelta(hours=24),
            )
        )

    who = manager_for(rt).get_or_create_channel_session(FIRST_USER)

    assert who["session_id"] != expired_id
    row = login_session(rt, who["session_id"])
    assert row.user_id == FIRST_USER
    assert row.expires_at - row.created_at == timedelta(hours=rt.settings.session_hours)


def test_a_missing_login_session_is_created_with_the_demo_window(rt):
    """没有会话就开一条：形状是 `who`，寿命 24 小时，identity 与 turns 都认。"""

    who = manager_for(rt).get_or_create_channel_session(FIRST_USER)

    assert set(who) == {"user_id", "session_id"}
    assert who["user_id"] == FIRST_USER

    row = login_session(rt, who["session_id"])
    assert row.user_id == FIRST_USER
    assert row.expires_at - row.created_at == timedelta(hours=rt.settings.session_hours)
    assert row.expires_at > datetime.now(timezone.utc) + timedelta(hours=23)

    # 真实账号之后 `session` 表按 token 摘要认人（`authenticate` 就是这么查的），
    # 模型注释也写明没有 token 的老行必须保持不可用。渠道自己不用这把 token，但开出
    # 来的必须是一条**完整登录态**，不能是只有 user_id 的空壳。
    assert row.token_hash and row.csrf_hash
    assert row.absolute_expires_at == row.expires_at
    assert row.revoked_at is None


def test_a_session_for_an_unknown_user_is_refused(rt):
    """会话只能开给真实存在的人：外键会拒绝，接口先用 NOT_FOUND 说清楚。"""

    with pytest.raises(AppError) as raised:
        manager_for(rt).get_or_create_channel_session(UNKNOWN_USER)

    assert raised.value.code == "NOT_FOUND"


# --- conversation 幂等 -----------------------------------------------------------------


def test_get_or_create_conversation_is_idempotent(rt):
    """同一个绑定的两次取用返回同一个 conversation_id，且只落一条会话行。"""

    mgr = manager_for(rt)
    binding = mgr.redeem_code(
        mgr.generate_binding_code(FIRST_USER), "feishu", "ou_chat", "聊天的人"
    )
    assert binding is not None

    first = mgr.get_or_create_conversation(binding, rt)
    second = mgr.get_or_create_conversation(binding, rt)

    assert first == second
    assert conversation_count(rt, FIRST_USER) == 1
    assert stored_binding(rt, "ou_chat").conversation_id == first
    # 走的是真实的会话服务：这个 id 归绑定用户所有，线程也随之建好。
    assert rt.conversations.get(FIRST_USER, first).conversation_id == first


def test_a_binding_cannot_name_a_conversation_that_never_existed(rt):
    """绑定行指不到不存在的会话：写库时就该被外键拒掉，而不是留个死 id 下去。

    「绑定指着一条查不到的会话」原本是这条用例手工造出来的状态，加了
    `channel_binding.conversation_id -> conversation.conversation_id` 之后就造不出来了——
    这正是外键要的效果：渠道层不必靠 404 才发现自己把回答发进了一条不存在的会话。
    于是两种「会话没了」的真实路径只剩下面这些：软删 / 归档（用例紧随其后），以及会话被
    硬删（外键会先拒绝，见后一条）。引擎里的兜底分支仍然留着，那是防外键被谁去掉的护网。
    """

    mgr = manager_for(rt)
    binding = mgr.redeem_code(
        mgr.generate_binding_code(FIRST_USER), "feishu", "ou_lost", "丢会话的人"
    )
    assert binding is not None
    with pytest.raises(IntegrityError):
        with rt.database.sessions.begin() as db:
            row = db.scalar(
                select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_lost")
            )
            row.conversation_id = "conv_deleted_by_nobody"

    # 写失败之后那一行仍是原样，没有被这次失败的更新留下半截状态。
    mgr.get_or_create_conversation(binding, rt)


def test_a_conversation_named_by_a_binding_cannot_be_hard_deleted(rt):
    """被绑定指着的会话删不掉：删了就有一行绑定指着空气，而这正是外键存在的理由。

    业务上会话只软删（`Conversation.soft_delete`），所以这条约束不会挡住任何正常路径；它挡的是
    有人绕过服务层直接 DELETE，让渠道下一轮把回答发进一个已经消失的 id。
    """

    mgr = manager_for(rt)
    mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_bound")
    binding = mgr.get_binding("feishu", "ou_bound")
    assert binding is not None
    conversation_id = mgr.get_or_create_conversation(binding, rt)

    with pytest.raises(IntegrityError):
        with rt.database.sessions.begin() as db:
            db.execute(delete(Conversation).where(Conversation.conversation_id == conversation_id))

    assert rt.conversations.get(FIRST_USER, conversation_id) is not None


@pytest.mark.parametrize("stamp", ["deleted_at", "archived_at"])
def test_get_or_create_conversation_replaces_a_closed_one(rt, stamp):
    """软删除与归档的会话都拒收新消息（require_writable），所以换一条新的续上。"""

    mgr = manager_for(rt)
    binding = mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_closed")
    assert binding is not None
    view = rt.conversations.create(FIRST_USER, "关掉的会话")
    # 直接写库打标记：走 service 得先铺一层审计上下文，这里要钉的是「关掉之后怎么办」。
    with rt.database.sessions.begin() as db:
        db.execute(
            update(Conversation)
            .where(Conversation.conversation_id == view.conversation_id)
            .values(**{stamp: datetime.now(timezone.utc)})
        )
        row = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_closed")
        )
        row.conversation_id = view.conversation_id

    replacement = mgr.get_or_create_conversation(binding, rt)

    assert replacement != view.conversation_id
    fresh = rt.conversations.get(FIRST_USER, replacement)
    assert fresh.deleted is False
    assert fresh.archived is False
    assert stored_binding(rt, "ou_closed").conversation_id == replacement


def test_concurrent_conversation_lookup_converges_on_one_id(rt):
    """同一绑定两个线程同时取会话：拿到同一个 id，且库里那一行就是它。

    并发败者可能留下一条没人用的会话（那也是合法会话、不会互相作废），所以行数上界是 2；
    但返回值必须以库里那一行为准，否则两个线程会把消息发进两条不同的会话。
    """

    mgr = manager_for(rt)
    mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_parallel", "并发的人")
    start = threading.Barrier(2)
    opened: dict[int, object] = {}

    def open_conversation(index: int) -> None:
        own = mgr.get_binding("feishu", "ou_parallel")  # 每个请求手里是自己那一份对象
        assert own is not None
        start.wait(timeout=10)
        try:
            opened[index] = mgr.get_or_create_conversation(own, rt)
        except BaseException as exc:
            opened[index] = exc

    threads = [threading.Thread(target=open_conversation, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(opened) == 2, opened
    for value in opened.values():
        assert isinstance(value, str), value
    assert opened[0] == opened[1]
    assert stored_binding(rt, "ou_parallel").conversation_id == opened[0]
    assert 1 <= conversation_count(rt, FIRST_USER) <= 2
    assert rt.conversations.get(FIRST_USER, opened[0]).conversation_id == opened[0]


def test_a_concurrent_writer_wins_over_the_locally_created_conversation(rt, monkeypatch):
    """读到空、写回之前库里的那一行被别人改了：以库里的为准，绝不覆盖回去。"""

    mgr = manager_for(rt)
    binding = mgr.redeem_code(
        mgr.generate_binding_code(FIRST_USER), "feishu", "ou_overtaken", "被人抢写的人"
    )
    assert binding is not None
    racing: dict[str, str] = {}
    create = rt.conversations.create

    def create_behind_our_back(user_id, title=None):
        # 本次调用刚决定「没有会话，开一条」：另一个请求就在这个空档里先落了行——这正是
        # _remember_conversation 的 with_for_update 与三态判断要挡住的情形。
        rival = create(user_id, "并发赢家开的会话")
        racing["rival"] = rival.conversation_id
        with rt.database.sessions.begin() as db:
            db.execute(
                update(ChannelBinding)
                .where(ChannelBinding.id == binding.id)
                .values(conversation_id=rival.conversation_id)
            )
        return create(user_id, title=title)

    monkeypatch.setattr(rt.conversations, "create", create_behind_our_back)

    chosen = mgr.get_or_create_conversation(binding, rt)

    assert chosen == racing["rival"]
    assert stored_binding(rt, "ou_overtaken").conversation_id == racing["rival"]
    assert binding.conversation_id == racing["rival"]
    # 自己刚建的那条空会话留着不碍事（它也是合法会话），只是没被写成这个绑定的会话。
    assert conversation_count(rt, FIRST_USER) == 2


def test_a_racing_insert_falls_back_to_the_winner_row(rt):
    """两个写手同时插同一个 (channel, platform_user_id)：唯一键只放一个进来。

    输家的 INSERT 撞 uq_channel_platform_user 之后要走兜底：把赢家那一行读回来交出去，
    不抛异常、也不在库里留下第二行。
    """

    mgr = manager_for(rt)
    outcome: list[object] = []

    def racer() -> None:
        try:
            outcome.append(
                mgr._bind(
                    channel="feishu",
                    platform_user_id="ou_insert_race",
                    platform_username="挑战者",
                    user_id=FIRST_USER,
                )
            )
        except BaseException as exc:
            outcome.append(exc)

    with rt.database.sessions() as holder:
        holder.add(
            ChannelBinding(
                channel="feishu",
                platform_user_id="ou_insert_race",
                platform_username="先到的行",
                user_id=FIRST_USER,
            )
        )
        holder.flush()  # 已发给 Postgres、尚未提交：这正是输家 INSERT 会撞上的那一行
        thread = threading.Thread(target=racer)
        thread.start()
        blocked = wait_for_blocked_insert(rt)
        holder.commit()
    thread.join(timeout=30)

    assert blocked, "输家的 INSERT 没有排到唯一键上，这条用例没走到 IntegrityError 兜底"
    assert len(outcome) == 1, outcome
    assert not isinstance(outcome[0], BaseException), outcome
    winner = stored_binding(rt, "ou_insert_race")
    assert outcome[0].id == winner.id
    assert outcome[0].user_id == FIRST_USER
    # 交出去的是库里那一行，不是自己没落成的那次写：昵称仍是赢家的。
    assert outcome[0].platform_username == "先到的行"
    assert binding_count(rt) == 1


def test_a_conversation_owned_by_somebody_else_is_replaced(rt):
    """绑定行指着的会话不属于这个用户：按不可用处理，换一条自己的。"""

    second = second_session(rt)
    mgr = manager_for(rt)
    binding = mgr.redeem_code(
        mgr.generate_binding_code(FIRST_USER), "feishu", "ou_foreign", "指向别人会话的人"
    )
    assert binding is not None
    foreign = rt.conversations.create(second["user_id"], "别人的会话")
    with rt.database.sessions.begin() as db:
        db.execute(
            update(ChannelBinding)
            .where(ChannelBinding.id == binding.id)
            .values(conversation_id=foreign.conversation_id)
        )

    opened = mgr.get_or_create_conversation(binding, rt)

    assert opened != foreign.conversation_id
    assert rt.conversations.get(FIRST_USER, opened).conversation_id == opened
    assert stored_binding(rt, "ou_foreign").conversation_id == opened
    # 别人的会话原封不动。
    kept = rt.conversations.get(second["user_id"], foreign.conversation_id)
    assert kept.conversation_id == foreign.conversation_id


def test_a_binding_deleted_mid_flight_is_not_a_crash(rt):
    """绑定行在取会话的过程中被删（Web 端解绑）：会话 id 仍然可用，只是没地方记。"""

    mgr = manager_for(rt)
    binding = mgr.redeem_code(mgr.generate_binding_code(FIRST_USER), "feishu", "ou_revoked")
    assert binding is not None
    with rt.database.sessions.begin() as db:
        db.execute(delete(ChannelBinding).where(ChannelBinding.id == binding.id))

    opened = mgr.get_or_create_conversation(binding, rt)

    assert rt.conversations.get(FIRST_USER, opened).conversation_id == opened
    assert binding_count(rt) == 0


# --- 极简自动绑定 -----------------------------------------------------------------------


def test_auto_bind_is_off_when_the_setting_is_off(rt):
    """关掉开关就什么都不做：让人走 /bind 显式绑定。"""

    mgr = manager_for(rt, feishu_auto_bind=False, feishu_allow_from="ou_allowed")

    assert mgr.auto_bind_default_user("feishu", "ou_allowed", "白名单里的人") is None
    assert binding_count(rt) == 0


def test_auto_bind_skips_an_identity_outside_the_whitelist(rt):
    """白名单开着且不含这个人：自动绑定绝不能成为绕过访问控制的后门。"""

    mgr = manager_for(rt, feishu_auto_bind=True, feishu_allow_from=" ou_allowed , ,ou_second ")

    assert mgr.auto_bind_default_user("feishu", "ou_stranger", "陌生人") is None
    assert binding_count(rt) == 0
    # 白名单是逗号分隔的，两边留白与空项都不该让名单失效。
    assert mgr.auto_bind_default_user("feishu", "ou_second", "白名单里的人") is not None


def test_auto_bind_admits_anybody_when_the_whitelist_is_off(rt):
    """白名单关闭是部署方显式的决定：此时自动绑定对任何身份成立。"""

    mgr = manager_for(
        rt, feishu_auto_bind=True, feishu_whitelist_enabled=False, feishu_allow_from=""
    )

    binding = mgr.auto_bind_default_user("feishu", "ou_anyone", "谁都可以")

    assert binding is not None
    assert binding.user_id == "user_0001"
    assert binding.platform_username == "谁都可以"
    assert binding.conversation_id is None


def test_auto_bind_binds_a_whitelisted_identity_to_the_default_user(rt):
    """默认配置下，白名单用户的第一条消息就把身份落到 feishu_default_bound_user。"""

    second = second_session(rt)
    mgr = manager_for(
        rt,
        feishu_auto_bind=True,
        feishu_allow_from="ou_allowed",
        feishu_default_bound_user=second["user_id"],
    )

    binding = mgr.auto_bind_default_user("feishu", "ou_allowed", "白名单里的人")

    assert binding is not None
    assert binding.user_id == second["user_id"]
    stored = mgr.get_binding("feishu", "ou_allowed")
    assert stored is not None
    assert stored.id == binding.id
    assert stored.user_id == second["user_id"]


def test_auto_bind_is_idempotent(rt):
    """首次发消息之后每一条消息都会再问一次：第二次必须原样返回，不再插一行。"""

    mgr = manager_for(rt, feishu_auto_bind=True, feishu_allow_from="ou_allowed")

    first = mgr.auto_bind_default_user("feishu", "ou_allowed", "白名单里的人")
    again = mgr.auto_bind_default_user("feishu", "ou_allowed", "白名单里的人")

    assert first is not None
    assert again is not None
    assert again.id == first.id
    assert binding_count(rt) == 1


def test_auto_bind_never_moves_an_identity_bound_to_someone_else(rt):
    """已经被 /bind 绑到别人身上：自动绑定返回那一行，绝不改主人。"""

    second = second_session(rt)
    mgr = manager_for(rt, feishu_auto_bind=True, feishu_allow_from="ou_allowed")
    bound = mgr.redeem_code(
        mgr.generate_binding_code(second["user_id"]), "feishu", "ou_allowed", "已被显式绑定"
    )
    assert bound is not None
    assert bound.user_id == second["user_id"]

    auto = mgr.auto_bind_default_user("feishu", "ou_allowed", "白名单里的人")

    assert auto is not None
    assert auto.id == bound.id
    assert auto.user_id == second["user_id"]
    assert binding_count(rt) == 1


def test_auto_bind_fails_closed_under_the_default_configuration(rt):
    """默认配置（白名单开启、名单为空串）：谁都不许被自动绑定，库里零行。"""

    # 只看代码里的出厂默认值：不吃仓库里那份真机 .env.channels（扫码接入会往里写 FEISHU_*）
    settings = Settings(_env_file=None, database_url=rt.settings.database_url)
    mgr = ChannelBindingManager(db_factory=rt.database.sessions, settings=settings, clock=Clock())
    # 出厂默认值就是这条红线的依据：默认配置一旦漂移，这里先红。
    assert mgr.settings.feishu_auto_bind is True
    assert mgr.settings.feishu_whitelist_enabled is True
    assert mgr.settings.feishu_allow_from == ""

    assert mgr.auto_bind_default_user("feishu", "ou_anyone", "默认配置下的任何人") is None
    assert binding_count(rt) == 0


def test_auto_bind_fails_closed_when_the_default_user_does_not_exist(rt):
    """配错了默认用户就别绑：绑到一个不存在的人等于把人领进空房间。"""

    mgr = manager_for(
        rt,
        feishu_auto_bind=True,
        feishu_allow_from="ou_allowed",
        feishu_default_bound_user=UNKNOWN_USER,
    )

    assert mgr.auto_bind_default_user("feishu", "ou_allowed", "白名单里的人") is None
    assert binding_count(rt) == 0
