"""登录失败限速。

计数放在数据库里，不放在进程内存里：一个 4 worker 的进程如果各自计数，攻击者拿到的预算
就是 4 倍，而登录是唯一一个「多试几次就一定有收益」的接口。表的唯一约束
`(scope, key)` 让并发写入收敛到一行，`FOR UPDATE` 让同一行的读改写不丢计数。

两个 scope 各自独立计数：按账号挡住针对某个人的爆破，按 IP 挡住拿一堆账号试密码的撞库。
任一命中即拒绝，所以撞库和爆破都要付代价。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.identity.errors import AuthErrorCode, auth_error
from app.storage.models import LoginAttempt

SCOPE_ACCOUNT = "account"
SCOPE_IP = "ip"


class LoginLimiter:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    def _keys(self, login_name: str, client_ip: str | None) -> list[tuple[str, str]]:
        keys = [(SCOPE_ACCOUNT, login_name)]
        # A request with no usable client address is counted by account only. Inventing a
        # placeholder would let every anonymous request share one bucket and lock out
        # everybody at once.
        if client_ip:
            keys.append((SCOPE_IP, client_ip))
        return keys

    def ensure_allowed(self, login_name: str, client_ip: str | None) -> None:
        now = datetime.now(timezone.utc)
        with self.database.sessions() as db:
            for scope, key in self._keys(login_name, client_ip):
                row = db.scalar(
                    select(LoginAttempt).where(
                        LoginAttempt.scope == scope, LoginAttempt.key == key
                    )
                )
                if row and row.locked_until and row.locked_until > now:
                    raise auth_error(AuthErrorCode.LOGIN_RATE_LIMITED, retryable=True)

    def record_failure(self, login_name: str, client_ip: str | None) -> None:
        """Count one failure. Committed on its own, so a failed login that then raises still
        leaves the counter behind -- which is the entire point of counting it."""

        now = datetime.now(timezone.utc)
        window = timedelta(minutes=self.settings.auth_login_window_minutes)
        lock = timedelta(minutes=self.settings.auth_login_lock_minutes)
        limit = self.settings.auth_login_max_failures

        for scope, key in self._keys(login_name, client_ip):
            try:
                with self.database.sessions.begin() as db:
                    self._bump(db, scope, key, now, window, lock, limit)
            except IntegrityError:
                # Two requests created the same (scope, key) at once; the loser retries
                # against the winner's row instead of dropping the failure.
                with self.database.sessions.begin() as db:
                    self._bump(db, scope, key, now, window, lock, limit)

    def _bump(self, db, scope, key, now, window, lock, limit) -> None:
        row = db.scalar(
            select(LoginAttempt)
            .where(LoginAttempt.scope == scope, LoginAttempt.key == key)
            .with_for_update()
        )
        if row is None:
            db.add(
                LoginAttempt(
                    scope=scope,
                    key=key,
                    window_start=now,
                    failure_count=1,
                    locked_until=now + lock if limit <= 1 else None,
                )
            )
            return
        if row.window_start <= now - window:
            # The window rolled over: the old failures are no longer evidence of anything.
            row.window_start = now
            row.failure_count = 1
            row.locked_until = None
        else:
            row.failure_count += 1
        if row.failure_count >= limit:
            row.locked_until = now + lock

    def clear(self, login_name: str, client_ip: str | None) -> None:
        """A successful login wipes the account counter but not the IP one.

        Clearing the IP bucket on any success would hand an attacker a reset: one valid
        account is enough to launder an unlimited number of guesses against every other.
        """

        with self.database.sessions.begin() as db:
            row = db.scalar(
                select(LoginAttempt)
                .where(LoginAttempt.scope == SCOPE_ACCOUNT, LoginAttempt.key == login_name)
                .with_for_update()
            )
            if row is not None:
                row.failure_count = 0
                row.locked_until = None
