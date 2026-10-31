"""Domain modules: one package per domain Agent.

One line per module. Adding a domain is adding a package and a line here; nothing else in
the application changes.
"""

from __future__ import annotations

from app.modules.base import ModuleDeps, ModuleSpec
from app.modules.bill import MODULE as BILL
from app.modules.card import MODULE as CARD
from app.modules.life_event import MODULE as LIFE_EVENT
from app.modules.subscription import MODULE as SUBSCRIPTION
from app.modules.transfer import MODULE as TRANSFER
from app.modules.wealth import MODULE as WEALTH

ALL_MODULES: tuple[ModuleSpec, ...] = (
    TRANSFER,  # 智能转账已完成并接入
    BILL,
    WEALTH,
    CARD,
    SUBSCRIPTION,
    LIFE_EVENT,
)


def production_modules(*, notification_service, database, writes) -> tuple[ModuleSpec, ...]:
    """把平台服务交给需要它们的模块，其余原样返回。

    装配放在组合根，而不是让 `build_registry` 自己去猜：注册表只认识 `ModuleSpec`，
    而数据库句柄、`NotificationService` 与写门面是运行时才有的东西（见 `app/runtime.py`
    的 `Runtime.__init__`，它调用本函数后把结果交给 `build_registry`）。

    这里只做三件事：校验依赖、把三个服务装进一把 `ModuleDeps`、让每个模块用自己的 binder
    装配自己（`ModuleSpec.binder`，见 `modules/base.py`）。所以「某个域要一样新依赖」是给
    那个模块写一个 binder，而不是在本函数里再加一条分支 —— 组合根不认识任何一个域。

    三个依赖都是**必填**的：默认成 `None` 会让「忘了传」悄悄退化成「账单工具不存在」，
    而账单模块没被装配起来，从注册表那一侧完全看不出来 —— 生产环境里就是一堆工具凭空
    消失，却没有任何报错。写门面同理：漏了它，转账与理财会各自造一把没有审计钩子的
    facade，钱照写，账照缺。宁可在这里炸，也不要那种静默降级。
    """
    if notification_service is None or database is None or writes is None:
        raise RuntimeError("账单模块需要数据库与通知服务，转账与理财需要写门面；组合根必须显式注入")

    deps = ModuleDeps(notification_service=notification_service, database=database, writes=writes)
    return tuple(module.binder(module, deps) if module.binder else module for module in ALL_MODULES)


__all__ = ["ALL_MODULES", "ModuleSpec", "production_modules"]
