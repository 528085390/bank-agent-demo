"""渠道绑定与身份会话：把飞书身份映射到网银用户，并替它换发一条合法会话。

三件事，各自有一条硬边界。

**一、绑定码只活在本进程的内存里。** 它是十分钟量级的临时凭据，进程重启即失效——用户
重新生成一个就行，代价远小于为它建表、写迁移、再操心过期清理。码本身**只存
HMAC-SHA256 摘要**，密钥是每个进程现抽的 32 字节随机盐：明文不入库、不入日志、不入
审计、不进异常消息（Global Constraints：验证码类机密不进日志与 Ledger）。带进程局部
盐的摘要还顺带废掉了「离线枚举 10^6 个码做反查」这条路——同一个码在另一个进程里算不出
同样的摘要。

**二、6 位数字码是 10^6 的空间，必须防枚举。** 这里按平台身份计连续失败次数：到
`MAX_REDEEM_FAILURES` 次就锁 `REDEEM_LOCKOUT_SECONDS` 秒，锁定期间连正确的
码也不核销（fail closed）。锁按身份下，别人的核销不受牵连；成功核销清零。取值理由：真
主人只会手滑一两次，5 次足够容错；60 秒窗口把猜码速率压到 5 次/分钟，而一个码最多活
10 分钟，于是单个码被猜中的概率上限约 50/10^6。更强的手段（一次性挑战、人机校验）属于
治理扩展，不在本阶段。

**三、会话就是 identity/service.py 的那一种会话。** who 由 LoginSession 里的真实行
支撑：复用一条 authenticate 仍会认的（没注销、没到绝对上限、没闲置超时），否则按
settings.session_hours（24 小时）新开一条 sess_*，照 Identity._issue_session 的形状把
token_hash / csrf_hash / last_seen_at / absolute_expires_at 一并写齐。渠道不发明第二套
凭据，也不留一把只有 user_id 的空壳会话（真实账号之后，没有 token 的行按模型注释必须
保持不可用）。渠道自己不需要那把 token，明文 token 与 csrf 生成即丢弃。

**核销是「先占用、再落库」。** 码在锁内被标成 claimed，落库期间别人看不见它；落库成功
才算用掉，因为身份已被别人占用而被拒，就原样放回去。否则「查码」与「删码」之间那段锁外
的数据库事务，正好是两个线程同时核销同一枚码、把两个身份都绑上去的窗口。

**重复核销的语义是拒绝，不是覆盖。** (channel, platform_user_id) 唯一：同一个飞书账号
只属于一个网银用户。同一个用户再次核销同一个身份 → 刷新那一行（平台昵称、
updated_at）；**另一个**用户来核销一个已被占用的身份 → 返回 None，且**不消耗**他的码
（码是对的，他有权绑自己的另一个身份，这里拒绝的只是「抢别人的身份」）。

**两套时钟各司其职。** 绑定码 TTL 与防爆破锁定读 clock（默认 time.monotonic：单调、
不受校时跳变影响，测试可以拨表而不真等十分钟）；LoginSession 的过期读墙钟，因为
Identity.authenticate 就是拿墙钟比的——换成别的坐标系就会放行一条早已失效的会话。
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError
from app.core.types import new_id
from app.identity.tokens import digest, new_csrf_token, new_session_token
from app.storage.models import AppUser, ChannelBinding, LoginSession

if TYPE_CHECKING:
    # 只为标注。app.runtime 是组合根，渠道层不许在 import 链上反向依赖它。
    from app.core.config import Settings
    from app.runtime import Runtime

logger = logging.getLogger(__name__)

#: 绑定码的位数；码从 000001 取到 999999，全零留给用户手输的占位串。
CODE_DIGITS = 6
_CODE_SPACE = 10**CODE_DIGITS

#: 规格 §3.1 的 10 分钟有效期。
DEFAULT_CODE_TTL_SECONDS = 600

#: 同一平台身份连续猜错多少次即锁定，以及锁定多久。
MAX_REDEEM_FAILURES = 5
REDEEM_LOCKOUT_SECONDS = 60.0

#: 防爆破状态表的容量上限。常驻进程里一个只增不减的 dict 就是慢性内存事故（同
#: app/channels/dedup.py 的理由）；淘汰一个身份只是把它那一份计数归零，核销对错从不
#: 依赖这张表。
MAX_TRACKED_IDENTITIES = 2000

#: 渠道替用户开的第一条会话的标题：用户在 Web 会话列表里要能认出这是飞书那头来的。
CHANNEL_CONVERSATION_TITLE = "飞书对话"


@dataclass(slots=True)
class _PendingCode:
    """一枚待核销的码：只有摘要和到期时刻，明文这一层从不留存。

    `claimed` 是锁内的占用标记：核销正在进行（`_bind` 那段锁外事务）时，这枚码对别的
    核销请求不可见。它在 `del self._codes[...]` 之前挡住的正是「一枚码核销两次」。
    """

    digest: str
    expires_at: float
    claimed: bool = False


@dataclass(slots=True)
class _Attempts:
    """某个平台身份的连续失败计数与锁定期。"""

    failures: int = 0
    locked_until: float = 0.0


def _is_code_shaped(value: str) -> bool:
    """六位 ASCII 数字才算「一个码」。畸形输入不占失败额度，否则谁都能把身份锁死。"""

    return len(value) == CODE_DIGITS and value.isascii() and value.isdigit()


def _identity_key(channel: str, platform_user_id: str) -> str:
    """防爆破与唯一约束用的是同一个键：渠道 + 平台自己的 id。"""

    return f"{channel}:{platform_user_id}"


def _allow_list(value: str | None) -> set[str]:
    """feishu_allow_from 的解析：逗号分隔，两边留白与空项都不该让名单失效。"""

    return {part.strip() for part in (value or "").split(",") if part.strip()}


def _detach(db, row):
    """把行从 session 里摘出来再交出去：调用方拿到的是已加载的属性，不是断线代理。"""

    db.expunge(row)
    return row


class ChannelBindingManager:
    """绑定码、channel_binding 持久化、会话换发与极简自动绑定。

    db_factory 是零参可调用，返回一个 SQLAlchemy Session；生产与测试都传
    Database.sessions（即 rt.database.sessions）。settings 缺省时读 get_settings()，
    clock 缺省 time.monotonic，只影响绑定码 TTL 与防爆破锁定。
    """

    def __init__(
        self,
        db_factory: Callable[[], object],
        *,
        settings: Settings | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._settings = settings
        self._clock = clock or time.monotonic
        # 进程局部盐：摘要只在本进程内有意义，重启后与码一起作废。
        self._salt = secrets.token_bytes(32)
        self._codes: dict[str, _PendingCode] = {}
        self._attempts: dict[str, _Attempts] = {}
        # 绑定码与失败计数会被不同线程读写（核销来自消息入口，生成来自 Web 接口），
        # 而 redeem 的「先查后写」不是原子操作，所以两张表都在这把锁里读写。
        self._lock = threading.Lock()

    # --- 配置与基础设施 ---------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """延迟解析：测试可以直接注入一个 Settings，进程启动时也不必先有配置。"""

        if self._settings is None:
            from app.core.config import get_settings

            self._settings = get_settings()
        return self._settings

    @contextmanager
    def _session(self) -> Iterator[object]:
        db = self._db_factory()
        try:
            yield db
        finally:
            db.close()

    def _digest(self, code: str) -> str:
        return hmac.new(self._salt, code.encode("ascii"), sha256).hexdigest()

    # --- 绑定码 -----------------------------------------------------------------------

    def generate_binding_code(
        self, user_id: str, ttl_seconds: int = DEFAULT_CODE_TTL_SECONDS
    ) -> str:
        """给一个真实用户发一枚新码；同一用户重复生成会覆盖旧码（旧码立刻作废）。

        返回的明文只交给调用方（接口层写进响应体），这里只留摘要与到期时刻。
        """

        with self._session() as db:
            self._require_user(db, user_id)
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            code = self._fresh_code()
            self._codes[user_id] = _PendingCode(
                digest=self._digest(code), expires_at=now + max(0, ttl_seconds)
            )
        return code

    def redeem_code(
        self,
        code: str,
        channel: str,
        platform_user_id: str,
        platform_username: str | None = None,
    ) -> ChannelBinding | None:
        """用一枚码把平台身份绑到它主人身上；码错、过期、空、被锁定或被占用都返回 None。

        语义见模块文档：同一用户重核销是刷新，另一个用户来抢已被占用的身份是拒绝且不
        消耗他的码。锁定阈值见 MAX_REDEEM_FAILURES。
        """

        if not isinstance(code, str):
            # 解析层把 `/bind 123456` 递成 int 是可能的：畸形输入一律 None，不能抛 TypeError。
            return None
        normalised = code.strip()
        if not _is_code_shaped(normalised):
            return None
        key = _identity_key(channel, platform_user_id)
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            attempts = self._attempts_for(key, now)
            if attempts is not None and attempts.locked_until > now:
                return None
            claimed = self._claim(normalised, now)
            if claimed is None:
                # 码错、过期、或者正被另一个人核销：都不算这位的失败依据，记一次失败。
                if attempts is not None:
                    self._count_failure(key, attempts, now)
                return None
            user_id, pending = claimed
            self._attempts.pop(key, None)
        binding = self._bind(
            channel=channel,
            platform_user_id=platform_user_id,
            platform_username=platform_username,
            user_id=user_id,
        )
        if binding is None:
            # 身份已被别人占用：码不烧掉，主人还能拿它绑自己的另一个身份。
            self._release(user_id, pending)
            return None
        self._consume(user_id, pending)
        return binding

    def get_binding(self, channel: str, platform_user_id: str) -> ChannelBinding | None:
        """这个平台身份绑在谁身上；没绑过返回 None。"""

        with self._session() as db:
            row = db.scalar(
                select(ChannelBinding).where(
                    ChannelBinding.channel == channel,
                    ChannelBinding.platform_user_id == platform_user_id,
                )
            )
            return None if row is None else _detach(db, row)

    def _claim(self, code: str, now: float) -> tuple[str, _PendingCode] | None:
        """哪个人手里这枚码还活着，并在锁内把它标成 claimed。**必须在锁内调用。**

        占用与删除是同一个动作的两半：标了 claimed 的码对 `_claim` 不可见，所以哪怕
        `_bind` 那段事务跑得再慢，第二个拿着同一枚码的核销请求也已经看不到它了。
        """

        digest = self._digest(code)
        for user_id, pending in self._codes.items():
            if pending.claimed or pending.expires_at <= now:
                continue
            if hmac.compare_digest(pending.digest, digest):
                pending.claimed = True
                return user_id, pending
        return None

    def _release(self, user_id: str, pending: _PendingCode) -> None:
        """把没用掉的占用原样放回：核销被拒不等于码被烧掉。

        只放自己占的那一枚——这期间用户重新生成过码的话，`_codes` 里已经是另一个对象，
        动它就是给已经作废的旧码开一条后门。
        """

        with self._lock:
            if self._codes.get(user_id) is pending:
                pending.claimed = False

    def _consume(self, user_id: str, pending: _PendingCode) -> None:
        """核销即失效。码在这期间被重新生成过就什么都不做——旧码本来也已经死了。"""

        with self._lock:
            if self._codes.get(user_id) is pending:
                del self._codes[user_id]

    def _purge_expired(self, now: float) -> None:
        """顺手清掉过期的码：没人来核销的码也要有终点。**必须在锁内调用。**"""

        self._codes = {
            user_id: pending for user_id, pending in self._codes.items() if pending.expires_at > now
        }

    def _fresh_code(self) -> str:
        """抽一枚与本进程现有码都不撞的六位数字。**必须在锁内调用。**"""

        while True:
            code = f"{secrets.randbelow(_CODE_SPACE - 1) + 1:06d}"
            digest = self._digest(code)
            if not any(hmac.compare_digest(p.digest, digest) for p in self._codes.values()):
                return code

    def _attempts_for(self, key: str, now: float) -> _Attempts | None:
        """取（或建）一个身份的失败计数，满了只淘汰已经解锁的项。**必须在锁内调用。**"""

        state = self._attempts.get(key)
        if state is not None:
            return state
        if len(self._attempts) >= MAX_TRACKED_IDENTITIES:
            unlocked = [
                other for other, attempts in self._attempts.items() if attempts.locked_until <= now
            ]
            if not unlocked:
                # 全表都在锁定期内：这一次就不计数（返回 None 让调用方放弃记账），绝不
                # 用「淘汰一条锁着的记录」换容量——那等于把别人的锁定悄悄解除。
                return None
            del self._attempts[unlocked[0]]
        state = _Attempts()
        self._attempts[key] = state
        return state

    def _count_failure(self, key: str, attempts: _Attempts, now: float) -> None:
        """记一次失败；到阈值就锁一段时间。**必须在锁内调用。**"""

        attempts.failures += 1
        if attempts.failures >= MAX_REDEEM_FAILURES:
            attempts.failures = 0
            attempts.locked_until = now + REDEEM_LOCKOUT_SECONDS
            logger.warning(
                "binding code attempts locked out for %s after %d failures",
                key,
                MAX_REDEEM_FAILURES,
            )

    # --- channel_binding 持久化 -------------------------------------------------------

    def _bind(
        self,
        *,
        channel: str,
        platform_user_id: str,
        platform_username: str | None,
        user_id: str,
    ) -> ChannelBinding | None:
        """落（或刷新）一行绑定；这个身份已属于别人时返回 None。"""

        with self._session() as db:
            existing = db.scalar(
                select(ChannelBinding)
                .where(
                    ChannelBinding.channel == channel,
                    ChannelBinding.platform_user_id == platform_user_id,
                )
                .with_for_update()
            )
            if existing is not None:
                return self._refresh(
                    db,
                    existing,
                    user_id=user_id,
                    platform_username=platform_username,
                )
            row = ChannelBinding(
                channel=channel,
                platform_user_id=platform_user_id,
                platform_username=platform_username,
                user_id=user_id,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                # 两个身份同时核销：唯一键只放进来一个，赢家那一行才是答案。
                db.rollback()
                winner = db.scalar(
                    select(ChannelBinding).where(
                        ChannelBinding.channel == channel,
                        ChannelBinding.platform_user_id == platform_user_id,
                    )
                )
                if winner is None or winner.user_id != user_id:
                    return None
                return _detach(db, winner)
            return _detach(db, row)

    def _refresh(
        self,
        db,
        row: ChannelBinding,
        *,
        user_id: str,
        platform_username: str | None,
    ) -> ChannelBinding | None:
        """同一用户重核销：改昵称、动 updated_at，不插新行。"""

        if row.user_id != user_id:
            logger.warning(
                "refused channel binding: %s identity already bound to another user",
                row.channel,
            )
            return None
        if platform_username and row.platform_username != platform_username:
            row.platform_username = platform_username
        # onupdate 只在真的改了列时才触发，而「刷新」这个动作本身该在行上留下时间：显式
        # 写 updated_at，否则同昵称重核销在库里看不出任何变化。
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return _detach(db, row)

    def _require_user(self, db, user_id: str) -> None:
        """绑定与会话都要求这个人真的存在：外键会拒绝，但先说清楚是哪一步。"""

        if db.scalar(select(AppUser.user_id).where(AppUser.user_id == user_id)) is None:
            raise AppError("NOT_FOUND", "用户不存在", 404)

    def _user_exists(self, user_id: str) -> bool:
        with self._session() as db:
            return db.scalar(select(AppUser.user_id).where(AppUser.user_id == user_id)) is not None

    # --- 会话换发 ---------------------------------------------------------------------

    def get_or_create_channel_session(self, user_id: str) -> dict[str, str]:
        """给渠道一条能直接喂给 turns.prepare 的 who。

        复用判定只看 expires_at > now（墙钟），并且取到期最晚的那条：用户可能同时有 Web
        端的会话，那条还能用就别再开一条，免得同一用户的两个入口落在两条并行会话上。一条
        可用的都没有就按 settings.session_hours 新开——「几天前绑定、会话早已过期」的用户
        因此永远不会被中断（Review Focus 第 4 条）。

        并发下两个请求可能各开一条：两条都是合法会话，谁先到都能跑，这里不做无谓的串行。
        多出来的那条空会话**不会**互相作废，也不该当成错误——所以别按「每人只有一条会话」
        写下游逻辑：会话可以不止一条，取哪条一律以本方法的返回值为准。
        """

        now = datetime.now(timezone.utc)
        idle_cutoff = now - timedelta(minutes=self.settings.auth_session_idle_minutes)
        with self._session() as db:
            self._require_user(db, user_id)
            # 复用条件与 `Identity.authenticate` 认不认这条会话一致：没被注销、没到绝对上限、
            # 也没闲置超时。少判一条就会把一条已失效的会话发给 turns，用户看到的却是「为什么
            # 我明明登录着却说没权限」。
            live = db.scalar(
                select(LoginSession)
                .where(
                    LoginSession.user_id == user_id,
                    LoginSession.revoked_at.is_(None),
                    LoginSession.expires_at > now,
                    LoginSession.last_seen_at > idle_cutoff,
                )
                .order_by(LoginSession.expires_at.desc(), LoginSession.created_at.desc())
                .limit(1)
            )
            if live is not None:
                return {"user_id": live.user_id, "session_id": live.session_id}
            # 新开的这条必须是**一条正常登录会话**，不是只有 user_id 的空壳：真实账号之后，
            # `session` 表用 token_hash 认人（`authenticate` 按摘要查），没有 token 的行按模型
            # 注释是「保持不可用」。渠道不需要那把 token，但行本身要是合法的登录态——所以照
            # `Identity._issue_session` 的形状写齐字段，明文 token 与 csrf 就地丢弃。
            expires = now + timedelta(hours=self.settings.session_hours)
            row = LoginSession(
                session_id=new_id("sess"),
                user_id=user_id,
                created_at=now,
                expires_at=expires,
                absolute_expires_at=expires,
                last_seen_at=now,
                token_hash=digest(new_session_token()),
                csrf_hash=digest(new_csrf_token()),
            )
            db.add(row)
            db.commit()
            return {"user_id": row.user_id, "session_id": row.session_id}

    # --- conversation ------------------------------------------------------------------

    def get_or_create_conversation(self, binding: ChannelBinding, rt: Runtime) -> str:
        """这个绑定私聊用的那条会话：有就沿用，没有就经 rt.conversations 开一条并写回。

        幂等的依据是**库里的那一行**，不是传进来的对象：一个绑定只可能有一条会话，所以两
        次取用必然拿到同一个 id。绑定行上记着的那条会话如果已经不存在、被软删除或被归档，
        就换一条——归档与软删除的会话拒收新消息（require_writable），把一个发不进去的 id
        传下去只会让用户看到一句「会话不可写」。
        """

        stored = self._stored_conversation_id(binding)
        if stored is not None:
            reusable = self._still_usable(binding.user_id, stored, rt)
            if reusable is not None:
                binding.conversation_id = reusable
                return reusable
        view = rt.conversations.create(binding.user_id, title=CHANNEL_CONVERSATION_TITLE)
        return self._remember_conversation(binding, view.conversation_id, previous=stored)

    def open_new_conversation(self, binding: ChannelBinding, rt: Runtime) -> str:
        """换一条新会话，并把绑定的那一行指过去。

        用在"这条会话已经接不下去"的场合：图在某一轮中途失败，checkpointer 里留着未完成的
        状态，runtime 从此对这条会话一律拒收（EXECUTION_RECOVERY_REQUIRED）。网页那边用户
        点「新建会话」就绕开了，渠道只有一个固定会话，不换就等于让用户永久卡在同一句话上。

        旧会话不删：上面可能已经有用户看过的答复，留着可查；只是绑定不再指向它。
        """

        previous = self._stored_conversation_id(binding)
        view = rt.conversations.create(binding.user_id, title=CHANNEL_CONVERSATION_TITLE)
        return self._remember_conversation(binding, view.conversation_id, previous=previous)

    def _stored_conversation_id(self, binding: ChannelBinding) -> str | None:
        with self._session() as db:
            return db.scalar(
                select(ChannelBinding.conversation_id).where(ChannelBinding.id == binding.id)
            )

    def _still_usable(self, user_id: str, conversation_id: str, rt: Runtime) -> str | None:
        """这条会话还收不收新消息；查不到（或不属于这个人）也算不可用。"""

        try:
            view = rt.conversations.get(user_id, conversation_id)
        except AppError:
            return None
        if view.deleted or view.archived:
            return None
        return view.conversation_id

    def _remember_conversation(
        self, binding: ChannelBinding, conversation_id: str, *, previous: str | None
    ) -> str:
        """把会话 id 写回绑定的那一行；锁住它，让并发的两次取用收敛到一个 id。

        previous 是刚才读到的那一版。只有库里变了（既不是空的、也不是我们读到的那个）才算
        别人抢先写了——否则「绑定行还指着我刚判定为不可用的那条」会被误认成赢家，死 id 就
        又被发下去了。
        """

        with self._session() as db:
            row = db.scalar(
                select(ChannelBinding).where(ChannelBinding.id == binding.id).with_for_update()
            )
            if row is None:
                # 绑定行在取会话的过程中被删了：这个 id 仍然可用，只是没地方记。
                binding.conversation_id = conversation_id
                return conversation_id
            changed_underneath = (
                row.conversation_id
                and row.conversation_id != previous
                and row.conversation_id != conversation_id
            )
            if changed_underneath:
                # 并发的那一次先落了行：以库里的为准，本次新建的空会话留着不碍事。
                binding.conversation_id = row.conversation_id
                return row.conversation_id
            row.conversation_id = conversation_id
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
        binding.conversation_id = conversation_id
        return conversation_id

    # --- 极简自动绑定 ------------------------------------------------------------------

    def is_allowed(self, platform_user_id: str) -> bool:
        """这个平台身份是否被放行：白名单关闭时对所有人成立。"""

        settings = self.settings
        if not settings.feishu_whitelist_enabled:
            return True
        return platform_user_id in _allow_list(settings.feishu_allow_from)

    def auto_bind_default_user(
        self,
        channel: str,
        platform_user_id: str,
        platform_username: str | None = None,
    ) -> ChannelBinding | None:
        """极简模式：白名单用户首次发消息就直接落到 feishu_default_bound_user。

        调用方式（Task 8 的 dispatcher）::

            binding = manager.get_binding(msg.channel, msg.sender_id) or (
                manager.auto_bind_default_user(msg.channel, msg.sender_id, msg.sender_name)
            )

        四条前提缺一不可，任何一条不成立都返回 None（不落库、不抛异常）：
        feishu_auto_bind 为真、身份在白名单里（feishu_whitelist_enabled 为真时）、该身份
        还没被绑过、默认用户真实存在。已经被显式绑定到别人身上的身份原样返回那一行——
        自动绑定绝不改主人。
        """

        settings = self.settings
        if not settings.feishu_auto_bind:
            return None
        if not self.is_allowed(platform_user_id):
            logger.warning("auto-bind skipped: %s is not on the whitelist", platform_user_id)
            return None
        existing = self.get_binding(channel, platform_user_id)
        if existing is not None:
            return existing
        user_id = (settings.feishu_default_bound_user or "").strip()
        if not user_id or not self._user_exists(user_id):
            logger.warning("auto-bind skipped: feishu_default_bound_user does not exist")
            return None
        binding = self._bind(
            channel=channel,
            platform_user_id=platform_user_id,
            platform_username=platform_username,
            user_id=user_id,
        )
        if binding is not None:
            logger.info("auto-bound %s identity to %s", channel, user_id)
        return binding
