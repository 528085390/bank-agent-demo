"""HttpWealthBank：理财 Agent 的正式银行实现，唯一数据来源是银行 HTTP。

它只做四件事：

1. 把 `TrustedContext.bank_customer_id` 显式注入 `BankClient`；没有绑定就在**发请求之前**
   失败（银行的 `user_id` 是可选的，省略会读到银行默认客户的账户和持仓）。
2. 把银行的 `{code, message, data}` 映射成 `contracts` 里的 DTO；映射错了报协议错误，
   不补默认值假装成功。
3. 把失败翻译成 `WealthBankError`：资金写一律不重试，结果未知（超时／5xx／读不懂的响应）标
   `outcome_unknown`，银行回 1008（验证码不对）标 `verification_failed`（资金写路径上
   `confirm` 恒为 True）。
4. 资金写的授权字段在**发请求之前**检查：幂等键由运行时铸好，验证码按能力声明决定要不要带、
   并且只接受用户当场输入的那个值。缺任何一个都不发出去。资金写经 `WriteFacade`（见
   `docs/contracts/write-path.md`），不另开一条到银行的路。

它不做的事同样重要：不缓存账户／持仓／产品，不重试，不在网络故障时返回任何看起来成功的
结果，也不把 `order_id` 当成 `transaction_id`、把 `expected_return` 当成已赚收益。

依赖方向：`infrastructure → contracts`。这一层不承载对话流程，也不直接连银行数据库。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar
from urllib.parse import quote

from app.integration.bank_client import BankClient
from app.integration.bank_errors import KIND_TO_ERROR_CODE, BankCallError, BankErrorKind
from app.integration.write_facade import WriteFacade, WritePolicyError, require_customer_id
from app.modules.base import ERROR_MESSAGES
from app.modules.wealth.contracts.bank import (
    PURCHASE_OPERATION,
    REDEEM_OPERATION,
    Account,
    Holding,
    IdempotencyLookup,
    Product,
    ProductFilters,
    PurchaseIntent,
    PurchaseReceipt,
    RedeemIntent,
    RedemptionReceipt,
    RiskProfile,
    WealthBankError,
    WealthOrder,
    WriteAuthorization,
)
from app.tools.protocol import ErrorCode, TrustedContext

# 资金写请求体＝这些业务槽位＋运行时授权字段。这里用白名单而不是遍历意图的字段：以后给
# 意图加字段，也不会让它悄悄变成模型可控的请求参数。
PURCHASE_BODY_FIELDS: tuple[str, ...] = (
    "product_id",
    "from_account_id",
    "amount_minor",
    "term_months",
)
REDEEM_BODY_FIELDS: tuple[str, ...] = ("holding_id", "amount_minor", "to_account_id")

# 资金写失败里“银行可能已经受理”的那几种。连接被拒说明请求没到银行，业务拒绝说明银行已经
# 给出结论，两者都不算结果未知。
UNKNOWN_OUTCOME_KINDS = frozenset(
    {BankErrorKind.TIMEOUT, BankErrorKind.SERVER, BankErrorKind.PROTOCOL}
)

T = TypeVar("T")


def _protocol_error() -> WealthBankError:
    return WealthBankError(ErrorCode.PROTOCOL, ERROR_MESSAGES[ErrorCode.PROTOCOL])


def _path_segment(value: object) -> str:
    """把一个资源 ID 编码成单个路径段。

    ID 是数据：`h/../accounts` 若不编码会被 httpx 规范化成 `/holdings/accounts`，一个订单号
    就能让理财适配器改道去读别的银行接口。编码后它始终只是一个路径段。
    """

    return quote(str(value), safe="")


def _rows(data: Any) -> Sequence[Any]:
    """列表接口的 `data` 必须是列表。

    银行的 `data` 若是一个分页对象而被当成空列表读，Agent 会对用户说“您没有持仓”——一个
    由映射错误造成的业务谎言。所以形状不对时报协议错误，而不是给出空结果。
    """

    if not isinstance(data, list):
        raise _protocol_error()
    return data


def _mapped(factory: Callable[[Mapping[str, Any]], T], payload: Any) -> T:
    """把一条银行 data 映射成契约 DTO；结构不认识是协议错误，不是“成功但没有内容”。"""

    if not isinstance(payload, Mapping):
        raise _protocol_error()
    try:
        return factory(payload)
    except (KeyError, TypeError, ValueError) as error:
        raise _protocol_error() from error


def _mapped_rows(factory: Callable[[Mapping[str, Any]], T], data: Any) -> list[T]:
    return [_mapped(factory, row) for row in _rows(data)]


def _write_body(intent: object, fields: Sequence[str]) -> dict[str, Any]:
    """只取白名单里的业务槽位；未指定的可选项（如 term_months）不发送。"""

    body = {name: getattr(intent, name) for name in fields}
    return {name: value for name, value in body.items() if value is not None}


def _verification(code: str | None) -> str:
    """资金写的验证码必须是调用方交来的**非空**输入。

    空验证码永远不可能正确，发出去只会让银行多记一次验证失败（银行的失败计数是有阈值的），
    所以在这里就失败关闭：不发一次注定失败的写请求。

    它**不**从配置、环境或数据库里取一个"演示码"顶上：那样用户以为是自己点了确认成交的，
    实际上系统替他答了门禁——这正是"不能自动填入固定验证码来伪装点击确认已经接通"要挡住的事。
    所以只有用户当场输入的值能走到这里，缺了就抛 `VALIDATION`，让运行时说清楚"这笔需要验证码"。
    """

    if not isinstance(code, str) or not code.strip():
        raise WealthBankError(ErrorCode.VALIDATION, ERROR_MESSAGES[ErrorCode.VALIDATION])
    return code.strip()


class HttpWealthBank:
    """正式实现：复用已有 `BankClient`，不另开一条到银行的路。"""

    def __init__(self, client: BankClient, *, writes: WriteFacade | None = None) -> None:
        self._client = client
        self._writes = writes or WriteFacade(client)

    # ------------------------------------------------------------ 只读：模型可选

    def list_products(
        self, context: TrustedContext, filters: ProductFilters | None = None
    ) -> list[Product]:
        params: dict[str, Any] = {}
        if filters is not None:
            if filters.risk_level:
                params["risk_level"] = filters.risk_level
            if filters.product_type:
                params["product_type"] = filters.product_type
        return _mapped_rows(
            Product.from_bank, self._read(context, "/products", params=params or None)
        )

    def get_product(self, context: TrustedContext, product_id: str) -> Product:
        return _mapped(
            Product.from_bank, self._read(context, f"/products/{_path_segment(product_id)}")
        )

    def list_accounts(self, context: TrustedContext) -> list[Account]:
        return _mapped_rows(Account.from_bank, self._read(context, "/accounts"))

    def list_holdings(self, context: TrustedContext, status: str | None = None) -> list[Holding]:
        params = {"status": status} if status else None
        return _mapped_rows(Holding.from_bank, self._read(context, "/holdings", params=params))

    def get_holding(self, context: TrustedContext, holding_id: str) -> Holding:
        return _mapped(
            Holding.from_bank, self._read(context, f"/holdings/{_path_segment(holding_id)}")
        )

    def get_risk_profile(self, context: TrustedContext) -> RiskProfile | None:
        data = self._read(context, "/wealth/risk-assessments/latest")
        return None if data is None else _mapped(RiskProfile.from_bank, data)

    def get_order(self, context: TrustedContext, order_id: str) -> WealthOrder:
        return _mapped(
            WealthOrder.from_bank, self._read(context, f"/wealth/orders/{_path_segment(order_id)}")
        )

    # ------------------------------------------------- 非资金写：完成问卷的流程调用

    def submit_risk_assessment(
        self, context: TrustedContext, answers: Mapping[str, int]
    ) -> RiskProfile:
        """保存问卷；等级由银行按 wealth-risk-v1 计算。

        请求体只放 `answers`：模型若把 `risk_level` 之类的字段塞进答案里，银行的问卷模型
        `extra="forbid"` 会直接以 1012 拒绝，而不是把它当成一个可选输入。
        """

        body = {"answers": dict(answers)}
        return _mapped(
            RiskProfile.from_bank,
            self._send("POST", "/wealth/risk-assessments", context, json=body),
        )

    # ------------------------------------------------- 资金写：运行时受控节点专用

    def write_authorization(self, operation: str, amount_minor: int) -> WriteAuthorization:
        """本仓库的银行要求**点击 + 验证码**，如实回答 `CLICK_AND_CODE`。

        银行的写门禁（`bank_backend/app/domain/investment_service.py::_require_verification`）
        同时要求 `confirm=true` 与 `mfa_code` 等于配置的演示码，不按金额分级，也没有小额免验证。
        所以这里回答 `CLICK_AND_CODE`：确认框会向用户要验证码，用户输入后连同确认一起提交。

        两个参数都收下但今天不用：`operation`（申购／赎回）与 `amount_minor` 是留给**按金额分级**
        的银行的（例如大额才要验证码）。那时改这一个方法就够了，运行时的状态机、确认卡片和前端
        都不用动——"要不要验证码"只有这一个判断源。

        回答 `UNSUPPORTED` 表示这条通道**根本走不通**（连带验证码也不行）：运行时会在发出任何
        资金请求之前如实返回 `confirmation_transport_unsupported`，而不是偷偷带一个码去试。
        """

        del operation, amount_minor
        return WriteAuthorization.CLICK_AND_CODE

    def purchase(
        self,
        context: TrustedContext,
        intent: PurchaseIntent,
        verification_code: str | None,
        idempotency_key: str,
    ) -> PurchaseReceipt:
        """申购。能走到这里就说明运行时已经取得用户确认，所以 `confirm` 恒为 True。"""

        body = _write_body(intent, PURCHASE_BODY_FIELDS)
        body["confirm"] = True
        body.update(self._authorization(PURCHASE_OPERATION, intent.amount_minor, verification_code))
        body["idempotent_key"] = idempotency_key
        data = self._send(
            "POST",
            "/products/purchase",
            context,
            json=body,
            idempotency_key=idempotency_key,
            fund_write=True,
        )
        return _mapped(PurchaseReceipt.from_bank, data)

    def redeem(
        self,
        context: TrustedContext,
        intent: RedeemIntent,
        verification_code: str | None,
        idempotency_key: str,
    ) -> RedemptionReceipt:
        body = _write_body(intent, REDEEM_BODY_FIELDS)
        body["confirm"] = True
        body.update(self._authorization(REDEEM_OPERATION, intent.amount_minor, verification_code))
        body["idempotent_key"] = idempotency_key
        data = self._send(
            "POST",
            "/products/redeem",
            context,
            json=body,
            idempotency_key=idempotency_key,
            fund_write=True,
        )
        return _mapped(RedemptionReceipt.from_bank, data)

    def _authorization(
        self, operation: str, amount_minor: int, verification_code: str | None
    ) -> dict[str, Any]:
        """这次资金写要带哪些授权字段——由**这一侧的能力**决定，而不是由调用方决定。

        - `CLICK`：不带 `mfa_code`。一次可信的按钮确认本身就是授权，再要求一个验证码就等于
          把用户退回旧流程。
        - `CLICK_AND_CODE`：`mfa_code` 必填，值是**用户当场输入**的那个；缺了就在这里失败关闭，
          不发一次注定被银行拒绝的请求（银行的验证失败计数是有阈值的）。
        - `UNSUPPORTED`：这条通道走不通，但那是运行时的判断（它要在预览时就告诉用户），走到
          这里说明调用方绕过了它——所以同样按"缺授权"失败关闭，不发请求。

        放在一个方法里，是为了让"要不要验证码"只有一个判断点：`purchase` 与 `redeem` 共用它，
        子类或替身只要改写 `write_authorization()`，两条写路径的行为就一起变。
        """

        if self.write_authorization(operation, amount_minor) is WriteAuthorization.CLICK:
            return {}
        return {"mfa_code": _verification(verification_code)}

    def find_order_by_key(
        self, context: TrustedContext, operation_type: str, idempotency_key: str
    ) -> IdempotencyLookup:
        """按原幂等键核实“刚才那笔到底成交没有”。

        `operation_type` 是否合法由银行判断（它只允许 purchase_product／redeem_product）：
        适配器不复制银行的规则，也就不会和银行判断得不一致。
        """

        params = {"operation_type": operation_type, "idempotent_key": idempotency_key}
        return _mapped(
            IdempotencyLookup.from_bank,
            self._read(context, "/wealth/orders/by-idempotency-key", params=params),
        )

    # ------------------------------------------------------------------ 传输

    def _read(
        self, context: TrustedContext, path: str, *, params: Mapping[str, Any] | None = None
    ) -> Any:
        return self._send("GET", path, context, params=params)

    def _send(
        self,
        method: str,
        path: str,
        context: TrustedContext,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        fund_write: bool = False,
    ) -> Any:
        if fund_write:
            # 资金写经 Write Facade：身份、幂等、审计脱敏；业务体仍由 purchase/redeem 组装。
            try:
                response = self._writes.send(
                    method,
                    path,
                    context=context,
                    json=json or {},
                    idempotency_key=idempotency_key or "",
                    operation=path,
                )
            except WritePolicyError as policy:
                raise WealthBankError(policy.code, ERROR_MESSAGES[policy.code]) from policy
            except BankCallError as failure:
                raise _translated(failure, fund_write=True) from failure
            return response.data

        user_id = self._customer(context)
        try:
            response = self._client.request(
                method,
                path,
                user_id=user_id,
                params=params,
                json=json,
                idempotency_key=idempotency_key,
            )
        except BankCallError as failure:
            # 一次调用就是一次调用：`BankClient` 不重试，这一层也不重试。
            raise _translated(failure, fund_write=False) from failure
        return response.data

    @staticmethod
    def _customer(context: TrustedContext) -> str:
        try:
            return require_customer_id(context)
        except WritePolicyError as policy:
            raise WealthBankError(policy.code, ERROR_MESSAGES[policy.code]) from policy


def _translated(failure: BankCallError, *, fund_write: bool) -> WealthBankError:
    if fund_write:
        # 写请求不重发：`retryable` 一律 False，结果未知时由运行时按原幂等键核实。
        # 资金写路径上 `confirm` 恒为 True（见 purchase／redeem），所以"验证码不对"由银行自己
        # 用 1008 说清楚（1007 只表示缺码／还需要确认）：在这里标出来，运行时就不必从错误码里猜，
        # 也不会把一次验证失败误当成"还没有确认"而重新走一遍确认流程。
        return WealthBankError(
            KIND_TO_ERROR_CODE[failure.kind],
            failure.message,
            retryable=False,
            outcome_unknown=failure.kind in UNKNOWN_OUTCOME_KINDS,
            verification_failed=failure.kind is BankErrorKind.VERIFICATION_FAILED,
        )
    return WealthBankError(
        KIND_TO_ERROR_CODE[failure.kind], failure.message, retryable=failure.retryable
    )
