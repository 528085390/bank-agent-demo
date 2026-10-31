"""理财包的模块接缝：源码里不许出现前端／主平台运行时／Supervisor 自己的入口。

A15（tests/wealth_integration/test_acceptance.py）也断言过这件事，但那个模块被一个 autouse 的
fresh_demo_state 夹具把关：缺 WEALTH_TEST_BANK_SCHEMA／WEALTH_TEST_BANK_DATABASE_URL 就整模块跳过。
也就是说那两行断言在普通门里根本不会执行——守卫是休眠的。这条守卫必须活在**不依赖银行、不依赖
数据库、不联网**的单元测试里，否则它保护不了任何东西。
"""

from __future__ import annotations

from pathlib import Path

import app.modules.wealth as wealth_package

FORBIDDEN = ("app.main", "app.agents.runtime", "fastapi")
SUPERVISOR_ENTRIES = ("build_supervisor", "supervisor_node", "supervisor_user_turn")

# 期望值故意钉死成恰好这一个文件：领域图从基座公开面（app.agents.sdk）借节点助手
# （conclude_turn／awaiting／call_tool／wait_for_user），card 也是这样接的
# （app/modules/card/workflow.py:15）。把 workflow/graph.py 改名或拆成多个文件，都必须回来改
# 这一行——这正是这条守卫要盯住的东西，不要把它放宽成"至少一个"。
EXPECTED_SDK_IMPORTERS = ["workflow/graph.py"]


def wealth_sources() -> list[tuple[str, str]]:
    """理财包里每一个源文件：(相对路径, 源码)。"""

    root = Path(wealth_package.__file__).resolve().parent
    return [
        (path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
    ]


def test_the_wealth_package_reaches_the_base_only_through_the_domain_graph() -> None:
    """平台应用、平台运行时与 Web 框架一律禁止；基座 SDK 只许"借助手"。

    放宽只放宽到借助手这一层：对 app.agents.sdk 的引用必须恰好只有领域图一处，而且不能出现
    Supervisor 自己的装配／决策入口——那才是"独立 Agent"的反面。
    """

    sources = wealth_sources()
    paths = [relative for relative, _ in sources]
    assert "workflow/graph.py" in paths, (
        f"源码扫描没有覆盖到领域图，守卫会变成空转：扫到的文件是 {paths}"
    )

    offenders: list[str] = []
    sdk_importers: list[str] = []
    for relative, source in sources:
        if "app.agents.sdk" in source:
            sdk_importers.append(relative)
        for line in source.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            if any(name in stripped for name in FORBIDDEN):
                offenders.append(f"{relative}: {stripped}")
            if any(name in stripped for name in SUPERVISOR_ENTRIES):
                offenders.append(f"{relative}: 不应装配 Supervisor 自己的入口：{stripped}")

    assert offenders == [], f"理财包不应依赖前端／主平台运行时／Web 框架：{offenders}"
    assert sdk_importers == EXPECTED_SDK_IMPORTERS, (
        "理财包只允许领域图从基座公开面借节点助手（conclude_turn／awaiting／call_tool／wait_for_user），"
        f"实际引用 app.agents.sdk 的文件是 {sdk_importers}"
    )
