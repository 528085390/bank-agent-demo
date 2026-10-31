"""理财的七个只读工具：声明、处理器和事实组织。

任务6 的工具白名单只有这七项，与需求 §6.3／计划 §3.4 的只读方法一一对应：产品列表、产品
详情、账户列表、持仓列表、持仓详情、最近测评、订单详情。**这里没有资金写工具**：申购、赎回
和按键核实由运行时的受控节点在后续任务里接入，不进入模型可选的白名单。

一个工具只打一个银行端点（工具契约 §3）。处理器只做三件事：把模型给的参数翻成 `WealthBank`
调用、把返回的 DTO 翻成公共 `ToolData`、把事实按资源归类。它不重算金额、不推算收益、不缓存
任何东西——银行是唯一数据源，第二次查询就是第二次 HTTP 调用。

同一份实现有两种调用形状：

- 任务6 的独立运行时已经持有 `WealthBank`，直接 `READ_TOOLS[name].run(bank, context, args)`；
- 主平台经 `ToolGateway` 调用时用 `public_handlers()`，形状是公共 `ToolHandler`（基座注入
  `BankClient`，处理器把它包成 `HttpWealthBank`）。两处没有第二套工具逻辑。

`check_arguments` / `check_facts` 是这一层的自检：模型给的参数要过工具自己声明的 schema，
处理器产出的事实要过自己声明的 `output_schema`。两者都只报路径与规则，不回报值——参数和事实
都可能带业务数据，不该被写进日志。

依赖方向：`infrastructure → contracts`（外加复用公共 `ToolSpec`／`ToolData`／`BankClient`）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, TypeVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.integration.bank_client import BankClient
from app.modules.base import ToolData, ToolFailure, ToolHandler
from app.modules.wealth.contracts.bank import (
    ProductFilters,
    WealthBank,
)
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import SideEffect, ToolInvocation, ToolSpec, TrustedContext

__all__ = [
    "ORDER_STATUS_LABELS",
    "READ_TOOLS",
    "REDEEM_LABELS",
    "RISK_LABELS",
    "ReadTool",
    "check_arguments",
    "check_facts",
    "extra_arguments",
    "public_handlers",
    "read_with_one_retry",
    "tool_menu",
    "yuan",
]

ReadHandler = Callable[[WealthBank, TrustedContext, Mapping[str, Any]], ToolData]

T = TypeVar("T")

RISK_LABELS: Mapping[str, str] = {"low": "低", "medium": "中", "high": "高"}
REDEEM_LABELS: Mapping[str, str] = {
    "flexible": "可灵活赎回",
    "maturity_only": "到期后可赎回",
}
ORDER_STATUS_LABELS: Mapping[str, str] = {
    "completed": "已完成",
    "pending": "处理中",
    "failed": "失败",
    "cancelled": "已取消",
}


@dataclass(frozen=True, slots=True)
class ReadTool:
    """一个只读工具：公共 `ToolSpec` + 它对应的 `WealthBank` 方法名 + 处理器。

    `method` 只是把工具与银行协议对齐（`READ_ONLY_METHODS`），运行时按它做白名单断言，
    不参与调用——调用永远走处理器，处理器里写死一个端点。
    """

    spec: ToolSpec
    method: str
    run: ReadHandler


def yuan(minor: int) -> str:
    """整数分的展示形式。用整除与取余，不引入浮点。"""

    sign = "-" if minor < 0 else ""
    amount = abs(int(minor))
    return f"{sign}{amount // 100}.{amount % 100:02d} 元"


def read_with_one_retry(operation: Callable[[], T]) -> T:
    """一次**只读**银行调用：明确可重试的失败最多再试一次（计划任务11）。

    只读没有副作用，所以重试是安全的；"最多一次"是硬边界，不是"重试到成功"。资金写**不**走这条路：
    写的重发由运行时按原幂等键核实，从不自动重放（`runtime._reconcile`）。

    只有 `ToolFailure.retryable` 为真的失败才重试——那是边界自己说的"这次失败值得再发一次"；
    业务拒绝（余额不足、状态非法、未绑定客户号）重试只会再收到同一个答案。

    独立运行时（`workflow/nodes.py`、`application/runtime.py`）走这里。主平台那条路
    （`public_handlers` → 公共 `ToolGateway`）**不**加这一层：基座的 `sdk.run_tool` 已经自带
    "瞬时失败重试一次"，两层叠加会让一次用户动作发出四次请求。
    """

    try:
        return operation()
    except ToolFailure as failure:
        if not failure.retryable:
            raise
    return operation()


def _label(table: Mapping[str, str], value: str) -> str:
    """把银行返回的枚举值翻成中文；不认识的取值原样显示，不猜。"""

    return table.get(value, value)


def _text(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _parameters(properties: Mapping[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    """工具参数 schema。

    `additionalProperties: False` 不是装饰：模型若把 `user_id`、`confirm` 之类的授权字段塞进
    `arguments`，Gateway 和这一层的校验都会以"多余参数"拒绝它，而不是让它跟着请求走。
    """

    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _facts_schema(**properties: Any) -> dict[str, Any]:
    """事实 schema：键是资源名，值是列表（或单值），键集合必须完全一致。"""

    return {
        "type": "object",
        "properties": dict(properties),
        "required": sorted(properties),
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------- 处理器


def _products(bank: WealthBank, context: TrustedContext, arguments: Mapping[str, Any]) -> ToolData:
    risk_level = _text(arguments, "risk_level")
    products = bank.list_products(
        context, ProductFilters(risk_level=risk_level) if risk_level else None
    )
    flexible = sum(1 for product in products if product.redeem_rule == "flexible")
    return ToolData(
        summary=f"共查到 {len(products)} 只产品，其中可灵活赎回 {flexible} 只。",
        display={},
        facts={"products": [asdict(product) for product in products]},
    )


def _product(bank: WealthBank, context: TrustedContext, arguments: Mapping[str, Any]) -> ToolData:
    product = bank.get_product(context, _text(arguments, "product_id") or "")
    return ToolData(
        summary=(
            f"{product.name}：{_label(RISK_LABELS, product.risk_level)}风险，"
            f"期限 {product.duration_months} 个月，起购 {yuan(product.min_amount_minor)}，"
            f"{_label(REDEEM_LABELS, product.redeem_rule)}。"
        ),
        display={},
        facts={"products": [asdict(product)]},
        business_ids={"product_id": product.product_id},
    )


def _accounts(bank: WealthBank, context: TrustedContext, arguments: Mapping[str, Any]) -> ToolData:
    del arguments
    accounts = bank.list_accounts(context)
    total = sum(account.balance_minor for account in accounts)
    return ToolData(
        summary=f"共 {len(accounts)} 个账户，余额合计 {yuan(total)}。",
        display={},
        facts={"accounts": [asdict(account) for account in accounts]},
    )


def _holdings(bank: WealthBank, context: TrustedContext, arguments: Mapping[str, Any]) -> ToolData:
    del arguments
    holdings = bank.list_holdings(context)
    value = sum(holding.current_value_minor for holding in holdings)
    return ToolData(
        summary=f"共 {len(holdings)} 笔持仓，当前价值合计 {yuan(value)}。",
        display={},
        facts={"holdings": [asdict(holding) for holding in holdings]},
    )


def _holding(bank: WealthBank, context: TrustedContext, arguments: Mapping[str, Any]) -> ToolData:
    holding = bank.get_holding(context, _text(arguments, "holding_id") or "")
    return ToolData(
        summary=(
            f"本金 {yuan(holding.principal_minor)}，当前价值 {yuan(holding.current_value_minor)}，"
            f"持有收益 {yuan(holding.accrued_interest_minor)}，"
            f"已实现收益 {yuan(holding.realized_income_minor)}，"
            f"可赎回 {yuan(holding.redeemable_minor)}。"
        ),
        display={},
        facts={"holdings": [asdict(holding)]},
        business_ids={"holding_id": holding.holding_id},
    )


def _risk_profile(
    bank: WealthBank, context: TrustedContext, arguments: Mapping[str, Any]
) -> ToolData:
    del arguments
    profile = bank.get_risk_profile(context)
    if profile is None:
        return ToolData(
            summary="银行还没有保存过风险测评。", display={}, facts={"risk_profile": None}
        )
    validity = "当前有效" if profile.valid else "已过期"
    return ToolData(
        summary=(
            f"最近一次测评等级为 {_label(RISK_LABELS, profile.risk_level)}风险"
            f"（{validity}，有效期至 {profile.expires_at}）。"
        ),
        display={},
        facts={"risk_profile": asdict(profile)},
        business_ids={"assessment_id": profile.assessment_id},
    )


def _order(bank: WealthBank, context: TrustedContext, arguments: Mapping[str, Any]) -> ToolData:
    order = bank.get_order(context, _text(arguments, "order_id") or "")
    return ToolData(
        summary=(
            f"{_label(ORDER_STATUS_LABELS, order.order_type)}订单状态为 "
            f"{_label(ORDER_STATUS_LABELS, order.status)}，金额 {yuan(order.amount_minor)}。"
        ),
        display={},
        facts={"orders": [asdict(order)]},
        business_ids={"order_id": order.order_id},
    )


def _read_tool(
    *,
    name: str,
    description: str,
    method: str,
    handler: ReadHandler,
    parameters: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> ReadTool:
    return ReadTool(
        spec=ToolSpec(
            name=name,
            description=description,
            parameters=dict(parameters),
            # 显式声明 READ：工具契约 §3 的填写要求，不依赖默认值。
            side_effect=SideEffect.READ,
            output_schema=dict(output_schema),
        ),
        method=method,
        run=handler,
    )


# ---------------------------------------------------------------------- 七个只读工具

_READ_TOOLS: Mapping[str, ReadTool] = {
    tool.spec.name: tool
    for tool in (
        _read_tool(
            name="wealth_products",
            description=(
                "查询理财产品列表，可按风险等级（low/medium/high）筛选。"
                "返回每只产品的期限、起购额、赎回规则、模拟年化参数和状态。"
            ),
            method="list_products",
            handler=_products,
            parameters=_parameters(
                {"risk_level": {"type": "string", "enum": ["low", "medium", "high"]}}
            ),
            output_schema=_facts_schema(products={"type": "array"}),
        ),
        _read_tool(
            name="wealth_product",
            description="查询一只理财产品的详情；product_id 必须来自产品列表的返回，不能编造。",
            method="get_product",
            handler=_product,
            parameters=_parameters(
                {"product_id": {"type": "string", "minLength": 1}}, ["product_id"]
            ),
            output_schema=_facts_schema(products={"type": "array"}),
        ),
        _read_tool(
            name="wealth_accounts",
            description="查询本人名下的银行账户及其余额。",
            method="list_accounts",
            handler=_accounts,
            parameters=_parameters({}),
            output_schema=_facts_schema(accounts={"type": "array"}),
        ),
        _read_tool(
            name="wealth_holdings",
            description=(
                "查询本人持有的理财产品持仓，返回本金、当前价值、持有收益、已实现收益和可赎回金额。"
                "不带筛选参数：银行返回本人全部持仓。"
            ),
            method="list_holdings",
            handler=_holdings,
            parameters=_parameters({}),
            output_schema=_facts_schema(holdings={"type": "array"}),
        ),
        _read_tool(
            name="wealth_holding",
            description="查询一笔持仓的详情；holding_id 必须来自持仓列表的返回，不能编造。",
            method="get_holding",
            handler=_holding,
            parameters=_parameters(
                {"holding_id": {"type": "string", "minLength": 1}}, ["holding_id"]
            ),
            output_schema=_facts_schema(holdings={"type": "array"}),
        ),
        _read_tool(
            name="wealth_risk_profile",
            description="查询本人在银行保存的最近一次风险测评及其有效性；没有测评时返回空。",
            method="get_risk_profile",
            handler=_risk_profile,
            parameters=_parameters({}),
            output_schema=_facts_schema(risk_profile={"type": ["object", "null"]}),
        ),
        _read_tool(
            name="wealth_order",
            description="查询一笔本人理财订单的执行结果；order_id 必须来自业务返回，不能编造。",
            method="get_order",
            handler=_order,
            parameters=_parameters({"order_id": {"type": "string", "minLength": 1}}, ["order_id"]),
            output_schema=_facts_schema(orders={"type": "array"}),
        ),
    )
}

# 只读白名单对外是**不可变**视图：没有一条代码路径能往这张表里加一个资金写工具，所以模型文本里
# 的"请调用 wealth.purchase" 无法把它变成一个真的可调用工具（计划任务11：注入不能改变工具
# 白名单）。换一套工具集合只能在测试里显式传自己的 `tools=`。
READ_TOOLS: Mapping[str, ReadTool] = MappingProxyType(dict(_READ_TOOLS))


def tool_menu(tools: Mapping[str, ReadTool] = READ_TOOLS) -> list[tuple[str, str]]:
    """工具菜单：`(工具名, 说明)`，交给公共 `describe_tools` 渲染。"""

    return [(name, tool.spec.description) for name, tool in tools.items()]


def public_handlers(tools: Mapping[str, ReadTool] = READ_TOOLS) -> Mapping[str, ToolHandler]:
    """同一批处理器的公共形状：基座注入 `BankClient`，处理器把它包成 `HttpWealthBank`。

    独立运行时不走这条路径（它已经持有 `WealthBank`），但主平台注册这些工具时用的是
    公共 `ToolHandler` 签名，这里把两者接上，避免出现第二套工具实现。
    """

    def bind(tool: ReadTool) -> ToolHandler:
        def handler(invocation: ToolInvocation, client: BankClient) -> ToolData:
            return tool.run(HttpWealthBank(client), invocation.context, invocation.call.arguments)

        return handler

    return {name: bind(tool) for name, tool in tools.items()}


# ---------------------------------------------------------------------- 自检


_VALIDATORS: dict[tuple[str, str], Draft202012Validator] = {}


def _validator(name: str, kind: str, schema: Mapping[str, Any]) -> Draft202012Validator:
    key = (name, kind)
    validator = _VALIDATORS.get(key)
    if validator is None:
        try:
            validator = Draft202012Validator(dict(schema))
        except SchemaError as error:
            # 声明本身写错是模块作者的 bug，不是模型的错：在构造点抛，不放行。
            raise ValueError(f"tool {name} declares an invalid {kind} schema: {error}") from error
        _VALIDATORS[key] = validator
    return validator


def _complaint(
    name: str, kind: str, schema: Mapping[str, Any], instance: Mapping[str, Any], *, prefix: str
) -> str | None:
    """第一条不满足的约束，形如 `argument product_id: failed minLength`。

    只说路径与规则，不回报值：`jsonschema` 自己的 message 会带上出错值的 repr，参数与事实
    都可能含业务数据，所以一律不用它。
    """

    errors = sorted(
        _validator(name, kind, schema).iter_errors(dict(instance)),
        key=lambda error: list(error.path),
    )
    if not errors:
        return None
    first = errors[0]
    location = ".".join(str(item) for item in first.path) or "(root)"
    return f"{prefix} {location}: failed {first.validator}"


def check_arguments(tool: ReadTool, arguments: Mapping[str, Any]) -> str | None:
    """模型给的参数是否满足工具声明的 `parameters`；返回第一条问题或 None。"""

    return _complaint(
        tool.spec.name, "parameters", tool.spec.parameters, arguments, prefix="argument"
    )


def extra_arguments(tool: ReadTool, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """模型给的参数里，工具**没有声明过**的字段名（按字母序）。

    授权字段只能从这里进来：`user_id`、`confirm`、`mfa_code`、`idempotent_key` 都不在任何一个
    只读工具的 schema 里，所以模型若想顺手带上身份或确认标志，它只会以"多余参数"的形态出现。
    运行时据此把它算作一次**绕过门禁**的尝试，而不是一次普通的参数格式错误（计划任务11）。

    这里只回报字段名，不回报取值：参数可能带业务数据，不该被写进日志。
    """

    declared = set((tool.spec.parameters or {}).get("properties") or {})
    return tuple(sorted(str(name) for name in arguments if str(name) not in declared))


def check_facts(tool: ReadTool, facts: Mapping[str, Any]) -> str | None:
    """处理器产出的事实是否满足工具自己声明的 `output_schema`；返回第一条问题或 None。"""

    if tool.spec.output_schema is None:
        return None
    return _complaint(tool.spec.name, "output", tool.spec.output_schema, facts, prefix="facts")
