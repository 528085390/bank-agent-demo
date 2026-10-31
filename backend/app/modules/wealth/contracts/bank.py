"""理财 Agent 与银行之间的契约：协议、意图、回执和 DTO。

三条硬边界写在这里，测试也照着它们断言：

1. **授权字段不属于模型输入。** 交易意图只带业务槽位，身份、确认、验证码和幂等键由
   运行时的专用方法参数传入。意图是 frozen + slots 的 dataclass，模型给的多余字段会直接
   `TypeError`，无法“顺手”带上 `user_id`、`confirm`、`mfa_code` 或幂等键。
2. **`order_id` 与 `transaction_id` 不混用。** 前者是业务订单号，后者是资金流水号；两者
   在 DTO 里各有一个字段，谁都不顶替谁。
3. **`expected_return` 是演示年化参数，不是已赚收益。** 持有收益与已实现收益分别来自银行
   的 `accrued_interest_minor` 与 `realized_income_minor`；DTO 不把演示参数算成收益。

`from_bank` 只做字段搬运：银行的 data 结构与这里的 DTO 一一对应，缺字段是协议错误，
由适配器翻译成 `WealthBankError(PROTOCOL)`，而不是补一个默认值假装成功。

公共类型一律复用（需求 §6.1.1）：身份来自 `app.tools.protocol`，失败结果沿用
`app.modules.base.ToolFailure`，银行传输沿用 `app.integration.BankClient`。这一层只定义
理财领域自己的内容，不重新定义同名类型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.modules.base import ToolFailure
from app.tools.protocol import TrustedContext

__all__ = [
    "CONFIRMATION_TRANSPORT_UNSUPPORTED",
    "FLOW_ONLY_METHODS",
    "PURCHASE_OPERATION",
    "READ_ONLY_METHODS",
    "REDEEM_OPERATION",
    "RUNTIME_ONLY_METHODS",
    "RUNTIME_OWNED_FIELDS",
    "WEALTH_OPERATION_TYPES",
    "Account",
    "Holding",
    "IdempotencyLookup",
    "Product",
    "ProductFilters",
    "PurchaseIntent",
    "PurchaseReceipt",
    "PurchaseSnapshot",
    "RedeemIntent",
    "RedeemSnapshot",
    "RedemptionReceipt",
    "RiskProfile",
    "WealthBank",
    "WealthBankError",
    "WealthOrder",
    "WriteAuthorization",
]

# 结果码：这一次确认**没有**变成一笔资金请求，因为当前通道根本走不通。它不是失败原因不明的
# 错误，而是明确的集成阻塞（交接文档第 6 节 D2 的形态之一）。接入方提供可用能力后不再出现。
CONFIRMATION_TRANSPORT_UNSUPPORTED = "confirmation_transport_unsupported"


class WriteAuthorization(StrEnum):
    """一次资金写**怎么才算被授权**：银行侧的能力声明，不是调用方的偏好。

    它是"确认框长什么样"和"提交时带什么"这**同一个答案**的来源。两件事必须由它一起驱动：
    如果卡片不问验证码而银行要，用户点了也提交不了；如果卡片问了而银行不要，用户就在为一个
    不存在的门禁输入秘密。所以两个判断点都只查这一个值，不各自猜。

    它**不是**策略开关，也不表达"要不要用户确认"——确认永远是必须的（`confirm=true` 由适配器
    固定发出）。它只回答"确认之外还缺什么"。
    """

    CLICK = "click"
    """一次可信点击本身就是授权：提交时不带验证码，确认框上也没有验证码输入。"""

    CLICK_AND_CODE = "click_and_code"
    """点击之外还要用户当场输入的验证码：确认框必须出现验证码输入，提交时带上它。"""

    UNSUPPORTED = "unsupported"
    """当前通道无法完成这次确认（集成阻塞）：在**发出任何资金请求之前**如实返回。"""


# 幂等记录里的 operation_type（bank_backend 既有取值），只允许理财这两个。
PURCHASE_OPERATION = "purchase_product"
REDEEM_OPERATION = "redeem_product"
WEALTH_OPERATION_TYPES = (PURCHASE_OPERATION, REDEEM_OPERATION)

# 只能由运行时（而不是模型）提供的字段。它不在任何意图上，这里列出来是为了让“模型不能
# 覆盖授权”有一条可断言的边界：意图的字段集合与它必须不相交。
RUNTIME_OWNED_FIELDS = frozenset(
    {"user_id", "confirm", "mfa_code", "idempotent_key", "confirmed", "verified"}
)

# 模型可以自由选择的七个只读方法（计划 §3.4）。
READ_ONLY_METHODS = frozenset(
    {
        "list_products",
        "get_product",
        "list_accounts",
        "list_holdings",
        "get_holding",
        "get_risk_profile",
        "get_order",
    }
)
# 资金写与按幂等键核实：只能由运行时的受控节点调用，不进入模型可调用的工具白名单。
RUNTIME_ONLY_METHODS = frozenset({"purchase", "redeem", "find_order_by_key"})
# 非资金写：由“问卷已完成”的流程调用，需要审计但不需要交易验证码。
FLOW_ONLY_METHODS = frozenset({"submit_risk_assessment"})


class WealthBankError(ToolFailure):
    """适配器对运行时的唯一失败出口，同时就是公共的 `ToolFailure`。

    它**不是**另一套错误语义（需求 §6.1.1）：`code` 用共享的 `ErrorCode` 词表，`message`
    是这一侧的中文句子（银行的原文永远不会出现在这里），可重试性由公共的
    `ToolFailure.retryable` 表达。因此理财工具不必写翻译层——公共 `BankToolAdapter` 已经
    会把它渲染成带同一个错误码的失败结果，而不是笼统的“工具内部错误”。

    两个理财特有的补充：

    - `retryable` 对读请求沿用 `BankClient` 的判断，对资金写请求一律为 `False`：写请求的
      重发不由重试决定，而由运行时按原幂等键核实。
    - `outcome_unknown` 表示“银行可能已经受理”，用来区分超时与明确拒绝——它是“去核实”的
      信号，不是“可以重发”的信号。
    - `verification_failed` 表示“这次资金写的验证码被银行明确判错”。资金写路径上适配器总是
      发出 `confirm=true`，所以银行回 1007 只可能是验证码不对（见 `bank_adapter._translated`）；
      运行时据此累计尝试数，而不是从错误码里猜。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        outcome_unknown: bool = False,
        verification_failed: bool = False,
    ) -> None:
        # 公共基类已经校验 code 属于 ErrorCode：拼错的 code 前端永远分支不到，会在抛出点失败。
        super().__init__(code, message, retryable=retryable)
        self.outcome_unknown = outcome_unknown
        self.verification_failed = verification_failed


