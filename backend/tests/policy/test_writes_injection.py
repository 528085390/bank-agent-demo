"""写门面由组合根注入：转账与理财拿到的是**同一把** `WriteFacade`。

三条钉子：

1. `Runtime` 自己造一把 `WriteFacade`（`rt.writes`）。它和 `bank_client` 一样属于进程，
   不属于任何一个模块；
2. 每个模块自己声明 binder（`bind_transfer` / `bind_wealth`），它们经 `base.bind_writes` 给
   `graph_builder` 包一层 `functools.partial`，把**同一把** facade 绑进去；组合根
   （`production_modules`）只负责把 `ModuleDeps` 交给 binder，不认识任何一个域。基座
   （`agents/supervisor/graph.py:428-430`）只按关键字调用 builder，partial 因此直接生效；
3. `writes` 是**必填**依赖：漏传是 `TypeError`，而不是每个模块悄悄各造一把 facade —— 那样
   审计钩子与门面策略会有第二份，从注册表那一侧完全看不出来。
"""

from __future__ import annotations

import functools

import pytest

from app.integration.write_facade import WriteFacade
from app.modules import production_modules
from app.modules.registry import build_registry

#: 会真的发一笔银行写的两个域（`POST /transfers`、理财申购／赎回）。
WRITE_AGENTS = ("transfer", "wealth")


def test_the_runtime_owns_the_write_facade(rt):
    # 门面与 bank_client 同源：一个进程一把，审计钩子与策略因此也只有一份。
    assert isinstance(rt.writes, WriteFacade)


def test_the_composition_root_binds_the_same_facade_into_transfer_and_wealth(rt):
    registry = build_registry(
        production_modules(
            notification_service=rt.notification,
            database=rt.database,
            writes=rt.writes,
        )
    )
    builders = {
        module.name: module.declaration.graph_builder
        for module in registry.modules
        if module.declaration is not None
    }
    for name in WRITE_AGENTS:
        builder = builders[name]
        assert isinstance(builder, functools.partial), f"{name} 的图构建器没有被包一层"
        assert builder.keywords["writes"] is rt.writes, f"{name} 拿到的不是组合根那把门面"


def test_writes_is_a_required_dependency():
    # 必填在签名上：漏传在进入函数体之前就是 TypeError，不会退化成「各模块自己造一把」。
    with pytest.raises(TypeError):
        production_modules(notification_service=object(), database=object())
