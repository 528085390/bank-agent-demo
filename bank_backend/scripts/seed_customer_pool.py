"""生成注册用的客户池：`u_pool_01` … `u_pool_NN`。

什么时候要跑：

* 首次部署，在 `seed_wealth_demo` / `seed_subscription_demo` 之后跑一次。
* 银行库被 `POST /api/seed/reset` 清过之后。reset 会删掉所有客户，包括已经被注册用户领走
  的那些，而 app 库里的领取记录还在——所以恢复方式是**再跑一次这个脚本**，它会把 1..N
  的客户全部补回来（已存在的跳过，不覆盖任何余额），用户那边的绑定因此自动恢复。

    cd bank_backend
    uv run python -m scripts.seed_customer_pool --count 20

脚本不清理任何数据，也不在服务启动时自动执行。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.domain.pool_service import provision_pool  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.models import SimulationState  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成注册客户池（幂等）")
    parser.add_argument("--count", type=int, default=20, help="池子大小，默认 20")
    parser.add_argument("--database-url", default=None, help="默认取配置的 DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写数据")
    args = parser.parse_args(argv)

    if args.count <= 0:
        print("--count 必须为正数", file=sys.stderr)
        return 2

    database = Database(args.database_url or get_settings().database_url)
    try:
        with database.sessions() as session:
            state = session.get(SimulationState, 1)
            now = state.current_time if state else datetime.now(timezone.utc)
            if state is None:
                # 客户数据必须落在模拟时钟里，否则开户流水会跑到演示时间线之外，账单按
                # 月份聚合时会看不见它们。
                print(
                    "模拟状态未初始化：先执行一次 POST /api/seed/reset 再跑本脚本",
                    file=sys.stderr,
                )
                return 3

        if args.dry_run:
            from app.domain.pool_service import pool_customer_id
            from app.storage.models import User

            with database.sessions() as session:
                present = [
                    pool_customer_id(index)
                    for index in range(1, args.count + 1)
                    if session.get(User, pool_customer_id(index)) is not None
                ]
            print(f"已存在 {len(present)}/{args.count}：{present}")
            return 0

        with database.sessions.begin() as session:
            result = provision_pool(session, size=args.count, now=now)
        print(
            f"新建 {len(result['created'])} 个，已存在 {len(result['existing'])} 个，池子大小 {args.count}"
        )
        if result["created"]:
            print("新建：", ", ".join(result["created"]))
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
