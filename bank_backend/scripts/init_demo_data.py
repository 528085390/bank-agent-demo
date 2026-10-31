"""首次初始化银行演示数据：基础种子 → 理财 → 订阅 → 注册客户池。

为什么需要这个脚本
------------------
一键启动（`start.ps1`）在全新库上会卡死在一个死结里：

* `GET /api/health` 要求 `simulation_state` 存在，全新库上必然 503，健康检查永远等不到；
* 补种子的 `POST /api/seed/reset` 会清空整库，所以 `README.md` 规定它只能由开发者显式执行。

本脚本把「判断」和「初始化」收在一处，并且**只在库为空时**动手：先读 `simulation_state`，
有行就直接跳过，没有才按顺序建种子。这样既解开了死结，又不会碰任何已有数据。

三种用法
--------

    cd bank_backend
    uv run python -m scripts.init_demo_data --check   # 只判断，不写数据
    uv run python -m scripts.init_demo_data           # 空库才初始化
    uv run python -m scripts.init_demo_data --pool-size 40

退出码：0 = 已初始化或初始化成功；3 = 空库（`--check` 时表示「需要初始化」）；1 = 失败。

与 `deploy/deploy.sh` 的关系：那边走 HTTP（`POST /api/seed/reset` + 三个脚本），
因为容器里的服务已经起来了；这里走进程内调用，因为一键启动时服务还没拉起来。
两条路径的最终数据完全一致。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    # 允许 `python scripts/init_demo_data.py` 直接运行。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.domain.seed_service import SeedService  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.models import SimulationState  # noqa: E402
from scripts import seed_customer_pool, seed_subscription_demo, seed_wealth_demo  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_EMPTY = 3


def is_initialised(database: Database) -> bool:
    """模拟状态是否存在——这就是「库是否为空」的唯一判据。"""
    with database.sessions() as session:
        return session.get(SimulationState, 1) is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="首次初始化银行演示数据（只补空库）")
    parser.add_argument("--check", action="store_true", help="只判断状态，不写数据")
    parser.add_argument("--database-url", default=None, help="默认取配置的 DATABASE_URL")
    parser.add_argument("--pool-size", type=int, default=20, help="注册客户池大小，默认 20")
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().database_url
    database = Database(url)
    try:
        initialised = is_initialised(database)
    finally:
        database.dispose()

    if initialised:
        print("模拟状态已存在")
        return EXIT_OK
    if args.check:
        print("空库：需要初始化")
        return EXIT_EMPTY

    # 顺序不能换：理财/订阅/客户池都要求模拟时钟已存在。
    print("1/4 基础种子 + 模拟时钟")
    SeedService(Database(url)).reset()

    # 下面三个脚本各自会新建 Database 实例并 dispose，这里不共享连接池：
    # 它们之间必须按顺序提交，共用连接反而容易踩到未提交事务。
    for label, runner in (
        ("2/4 理财演示数据", seed_wealth_demo.main),
        ("3/4 订阅演示数据（u_wealth）", seed_subscription_demo.main),
        ("4/4 注册客户池", seed_customer_pool.main),
    ):
        print(label)
        if label.startswith("4/4"):
            code = runner(["--count", str(args.pool_size), "--database-url", url])
        else:
            code = runner(["--database-url", url])
        if code != EXIT_OK:
            print(f"{label} 失败（退出码 {code}）", file=sys.stderr)
            return EXIT_FAILED

    print("演示数据初始化完成")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