# ---------------------------------------------------------------------- 只读 DTO


@dataclass(frozen=True, slots=True)
class ProductFilters:
    """产品列表的筛选条件；银行只认这两个查询参数。"""

    risk_level: str | None = None
    product_type: str | None = None


@dataclass(frozen=True, slots=True)
class Product:
    product_id: str
    name: str
    product_type: str
    min_amount_minor: int
    max_amount_minor: int
    # 演示年化参数：既不是已实现收益，也不是收益保证。
    expected_return: float
    risk_level: str
    duration_months: int
    # flexible 可随时部分／全部赎回；maturity_only 到期后才可赎回，判定在银行。
    redeem_rule: str
    status: str

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> Product:
        return cls(
            product_id=str(payload["product_id"]),
            name=str(payload["name"]),
            product_type=str(payload["product_type"]),
            min_amount_minor=int(payload["min_amount_minor"]),
            max_amount_minor=int(payload["max_amount_minor"]),
            expected_return=float(payload["expected_return"]),
            risk_level=str(payload["risk_level"]),
            duration_months=int(payload["duration_months"]),
            redeem_rule=str(payload["redeem_rule"]),
            status=str(payload["status"]),
        )


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    user_id: str
    account_type: str
    balance_minor: int
    available_minor: int
    currency: str
    status: str

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> Account:
        return cls(
            account_id=str(payload["account_id"]),
            user_id=str(payload["user_id"]),
            account_type=str(payload["account_type"]),
            balance_minor=int(payload["balance_minor"]),
            available_minor=int(payload["available_minor"]),
            currency=str(payload["currency"]),
            status=str(payload["status"]),
        )


