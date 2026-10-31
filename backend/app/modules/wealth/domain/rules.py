"""理财领域规则：风险问卷、等级词表和产品适配硬约束。

这一层只有规则：不调用银行、不调用模型、不读环境变量（需求 §6.1）。它服务两件事：

1. **风险问卷**（需求 §7.1）：五题 q1..q5、每题选项 0／1／2，题面与银行 `wealth-risk-v1` 对齐。
   Agent 只负责把用户的明确回答映射成标准选项；**评分、等级和有效期一律以银行返回为准**，
   所以这里没有评分函数，也没有本地档案——模型写"我是高风险"不改变任何东西。
2. **适配硬约束**（需求 §7.2）：风险不高于测评等级、产品在售、金额在起购与单笔上限之间、期限不
   超过用户的用钱时间、流动性要求匹配。每条规则都朝保守方向写：判断不了就算不适配，不为了让
   候选变多而放宽（"没有候选就如实说明"是需求，不是失败）。

金额只做比较与展示：比较用整数分，展示走公共的 `app.modules.money.format_minor`，这一层不引入
浮点，也不重算银行给的任何数值。

任务9 在这一层加上**申购规则**：金额规范化（`normalize_amount_minor`）、提交前的预检
（`check_purchase`）和由程序模板生成的预览／成交话术（`purchase_preview`／`purchase_success`）。
预检与推荐共用同一套硬约束（风险、在售、金额、流动性），只是这里针对的是一笔具体的、已经
由用户选定的申购：账户归属、状态、余额也在其中。三条规则都朝保守方向写：判断不了就不放行。

任务10 加上**赎回规则**：金额的三种表达（具体金额／全部／一半，`parse_redeem_amount` +
`RedeemAmount.resolve`）、提交前的预检（`check_redeem`）和预览／成交话术（`redeem_preview`／
`redeem_success`）。赎回的预检**不查风险测评**（取回自己的持仓不要求重新做问卷），但持仓归属、
可赎回额和到账账户仍然逐条检查；释放本金与实现收益是银行按持仓当前价值算的分摊，这一层只搬运。

依赖方向：`domain → contracts`（外加公共的金额格式化）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.modules.money import format_minor
from app.modules.wealth.contracts.bank import (
    Account,
    Holding,
    Product,
    PurchaseReceipt,
    RedemptionReceipt,
)

__all__ = [
    "MAX_CANDIDATES",
    "OPTIONS",
    "QUESTIONS",
    "QUESTION_IDS",
    "REDEEM_LABELS",
    "REDEEM_STATUS_LABELS",
    "RISK_LABELS",
    "RISK_ORDER",
    "SIMULATION_NOTE",
    "STATUS_LABELS",
    "AmountError",
    "Fit",
    "PurchaseCheck",
    "RedeemAmount",
    "RedeemAmountError",
    "RedeemCheck",
    "check_purchase",
    "check_redeem",
    "evaluate",
    "normalize_amount_minor",
    "parse_redeem_amount",
    "purchase_preview",
    "purchase_success",
    "questionnaire_lines",
    "questionnaire_question",
    "redeem_preview",
    "redeem_success",
    "risk_allows",
]

# 推荐最多三个候选（需求 §7.2）。
MAX_CANDIDATES = 3

# 等级高低：判断产品风险是否高于测评等级。不认识的取值一律按"判断不了"处理。
RISK_ORDER: Mapping[str, int] = {"low": 0, "medium": 1, "high": 2}

RISK_LABELS: Mapping[str, str] = {"low": "低", "medium": "中", "high": "高"}
REDEEM_LABELS: Mapping[str, str] = {
    "flexible": "可随时赎回",
    "maturity_only": "到期后可赎回",
}
STATUS_LABELS: Mapping[str, str] = {"available": "在售", "sold_out": "已停售"}
# 持仓状态（银行 holding.status）：赎回回执里也用它说明"这次赎回之后还剩什么"。
REDEEM_STATUS_LABELS: Mapping[str, str] = {
    "holding": "持有中",
    "partial": "部分赎回",
    "redeemed": "已全部赎回",
}

# 模拟收益的边界话术（需求 §7.2）：演示年化参数不是收益预测，也不是保本承诺。
SIMULATION_NOTE = "模拟年化参数是演示数据，不代表实际收益，也不构成保本承诺。"

# 风险问卷：`(题目编号, 题面, (0 分选项, 1 分选项, 2 分选项))`，与银行 wealth-risk-v1 对齐。
QUESTIONS: tuple[tuple[str, str, tuple[str, str, str]], ...] = (
    ("q1", "本金损失接受度", ("不能接受本金损失", "接受少量波动和损失", "接受较大波动和损失")),
    ("q2", "投资经验", ("无经验", "有低风险产品经验", "有较多波动型产品经验")),
    ("q3", "收入稳定性", ("不稳定、备用金少", "较稳定、备用金一般", "稳定、有足够备用金")),
    ("q4", "计划投资期限", ("3 个月以内", "超过 3 个月且不超过 12 个月", "超过 12 个月")),
    ("q5", "短期资金需求", ("随时可能急用", "部分资金可能短期使用", "这部分资金短期不使用")),
)

QUESTION_IDS: tuple[str, ...] = tuple(question for question, _title, _options in QUESTIONS)
OPTIONS: tuple[int, ...] = (0, 1, 2)


def questionnaire_lines() -> str:
    """提示词里的五题问卷：模型按它提问，并按编号把用户的回答映射成 0／1／2。"""

    return "\n".join(
        f"- {question} {title}："
        + "；".join(f"{value}={option}" for value, option in enumerate(options))
        for question, title, options in QUESTIONS
    )


def questionnaire_question() -> str:
    """没有有效测评时直接问用户的问卷文本。

    由程序生成而不是由模型生成：没有测评就无法推荐，这一句不能被模型"忘记"。
    """

    lines = ["为了推荐合适的产品，需要先完成银行的风险测评（五题，每题在 0/1/2 里选一个）："]
    for index, (_question, title, options) in enumerate(QUESTIONS, start=1):
        choices = "；".join(f"{value} {option}" for value, option in enumerate(options))
        lines.append(f"{index}. {title}：{choices}")
    lines.append("请按 q1..q5 的顺序告诉我您的选择，例如：1、0、2、1、1。")
    return "\n".join(lines)


def _label(table: Mapping[str, str], value: str) -> str:
    """把银行返回的枚举值翻成中文；不认识的取值原样显示，不猜。"""

    return table.get(value, value)


def risk_allows(risk_level: str | None, product_risk_level: str) -> bool:
    """产品风险是否不高于测评等级。

    测评等级缺失、产品或测评等级不是已知取值时一律返回 `False`：`risk_allows` 是推荐和预检
    的硬约束，"判断不了"必须落在这条约束的不允许一侧，而不是悄悄放行。
    """

    ceiling = RISK_ORDER.get(str(risk_level or ""))
    actual = RISK_ORDER.get(str(product_risk_level))
    if ceiling is None or actual is None:
        return False
    return actual <= ceiling


@dataclass(frozen=True, slots=True)
class Fit:
    """一个产品对某个用户条件的适配结论：能不能，以及原因（中文，可直接展示给用户）。

    `reasons` 在适配时是"为什么适合"，在不适配时是"为什么不适合"；两种情况都只复述规则与银行
    返回的数值，不替用户下投资结论。
    """

    suitable: bool
    reasons: tuple[str, ...]


def evaluate(
    product: Product,
    *,
    risk_level: str | None,
    amount_minor: int | None = None,
    horizon_months: int | None = None,
    liquidity: str | None = None,
) -> Fit:
    """按五条硬约束判断一个产品是否适配，并给出原因。

    五条检查：在售、风险不高于测评等级、金额在起购与单笔上限之间、期限不超过用钱时间、流动性
    匹配。缺一个判断依据（没有测评、没有金额、没有用钱时间、没有流动性要求）就不算适配：
    "无法判断"不等于"适合"。
    """

    failures: list[str] = []
    reasons: list[str] = []

    if product.status == "available":
        reasons.append("产品在售")
    else:
        failures.append(f"产品{_label(STATUS_LABELS, product.status)}")

    if risk_allows(risk_level, product.risk_level):
        reasons.append(
            f"风险等级（{_label(RISK_LABELS, product.risk_level)}）不高于您的测评等级"
            f"（{_label(RISK_LABELS, str(risk_level))}）"
        )
    elif risk_level is None:
        failures.append("还没有有效的银行风险测评，无法判断风险是否匹配")
    else:
        failures.append(
            f"风险等级（{_label(RISK_LABELS, product.risk_level)}）高于您的测评等级"
            f"（{_label(RISK_LABELS, risk_level)}）"
        )

    if amount_minor is None:
        failures.append("还没有确认计划金额，无法判断起购额是否合适")
    elif amount_minor < product.min_amount_minor:
        failures.append(
            f"起购 {format_minor(product.min_amount_minor)} 高于您计划的 {format_minor(amount_minor)}"
        )
    elif amount_minor > product.max_amount_minor:
        failures.append(
            f"计划金额 {format_minor(amount_minor)} 超过单笔上限 "
            f"{format_minor(product.max_amount_minor)}"
        )
    else:
        reasons.append(
            f"起购 {format_minor(product.min_amount_minor)} 不超过您计划的 {format_minor(amount_minor)}"
        )

    if horizon_months is None:
        failures.append("还不知道您的用钱时间，无法判断期限是否合适")
    elif product.duration_months > horizon_months:
        failures.append(
            f"期限 {product.duration_months} 个月超过您的用钱时间 {horizon_months} 个月"
        )
    else:
        reasons.append(
            f"期限 {product.duration_months} 个月不超过您的用钱时间 {horizon_months} 个月"
        )

    if liquidity is None:
        failures.append("还不知道您是否需要随时赎回")
    elif liquidity == "flexible" and product.redeem_rule != "flexible":
        failures.append(f"{_label(REDEEM_LABELS, product.redeem_rule)}，不符合您随时可以赎回的要求")
    elif liquidity == "flexible":
        reasons.append("可随时赎回，符合您随时可以赎回的要求")
    else:
        # 用户表示可以接受到期才赎回：可随时赎回的产品也在可接受范围内。
        reasons.append(
            f"您表示可以接受到期赎回，该产品{_label(REDEEM_LABELS, product.redeem_rule)}"
        )

    if failures:
        return Fit(suitable=False, reasons=tuple(failures))
    return Fit(suitable=True, reasons=tuple(reasons))


# ---------------------------------------------------------------------- 申购规则（任务9）


class AmountError(ValueError):
    """金额不是可提交的整数分。理由只说规则，不回显模型或用户给的原文。"""


def normalize_amount_minor(value: object) -> int:
    """把金额规范成可提交的**整数分**；含糊、负数、零、超过两位小数一律拒绝。

    金额只有一种可提交形式：正整数分。所以

    - 负数、零拒绝——它们不是一笔申购；
    - 浮点数只有在**恰好是整分**时才通过（`2000.0` 通过、`2000.5` 与 `200000.5` 拒绝）；
      换算成元就是"超过两位小数"的金额，它要么是用户说错了，要么需要先澄清；
    - 字符串、`bool`、缺失一律拒绝：`"两千块"` 和 `"2000"` 都没有说清单位，属于含糊金额，
      不能由程序替用户猜一个数字去提交。

    拒绝的结果是"纠正一次或追问用户"，不是"取一个近似的整数"。
    """

    if isinstance(value, bool) or value is None:
        raise AmountError("金额必须是正整数分，不能为空或布尔值")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, float):
        if value != int(value):
            raise AmountError("金额必须精确到分（最多两位小数），不能有更小的零头")
        amount = int(value)
    else:
        raise AmountError("金额必须是整数分，请不要用文字或带单位的字符串表达金额")
    if amount <= 0:
        raise AmountError("金额必须是正整数分")
    return amount


@dataclass(frozen=True, slots=True)
class PurchaseCheck:
    """一笔具体申购的预检结论：能不能提交，以及原因（中文，可直接展示给用户）。

    `reasons` 在放行时是"为什么可以提交"，在拦截时是"为什么不能"；两种情况都只复述规则与银行
    返回的数值，不替用户下投资结论。
    """

    allowed: bool
    reasons: tuple[str, ...]


def check_purchase(
    product: Product,
    *,
    account: Account | None,
    risk_level: str | None,
    amount_minor: int,
    liquidity: str | None = None,
    user_id: str | None = None,
) -> PurchaseCheck:
    """提交前对一笔申购做硬约束预检。

    九条检查：在售、风险不高于测评等级、金额在起购与单笔上限之间、账户存在且属于本人、账户
    状态正常、不是定期账户、可用余额足够、用户已表达的流动性要求被满足。缺一个判断依据
    （没有测评、没有账户）就不放行——"判断不了"不等于"可以买"。

    这是 Agent 侧的预检，**不替代银行的复核**：银行仍会按自己的规则重查一遍，所以这里放行
    不代表银行一定接受，而这里拦截则一定不提交。
    """

    failures: list[str] = []
    reasons: list[str] = []

    if product.status == "available":
        reasons.append("产品在售")
    else:
        failures.append(f"产品{_label(STATUS_LABELS, product.status)}")

    if risk_allows(risk_level, product.risk_level):
        reasons.append(
            f"风险等级（{_label(RISK_LABELS, product.risk_level)}）不高于您的测评等级"
            f"（{_label(RISK_LABELS, str(risk_level))}）"
        )
    elif risk_level is None:
        failures.append("还没有有效的银行风险测评，无法确认风险匹配")
    else:
        failures.append(
            f"风险等级（{_label(RISK_LABELS, product.risk_level)}）高于您的测评等级"
            f"（{_label(RISK_LABELS, risk_level)}）"
        )

    if amount_minor < product.min_amount_minor:
        failures.append(
            f"金额 {format_minor(amount_minor)} 低于起购 {format_minor(product.min_amount_minor)}"
        )
    elif amount_minor > product.max_amount_minor:
        failures.append(
            f"金额 {format_minor(amount_minor)} 超过单笔上限 "
            f"{format_minor(product.max_amount_minor)}"
        )
    else:
        reasons.append(
            f"金额 {format_minor(amount_minor)} 在起购 {format_minor(product.min_amount_minor)}"
            f" 与单笔上限 {format_minor(product.max_amount_minor)} 之间"
        )

    if account is None:
        failures.append("没有找到您要付款的账户")
    elif user_id is not None and account.user_id != user_id:
        failures.append("付款账户不属于本人")
    elif account.status != "active":
        failures.append(f"付款账户状态为 {account.status}，不能扣款")
    elif account.account_type == "fixed":
        failures.append("定期账户不支持申购扣款")
    elif account.available_minor < amount_minor:
        failures.append(
            f"付款账户可用余额 {format_minor(account.available_minor)} 不足 "
            f"{format_minor(amount_minor)}"
        )
    else:
        reasons.append(
            f"付款账户可用余额 {format_minor(account.available_minor)} 足够支付"
            f" {format_minor(amount_minor)}"
        )

    if liquidity == "flexible" and product.redeem_rule != "flexible":
        failures.append(f"{_label(REDEEM_LABELS, product.redeem_rule)}，不符合您随时可以赎回的要求")
    elif liquidity == "flexible":
        reasons.append("可随时赎回，符合您随时可以赎回的要求")

    if failures:
        return PurchaseCheck(allowed=False, reasons=tuple(failures))
    return PurchaseCheck(allowed=True, reasons=tuple(reasons))


def purchase_preview(
    product: Product,
    account: Account,
    *,
    risk_level: str | None,
    amount_minor: int,
    term_months: int | None,
) -> str:
    """待确认预览的原文，由程序模板生成。

    它一起被冻结进快照（`PurchaseSnapshot.preview`）：用户确认的是**这一段话**，所以金额、
    账户、风险、赎回规则和模拟交易提示都必须由程序写死，而不是由模型自由表达。
    """

    months = "无固定期限" if not term_months else f"{term_months} 个月"
    return "\n".join(
        [
            "请确认这笔模拟申购：",
            f"- 产品：{product.name}（{product.product_id}）",
            f"- 金额：{format_minor(amount_minor)}（{amount_minor} 分）",
            f"- 付款账户：{account.account_id}（可用余额 {format_minor(account.available_minor)}）",
            f"- 风险：产品{_label(RISK_LABELS, product.risk_level)}风险，"
            f"您的测评等级为{_label(RISK_LABELS, str(risk_level))}风险",
            f"- 期限：{months}，{_label(REDEEM_LABELS, product.redeem_rule)}",
            f"- 起购 {format_minor(product.min_amount_minor)}，"
            f"单笔上限 {format_minor(product.max_amount_minor)}",
            # 这一段由程序写死，但它**不能**替银行回答"要不要验证码"：那取决于银行的能力声明，
            # 由确认卡片自己显示（需要时卡片上就有输入框）。在这里写一句"不需要验证码"，一旦
            # 银行要，卡片上就出现输入框——同一屏上自相矛盾。
            "点击确认即提交给银行；取消不会产生任何交易。",
            SIMULATION_NOTE,
        ]
    )


def purchase_success(receipt: PurchaseReceipt, *, balance_minor: int | None = None) -> str:
    """成交话术，由程序模板生成：关键金额与业务编号都来自银行回执。

    只有在收到银行已提交回执、并且回执与意图一致时才用这一段；余额是写后重新读到的银行事实，
    读不到就只报告回执里的编号，不编一个数字。
    """

    lines = [
        "模拟申购已完成：",
        f"- 产品：{receipt.product_id}",
        f"- 金额：{format_minor(receipt.amount_minor)}（本金 {format_minor(receipt.principal_minor)}）",
        f"- 订单号：{receipt.order_id}",
        f"- 持仓号：{receipt.holding_id}",
        f"- 资金流水号：{receipt.transaction_id}",
    ]
    if balance_minor is not None:
        lines.append(f"- 付款账户余额：{format_minor(balance_minor)}")
    if receipt.replayed:
        lines.append("（银行按原幂等键返回了同一笔订单，没有重复扣款。）")
    lines.append("以上是模拟交易记录，不是真实成交。")
    return "\n".join(lines)


# ---------------------------------------------------------------------- 赎回规则（任务10）


class RedeemAmountError(ValueError):
    """赎回金额不是可提交的表达。理由只说规则，不回显模型或用户给的原文。"""


@dataclass(frozen=True, slots=True)
class RedeemAmount:
    """赎回金额的三种表达：具体金额、全部、一半。

    - `amount`：用户给了具体金额（正整数分）；
    - `all`：全部赎回，取持仓的可赎回额；
    - `half`：一半，按可赎回额向下取整到分。

    "全部"和"一半"都必须**拿到银行的可赎回额之后**才解析成一个具体金额：所以预览前它一定
    是一个确定的整数分，而不是让模型自己算一个数，也不是让用户去核对一个四舍五入过的金额。
    """

    kind: str
    amount_minor: int | None = None

    def resolve(self, redeemable_minor: int) -> int:
        """按银行的可赎回额解析出本次赎回的具体金额（整数分）。"""

        if self.kind == "amount":
            assert self.amount_minor is not None
            return self.amount_minor
        if self.kind == "all":
            return redeemable_minor
        return redeemable_minor // 2

    def as_state(self) -> dict[str, Any]:
        """进工作流状态的形状：只有普通数据。"""

        return {"kind": self.kind, "amount_minor": self.amount_minor}


def parse_redeem_amount(value: object) -> RedeemAmount:
    """把模型的赎回金额表达读成一个 `RedeemAmount`。

    三种可提交形式：正整数分、`{"all": true}`、`{"half": true}`。其余一律拒绝——含糊字符串、
    负数、零、超过两位小数、序号引用都是"要纠正或追问"的信号，而不是"猜一个数字去赎回"。
    """

    if isinstance(value, Mapping):
        if set(value) == {"all"} and value["all"] is True:
            return RedeemAmount("all")
        if set(value) == {"half"} and value["half"] is True:
            return RedeemAmount("half")
        raise RedeemAmountError('赎回金额只接受整数分、{"all": true} 或 {"half": true}')
    try:
        return RedeemAmount("amount", normalize_amount_minor(value))
    except AmountError as error:
        raise RedeemAmountError(str(error)) from error


@dataclass(frozen=True, slots=True)
class RedeemCheck:
    """一笔具体赎回的预检结论：能不能提交，以及原因（中文，可直接展示给用户）。"""

    allowed: bool
    reasons: tuple[str, ...]


def check_redeem(
    holding: Holding | None,
    *,
    account: Account | None,
    amount_minor: int,
    user_id: str | None = None,
) -> RedeemCheck:
    """提交前对一笔赎回做硬约束预检。

    五条检查：持仓存在且属于本人、没有全部赎回、可赎回额足够、到账账户存在且属于本人、账户
    状态正常。**不检查风险测评**：赎回是取回自己的持仓，不要求重新做问卷（计划任务10）。

    这是 Agent 侧的预检，**不替代银行的复核**：银行仍会按自己的规则重查一遍（归属、到期、
    可赎回额），所以这里放行不代表银行一定接受，而这里拦截则一定不提交。
    """

    failures: list[str] = []
    reasons: list[str] = []

    if amount_minor <= 0:
        failures.append("赎回金额必须是正整数分")

    if holding is None:
        failures.append("没有找到要赎回的持仓")
    elif user_id is not None and holding.user_id != user_id:
        failures.append("该持仓不属于本人")
    elif holding.status == "redeemed":
        failures.append("该持仓已经全部赎回")
    elif holding.redeemable_minor <= 0:
        failures.append("这笔持仓当前不可赎回（定期产品未到期，或已经全部赎回）")
    elif amount_minor > holding.redeemable_minor:
        failures.append(
            f"赎回金额 {format_minor(amount_minor)} 超过可赎回金额 "
            f"{format_minor(holding.redeemable_minor)}"
        )
    else:
        reasons.append(
            f"可赎回金额 {format_minor(holding.redeemable_minor)} 足够本次赎回 "
            f"{format_minor(amount_minor)}"
        )

    if account is None:
        failures.append("没有找到到账账户")
    elif user_id is not None and account.user_id != user_id:
        failures.append("到账账户不属于本人")
    elif account.status != "active":
        failures.append(f"到账账户状态为 {account.status}，不能入账")
    else:
        reasons.append(f"到账账户 {account.account_id} 状态正常")

    if failures:
        return RedeemCheck(allowed=False, reasons=tuple(failures))
    return RedeemCheck(allowed=True, reasons=tuple(reasons))


def redeem_preview(
    holding: Holding,
    account: Account,
    *,
    amount_minor: int,
    redeemable_minor: int,
) -> str:
    """待确认赎回预览的原文，由程序模板生成。

    它一起被冻结进快照（`RedeemSnapshot.preview`）：用户确认的是**这一段话**，所以持仓、金额、
    到账账户和模拟交易提示都必须由程序写死，而不是由模型自由表达。本金与已实现收益的分摊由
    银行按持仓当前价值计算，所以预览只展示本次赎回的具体金额，不预告一个自己算出来的分摊。
    """

    full = amount_minor == redeemable_minor
    return "\n".join(
        [
            "请确认这笔模拟赎回：",
            f"- 产品：{holding.product_id}",
            f"- 持仓号：{holding.holding_id}",
            f"- 赎回金额：{format_minor(amount_minor)}（{amount_minor} 分）"
            f"（{'全部赎回' if full else '部分赎回'}）",
            f"- 可赎回金额：{format_minor(redeemable_minor)}",
            f"- 到账账户：{account.account_id}",
            "- 本次释放本金与已实现收益由银行按持仓当前价值计算，以银行回执为准。",
            "点击确认即提交给银行；取消不会产生任何交易。",
            SIMULATION_NOTE,
        ]
    )


def redeem_success(receipt: RedemptionReceipt, *, balance_minor: int | None = None) -> str:
    """赎回成交话术，由程序模板生成：关键金额与业务编号都来自银行回执。

    释放本金与实现收益是**银行算出来的**分摊（需求 §6.2：Agent 不另算权威数值），这里只搬运。
    只有在收到银行已提交回执、并且回执与意图一致时才用这一段。
    """

    lines = [
        "模拟赎回已完成：",
        f"- 赎回金额：{format_minor(receipt.amount_minor)}",
        f"- 释放本金：{format_minor(receipt.principal_redeemed_minor)}（银行分摊）",
        f"- 本次实现收益：{format_minor(receipt.interest_minor)}（银行分摊）",
        f"- 订单号：{receipt.order_id}",
        f"- 持仓号：{receipt.holding_id}",
        f"- 资金流水号：{receipt.transaction_id}",
        f"- 持仓状态：{_label(REDEEM_STATUS_LABELS, receipt.holding_status)}，"
        f"剩余本金 {format_minor(receipt.remaining_principal_minor)}",
    ]
    if balance_minor is not None:
        lines.append(f"- 到账账户余额：{format_minor(balance_minor)}")
    if receipt.replayed:
        lines.append("（银行按原幂等键返回了同一笔订单，没有重复入账。）")
    lines.append("以上是模拟交易记录，不是真实成交。")
    return "\n".join(lines)
