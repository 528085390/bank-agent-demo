"""运维：给一个已有账号设置登录名和密码，或把它提成管理员。

为什么需要这个脚本：`user_0001` 是演示档案，它保留了历史对话和银行绑定，但迁移不会凭空
给它造一个密码——那等于在版本库里写一个后门。想用这个账号登录，就得有人显式地跑一次这里。

用法（在 `backend/` 下）：

    uv run python -m scripts.set_password --login-name demo --password '一个足够长的口令'
    uv run python -m scripts.set_password --user-id user_0001 --login-name demo \\
        --password '...' --role admin

重复执行是安全的：登录名和密码被覆盖，角色只在显式给出 `--role` 时改动。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.identity.password import hash_password, normalise_login_name, validate_password
from app.storage.database import Database
from app.storage.models import AppUser, AuthCredential


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给账号设置登录名与密码")
    parser.add_argument("--user-id", default="user_0001", help="目标账号，默认 user_0001")
    parser.add_argument("--login-name", required=True, help="登录名（3-32 位字母数字下划线）")
    parser.add_argument("--password", required=True, help="密码（12-128 位）")
    parser.add_argument("--role", choices=("user", "admin"), default=None)
    args = parser.parse_args(argv)

    login_name = normalise_login_name(args.login_name)
    validate_password(args.password, login_name=login_name)
    now = datetime.now(timezone.utc)

    database = Database(get_settings().database_url)
    try:
        with database.sessions.begin() as db:
            user = db.scalar(select(AppUser).where(AppUser.user_id == args.user_id))
            if user is None:
                print(f"账号不存在: {args.user_id}", file=sys.stderr)
                return 2

            taken = db.scalar(
                select(AuthCredential).where(AuthCredential.login_name == login_name)
            )
            if taken is not None and taken.user_id != user.user_id:
                print(f"登录名已被 {taken.user_id} 占用: {login_name}", file=sys.stderr)
                return 3

            credential = db.scalar(
                select(AuthCredential).where(AuthCredential.user_id == user.user_id)
            )
            if credential is None:
                db.add(
                    AuthCredential(
                        user_id=user.user_id,
                        login_name=login_name,
                        password_hash=hash_password(args.password),
                        password_updated_at=now,
                        created_at=now,
                    )
                )
            else:
                credential.login_name = login_name
                credential.password_hash = hash_password(args.password)
                credential.password_updated_at = now

            if args.role:
                user.role = args.role
            user.updated_at = now

            print(
                f"已设置 user_id={user.user_id} login_name={login_name} role={user.role}",
            )
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