@dataclass(frozen=True, slots=True)
class Holding:
    holding_id: str
    user_id: str
    product_id: str
    source_account_id: str | None
    principal_minor: int
    current_value_minor: int
    # 持有收益＝当前价值−本金，由银行计算并返回；不在这一层重算。
    accrued_interest_minor: int
    # 已实现收益来自已完成的赎回订单汇总，不是演示年化参数。
    realized_income_minor: int
    # 可赎回额由银行按赎回规则和到期日派生；maturity_only 未到期时为 0。
    redeemable_minor: int
    expected_return: float
    status: str
    purchased_at: str
    maturity_date: str | None
    product_name: str | None = None

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> Holding:
        return cls(
            holding_id=str(payload["holding_id"]),
            user_id=str(payload["user_id"]),
            product_id=str(payload["product_id"]),
            source_account_id=payload["source_account_id"],
            principal_minor=int(payload["principal_minor"]),
            current_value_minor=int(payload["current_value_minor"]),
            accrued_interest_minor=int(payload["accrued_interest_minor"]),
            realized_income_minor=int(payload["realized_income_minor"]),
            redeemable_minor=int(payload["redeemable_minor"]),
            expected_return=float(payload["expected_return"]),
            status=str(payload["status"]),
            purchased_at=str(payload["purchased_at"]),
            maturity_date=payload["maturity_date"],
            product_name=payload.get("product_name"),
        )


@dataclass(frozen=True, slots=True)
class RiskProfile:
    """银行保存的最近一次风险测评。等级、有效期都是银行算的，这一层不重算。"""

    assessment_id: str
    user_id: str
    answers: Mapping[str, int]
    score: int
    risk_level: str
    rule_version: str
    assessed_at: str
    expires_at: str
    valid: bool

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> RiskProfile:
        return cls(
            assessment_id=str(payload["assessment_id"]),
            user_id=str(payload["user_id"]),
            answers={str(key): int(value) for key, value in dict(payload["answers"]).items()},
            score=int(payload["score"]),
            risk_level=str(payload["risk_level"]),
            rule_version=str(payload["rule_version"]),
            assessed_at=str(payload["assessed_at"]),
            expires_at=str(payload["expires_at"]),
            valid=bool(payload["valid"]),
        )


