"""任务5 的“公共能力接入清单”：理财接在现有公共层上，不另起一套框架。

对照需求 §6.1.1 与计划 §4.2.1 逐项确认，并且是用**真实的公共实现**确认，不是用替身假装：

1. `wealth.MODULE` 进入现有注册表，`build_registry()` 收集得到它（没有第二套全局注册表）。
2. 理财工具经 `ToolGateway` / `AgentToolHandle` 调用时，拿到的是基座注入的绑定
   `TrustedContext`；模型在 `arguments` 里塞 `user_id` 覆盖不了它。
3. 工具处理器返回公共 `ToolData`，由公共 `BankToolAdapter` 变成 `ToolResult` 信封。
4. 银行调用只经过现有 `BankClient`（资金写经 `WriteFacade`）：`HttpWealthBank` 的状态只有
   注入的客户端与由它派生的写门面，没有自己的传输层、缓存或账户字典。
5. 公共类型是同一批对象，理财包不重新定义 `ToolSpec`／`TrustedContext`／`BankClient`／
   `ModelClient` 之类的同名类型。

第 2、3 条用一个探针工具验证：理财的只读工具要到任务6 才由 `infrastructure/tools.py`
声明，但“理财模块声明的工具能否拿到绑定身份”这条边界现在就必须接通。探针只存在于本文件，
正式代码里没有探针。
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from app.integration.bank_client import BankClient
from app.integration.write_facade import WriteFacade
from app.modules import ALL_MODULES
from app.modules.base import ModuleSpec, ToolData, ToolFailure
from app.modules.registry import build_registry
from app.modules.wealth import WEALTH
from app.modules.wealth.contracts.bank import WealthBankError
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools import protocol
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ErrorCode, ToolInvocation, ToolResult, ToolSpec, ToolStatus
from tests.wealth import support

PROBE = ToolSpec(
    name="wealth_probe_product",
    description="任务5 探针：确认理财工具经公共网关拿到绑定身份",
    parameters={"type": "object", "properties": {"product_id": {"type": "string"}}},
)

# §6.1.1 要求复用的公共类型：理财包若绑定了同名对象，必须就是这同一个。
SHARED_TYPES = (
    "ModuleSpec",
    "ToolData",
    "ToolFailure",
    "ToolSpec",
    "ToolInvocation",
    "ToolResult",
    "TrustedContext",
    "ErrorCode",
    "BankClient",
    "ModelClient",
)


# ---------------------------------------------------------------- 1. 注册表


def test_the_existing_registry_collects_the_wealth_module():
    registry = build_registry()

    assert any(module.name == "wealth" for module in registry.modules)
    assert WEALTH in registry.roster
    assert WEALTH in [module.declaration for module in ALL_MODULES]


def test_the_wealth_agent_gets_its_handle_from_the_shared_gateway(double):
    """句柄由公共网关构造并带上调用方身份；理财模块自己不构造句柄。"""

    handle = _gateway_with_probe(_product_handler(), double).handle(
        "wealth", support.bound_context()
    )

    assert handle.agent_id == "wealth"
    assert handle.trusted_context.bank_customer_id == support.DEMO_CUSTOMER
    assert handle.tool_names == frozenset({PROBE.name})


# ------------------------------------------------- 2/3. 网关 + 公共结果信封


def test_a_wealth_tool_gets_the_bound_context_and_returns_common_tool_data(double):
    double.on("GET", "/products/p_demo_cash", data=support.product_payload())

    result = _call_probe(_product_handler(), double)

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == {
        "summary": "已读取 演示灵活理财",
        "display": {"name": "演示灵活理财"},
        "facts": {"product_id": "p_demo_cash"},
        "business_ids": {"product_id": "p_demo_cash"},
    }
    # 处理器拿到的身份来自基座注入，银行调用也带着它。
    assert double.only_call().query == {"user_id": support.DEMO_CUSTOMER}


def test_a_model_supplied_customer_cannot_override_the_bound_one(double):
    """模型写进 `arguments` 的 user_id 只是数据：上下文与出网请求都用绑定的客户。"""

    double.on("GET", "/products/p_demo_cash", data=support.product_payload())

    result = _call_probe(
        _product_handler(),
        double,
        arguments={"product_id": "p_demo_cash", "user_id": "u_li", "confirm": True},
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data["facts"]["product_id"] == "p_demo_cash"
    assert double.only_call().query == {"user_id": support.DEMO_CUSTOMER}


# ------------------------------------------------- 4. 银行客户端复用


def test_the_adapter_holds_only_the_injected_bank_client_and_write_facade(bank):
    """没有自己的传输层、没有账户／持仓缓存：全部状态就是注入的 `BankClient` 与写门面。

    写门面（A4）由 `BankClient` 派生，不是第二份状态；若将来给适配器加上传输层或账户／持仓
    缓存，多出来的属性会让这条精确断言失败。
    """

    assert set(vars(bank)) == {"_client", "_writes"}
    assert isinstance(vars(bank)["_client"], BankClient)
    assert isinstance(vars(bank)["_writes"], WriteFacade)


# ------------------------------------------------- 5. 公共类型与结果约定


def test_a_wealth_failure_is_a_public_tool_failure(double):
    """适配器的失败就是公共 `ToolFailure`：公共适配器能直接把它渲染成失败结果。

    理财侧不写翻译层、不发明第二套错误语义——错误码是公共 `ErrorCode`，可重试性由
    `ToolFailure.retryable` 表达；只有“写请求结果未知”是理财特有的补充信息。
    """

    double.on_failure("GET", "/products/p_demo_cash", httpx.ConnectError("refused"))

    result = _call_probe(_product_handler(), double)

    assert result.status is ToolStatus.FAILED
    assert result.error_code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert result.retryable is True
    assert result.error == "银行服务暂时无法连接，请稍后重试。"


def test_a_wealth_bank_error_is_the_shared_failure_type(double):
    double.on_failure("GET", "/holdings", httpx.ReadTimeout("slow"))

    with pytest.raises(ToolFailure) as caught:
        HttpWealthBank(double.client()).list_holdings(support.bound_context())

    assert isinstance(caught.value, WealthBankError)
    assert caught.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert caught.value.retryable is True
    assert caught.value.outcome_unknown is False


def test_the_wealth_package_reuses_the_shared_types_instead_of_redefining_them():
    checked = 0
    for module in _wealth_modules():
        for name in SHARED_TYPES:
            bound = vars(module).get(name)
            if bound is None:
                continue
            checked += 1
            assert bound is _shared_type(name), (
                f"{module.__name__} 绑定了自己的 {name}，应当复用公共定义"
            )

    # 自证不是空循环：理财包确实绑定了公共类型（否则这条断言什么也没检查）。
    assert checked > 0, "理财包一个公共类型都没绑定？这条断言已经失去意义"


def test_every_shared_name_has_a_public_definition():
    """上面那条断言要真的在检查东西：这些公共名字都必须能解析到定义。"""

    for name in SHARED_TYPES:
        assert _shared_type(name) is not None, name


# ---------------------------------------------------------------- 工具


def _gateway_with_probe(handler, double) -> ToolGateway:
    """一个只含理财探针工具的模块，走真实的注册表、目录、网关和适配器。"""

    module = ModuleSpec(
        name="wealth",
        declaration=WEALTH,
        tools={PROBE.name: PROBE},
        handlers={PROBE.name: handler},
    )
    registry = build_registry([module])
    return ToolGateway(registry.catalog, BankToolAdapter(registry, double.client()))


def _call_probe(handler, double, *, arguments: Mapping[str, Any] | None = None) -> ToolResult:
    handle = _gateway_with_probe(handler, double).handle("wealth", support.bound_context())
    return handle.call(
        {
            "tool": PROBE.name,
            "arguments": dict(arguments or {"product_id": "p_demo_cash"}),
            "call_id": "c1",
        }
    )


def _product_handler():
    """一个理财只读工具该有的样子：公共适配器注入 `BankClient`，理财适配器包住它读银行。"""

    def handler(invocation: ToolInvocation, client: BankClient) -> ToolData:
        product = HttpWealthBank(client).get_product(invocation.context, "p_demo_cash")
        return ToolData(
            summary=f"已读取 {product.name}",
            display={"name": product.name},
            facts={"product_id": product.product_id},
            business_ids={"product_id": product.product_id},
        )

    return handler


def _wealth_modules():
    package = importlib.import_module("app.modules.wealth")
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        yield importlib.import_module(info.name)


def _shared_type(name: str):
    for source in (
        protocol,
        importlib.import_module("app.modules.base"),
        importlib.import_module("app.integration.bank_client"),
        importlib.import_module("app.integration.model_client"),
    ):
        if hasattr(source, name):
            return getattr(source, name)
    return None
