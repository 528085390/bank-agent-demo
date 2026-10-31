"""受控的财富演示数据初始化入口。

只对**明确指定的**数据库执行，不做任何清理，也不在服务启动时自动运行：

    cd bank_backend
    uv run python -m scripts.seed_wealth_demo --dry-run
    uv run python -m scripts.seed_wealth_demo

默认使用配置的 `DATABASE_URL`（本工程主库 `bank_db`），可用 `--database-url` 覆盖。
脚本要求模拟时钟已存在（先执行一次 `POST /api/seed/reset` 或 `uv run alembic upgrade head`
后由开发者在受控演示库初始化种子）。重复执行不会重复入金：已有完整演示数据时校验并
复用，只存在一部分时明确拒绝。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    # 允许 `python scripts/seed_wealth_demo.py` 直接运行。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import BankError  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.models import SimulationState  # noqa: E402
from app.storage.wealth_seed import apply_wealth_seed, demo_status  # noqa: E402


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写入财富演示数据（显式、可重复执行）")
    parser.add_argument("--database-url", default=None, help="默认取配置的 DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="只查看状态，不写数据")
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
        with database.sessions() as session:
            # session.get() 会自动开启事务，因此读写都在同一个显式事务里，
            # 不再嵌套 session.begin()。
            with session.begin():
                state = session.get(SimulationState, 1)
                if state is None:
                    print(
                        "模拟时钟未初始化：请先在本库执行一次 POST /api/seed/reset"
                        "（或由开发者按数据库设计文档初始化基础种子）"
                    )
                    return 3
                if args.dry_run:
                    print(json.dumps({"status": demo_status(session)}, ensure_ascii=False))
                    return 0
                result = apply_wealth_seed(session, now=state.current_time)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except BankError as exc:
        print(f"初始化失败：{exc.reason}")
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
