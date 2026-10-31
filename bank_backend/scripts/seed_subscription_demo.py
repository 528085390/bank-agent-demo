"""受控的订阅演示用户初始化入口（前端演示会话的银行客户）。

为什么需要它：backend 的演示会话固定为 `user_0001`，它绑定的银行客户是 `u_wealth`；
而 bank_backend 自带的种子只给 `u_zhang` 造了订阅。本脚本在受控演示库里补一个
`u_wealth`（一个客户 + 一个活期账户 + 两条订阅），前端登录后才能看到"我的订阅"。

只对**明确指定的**数据库执行，不做整体清理，也不在服务启动时自动运行：

    cd bank_backend
    uv run python -m scripts.seed_subscription_demo --dry-run
    uv run python -m scripts.seed_subscription_demo

默认使用配置的 `DATABASE_URL`（本工程主库 `bank_db`），可用 `--database-url` 覆盖。
`POST /api/seed/reset` 会清空整库，所以每次重置后重跑本脚本即可；`--reset` 只删除本
脚本造的演示客户订阅（取消是终态，演示几轮后想从头再来时用它可免于整库重置）。
重复执行不会重复插入：已存在的客户／账户／订阅会跳过。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    # 允许 `python scripts/seed_subscription_demo.py` 直接运行。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlalchemy as sa  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.storage.database import Database  # noqa: E402

CUSTOMER = "u_wealth"
ACCOUNT = "a_wealth_current"
SUBSCRIPTIONS = (
    # (订阅 id, 商户名, 金额（分）, 开始日, 下次扣款日)
    ("sub_wealth_netflix", "Netflix", 5900, "2026-01-10", "2026-10-10"),
    ("sub_wealth_spotify", "Spotify", 2900, "2026-02-01", "2026-10-01"),
)


def mask_url(url: str) -> str:
    """输出目标时隐藏密码。"""

    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def database_name(url: str) -> str:
    tail = url.rsplit("/", 1)[-1]
    return tail.split("?", 1)[0]


def _exists(conn, table: str, key: str) -> bool:
    return (
        conn.execute(sa.text(f"select 1 from {table} where id=:i"), {"i": key}).fetchone()
        is not None
    )


def demo_status(conn) -> dict:
    """本脚本关心的三类数据是否已存在，供 --dry-run 查看。"""

    return {
        "customer": _exists(conn, "users", CUSTOMER),
        "account": _exists(conn, "accounts", ACCOUNT),
        "subscriptions": {
            sub_id: _exists(conn, "subscriptions", sub_id) for sub_id, *_ in SUBSCRIPTIONS
        },
    }


def apply_demo_customer(conn, *, reset: bool) -> dict:
    if reset:
        conn.execute(sa.text("delete from subscriptions where user_id=:u"), {"u": CUSTOMER})
        print("已清空演示客户的订阅")
    created: list[str] = []
    if not _exists(conn, "users", CUSTOMER):
        conn.execute(
            sa.text(
                """insert into users (id, name, phone, email, birthday, risk_level, status, created_at)
                   values (:i, '演示用户', '13900139000', null, null, 'medium', 'active', now())"""
            ),
            {"i": CUSTOMER},
        )
        created.append(f"客户 {CUSTOMER}")
    if not _exists(conn, "accounts", ACCOUNT):
        conn.execute(
            sa.text(
                """insert into accounts
                   (id, user_id, account_type, currency, balance_minor, available_minor,
                    interest_rate, status, created_at)
                   values (:i, :u, 'current', 'CNY', 1250050, 1250050, 0.003, 'active', now())"""
            ),
            {"i": ACCOUNT, "u": CUSTOMER},
        )
        created.append(f"账户 {ACCOUNT}")
    for sub_id, name, amount, start, due in SUBSCRIPTIONS:
        if _exists(conn, "subscriptions", sub_id):
            print(f"订阅已存在，跳过：{name}")
            continue
        conn.execute(
            sa.text(
                """insert into subscriptions
                   (id, user_id, from_account_id, name, merchant_name, amount_minor, frequency,
                    start_date, next_due_date, status, auto_renew, last_charge_id, created_at)
                   values (:i, :u, :a, :n, :n, :amt, 'monthly', :s, :d, 'active', true, null, now())"""
            ),
            {
                "i": sub_id,
                "u": CUSTOMER,
                "a": ACCOUNT,
                "n": name,
                "amt": amount,
                "s": start,
                "d": due,
            },
        )
        created.append(f"订阅 {name}")
    return {"customer": CUSTOMER, "created": created, "status": demo_status(conn)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写入订阅演示用户数据（显式、可重复执行）")
    parser.add_argument("--database-url", default=None, help="默认取配置的 DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="只查看状态，不写数据")
    parser.add_argument("--reset", action="store_true", help="先删掉演示客户的订阅再重建")
    parser.add_argument(
        "--allow-test-database",
        action="store_true",
        help="允许对名字以 test 结尾的库执行（默认拒绝，避免污染共享测试库）",
    )
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().database_url
    name = database_name(url)
    print(f"目标数据库: {mask_url(url)}")
    if name.endswith("test") and not args.allow_test_database:
        print("拒绝执行：目标库名以 test 结尾，如确需初始化请显式加 --allow-test-database")
        return 2

    database = Database(url)
    try:
        with database.engine.begin() as conn:
            if args.dry_run:
                print(json.dumps({"status": demo_status(conn)}, ensure_ascii=False))
                return 0
            result = apply_demo_customer(conn, reset=args.reset)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
