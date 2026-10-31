"""账号身份：注册、登录、登录态校验与撤销。

这个模块是「谁是当前用户」的唯一答案。它不碰对话、不碰银行，只回答三件事：这个凭据
对不对、这个登录态还有效吗、这个用户是谁。

三条设计约束，写在这里是因为后面每一行都在遵守它们：

1. **浏览器拿 token，库里存摘要。** `session.session_id` 是内部标识，出现在外键、日志和
   追踪里，但它不能当凭证用——它可预测、可枚举、也不该出现在 URL 里。
2. **每次请求都查库。** 上一版有一个进程内缓存直接放行，撤销路径一旦出现它就变成漏洞。
   P1 先要正确性：一次按唯一索引的查询，比一个跨 worker 不一致的缓存便宜得多。
3. **失败只有一种说法。** 账号不存在、密码错、账号禁用都抛同一个 `INVALID_CREDENTIALS`，
   并且都要付出一次 argon2 的时间，否则响应时间本身就泄露了账号是否存在。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.types import new_id
from app.identity.errors import AuthErrorCode, auth_error
from app.identity.password import (
    hash_password,
    needs_rehash,
    normalise_login_name,
    spend_verification_time,
    validate_password,
    verify_password,
)
from app.identity.ratelimit import LoginLimiter
from app.identity.tokens import digest, matches, new_csrf_token, new_session_token
from app.storage.models import AppUser, AuthCredential, LoginSession

# `last_seen_at` 的写入节流。空闲超时是 30 分钟，一分钟的精度绰绰有余；而一次会话里
# 每秒都可能有一个轮询请求，不节流就等于把每个 GET 变成一次 UPDATE。
_LAST_SEEN_WRITE_INTERVAL = timedelta(seconds=60)


class Identity:
    def __init__(self, database, settings, refiller=None):
        self.database = database
        self.settings = settings
        self.limiter = LoginLimiter(database, settings)
        # 可选：没有补位器时行为与 P1 一致（池子领完为止）。`Runtime` 总会注入一个。
        self.refiller = refiller

    # --- registration -----------------------------------------------------------------

    def register(self, *, login_name: str, password: str) -> dict:
        """Create an account and claim one bank customer for it.

        The claim happens inside the same transaction as the account, so a user can never
        exist without having at least tried to take a customer, and two concurrent
        registrations cannot end up on the same one.

        领取之后分两种情况收尾，都在事务之外、都不影响注册是否成功：

        * 领到了 -> 让银行侧再建一个补到池尾（自愈水位）。
        * 池子是空的 -> 直接把补出来的那个绑给这位用户。否则他会永远停在未绑定状态：
          补池脚本只恢复「已经领过」的人，而他从没领到过。

        这就是 P1 留下的那个缺口（池空即永久失联）的闭合点。
        """

        name = normalise_login_name(login_name)
        validate_password(password, login_name=name)
        now = datetime.now(timezone.utc)
        password_hash = hash_password(password)

        try:
            with self.database.sessions.begin() as db:
                user = AppUser(
                    user_id=new_id("user"),
                    status="active",
                    role="user",
                    created_at=now,
                    updated_at=now,
                )
                db.add(user)
                db.flush()
                db.add(
                    AuthCredential(
                        user_id=user.user_id,
                        login_name=name,
                        password_hash=password_hash,
                        password_updated_at=now,
                        created_at=now,
                    )
                )
                user.bank_customer_id = self._claim_customer(db, user.user_id, now)
                db.flush()
                created = {
                    "user_id": user.user_id,
                    "login_name": name,
                    "bank_customer_id": user.bank_customer_id,
                }
        except IntegrityError:
            # The unique constraint on `login_name` is the arbiter, not a prior SELECT:
            # checking first would still lose the race between the check and the insert.
            raise auth_error(AuthErrorCode.LOGIN_NAME_TAKEN) from None

        if created["bank_customer_id"] is None:
            created["bank_customer_id"] = self._provision_for(created["user_id"])
        else:
            self._refill_pool()
        return created

    def _claim_customer(self, db, user_id: str, now: datetime) -> str | None:
        """Take one free customer, atomically.

        `SKIP LOCKED` is what makes this safe under concurrency: the second registration
        moves on to the next free row instead of blocking behind the first, and neither can
        see a row the other is about to claim.
        """

        return db.execute(
            text(
                """
                UPDATE bank_customer_pool
                SET claimed_by_user_id = :user_id, claimed_at = :now
                WHERE customer_id = (
                    SELECT customer_id FROM bank_customer_pool
                    WHERE claimed_by_user_id IS NULL
                    ORDER BY customer_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING customer_id
                """
            ),
            {"user_id": user_id, "now": now},
        ).scalar()

    def _refill_pool(self) -> None:
        """领走一格之后补一格回池尾。失败只是水位低了一格，不是用户的问题。"""

        if self.refiller is None:
            return
        try:
            self.refiller.refill()
        except Exception:  # noqa: BLE001 - 注册已经成功，补位不能把它变成失败
            pass

    def _provision_for(self, user_id: str) -> str | None:
        """池子空了：给这位用户直接建一个客户并绑定。"""

        if self.refiller is None:
            return None
        try:
            return self.refiller.provision_for(user_id)
        except Exception:  # noqa: BLE001
            return None

    # --- login ------------------------------------------------------------------------

    def login(self, *, login_name: str, password: str, client_ip: str | None = None) -> dict:
        name = normalise_login_name(login_name)
        self.limiter.ensure_allowed(name, client_ip)

        now = datetime.now(timezone.utc)
        with self.database.sessions() as db:
            found = db.execute(
                select(AuthCredential, AppUser)
                .join(AppUser, AppUser.user_id == AuthCredential.user_id)
                .where(AuthCredential.login_name == name)
            ).first()

        if found is None:
            # Spend the same argon2 time as a real verification before refusing.
            spend_verification_time(password)
            self.limiter.record_failure(name, client_ip)
            raise auth_error(AuthErrorCode.INVALID_CREDENTIALS)

        credential, user = found
        password_ok = verify_password(credential.password_hash, password)
        if not password_ok or user.status != "active":
            self.limiter.record_failure(name, client_ip)
            raise auth_error(AuthErrorCode.INVALID_CREDENTIALS)

        self.limiter.clear(name, client_ip)
        if needs_rehash(credential.password_hash):
            # Transparent upgrade: the plaintext is in hand only here, so this is the one
            # moment a stronger parameter set can be applied without forcing a reset.
            with self.database.sessions.begin() as db:
                db.execute(
                    update(AuthCredential)
                    .where(AuthCredential.user_id == user.user_id)
                    .values(password_hash=hash_password(password), password_updated_at=now)
                )

        if not user.bank_customer_id:
            # 上一次注册时银行不可用，账号留下了但没有绑定。登录是本人主动动作，顺手补
            # 一次；成功与否都不影响登录本身，所以不开独立的「重试」接口。
            self._provision_for(user.user_id)

        return self._issue_session(user, now, login_name=credential.login_name)

    def _issue_session(self, user: AppUser, now: datetime, *, login_name: str | None) -> dict:
        token = new_session_token()
        csrf = new_csrf_token()
        absolute = now + timedelta(hours=self.settings.session_hours)
        with self.database.sessions.begin() as db:
            row = LoginSession(
                session_id=new_id("sess"),
                user_id=user.user_id,
                created_at=now,
                expires_at=absolute,
                absolute_expires_at=absolute,
                token_hash=digest(token),
                csrf_hash=digest(csrf),
                last_seen_at=now,
            )
            db.add(row)
            session_id = row.session_id
        return {
            "session_id": session_id,
            "token": token,
            "csrf_token": csrf,
            "user_id": user.user_id,
            # None only for a credential-less archive account (the `user_0001` demo entry).
            "login_name": login_name,
            "role": user.role,
        }

    # --- authentication ---------------------------------------------------------------

    def authenticate(self, token: str | None) -> dict:
        """Resolve a browser token to the caller, or refuse.

        Every failure mode collapses to `SESSION_REQUIRED`: a client cannot tell a revoked
        session from an expired one from a token that never existed, and it does not need to
        -- all three mean "log in again".
        """

        if not token:
            raise auth_error(AuthErrorCode.SESSION_REQUIRED)
        now = datetime.now(timezone.utc)
        with self.database.sessions() as db:
            found = db.execute(
                select(LoginSession, AppUser, AuthCredential.login_name)
                .join(AppUser, AppUser.user_id == LoginSession.user_id)
                .outerjoin(AuthCredential, AuthCredential.user_id == AppUser.user_id)
                .where(LoginSession.token_hash == digest(token))
            ).first()
        if found is None:
            raise auth_error(AuthErrorCode.SESSION_REQUIRED)

        row, user, login_name = found
        if row.revoked_at is not None:
            raise auth_error(AuthErrorCode.SESSION_REQUIRED)
        ceiling = row.absolute_expires_at or row.expires_at
        if ceiling <= now:
            raise auth_error(AuthErrorCode.SESSION_REQUIRED)
        last = row.last_seen_at or row.created_at
        if last + timedelta(minutes=self.settings.auth_session_idle_minutes) <= now:
            raise auth_error(AuthErrorCode.SESSION_REQUIRED)
        # A disabled account is refused here rather than at login only: disabling someone
        # has to take effect on the session they are already holding.
        if user.status != "active":
            raise auth_error(AuthErrorCode.SESSION_REQUIRED)

        if now - last >= _LAST_SEEN_WRITE_INTERVAL:
            self._touch(row.session_id, now)

        return {
            "session_id": row.session_id,
            "user_id": user.user_id,
            "login_name": login_name,
            "role": user.role,
            "csrf_hash": row.csrf_hash,
        }

    def _touch(self, session_id: str, now: datetime) -> None:
        try:
            with self.database.sessions.begin() as db:
                db.execute(
                    update(LoginSession)
                    .where(LoginSession.session_id == session_id, LoginSession.revoked_at.is_(None))
                    .values(last_seen_at=now)
                )
        except Exception:  # noqa: BLE001 - a missed heartbeat must not fail the request
            # The idle clock simply advances from the older timestamp next time; refusing a
            # request because a bookkeeping write lost a race would be worse than the drift.
            pass

    def require_csrf(self, who: dict, presented: str | None) -> None:
        # Two different failures, two different fixes: a missing header means the page never
        # got a token (no session yet, or the bundle was served from a stale cache), while a
        # mismatched one means the cookie and the header came from different sessions --
        # typically two tabs that each logged in. Collapsing them into one message left the
        # user with "refresh and retry", which fixes only the second.
        if not presented:
            raise auth_error(AuthErrorCode.CSRF_TOKEN_MISSING)
        if not matches(who.get("csrf_hash"), presented):
            raise auth_error(AuthErrorCode.CSRF_TOKEN_INVALID)

    def require_admin(self, who: dict) -> None:
        if who.get("role") != "admin":
            raise auth_error(AuthErrorCode.FORBIDDEN_ROLE)

    # --- session lifecycle ------------------------------------------------------------

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.database.sessions.begin() as db:
            db.execute(
                update(LoginSession)
                .where(LoginSession.token_hash == digest(token), LoginSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(timezone.utc))
            )

    def logout_all(self, user_id: str) -> int:
        with self.database.sessions.begin() as db:
            result = db.execute(
                update(LoginSession)
                .where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(timezone.utc))
            )
            return result.rowcount or 0

    def change_password(self, user_id: str, current: str, new: str) -> None:
        """Change the password and revoke every session, including this one.

        Revoking all of them is the point: if someone else is holding a session, changing
        the password has to end it, and the owner simply logs in again.
        """

        with self.database.sessions() as db:
            credential = db.scalar(
                select(AuthCredential).where(AuthCredential.user_id == user_id)
            )
        if credential is None or not verify_password(credential.password_hash, current):
            raise auth_error(AuthErrorCode.CURRENT_PASSWORD_INVALID)
        if verify_password(credential.password_hash, new):
            raise auth_error(AuthErrorCode.PASSWORD_UNCHANGED)
        validate_password(new, login_name=credential.login_name)

        now = datetime.now(timezone.utc)
        with self.database.sessions.begin() as db:
            db.execute(
                update(AuthCredential)
                .where(AuthCredential.user_id == user_id)
                .values(password_hash=hash_password(new), password_updated_at=now)
            )
        self.logout_all(user_id)

    # --- demo entry -------------------------------------------------------------------

    def create_demo_session(self) -> dict:
        """Mint a session for the fixed demo archive, with no credential.

        Only the route decides whether this is reachable; the method itself stays because
        tests and `scripts/` use it to build a caller. It refuses outright when demo mode is
        off, so a production process cannot produce one by accident.
        """

        if not self.settings.demo_mode:
            raise auth_error(AuthErrorCode.DEMO_DISABLED)
        now = datetime.now(timezone.utc)
        with self.database.sessions() as db:
            user = db.scalar(select(AppUser).where(AppUser.user_id == "user_0001"))
        if user is None:
            raise auth_error(AuthErrorCode.DEMO_DISABLED)
        issued = self._issue_session(user, now, login_name=None)
        issued["demo"] = True
        return issued

    # --- user-facing views ------------------------------------------------------------

    def me(self, who: dict) -> dict:
        with self.database.sessions() as db:
            user = db.scalar(select(AppUser).where(AppUser.user_id == who["user_id"]))
            login_name = db.scalar(
                select(AuthCredential.login_name).where(
                    AuthCredential.user_id == who["user_id"]
                )
            )
        return {
            "user_id": who["user_id"],
            "role": who["role"],
            "login_name": login_name,
            "email": user.email if user else None,
            "bank_ready": bool(user and user.bank_customer_id),
        }

    def bank_customer_id(self, user_id: str) -> str | None:
        """The bank customer this user acts as, or None when there is no binding.

        Returning None is the whole point: the caller fails closed, because bank_backend
        treats a missing `user_id` as its own default customer.
        """

        with self.database.sessions() as db:
            return db.scalar(select(AppUser.bank_customer_id).where(AppUser.user_id == user_id))