@dataclass(frozen=True, slots=True)
class WealthOrder:
    """一笔本人理财订单。订单号与资金流水号是两个字段。"""

    order_id: str
    order_type: str
    user_id: str
    product_id: str | None
    product_name: str | None
    holding_id: str | None
    from_account_id: str | None
    to_account_id: str | None
    amount_minor: int
    status: str
    transaction_id: str | None
    details: Mapping[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> WealthOrder:
        return cls(
            order_id=str(payload["order_id"]),
            order_type=str(payload["order_type"]),
            user_id=str(payload["user_id"]),
            product_id=payload["product_id"],
            product_name=payload["product_name"],
            holding_id=payload["holding_id"],
            from_account_id=payload["from_account_id"],
            to_account_id=payload["to_account_id"],
            amount_minor=int(payload["amount_minor"]),
            status=str(payload["status"]),
            transaction_id=payload["transaction_id"],
            details=dict(payload["details"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class IdempotencyLookup:
    """按 (user_id, operation_type, idempotent_key) 核实的结果。

    `found=False` 只说明“按这个键查不到记录”，不等于“这笔没提交”：调用方对之前的写
    超时仍要按 unknown 处理，而不是宣布失败。
    """

    found: bool
    operation_type: str
    idempotent_key: str
    result: Mapping[str, Any] | None
    order: WealthOrder | None
    created_at: str | None

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> IdempotencyLookup:
        order = payload["order"]
        return cls(
            found=bool(payload["found"]),
            operation_type=str(payload["operation_type"]),
            idempotent_key=str(payload["idempotent_key"]),
            result=None if payload["result"] is None else dict(payload["result"]),
            order=None if order is None else WealthOrder.from_bank(order),
            created_at=payload["created_at"],
        )


# ---------------------------------------------------------------------- 交易意图


def _require_positive_minor(amount_minor: object) -> int:
    """金额是整数分，且必须是正数；bool 不算数值。"""

    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError("amount_minor 必须是正整数分")
    return amount_minor


def _require_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value


@dataclass(frozen=True, slots=True)
class PurchaseIntent:
    """一次申购的业务槽位：模型能提供的部分到此为止。

    没有 `user_id`、`confirm`、`mfa_code`、`idempotent_key`：它们由运行时的专用方法传入，
    模型无法用业务参数覆盖（见模块 docstring 第 1 条）。
    """

    product_id: str
    from_account_id: str
    amount_minor: int
    # 缺省使用产品期限；显式传入必须与产品一致，银行会拒绝缩短期限的请求。
    term_months: int | None = None

    def __post_init__(self) -> None:
        _require_id(self.product_id, "product_id")
        _require_id(self.from_account_id, "from_account_id")
        _require_positive_minor(self.amount_minor)
        if self.term_months is not None:
            if isinstance(self.term_months, bool) or not isinstance(self.term_months, int):
                raise ValueError("term_months 必须是整数或 None")
            if self.term_months < 0:
                raise ValueError("term_months 不能为负数")


@dataclass(frozen=True, slots=True)
class PurchaseSnapshot:
    """一次申购预览被确认时**冻结**下来的事实：工作流 → 运行时的契约。

    它是"用户确认的是这一笔"的全部内容：产品、账户、金额、期限，加上用户当时看到的那段
    预览原文。运行时按它铸待办、生成幂等键，并在提交前重新读银行事实与它比对——金额和账户
    在确认之后不再改变，条件变了就失败或重新预览，绝不"用已确认的金额去凑一次写"。

    和 `PurchaseIntent` 一样，它**没有**身份、确认、验证码和幂等键字段（`RUNTIME_OWNED_FIELDS`）：
    那些是运行时的授权，不是业务快照的一部分。`preview` 一起冻结是因为"用户确认的原文"属于
    这次授权，之后重新渲染一段不同的话就不再是同一个确认。

    `from_state` 只做形状检查，不做类型转换：缺字段或类型不对是协议错误（工作流与运行时之间
    的实现 bug），补一个默认值会让一次错的写看起来正常。
    """

    product_id: str
    product_name: str
    from_account_id: str
    amount_minor: int
    term_months: int | None
    risk_level: str
    redeem_rule: str
    balance_minor: int
    preview: str

    def __post_init__(self) -> None:
        _require_id(self.product_id, "product_id")
        _require_id(self.product_name, "product_name")
        _require_id(self.from_account_id, "from_account_id")
        _require_id(self.risk_level, "risk_level")
        _require_id(self.redeem_rule, "redeem_rule")
        _require_id(self.preview, "preview")
        _require_positive_minor(self.amount_minor)
        if self.term_months is not None:
            if isinstance(self.term_months, bool) or not isinstance(self.term_months, int):
                raise ValueError("term_months 必须是整数或 None")
            if self.term_months < 0:
                raise ValueError("term_months 不能为负数")
        if isinstance(self.balance_minor, bool) or not isinstance(self.balance_minor, int):
            raise ValueError("balance_minor 必须是整数分")
        if self.balance_minor < 0:
            raise ValueError("balance_minor 不能为负数")

    def as_state(self) -> dict[str, Any]:
        """进工作流状态的形状：只有普通数据。"""

        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "from_account_id": self.from_account_id,
            "amount_minor": self.amount_minor,
            "term_months": self.term_months,
            "risk_level": self.risk_level,
            "redeem_rule": self.redeem_rule,
            "balance_minor": self.balance_minor,
            "preview": self.preview,
        }

    @classmethod
    def from_state(cls, payload: Mapping[str, Any] | None) -> PurchaseSnapshot:
        """从工作流状态还原；形状不对抛 `ValueError`。"""

        if not isinstance(payload, Mapping):
            raise ValueError("purchase 必须是对象")
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        if missing:
            raise ValueError("purchase 缺少字段：" + "、".join(missing))
        extra = sorted(set(payload) - expected)
        if extra:
            raise ValueError("purchase 含未知字段：" + "、".join(extra))
        try:
            return cls(**{name: payload[name] for name in expected})
        except (TypeError, ValueError) as error:
            raise ValueError(f"purchase 字段不可用：{error}") from error

    def intent(self) -> PurchaseIntent:
        """银行写请求的业务参数：只有产品、付款账户、金额和期限。"""

        return PurchaseIntent(
            product_id=self.product_id,
            from_account_id=self.from_account_id,
            amount_minor=self.amount_minor,
            term_months=self.term_months,
        )


@dataclass(frozen=True, slots=True)
class RedeemSnapshot:
    """一次赎回预览被确认时**冻结**下来的事实：工作流 → 运行时的契约（任务10）。

    和 `PurchaseSnapshot` 是同一件事的另一半：它是"用户确认的是这一笔"的全部内容——持仓、
    金额、到账账户，加上用户当时看到的那段预览原文。区别只有一处，而且是刻意的：金额是**已经
    按银行可赎回额解析过的具体整数分**（"全部"和"一半"都在预览前就落成了一个确定的金额），
    所以运行时提交的永远是用户看到并确认过的那个数。

    它同样**没有**身份、确认、验证码和幂等键字段（`RUNTIME_OWNED_FIELDS`）。`full` 只说明这次
    是不是全部赎回，用于预览与话术；它不改变银行的分摊规则——释放本金与已实现收益由银行按持仓
    当前价值计算（需求 §6.2：Agent 不另算权威数值）。
    """

    holding_id: str
    product_id: str
    product_name: str
    to_account_id: str
    amount_minor: int
    redeemable_minor: int
    redeem_rule: str
    full: bool
    preview: str

    def __post_init__(self) -> None:
        _require_id(self.holding_id, "holding_id")
        _require_id(self.product_id, "product_id")
        _require_id(self.product_name, "product_name")
        _require_id(self.to_account_id, "to_account_id")
        _require_id(self.redeem_rule, "redeem_rule")
        _require_id(self.preview, "preview")
        _require_positive_minor(self.amount_minor)
        if isinstance(self.redeemable_minor, bool) or not isinstance(self.redeemable_minor, int):
            raise ValueError("redeemable_minor 必须是整数分")
        if self.redeemable_minor < 0:
            raise ValueError("redeemable_minor 不能为负数")

    def as_state(self) -> dict[str, Any]:
        """进工作流状态的形状：只有普通数据。"""

        return {
            "holding_id": self.holding_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "to_account_id": self.to_account_id,
            "amount_minor": self.amount_minor,
            "redeemable_minor": self.redeemable_minor,
            "redeem_rule": self.redeem_rule,
            "full": self.full,
            "preview": self.preview,
        }

    @classmethod
    def from_state(cls, payload: Mapping[str, Any] | None) -> RedeemSnapshot:
        """从工作流状态还原；形状不对抛 `ValueError`。"""

        if not isinstance(payload, Mapping):
            raise ValueError("redeem 必须是对象")
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        if missing:
            raise ValueError("redeem 缺少字段：" + "、".join(missing))
        extra = sorted(set(payload) - expected)
        if extra:
            raise ValueError("redeem 含未知字段：" + "、".join(extra))
        try:
            return cls(**{name: payload[name] for name in expected})
        except (TypeError, ValueError) as error:
            raise ValueError(f"redeem 字段不可用：{error}") from error

    def intent(self) -> RedeemIntent:
        """银行写请求的业务参数：只有持仓、金额和到账账户。"""

        return RedeemIntent(
            holding_id=self.holding_id,
            amount_minor=self.amount_minor,
            to_account_id=self.to_account_id,
        )


@dataclass(frozen=True, slots=True)
class RedeemIntent:
    """一次赎回的业务槽位。

    `to_account_id` 是必填：到账账户是用户确认过的事实，不把服务端的缺省账户当成授权。
    """

    holding_id: str
    amount_minor: int
    to_account_id: str

    def __post_init__(self) -> None:
        _require_id(self.holding_id, "holding_id")
        _require_positive_minor(self.amount_minor)
        _require_id(self.to_account_id, "to_account_id")


# ---------------------------------------------------------------------- 交易回执


@dataclass(frozen=True, slots=True)
class PurchaseReceipt:
    order_id: str
    holding_id: str
    transaction_id: str
    product_id: str
    amount_minor: int
    status: str
    principal_minor: int
    maturity_date: str | None
    redeem_rule: str
    risk_level: str
    assessment_id: str
    # True 表示银行按原幂等键返回了首笔回执，没有第二次扣款。
    replayed: bool

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> PurchaseReceipt:
        return cls(
            order_id=str(payload["order_id"]),
            holding_id=str(payload["holding_id"]),
            transaction_id=str(payload["transaction_id"]),
            product_id=str(payload["product_id"]),
            amount_minor=int(payload["amount_minor"]),
            status=str(payload["status"]),
            principal_minor=int(payload["principal_minor"]),
            maturity_date=payload["maturity_date"],
            redeem_rule=str(payload["redeem_rule"]),
            risk_level=str(payload["risk_level"]),
            assessment_id=str(payload["assessment_id"]),
            replayed=bool(payload.get("replayed", False)),
        )

    def as_state(self) -> dict[str, Any]:
        """进工作流状态的形状：只有普通数据（回执随待办一起进图状态）。"""

        return {
            "order_id": self.order_id,
            "holding_id": self.holding_id,
            "transaction_id": self.transaction_id,
            "product_id": self.product_id,
            "amount_minor": self.amount_minor,
            "status": self.status,
            "principal_minor": self.principal_minor,
            "maturity_date": self.maturity_date,
            "redeem_rule": self.redeem_rule,
            "risk_level": self.risk_level,
            "assessment_id": self.assessment_id,
            "replayed": self.replayed,
        }

    @classmethod
    def from_state(cls, payload: Mapping[str, Any] | None) -> PurchaseReceipt:
        """从工作流状态还原；形状不对抛 `ValueError`。"""

        if not isinstance(payload, Mapping):
            raise ValueError("purchase_receipt 必须是对象")
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        if missing:
            raise ValueError("purchase_receipt 缺少字段：" + "、".join(missing))
        extra = sorted(set(payload) - expected)
        if extra:
            raise ValueError("purchase_receipt 含未知字段：" + "、".join(extra))
        try:
            return cls(**{name: payload[name] for name in expected})
        except (TypeError, ValueError) as error:
            raise ValueError(f"purchase_receipt 字段不可用：{error}") from error


@dataclass(frozen=True, slots=True)
class RedemptionReceipt:
    order_id: str
    holding_id: str
    transaction_id: str
    amount_minor: int
    # 部分赎回按 floor(P×R/V) 释放本金，本次实现收益＝赎回金额−释放本金。
    principal_redeemed_minor: int
    interest_minor: int
    holding_status: str
    remaining_principal_minor: int
    remaining_value_minor: int
    status: str
    replayed: bool

    @classmethod
    def from_bank(cls, payload: Mapping[str, Any]) -> RedemptionReceipt:
        return cls(
            order_id=str(payload["order_id"]),
            holding_id=str(payload["holding_id"]),
            transaction_id=str(payload["transaction_id"]),
            amount_minor=int(payload["amount_minor"]),
            principal_redeemed_minor=int(payload["principal_redeemed_minor"]),
            interest_minor=int(payload["interest_minor"]),
            holding_status=str(payload["holding_status"]),
            remaining_principal_minor=int(payload["remaining_principal_minor"]),
            remaining_value_minor=int(payload["remaining_value_minor"]),
            status=str(payload["status"]),
            replayed=bool(payload.get("replayed", False)),
        )

    def as_state(self) -> dict[str, Any]:
        """进工作流状态的形状：只有普通数据（回执随待办一起进图状态）。"""

        return {
            "order_id": self.order_id,
            "holding_id": self.holding_id,
            "transaction_id": self.transaction_id,
            "amount_minor": self.amount_minor,
            "principal_redeemed_minor": self.principal_redeemed_minor,
            "interest_minor": self.interest_minor,
            "holding_status": self.holding_status,
            "remaining_principal_minor": self.remaining_principal_minor,
            "remaining_value_minor": self.remaining_value_minor,
            "status": self.status,
            "replayed": self.replayed,
        }

    @classmethod
    def from_state(cls, payload: Mapping[str, Any] | None) -> RedemptionReceipt:
        """从工作流状态还原；形状不对抛 `ValueError`。"""

        if not isinstance(payload, Mapping):
            raise ValueError("redemption_receipt 必须是对象")
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        if missing:
            raise ValueError("redemption_receipt 缺少字段：" + "、".join(missing))
        extra = sorted(set(payload) - expected)
        if extra:
            raise ValueError("redemption_receipt 含未知字段：" + "、".join(extra))
        try:
            return cls(**{name: payload[name] for name in expected})
        except (TypeError, ValueError) as error:
            raise ValueError(f"redemption_receipt 字段不可用：{error}") from error


# ---------------------------------------------------------------------- 协议


@runtime_checkable
class WealthBank(Protocol):
    """理财 Agent 唯一的银行数据来源。

    正式实现是 `HttpWealthBank`；测试替身只允许出现在测试目录。适配器不保存余额、持仓或
    交易副本，也不直接连银行数据库——所以这个协议里没有“查缓存”这一项。

    方法分三类（见上面三个 frozenset）：只读七项可由模型选择；`submit_risk_assessment`
    由完成问卷的流程调用；`purchase`／`redeem`／`find_order_by_key` 只能由运行时的受控
    节点调用，且必须显式传入可信上下文、验证码和运行时生成的幂等键。

    `write_authorization` 是**能力声明**：它回答"这家银行的写通道要什么才算授权"。运行时用它
    同时决定确认框要不要向用户要验证码、以及提交时带不带验证码。本仓库的 `HttpWealthBank`
    回答 `CLICK_AND_CODE`（银行要求 `confirm=true` 且 `mfa_code` 等于配置的演示码），所以确认框
    会要求用户输入验证码，用户输入后这笔写真的提交给银行。接入方提供无验证码的通道之后，换一个
    回答 `CLICK` 的适配器即可，运行时的状态机与前端都不用改。

    `amount_minor` 一起传进来，是为了让**按金额分级**的银行能自己表达（例如小额免验证码）：
    那样"不用的就确认框"是银行说的，不是接入方猜的。今天本仓库的银行不按金额分级，所以它忽略
    这个参数。
    """

    def write_authorization(self, operation: str, amount_minor: int) -> WriteAuthorization: ...

    def list_products(
        self, context: TrustedContext, filters: ProductFilters | None = None
    ) -> list[Product]: ...

    def get_product(self, context: TrustedContext, product_id: str) -> Product: ...

    def list_accounts(self, context: TrustedContext) -> list[Account]: ...

    def list_holdings(
        self, context: TrustedContext, status: str | None = None
    ) -> list[Holding]: ...

    def get_holding(self, context: TrustedContext, holding_id: str) -> Holding: ...

    def get_risk_profile(self, context: TrustedContext) -> RiskProfile | None: ...

    def submit_risk_assessment(
        self, context: TrustedContext, answers: Mapping[str, int]
    ) -> RiskProfile: ...

    def purchase(
        self,
        context: TrustedContext,
        intent: PurchaseIntent,
        verification_code: str | None,
        idempotency_key: str,
    ) -> PurchaseReceipt: ...

    def redeem(
        self,
        context: TrustedContext,
        intent: RedeemIntent,
        verification_code: str | None,
        idempotency_key: str,
    ) -> RedemptionReceipt: ...

    def get_order(self, context: TrustedContext, order_id: str) -> WealthOrder: ...

    def find_order_by_key(
        self, context: TrustedContext, operation_type: str, idempotency_key: str
    ) -> IdempotencyLookup: ...
