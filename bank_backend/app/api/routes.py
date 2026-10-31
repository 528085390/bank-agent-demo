"""REST 路由。

路由顺序有意为之：FastAPI 按声明顺序匹配，因此字面量路径
（/accounts/balance、/accounts/transactions）必须排在 /accounts/{account_id} 之前。

每个操作的 operation_id 与 `bank_backend/API接口文档.md` 的 operationId 逐字对应，
Task 8 的验收会直接比对两者。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import get_database, require_user_id, resolve_idempotent_key
from app.api.schemas import (
    AaCreateRequest,
    AaPayRequest,
    BillAnalyzeRequest,
    BirthdayOrderRequest,
    CardApplyRequest,
    CardLimitRequest,
    CardRestrictionRequest,
    ClockRequest,
    ConfirmRequest,
    ErrorResponse,
    ProductCompareRequest,
    ProductRecommendRequest,
    PurchaseRequest,
    RedeemRequest,
    RiskAssessmentRequest,
    ScheduledTransferRequest,
    SeedResetRequest,
    SimulateInterestRequest,
    SubscriptionRequest,
    TaskRunRequest,
    TransferRequest,
    UserRequest,
    envelope,
)
from app.core.errors import BankError, ConfirmationRequiredError, NotFoundError
from app.demo_data import build_demo_data
from app.domain import audit_service, idempotency_service
from app.domain.aa_service import AaService
from app.domain.account_service import AccountService
from app.domain.bill_service import BillService
from app.domain.birthday_service import BirthdayService
from app.domain.card_service import CardService
from app.domain.clock_service import ClockService
from app.domain.investment_service import InvestmentService
from app.domain.security_service import SecurityService
from app.domain.seed_service import SeedService
from app.domain.subscription_service import SubscriptionService
from app.domain.transfer_service import TransferService
from app.domain.wealth_order_service import WealthOrderService
from app.domain.wealth_risk_service import WealthRiskService
from app.storage.database import Database
from app.storage.models import AuditLog

router = APIRouter()

NOT_FOUND = {404: {"model": ErrorResponse, "description": "资源不存在"}}
CONFLICT = {409: {"model": ErrorResponse, "description": "业务执行失败"}}
INVALID = {422: {"model": ErrorResponse, "description": "参数校验失败"}}
NOT_FOUND_CONFLICT = {**NOT_FOUND, **CONFLICT}
NOT_FOUND_INVALID = {**NOT_FOUND, **INVALID}
CONFLICT_INVALID = {**CONFLICT, **INVALID}

TASK_STATUSES = ("scheduled", "running", "completed", "failed", "cancelled", "expired")


def _is_replay(
    database: Database, *, user_id: str | None, operation_type: str, key: str | None
) -> bool:
    """同键已有记录 ⇒ 这是一次重放，不要用「缺码」把它拦下。

    幂等指纹只含业务参数（不含 `mfa_code`／`confirm`），所以「重试时漏带验证码」
    必须仍然拿回原回执，而不是被缺码守卫拦成 1007。
    """

    if not key:
        return False
    with database.sessions() as session:
        record = idempotency_service.find(
            session,
            user_id=require_user_id(user_id),
            operation_type=operation_type,
            idempotent_key=key,
        )
    return record is not None


# ---------------------------------------------------------------- Accounts


@router.get(
    "/accounts/balance",
    operation_id="listBalances",
    tags=["Accounts"],
    summary="查询所有账户余额",
    responses=NOT_FOUND_INVALID,
)
def list_balances(
    user_id: str | None = Query(default=None, description="测试用户 ID，默认 u_zhang"),
    database: Database = Depends(get_database),
) -> dict:
    data = AccountService(database).list_balances(require_user_id(user_id))
    return envelope(data)


@router.get(
    "/accounts/transactions",
    operation_id="listTransactions",
    tags=["Accounts"],
    summary="查询用户全部流水",
    responses=INVALID,
)
def list_transactions(
    user_id: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    transaction_type: str | None = Query(default=None),
    category: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = AccountService(database).list_transactions(
        user_id=require_user_id(user_id),
        start_date=start_date,
        end_date=end_date,
        transaction_type=transaction_type,
        category=category,
        page=page,
        page_size=page_size,
    )
    return envelope(data)


@router.get(
    "/accounts",
    operation_id="listAccounts",
    tags=["Accounts"],
    summary="查询用户账户列表",
    responses=NOT_FOUND_INVALID,
)
def list_accounts(
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = AccountService(database).list_accounts(require_user_id(user_id))
    return envelope(data)


@router.get(
    "/accounts/{account_id}",
    operation_id="getAccount",
    tags=["Accounts"],
    summary="查询账户详情",
    responses=NOT_FOUND_INVALID,
)
def get_account(
    account_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = AccountService(database).get_account(account_id, require_user_id(user_id))
    return envelope(data)


@router.get(
    "/accounts/{account_id}/balance",
    operation_id="getAccountBalance",
    tags=["Accounts"],
    summary="查询账户余额",
    responses=NOT_FOUND_INVALID,
)
def get_account_balance(
    account_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(AccountService(database).get_balance(account_id, require_user_id(user_id)))


@router.get(
    "/accounts/{account_id}/transactions",
    operation_id="listAccountTransactions",
    tags=["Accounts"],
    summary="查询账户流水",
    responses=NOT_FOUND_INVALID,
)
def list_account_transactions(
    account_id: str,
    user_id: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    transaction_type: str | None = Query(default=None),
    category: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = AccountService(database).list_transactions(
        user_id=require_user_id(user_id),
        account_id=account_id,
        start_date=start_date,
        end_date=end_date,
        transaction_type=transaction_type,
        category=category,
        page=page,
        page_size=page_size,
    )
    return envelope(data)


# ---------------------------------------------------------------- Contacts


@router.get(
    "/contacts",
    operation_id="searchContacts",
    tags=["Contacts"],
    summary="搜索联系人",
    responses=NOT_FOUND_INVALID,
)
def search_contacts(
    q: str | None = Query(default=None, max_length=100, description="姓名、手机号或备注；不传则返回全部"),
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = AccountService(database).search_contacts(require_user_id(user_id), q)
    return envelope(data)


# ---------------------------------------------------------------- Transfers


@router.post(
    "/transfers",
    operation_id="createTransfer",
    tags=["Transfers"],
    summary="发起即时转账",
    responses=CONFLICT_INVALID,
)
def create_transfer(
    body: TransferRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = TransferService(database).transfer(
        user_id=require_user_id(body.user_id),
        from_account_id=body.from_account_id,
        to_account_id=body.to_account_id,
        amount_minor=body.amount_minor,
        remark=body.remark,
        confirm=body.confirm,
        mfa_code=body.mfa_code,
        idempotent_key=resolve_idempotent_key(idempotency_key, body.idempotent_key),
    )
    return envelope(data)


@router.get(
    "/transfers/scheduled",
    operation_id="listScheduledTransfers",
    tags=["Transfers"],
    summary="查询定时转账任务",
    responses=NOT_FOUND_INVALID,
)
def list_scheduled_transfers(
    user_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    database: Database = Depends(get_database),
) -> dict:
    data = TransferService(database).list_scheduled(
        user_id=require_user_id(user_id), status=status_filter
    )
    return envelope(data)


@router.post(
    "/transfers/scheduled",
    operation_id="createScheduledTransfer",
    tags=["Transfers"],
    summary="创建定时转账",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT_INVALID,
)
def create_scheduled_transfer(
    body: ScheduledTransferRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = TransferService(database).create_scheduled(
        user_id=require_user_id(body.user_id),
        from_account_id=body.from_account_id,
        to_account_id=body.to_account_id,
        amount_minor=body.amount_minor,
        execute_at=body.execute_at,
        remark=body.remark,
        confirm=body.confirm,
        mfa_code=body.mfa_code,
        idempotent_key=resolve_idempotent_key(idempotency_key, body.idempotent_key),
    )
    return envelope(data)


@router.get(
    "/transfers/scheduled/{task_id}",
    operation_id="getScheduledTransfer",
    tags=["Transfers"],
    summary="查询定时转账详情",
    responses=NOT_FOUND_INVALID,
)
def get_scheduled_transfer(
    task_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        TransferService(database).get_scheduled(task_id, user_id=require_user_id(user_id))
    )


@router.post(
    "/transfers/scheduled/{task_id}/cancel",
    operation_id="cancelScheduledTransfer",
    tags=["Transfers"],
    summary="取消定时转账",
    responses=CONFLICT_INVALID,
)
def cancel_scheduled_transfer(
    task_id: str,
    body: ConfirmRequest | None = None,
    user_id: str | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = TransferService(database).cancel_scheduled(
        task_id, user_id=require_user_id(user_id)
    )
    return envelope(data)


# ---------------------------------------------------------------- AA


@router.post(
    "/transfers/aa",
    operation_id="createAaRequest",
    tags=["Transfers"],
    summary="创建 AA 收款请求",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT_INVALID,
)
def create_aa_request(
    body: AaCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = AaService(database).create(
        user_id=require_user_id(body.user_id),
        total_amount_minor=body.total_amount_minor,
        participants=[item.model_dump() for item in body.participants],
        from_account_id=body.from_account_id,
        remark=body.remark,
        idempotent_key=resolve_idempotent_key(idempotency_key, body.idempotent_key),
    )
    return envelope(data)


@router.get(
    "/transfers/aa/{aa_request_id}",
    operation_id="getAaRequest",
    tags=["Transfers"],
    summary="查询 AA 请求",
    responses=NOT_FOUND_INVALID,
)
def get_aa_request(
    aa_request_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(AaService(database).get(aa_request_id, user_id=require_user_id(user_id)))


@router.post(
    "/transfers/aa/{aa_request_id}/pay",
    operation_id="payAaParticipant",
    tags=["Transfers"],
    summary="支付 AA 分摊项",
    responses=NOT_FOUND_CONFLICT,
)
def pay_aa_participant(
    aa_request_id: str,
    body: AaPayRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = AaService(database).pay(
        aa_request_id,
        participant_index=body.participant_index,
        confirm=body.confirm,
        user_id=require_user_id(body.user_id),
        idempotent_key=idempotency_key,
    )
    return envelope(data)


@router.post(
    "/transfers/aa/{aa_request_id}/cancel",
    operation_id="cancelAaRequest",
    tags=["Transfers"],
    summary="取消 AA 请求",
    responses=NOT_FOUND_CONFLICT,
)
def cancel_aa_request(
    aa_request_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(AaService(database).cancel(aa_request_id, user_id=require_user_id(user_id)))


# ---------------------------------------------------------------- Products

# 字面量子路径必须排在 /products/{product_id} 之前。


@router.get(
    "/products",
    operation_id="listProducts",
    tags=["Products"],
    summary="查询理财产品",
    responses=INVALID,
)
def list_products(
    risk_level: str | None = Query(default=None),
    product_type: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = InvestmentService(database).list_products(
        risk_level=risk_level, product_type=product_type
    )
    return envelope(data)


@router.post(
    "/products/recommend",
    operation_id="recommendProducts",
    tags=["Products"],
    summary="推荐理财产品",
    responses=INVALID,
)
def recommend_products(
    body: ProductRecommendRequest,
    database: Database = Depends(get_database),
) -> dict:
    data = InvestmentService(database).recommend(
        risk_level=body.risk_level, amount_minor=body.amount_minor
    )
    return envelope(data)


@router.post(
    "/products/compare",
    operation_id="compareProducts",
    tags=["Products"],
    summary="对比理财产品",
    responses=NOT_FOUND_INVALID,
)
def compare_products(
    body: ProductCompareRequest,
    database: Database = Depends(get_database),
) -> dict:
    return envelope(InvestmentService(database).compare(body.product_ids))


@router.post(
    "/products/purchase",
    operation_id="purchaseProduct",
    tags=["Products"],
    summary="申购理财产品",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT_INVALID,
)
def purchase_product(
    body: PurchaseRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    key = resolve_idempotent_key(idempotency_key, body.idempotent_key)
    if (
        body.confirm
        and not body.mfa_code
        and not _is_replay(
            database, user_id=body.user_id, operation_type="purchase_product", key=key
        )
    ):
        # 缺码是「还没确认」，不是「验证码错误」：领域层不区分（mfa_code=None 也抛
        # VerificationFailedError），所以在 HTTP 层先把它拦成 1007。
        # 同键重放不走这里 —— 那是已成交的重试，交给服务的幂等检查。
        raise ConfirmationRequiredError("这笔操作需要验证码")
    data = InvestmentService(database).purchase(
        user_id=require_user_id(body.user_id),
        from_account_id=body.from_account_id,
        product_id=body.product_id,
        amount_minor=body.amount_minor,
        term_months=body.term_months,
        confirm=body.confirm,
        mfa_code=body.mfa_code,
        idempotent_key=key,
    )
    return envelope(data)


@router.post(
    "/products/redeem",
    operation_id="redeemProduct",
    tags=["Holdings"],
    summary="赎回持仓",
    responses=CONFLICT_INVALID,
)
def redeem_product(
    body: RedeemRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    key = resolve_idempotent_key(idempotency_key, body.idempotent_key)
    if (
        body.confirm
        and not body.mfa_code
        and not _is_replay(
            database, user_id=body.user_id, operation_type="redeem_product", key=key
        )
    ):
        # 缺码是「还没确认」，不是「验证码错误」：领域层不区分（mfa_code=None 也抛
        # VerificationFailedError），所以在 HTTP 层先把它拦成 1007。
        # 同键重放不走这里 —— 那是已成交的重试，交给服务的幂等检查。
        raise ConfirmationRequiredError("这笔操作需要验证码")
    data = InvestmentService(database).redeem(
        user_id=require_user_id(body.user_id),
        holding_id=body.holding_id,
        amount_minor=body.amount_minor,
        to_account_id=body.to_account_id,
        confirm=body.confirm,
        mfa_code=body.mfa_code,
        idempotent_key=key,
    )
    return envelope(data)


@router.get(
    "/products/{product_id}",
    operation_id="getProduct",
    tags=["Products"],
    summary="查询理财产品详情",
    responses=NOT_FOUND_INVALID,
)
def get_product(
    product_id: str,
    database: Database = Depends(get_database),
) -> dict:
    return envelope(InvestmentService(database).get_product(product_id))


# ---------------------------------------------------------------- Holdings


@router.get(
    "/holdings",
    operation_id="listHoldings",
    tags=["Holdings"],
    summary="查询持仓",
    responses=NOT_FOUND_INVALID,
)
def list_holdings(
    user_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    database: Database = Depends(get_database),
) -> dict:
    data = InvestmentService(database).list_holdings(
        user_id=require_user_id(user_id), status=status_filter
    )
    return envelope(data)


@router.get(
    "/holdings/{holding_id}",
    operation_id="getHolding",
    tags=["Holdings"],
    summary="查询持仓详情",
    responses=NOT_FOUND_INVALID,
)
def get_holding(
    holding_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        InvestmentService(database).get_holding(
            holding_id, user_id=require_user_id(user_id)
        )
    )


@router.post(
    "/holdings/{holding_id}/simulate-interest",
    operation_id="simulateInterest",
    tags=["Holdings"],
    summary="模拟收益",
    responses=CONFLICT_INVALID,
)
def simulate_interest(
    holding_id: str,
    body: SimulateInterestRequest | None = None,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    as_of = body.as_of if body else None
    return envelope(
        InvestmentService(database).simulate_interest(
            holding_id, user_id=require_user_id(user_id), as_of=as_of
        )
    )


# ---------------------------------------------------------------- Wealth

# 理财专用查询：测评保存／最近测评、本人理财订单与按幂等键核实。
# /wealth/orders/by-idempotency-key 是字面量子路径，必须排在 /wealth/orders/{order_id} 之前。


@router.post(
    "/wealth/risk-assessments",
    operation_id="submitRiskAssessment",
    tags=["Wealth"],
    summary="保存风险测评",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT_INVALID,
)
def submit_risk_assessment(
    body: RiskAssessmentRequest,
    database: Database = Depends(get_database),
) -> dict:
    data = WealthRiskService(database).submit(
        user_id=require_user_id(body.user_id),
        answers=body.answers.model_dump(),
    )
    return envelope(data)


@router.get(
    "/wealth/risk-assessments/latest",
    operation_id="getLatestRiskAssessment",
    tags=["Wealth"],
    summary="查询最近一次风险测评",
    responses=NOT_FOUND_INVALID,
)
def get_latest_risk_assessment(
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(WealthRiskService(database).latest(user_id=require_user_id(user_id)))


@router.get(
    "/wealth/orders/by-idempotency-key",
    operation_id="findWealthOrderByKey",
    tags=["Wealth"],
    summary="按幂等键核实理财订单",
    responses=NOT_FOUND_INVALID,
)
def find_wealth_order_by_key(
    operation_type: str = Query(description="purchase_product 或 redeem_product"),
    idempotent_key: str = Query(min_length=1, max_length=128),
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = WealthOrderService(database).find_by_idempotency_key(
        user_id=require_user_id(user_id),
        operation_type=operation_type,
        idempotent_key=idempotent_key,
    )
    return envelope(data)


@router.get(
    "/wealth/orders/{order_id}",
    operation_id="getWealthOrder",
    tags=["Wealth"],
    summary="查询本人理财订单",
    responses=NOT_FOUND_INVALID,
)
def get_wealth_order(
    order_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = WealthOrderService(database).get_order(
        user_id=require_user_id(user_id), order_id=order_id
    )
    return envelope(data)


# ---------------------------------------------------------------- Bills


@router.get(
    "/bills/month",
    operation_id="getMonthlyBill",
    tags=["Bills"],
    summary="查询月度账单",
    responses=NOT_FOUND_INVALID,
)
def get_monthly_bill(
    month: str = Query(pattern=r"^\d{4}-\d{2}$", description="形如 2026-09"),
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(BillService(database).monthly(require_user_id(user_id), month))


@router.get(
    "/bills/year",
    operation_id="getYearlyBill",
    tags=["Bills"],
    summary="查询年度账单",
    responses=NOT_FOUND_INVALID,
)
def get_yearly_bill(
    year: int = Query(ge=2000, le=2100),
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(BillService(database).yearly(require_user_id(user_id), year))


@router.get(
    "/bills/categories",
    operation_id="getBillCategories",
    tags=["Bills"],
    summary="查询消费分类统计",
    responses=NOT_FOUND_INVALID,
)
def get_bill_categories(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = BillService(database).categories(
        require_user_id(user_id), start_date, end_date
    )
    return envelope(data)


@router.post(
    "/bills/analyze",
    operation_id="analyzeBills",
    tags=["Bills"],
    summary="分析账单异常",
    responses=NOT_FOUND_INVALID,
)
def analyze_bills(
    body: BillAnalyzeRequest,
    database: Database = Depends(get_database),
) -> dict:
    data = BillService(database).analyze(
        user_id=require_user_id(body.user_id),
        start_date=body.start_date,
        end_date=body.end_date,
        category=body.category,
        min_amount_minor=body.min_amount_minor,
        keywords=body.keywords,
    )
    return envelope(data)


# ---------------------------------------------------------------- Cards

# /cards/apply 是字面量子路径，必须排在 /cards/{card_id} 之前。


@router.get(
    "/cards",
    operation_id="listCards",
    tags=["Cards"],
    summary="查询卡片列表",
    responses=NOT_FOUND_INVALID,
)
def list_cards(
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(CardService(database).list_cards(require_user_id(user_id)))


@router.post(
    "/cards/apply",
    operation_id="applyCard",
    tags=["Cards"],
    summary="申请卡片",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT_INVALID,
)
def apply_card(
    body: CardApplyRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = CardService(database).apply(
        user_id=require_user_id(body.user_id),
        card_type=body.card_type,
        account_id=body.account_id,
        idempotent_key=resolve_idempotent_key(idempotency_key, body.idempotent_key),
    )
    return envelope(data)


@router.get(
    "/cards/{card_id}",
    operation_id="getCard",
    tags=["Cards"],
    summary="查询卡片详情",
    responses=NOT_FOUND_INVALID,
)
def get_card(
    card_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(CardService(database).get_card(card_id, require_user_id(user_id)))


@router.post(
    "/cards/{card_id}/limit",
    operation_id="updateCardLimit",
    tags=["Cards"],
    summary="修改卡片限额",
    responses=CONFLICT_INVALID,
)
def update_card_limit(
    card_id: str,
    body: CardLimitRequest,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = CardService(database).update_limit(
        card_id,
        user_id=require_user_id(user_id),
        daily_limit_minor=body.daily_limit_minor,
        monthly_limit_minor=body.monthly_limit_minor,
    )
    return envelope(data)


@router.post(
    "/cards/{card_id}/freeze",
    operation_id="freezeCard",
    tags=["Cards"],
    summary="挂失或冻结卡片",
    responses=CONFLICT_INVALID,
)
def freeze_card(
    card_id: str,
    body: ConfirmRequest | None = None,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(CardService(database).freeze(card_id, user_id=require_user_id(user_id)))


@router.post(
    "/cards/{card_id}/unfreeze",
    operation_id="unfreezeCard",
    tags=["Cards"],
    summary="解挂或解冻卡片",
    responses=CONFLICT_INVALID,
)
def unfreeze_card(
    card_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(CardService(database).unfreeze(card_id, user_id=require_user_id(user_id)))


@router.post(
    "/cards/{card_id}/block",
    operation_id="blockCard",
    tags=["Cards"],
    summary="永久阻断卡片",
    responses=CONFLICT_INVALID,
)
def block_card(
    card_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(CardService(database).block(card_id, user_id=require_user_id(user_id)))


@router.get(
    "/cards/{card_id}/restrictions",
    operation_id="getCardRestrictions",
    tags=["Cards"],
    summary="查询卡片限额和黑名单",
    responses=NOT_FOUND_INVALID,
)
def get_card_restrictions(
    card_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        CardService(database).get_restrictions(card_id, require_user_id(user_id))
    )


@router.post(
    "/cards/{card_id}/restrictions",
    operation_id="setCardRestrictions",
    tags=["Cards"],
    summary="设置卡片限额和黑名单",
    responses=CONFLICT_INVALID,
)
def set_card_restrictions(
    card_id: str,
    body: CardRestrictionRequest,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = CardService(database).set_restrictions(
        card_id,
        user_id=require_user_id(user_id),
        daily_limit_minor=body.daily_limit_minor,
        monthly_limit_minor=body.monthly_limit_minor,
        merchant_blacklist=body.merchant_blacklist,
    )
    return envelope(data)


# ---------------------------------------------------------------- Subscriptions

# /subscriptions/run-due 是字面量子路径，必须排在 /subscriptions/{subscription_id} 之前。


@router.get(
    "/subscriptions",
    operation_id="listSubscriptions",
    tags=["Subscriptions"],
    summary="查询订阅列表",
    responses=NOT_FOUND_INVALID,
)
def list_subscriptions(
    user_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    database: Database = Depends(get_database),
) -> dict:
    data = SubscriptionService(database).list_subscriptions(
        user_id=require_user_id(user_id), status=status_filter
    )
    return envelope(data)


@router.post(
    "/subscriptions",
    operation_id="createSubscription",
    tags=["Subscriptions"],
    summary="创建订阅",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT_INVALID,
)
def create_subscription(
    body: SubscriptionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = SubscriptionService(database).create(
        user_id=require_user_id(body.user_id),
        name=body.name,
        merchant_name=body.merchant_name,
        amount_minor=body.amount_minor,
        frequency=body.frequency,
        from_account_id=body.from_account_id,
        start_date=body.start_date,
        auto_renew=body.auto_renew,
        idempotent_key=resolve_idempotent_key(idempotency_key, body.idempotent_key),
    )
    return envelope(data)


@router.post(
    "/subscriptions/run-due",
    operation_id="runDueSubscriptions",
    tags=["Subscriptions"],
    summary="批量运行到期订阅",
    responses=CONFLICT,
)
def run_due_subscriptions(database: Database = Depends(get_database)) -> dict:
    return envelope(SubscriptionService(database).run_due())


@router.get(
    "/subscriptions/{subscription_id}",
    operation_id="getSubscription",
    tags=["Subscriptions"],
    summary="查询订阅详情",
    responses=NOT_FOUND_INVALID,
)
def get_subscription(
    subscription_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        SubscriptionService(database).get(subscription_id, user_id=require_user_id(user_id))
    )


@router.post(
    "/subscriptions/{subscription_id}/cancel",
    operation_id="cancelSubscription",
    tags=["Subscriptions"],
    summary="取消订阅",
    responses=CONFLICT_INVALID,
)
def cancel_subscription(
    subscription_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        SubscriptionService(database).cancel(
            subscription_id, user_id=require_user_id(user_id)
        )
    )


@router.post(
    "/subscriptions/{subscription_id}/pause",
    operation_id="pauseSubscription",
    tags=["Subscriptions"],
    summary="暂停订阅",
    responses=CONFLICT_INVALID,
)
def pause_subscription(
    subscription_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        SubscriptionService(database).pause(
            subscription_id, user_id=require_user_id(user_id)
        )
    )


@router.post(
    "/subscriptions/{subscription_id}/resume",
    operation_id="resumeSubscription",
    tags=["Subscriptions"],
    summary="恢复订阅",
    responses=CONFLICT_INVALID,
)
def resume_subscription(
    subscription_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        SubscriptionService(database).resume(
            subscription_id, user_id=require_user_id(user_id)
        )
    )


@router.post(
    "/subscriptions/{subscription_id}/charge",
    operation_id="chargeSubscription",
    tags=["Subscriptions"],
    summary="模拟本期订阅扣款",
    responses=CONFLICT_INVALID,
)
def charge_subscription(
    subscription_id: str,
    user_id: str | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = SubscriptionService(database).charge_now(
        subscription_id,
        user_id=require_user_id(user_id),
        idempotent_key=idempotency_key,
    )
    return envelope(data)


# ---------------------------------------------------------------- Birthdays


@router.get(
    "/mock-products",
    operation_id="listMockProducts",
    tags=["Birthdays"],
    summary="查询生日 mock 商品",
    responses=CONFLICT,
)
def list_mock_products() -> dict:
    return envelope(BirthdayService.list_mock_products())


@router.get(
    "/birthdays",
    operation_id="listBirthdays",
    tags=["Birthdays"],
    summary="查询生日联系人",
    responses=NOT_FOUND_INVALID,
)
def list_birthdays(
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    data = BirthdayService(database).list_birthday_contacts(require_user_id(user_id))
    return envelope(data)


@router.post(
    "/birthdays/orders",
    operation_id="createBirthdayOrder",
    tags=["Birthdays"],
    summary="创建生日礼物订单",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT_INVALID,
)
def create_birthday_order(
    body: BirthdayOrderRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    database: Database = Depends(get_database),
) -> dict:
    data = BirthdayService(database).create_order(
        user_id=require_user_id(body.user_id),
        contact_id=body.contact_id,
        product_code=body.product_code,
        from_account_id=body.from_account_id,
        birthday_date=body.birthday_date,
        confirm=body.confirm,
        mfa_code=body.mfa_code,
        idempotent_key=resolve_idempotent_key(idempotency_key, body.idempotent_key),
    )
    return envelope(data)


@router.get(
    "/birthdays/orders/{order_id}",
    operation_id="getBirthdayOrder",
    tags=["Birthdays"],
    summary="查询生日订单",
    responses=NOT_FOUND_INVALID,
)
def get_birthday_order(
    order_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        BirthdayService(database).get_order(order_id, user_id=require_user_id(user_id))
    )


@router.post(
    "/birthdays/orders/{order_id}/cancel",
    operation_id="cancelBirthdayOrder",
    tags=["Birthdays"],
    summary="取消生日订单",
    responses=CONFLICT_INVALID,
)
def cancel_birthday_order(
    order_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    return envelope(
        BirthdayService(database).cancel_order(order_id, user_id=require_user_id(user_id))
    )


# ---------------------------------------------------------------- Audit


@router.get(
    "/audit",
    operation_id="listAuditLogs",
    tags=["Audit"],
    summary="查询审计日志",
    responses=INVALID,
)
def list_audit_logs(
    user_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    database: Database = Depends(get_database),
) -> dict:
    with database.sessions() as session:
        entries, total = audit_service.list_logs(
            session,
            user_id=require_user_id(user_id),
            action=action,
            resource_type=resource_type,
            page=page,
            page_size=page_size,
        )
        return envelope(
            {
                "items": [audit_service.audit_view(entry) for entry in entries],
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": page * page_size < total,
            }
        )


@router.get(
    "/audit/{audit_id}",
    operation_id="getAuditLog",
    tags=["Audit"],
    summary="查询审计详情",
    responses=NOT_FOUND_INVALID,
)
def get_audit_log(
    audit_id: str,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    owner = require_user_id(user_id)
    with database.sessions() as session:
        entry = session.get(AuditLog, audit_id)
        # 系统事件（user_id 为空）不对任何终端用户开放：它们不属于任何人。
        if entry is None or entry.user_id != owner:
            raise NotFoundError(f"审计记录不存在: {audit_id}")
        return envelope(audit_service.audit_view(entry))


# ---------------------------------------------------------------- Security


def _security_payload(user_id: str, tools: list[dict]) -> dict:
    """文档的 Security schema 是单个工具；这里给出主工具并附上全部工具。"""
    primary = tools[0] if tools else {
        "user_id": user_id,
        "tool_name": None,
        "failure_count": 0,
        "locked_until": None,
        "last_reason": None,
    }
    return {**primary, "tools": tools}


@router.get(
    "/security/status",
    operation_id="getSecurityStatus",
    tags=["Security"],
    summary="查询模拟安全状态",
    responses=INVALID,
)
def get_security_status(
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    resolved = require_user_id(user_id)
    return envelope(_security_payload(resolved, SecurityService(database).status(resolved)))


@router.post(
    "/security/reset-lock",
    operation_id="resetSecurityLock",
    tags=["Security"],
    summary="清除模拟安全锁定",
    responses=INVALID,
)
def reset_security_lock(
    body: UserRequest | None = None,
    user_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict:
    # body 是可选的，所以身份还要能从 query 读到：后端调用「重置本人锁定」时可能不带
    # 请求体，只带 query，和 GET 走同一条通道。
    resolved = require_user_id((body.user_id if body else None) or user_id)
    return envelope(_security_payload(resolved, SecurityService(database).reset_lock(resolved)))


# ---------------------------------------------------------------- System


@router.post(
    "/seed/reset",
    operation_id="resetSeed",
    tags=["System"],
    summary="重置固定种子",
    responses=CONFLICT_INVALID,
)
def reset_seed(
    body: SeedResetRequest | None = None,
    database: Database = Depends(get_database),
) -> dict:
    service = SeedService(database)
    service.reset(expected_run_id=body.expected_run_id if body else None)
    return envelope(service.health())


@router.post(
    "/seed/demo",
    operation_id="buildDemoData",
    tags=["System"],
    summary="初始化完整演示数据",
    responses=CONFLICT,
)
def build_demo_seed(database: Database = Depends(get_database)) -> dict:
    return envelope(build_demo_data(database))


@router.get(
    "/health",
    operation_id="getHealth",
    tags=["System"],
    summary="查询服务健康状态",
    responses={503: {"model": ErrorResponse, "description": "服务不可用"}},
)
def get_health(database: Database = Depends(get_database)):
    try:
        return envelope(SeedService(database).ready())
    except BankError as exc:
        return JSONResponse(status_code=503, content=exc.to_payload())


@router.get(
    "/health/deep",
    operation_id="getHealthDeep",
    tags=["System"],
    summary="完整健康检查（含账户对账）",
    responses={503: {"model": ErrorResponse, "description": "服务不可用"}},
)
def get_health_deep(database: Database = Depends(get_database)):
    """排障用：逐账户校验 available == balance，并要求种子已写入。

    它比 `/health` 严格，所以不能拿当探针：新客户开户、正常交易都可能在极短的中间态
    让它报 503。容器健康检查看 `/health` 就够了。
    """

    try:
        return envelope(SeedService(database).health())
    except BankError as exc:
        return JSONResponse(status_code=503, content=exc.to_payload())


@router.post(
    "/pool/provision",
    operation_id="provisionPoolCustomer",
    tags=["System"],
    summary="补一个注册客户池名额",
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT,
)
def provision_pool_customer(database: Database = Depends(get_database)) -> dict:
    """注册领走一个之后由 app 调用，让池子长回一格。

    成功返回新建的客户号；补不上（并发抢号用尽重试）返回 `customer_id: null`，调用方
    应当忽略——这只是水位，不是用户能感知的失败。**不需要运维口令**：它只新增一个空
    客户，既不删数据也不改时间线。
    """

    from app.domain.pool_service import provision_next

    service = SeedService(database)
    now = service.current_time() or datetime.now(timezone.utc)
    with database.sessions.begin() as session:
        customer_id = provision_next(session, now=now)
    return envelope({"customer_id": customer_id})


# ---------------------------------------------------------------- Clock


@router.get(
    "/clock",
    operation_id="getClock",
    tags=["Clock"],
    summary="查询模拟时钟",
    responses=CONFLICT,
)
def get_clock(database: Database = Depends(get_database)) -> dict:
    return envelope(ClockService(database).get())


@router.post(
    "/clock",
    operation_id="setClock",
    tags=["Clock"],
    summary="设置或向前拨模拟时钟",
    responses=CONFLICT_INVALID,
)
def set_clock(
    body: ClockRequest,
    database: Database = Depends(get_database),
) -> dict:
    return envelope(ClockService(database).set_time(body.current_time))


@router.post(
    "/clock/run-tasks",
    operation_id="runDueTasks",
    tags=["Clock"],
    summary="运行到期任务",
    responses=CONFLICT,
)
def run_due_tasks(
    body: TaskRunRequest | None = None,
    database: Database = Depends(get_database),
) -> dict:
    outcome = body.birthday_outcome if body else "success"
    data = ClockService(database).run_due_tasks(birthday_outcome=outcome)
    return envelope(data)
