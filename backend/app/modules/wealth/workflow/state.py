"""理财内部对话工作流的状态、槽位词表、预算和事实累积。

这一层是工作流自己的词汇表：节点之间只通过它交换数据，不互相调用。它**不**包含
`TrustedContext`——调用方身份属于一次调用，不属于可被检查点保存的状态。基座对
`AgentState` 的立场相同（身份只走工具句柄），这里沿用：图在每次 `handle` 时按调用方重建，
身份由闭包带进工具调用，不落进 state。

三个预算写在这里，因为 `nodes` 与 `graph` 都要读它：一次 `handle` 最多 8 次模型调用、
12 次节点推进、格式错误最多纠正 1 次（计划任务6）。它们是这一层的硬边界，不是提示词里的建议。

任务7 在这一层加上**会话槽位**（`SLOTS`）：计划投入金额、产品、账户、持仓、用钱时间、流动性。槽位是
用户说过的业务信息，由运行时按 (user_id, session_id) 保存并在每次 `handle` 时作为输入写进
state；校验与解析（`normalize_slots`）也在这里，所以"槽位名必须是词表里的、金额必须是正整数分、
编号必须来自查到的记录、'第几个'必须按最近展示顺序解析"是结构性的，而不是靠提示词自律。

单笔订单金额（`amount_minor`）**不在** `SLOTS` 里，它是计划金额的窄兄弟：`plan_amount_minor` 是
用户在推荐阶段说的"我打算投多少"（筛选条件），`amount_minor` 是"这一笔买／赎多少"（订单参数）。
两者共用一个名字时，推荐阶段写进会话的计划金额会被申购节点直接当成订单金额用掉，所以订单金额只能
随 `prepare_purchase`／`prepare_redeem` 决策进入会话（`normalize_slots(allowed=...)`），`set_slots`
写不进去。

任务8 用同一份词表标注了**推荐必须凑齐的三个槽位**（`RECOMMENDATION_SLOTS`）：金额、用钱时间、
流动性。少一个就不推荐，由 `workflow` 的推荐节点直接追问——这也不是提示词建议。

`facts` 只由银行返回和 `domain` 规则算出的结论拼成：只读工具的返回、问卷保存的回执、以及推荐／
对比的结论。模型没有任何通道往这里写数值，所以"关键产品、金额和状态只能来自工具事实"是结构性
的，而不是靠提示词自律。

任务9 在这一层加上**申购预览的槽位**（`PURCHASE_SLOTS`：产品、金额、账户）和承载冻结快照的
`purchase` 通道：申购节点凑齐三个槽位、预检通过后把 `PurchaseSnapshot` 写进 `purchase`，运行时
据此铸待办并等确认。金额的规范化（`normalize_amount_minor`）来自 `domain.rules`，这一层只转译
它的拒绝理由。

任务10 加上**赎回预览的槽位**（`REDEEM_SLOTS`：持仓、金额、到账账户）、`redeem` 通道与
`recent_holding` 会话事实：赎回金额可以是具体整数分、`{"all": true}`（全部）或 `{"half": true}`
（一半），后两种在拿到银行可赎回额之后才解析成具体金额，所以 `normalize_redeem_slots` 只校验形状；
持仓写 `{"recent": true}` 时绑定本会话最近一次成功申购的新仓（"刚才买的"），没有就追问而不是猜。

依赖方向：`workflow → contracts/domain`（外加复用基座的追加型 reducer 与域声明的字段元数据）。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Annotated, Any, TypedDict

from app.agents.declaration import FieldMeta
from app.agents.state import AgentState, append_results
from app.modules.wealth.domain.rules import AmountError, normalize_amount_minor

__all__ = [
    "ASKABLE_SLOTS",
    "LIQUIDITY_LABELS",
    "LIQUIDITY_VALUES",
    "MAX_CLARIFICATIONS",
    "MAX_CORRECTIONS",
    "MAX_MODEL_CALLS",
    "MAX_STEPS",
    "PendingData",
    "PURCHASE_SLOTS",
    "RECENT_HOLDING_REF",
    "RECOMMENDATION_SLOTS",
    "REDEEM_SLOTS",
    "RESOURCE_ID_FIELDS",
    "SLOTS",
    "SLOT_LABELS",
    "SLOT_RESOURCES",
    "SLOT_UNITS",
    "SessionData",
    "SlotError",
    "WEALTH_FIELDS",
    "WealthState",
    "WealthWorkflowState",
    "candidate_lines",
    "clues_to_slots",
    "collected_ids",
    "counter",
    "initial_state",
    "merge_business_ids",
    "merge_facts",
    "merge_session",
    "missing_slot_lines",
    "missing_slots",
    "normalize_redeem_slots",
    "normalize_slots",
    "resource_ids",
    "session_of",
    "slot_lines",
]

# 一次 handle 的硬上限（计划任务6）。
#
# `MAX_STEPS` 数的是**做了工作的节点推进**：节点每执行一次 +1，而"发现预算用尽、只写一句结论"
# 的那一次收尾不计入——所以一次 handle 里真正推进的节点不超过 12 个，执行总数不超过 13。
# 超限的路径不调用模型、不调用银行，直接用已有事实收尾。
MAX_MODEL_CALLS = 8
MAX_STEPS = 12
MAX_CORRECTIONS = 1

# 同一任务连续澄清的次数上限（计划任务7）：到了上限就停止，不无限追问。
MAX_CLARIFICATIONS = 5

# 每个资源列表用哪个字段判重。同一份记录被列两次（先查列表再查详情）只留一条，
# 但两条都来自工具返回——这一层只做合并，不构造任何记录。
RESOURCE_ID_FIELDS: Mapping[str, str] = {
    "products": "product_id",
    "accounts": "account_id",
    "holdings": "holding_id",
    "orders": "order_id",
}

# ---------------------------------------------------------------------- 会话槽位（计划任务7）

# 会话保存的业务槽位。顺序就是提示词与待办里列出的顺序。
# `amount_minor`（单笔订单金额）不在这里：它只能随 prepare_purchase／prepare_redeem 进入会话。
SLOTS: tuple[str, ...] = (
    "plan_amount_minor",
    "product_id",
    "account_id",
    "holding_id",
    "horizon_months",
    "liquidity",
)

# 可以被追问的槽位：会话槽位，加上"本次操作金额"。
# 订单金额只能随 prepare_purchase／prepare_redeem 进入会话，但缺它的时候程序要能问用户，
# 所以追问这一条路径允许点名 `amount_minor`（`set_slots` 仍然不许写它）。
ASKABLE_SLOTS: tuple[str, ...] = (*SLOTS, "amount_minor")

SLOT_LABELS: Mapping[str, str] = {
    # `amount_minor` 不在 `SLOTS` 里，但缺槽追问要按这个名字渲染，所以标签留在这里。
    "amount_minor": "本次操作金额",
    "plan_amount_minor": "计划投入金额（推荐筛选）",
    "product_id": "产品编号",
    "account_id": "账户编号",
    "holding_id": "持仓编号",
    "horizon_months": "用钱时间（月）",
    "liquidity": "流动性要求",
}

# 域声明用的字段表（计划任务3）：每个可追问的槽位一个自由文本字段。
# 只借 `SLOT_LABELS` 渲染标签，不借 `SLOT_UNITS`——单位是给人看的说明，不是客户端认得的字段类型。
WEALTH_FIELDS: Mapping[str, FieldMeta] = {
    name: FieldMeta(label=SLOT_LABELS[name], type="text") for name in ASKABLE_SLOTS
}

# 推荐必须凑齐的三个槽位（需求 §7.2）：计划金额、用钱时间、是否需要随时赎回。
# 缺一个就不推荐，由推荐节点直接追问；产品、账户、持仓不在其中——它们是推荐的结果而不是输入。
# 计划金额用 `plan_amount_minor`：推荐筛的是规模，不是某一笔订单的金额。
RECOMMENDATION_SLOTS: tuple[str, ...] = ("plan_amount_minor", "horizon_months", "liquidity")

# 一次申购预览必须凑齐的三个槽位（计划任务9）：买哪只产品、多少钱、用哪个账户付款。
# 缺一个就不生成预览，由申购节点直接追问——**不默认挑第一个账户**去付款（计划任务7）。
PURCHASE_SLOTS: tuple[str, ...] = ("product_id", "amount_minor", "account_id")

# 一次赎回预览必须凑齐的三个槽位（计划任务10）：赎回哪一笔持仓、多少钱、入账到哪个账户。
# 到账账户复用 `account_id`：它同样是"用户点名的那个账户"，只是方向相反（入账而不是扣款）。
REDEEM_SLOTS: tuple[str, ...] = ("holding_id", "amount_minor", "account_id")

# "刚才买的"的引用形式：`{"recent": true}` 解析成本会话最近一次成功申购的那笔新仓，
# 而不是按数据库顺序猜第一笔（计划任务10）。
RECENT_HOLDING_REF = "recent"

SLOT_UNITS: Mapping[str, str] = {
    "amount_minor": "整数分",
    "plan_amount_minor": "整数分",
    "horizon_months": "月",
}

# 编号类槽位引用哪个资源。用户说"第二个"时，按这个资源**最近一次展示的顺序**解析，
# 而不是数据库返回的顺序。
SLOT_RESOURCES: Mapping[str, str] = {
    "product_id": "products",
    "account_id": "accounts",
    "holding_id": "holdings",
}

# 流动性只表达"要不要能随时赎回"；取值与产品的 redeem_rule 同词表，便于后续筛选直接对齐。
LIQUIDITY_VALUES: tuple[str, ...] = ("flexible", "maturity_only")

LIQUIDITY_LABELS: Mapping[str, str] = {
    "flexible": "需要随时可以赎回",
    "maturity_only": "可以接受到期才赎回",
}


class SlotError(ValueError):
    """模型给出的槽位名或槽位值不可用。理由只说规则，不回报值。"""


def resource_ids(facts: Mapping[str, Any] | None) -> dict[str, frozenset[str]]:
    """`facts` 里每个资源已经查到的编号，按资源分组。

    编号类槽位的"只能来自查到的记录"就是拿它比的：产品槽位只能填产品编号，不能填一个账户号。
    """

    found: dict[str, frozenset[str]] = {}
    for resource, field in RESOURCE_ID_FIELDS.items():
        rows = (facts or {}).get(resource)
        if not isinstance(rows, list):
            continue
        values = {
            row[field]
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get(field), str)
            and str(row[field]).strip()
        }
        if values:
            found[resource] = frozenset(values)
    return found


def _ordinal(value: Mapping[str, Any]) -> int | None:
    """读 `{"ordinal": n}` 形式的序号引用；不是这个形状就返回 None。"""

    if set(value) != {"ordinal"}:
        return None
    number = value["ordinal"]
    if isinstance(number, bool) or not isinstance(number, int):
        raise SlotError("ordinal 必须是整数")
    if number < 1:
        raise SlotError("ordinal 必须从 1 开始")
    return number


def _resolve_value(
    name: str,
    value: Any,
    *,
    known: Mapping[str, frozenset[str]],
    shown: Mapping[str, tuple[str, ...]],
    recent_holding: str | None = None,
) -> Any:
    """把一个槽位值解析成可保存的形式；不可用抛 `SlotError`。"""

    resource = SLOT_RESOURCES.get(name)
    if isinstance(value, Mapping):
        if (
            name == "holding_id"
            and set(value) == {RECENT_HOLDING_REF}
            and value[RECENT_HOLDING_REF] is True
        ):
            # "刚才买的"：绑定最近一次成功申购的那笔新仓；没有就追问，不猜一笔。
            if not recent_holding:
                raise SlotError("最近没有成功申购的持仓，请说明要赎回哪一笔")
            return recent_holding
        number = _ordinal(value)
        if number is None:
            raise SlotError(f"{name} 的取值形式不可用")
        if resource is None:
            raise SlotError(f"{name} 不接受序号引用")
        order = tuple(shown.get(resource) or ())
        if number > len(order):
            raise SlotError(f"最近没有展示过第 {number} 个候选")
        return order[number - 1]

    if name in ("amount_minor", "plan_amount_minor"):
        # 金额规则只有一处：`domain.rules.normalize_amount_minor`。整数分、正数、精确到分；
        # 负数、零、非整数分和含糊表达都在那里被拒，这一层不另写一套换算。
        try:
            return normalize_amount_minor(value)
        except AmountError as error:
            raise SlotError(str(error)) from error
    if name == "horizon_months":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SlotError("horizon_months 必须是不小于 0 的整数")
        return value
    if name == "liquidity":
        if value not in LIQUIDITY_VALUES:
            raise SlotError("liquidity 必须是 " + "／".join(LIQUIDITY_VALUES))
        return value
    if resource is not None:
        if not isinstance(value, str) or not value.strip():
            raise SlotError(f"{name} 必须是非空字符串")
        if value not in known.get(resource, frozenset()) and value not in (
            shown.get(resource) or ()
        ):
            raise SlotError(f"{name} 只能来自已经查到的记录")
        return value
    raise SlotError(f"未知的槽位 {name}")


def normalize_slots(
    values: Mapping[str, Any] | None,
    *,
    facts: Mapping[str, Any] | None = None,
    shown: Mapping[str, Sequence[str]] | None = None,
    recent_holding: str | None = None,
    allowed: Collection[str] = SLOTS,
) -> dict[str, Any]:
    """校验并解析模型给出的一组槽位值。

    三条机械规则：槽位名必须来自 `allowed`；金额是正整数分、时间是不小于 0 的整数月；编号类
    槽位只能填本轮 `facts` 里查到的编号或最近展示过的候选序号。任何一条不满足都抛 `SlotError`，
    由调用方决定是否纠正一次。

    `allowed` 默认是会话词表 `SLOTS`；`prepare_purchase`／`prepare_redeem` 传自己的窄词表，这样
    单笔订单金额（`amount_minor`）只在订单动作里合法，`set_slots` 写不进去。

    `recent_holding` 是本会话最近一次成功申购的持仓编号：`holding_id` 写 `{"recent": true}` 时
    解析成它（"刚才买的"），没有就抛 `SlotError`。
    """

    if not isinstance(values, Mapping) or not values:
        raise SlotError("slots 必须是一个非空对象")
    known = resource_ids(facts)
    display = {str(key): tuple(value) for key, value in (shown or {}).items()}
    resolved: dict[str, Any] = {}
    for name, value in values.items():
        if not isinstance(name, str) or name not in allowed:
            raise SlotError(f"未知的槽位 {name}")
        resolved[name] = _resolve_value(
            name, value, known=known, shown=display, recent_holding=recent_holding
        )
    return resolved


def normalize_redeem_slots(
    values: Mapping[str, Any] | None,
    *,
    facts: Mapping[str, Any] | None = None,
    shown: Mapping[str, Sequence[str]] | None = None,
    recent_holding: str | None = None,
) -> dict[str, Any]:
    """校验并解析模型给出的**赎回**槽位值（计划任务10）。

    与 `normalize_slots` 只有两处不同：槽位名来自 `REDEEM_SLOTS`；`amount_minor` 还接受
    `{"all": true}`（全部）与 `{"half": true}`（一半）。这两种形式要在**拿到银行可赎回额之后**
    才解析成一个具体金额，所以这里只校验形状，原样交给赎回节点去解析——预览前它一定是一个
    确定的整数分。
    """

    if not isinstance(values, Mapping) or not values:
        raise SlotError("slots 必须是一个非空对象")
    known = resource_ids(facts)
    display = {str(key): tuple(value) for key, value in (shown or {}).items()}
    resolved: dict[str, Any] = {}
    for name, value in values.items():
        if not isinstance(name, str) or name not in REDEEM_SLOTS:
            raise SlotError(f"未知的槽位 {name}")
        if name == "amount_minor" and isinstance(value, Mapping):
            if (set(value) == {"all"} and value["all"] is True) or (
                set(value) == {"half"} and value["half"] is True
            ):
                resolved[name] = dict(value)
                continue
            raise SlotError('amount_minor 只接受整数分、{"all": true} 或 {"half": true}')
        resolved[name] = _resolve_value(
            name, value, known=known, shown=display, recent_holding=recent_holding
        )
    return resolved


def missing_slots(slots: Mapping[str, Any] | None) -> tuple[str, ...]:
    """还没有填写的槽位，按 `SLOTS` 的顺序。"""

    filled = {name for name, value in (slots or {}).items() if value is not None}
    return tuple(name for name in SLOTS if name not in filled)


# ---------------------------------------------------------------------- 外层线索 → 槽位


def clues_to_slots(answered: Mapping[str, Any] | None) -> dict[str, Any]:
    """把外层记下的"已答项"读回会话槽位；不认识或读不出的条目一律丢掉。

    外层保存的是**语义线索**（文本），不是业务数据：它只知道"用户回答过用钱时间"，不知道
    槽位值该是什么类型。把线索读回业务含义是理财的事，规则只有三条，方向都是"宁可少恢复，
    不可恢复错"：

    - 槽位名必须在 `SLOTS` 里。外层可能存着更早版本记下的名字，理财不认识就不认它。
    - 金额与时间只接受能读成整数的写法（`"200000"`）；含糊的文本丢掉，让模型重新问清楚。
    - 编号类只接受非空字符串，而且只是**定位线索**：它到底存不存在，仍然要重新向银行查。

    恢复出来的槽位优先级最低：调用方按 `{**线索, **会话槽位}` 合并，本轮模型新读到的值再覆盖
    在上面（"当前用户明确纠正 > 较新的明确用户信息 > 较早上下文"）。
    """

    if not isinstance(answered, Mapping):
        return {}
    resolved: dict[str, Any] = {}
    for name, value in answered.items():
        if not isinstance(name, str) or name not in SLOTS:
            continue
        try:
            if name in ("plan_amount_minor", "amount_minor", "horizon_months"):
                resolved[name] = _resolve_value(
                    name,
                    _as_memory_number(value),
                    known={},
                    shown={},
                    recent_holding=None,
                )
            elif name == "liquidity":
                if value in LIQUIDITY_VALUES:
                    resolved[name] = value
            elif isinstance(value, str) and value.strip():
                resolved[name] = value.strip()
        except SlotError:
            continue
    return resolved


def _as_memory_number(value: Any) -> Any:
    """把记忆里的数字文本读回整数；读不出来就原样返回，让槽位校验去拒绝它。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def _slot_text(name: str, value: Any) -> str:
    """一个槽位值的展示形式；金额用元加分，不引入浮点。"""

    if name in ("amount_minor", "plan_amount_minor") and isinstance(value, int):
        sign = "-" if value < 0 else ""
        amount = abs(value)
        return f"{sign}{amount // 100}.{amount % 100:02d} 元（{value} 分）"
    if name == "liquidity":
        return LIQUIDITY_LABELS.get(str(value), str(value))
    return str(value)


def slot_lines(slots: Mapping[str, Any] | None) -> str:
    """提示词里的"已经知道的槽位"：带上槽位名，模型才能在 missing 里引用它。"""

    lines = [
        f"- {name}（{SLOT_LABELS[name]}）：{_slot_text(name, slots[name])}"
        for name in SLOTS
        if isinstance(slots, Mapping) and slots.get(name) is not None
    ]
    return "\n".join(lines) or "（还没有）"


def missing_slot_lines(slots: Mapping[str, Any] | None) -> str:
    """提示词里的"还没有填写的槽位"：模型只能从这里挑名字去追问。"""

    lines = [f"- {name}（{SLOT_LABELS[name]}）" for name in missing_slots(slots)]
    return "\n".join(lines) or "（已经齐了）"


def candidate_lines(shown: Mapping[str, Sequence[str]] | None) -> str:
    """提示词里的"最近展示顺序"：用户说"第几个"就按这份编号解析。"""

    lines = [
        f"- {resource} 第{index}个：{identifier}"
        for resource in sorted(shown or {})
        for index, identifier in enumerate(shown[resource], start=1)
    ]
    return "\n".join(lines) or "（还没有展示过候选）"


class WealthWorkflowState(TypedDict, total=False):
    """一次处理的状态。通道默认是"最后写入生效"，只有累积型字段带 reducer。

    `request_text`／`question`／`answer_text` 是运行时的输入（用户原话），不是模型输出；
    `task` 是"理解请求"节点归一化后给提示词用的任务描述。
    `slots`／`shown`／`clarifications`／`pending_prompt` 也是运行时的输入：它们来自会话表，
    由运行时在每次 `handle` 时写进来（计划任务7）。
    """

    # ---- 输入（运行时写入）
    request_text: str
    question: str | None
    answer_text: str | None
    # ---- 外层会话视图（运行时从 conversation 写入；没有会话时为空）
    # 这三块是**历史**，不是指令：`summary_text` 覆盖更早的轮次，`history_text` 是保留的近期
    # 轮次，`memory_text` 是外层记下的语义线索。当前这句话不在里面——它走 `request_text`。
    summary_text: str
    history_text: str
    memory_text: str
    # ---- 会话输入（运行时从会话表写入）
    slots: dict[str, Any]
    shown: dict[str, list[str]]
    clarifications: int
    pending_prompt: str | None
    pending_missing: list[str]
    # ---- 理解请求
    task: str
    # ---- 工具事实（累积型）
    observations: Annotated[list[dict[str, Any]], append_results]
    facts: dict[str, Any]
    business_ids: dict[str, str]
    # ---- 申购预览（任务9）：预检通过后被冻结的快照，运行时据此铸待办
    purchase: dict[str, Any] | None
    # ---- 赎回预览（任务10）：预检通过后被冻结的快照，运行时据此铸待办
    redeem: dict[str, Any] | None
    # ---- 会话事实：本会话最近一次成功申购的新仓（"刚才买的"），由运行时从会话表写入
    recent_holding: dict[str, Any] | None
    # ---- 决策与纠正
    decision: dict[str, Any] | None
    correction: str | None
    # ---- 预算计数
    steps: int
    model_calls: int
    corrections: int
    reads: int
    # ---- 门禁事件（计划任务11）：这一次处理里模型有几次试图越过门禁（不存在的动作、
    # 白名单外的工具、只带业务输入的决策里塞了授权字段）。由决策节点自己累加，运行时按用户
    # 累计并据此锁定本进程写操作。它不是"格式错误"计数：坏 JSON、缺字段、含糊金额都不算。
    gate_violations: int
    # ---- 结论
    answer: str | None
    wealth_asked_question: str | None
    missing_slots: list[str]
    failure: str | None
    error_code: str | None


# ------------------------------------------------- 跨轮状态（计划任务3／W-6）


class WealthState(AgentState, total=False):
    """理财子图在图上保存的全部状态：会话、写意图与读流程工作区。

    会话不再留在进程里（`WealthAgent` 上没有 `_sessions`／`_pending` 表）：领域图把上一轮
    从检查点里读回的 `SessionData`／`PendingData` 递给这一轮，再把新的写回去。
    `wealth_asked_question` 是 `WealthWorkflowState.pending_question` 改名后的样子——
    基座 `AgentState.pending_question` 是另一种类型（dict），同名会被覆盖。
    """

    # ---- 会话（原 runtime._Session）
    wealth_slots: dict[str, Any]
    wealth_shown: dict[str, Any]
    wealth_intent_version: int
    wealth_clarifications: int
    wealth_pending_id: str | None
    wealth_last_holding: dict[str, str] | None
    wealth_unresolved: dict[str, Any] | None
    # ---- 写意图（原 runtime._Pending ＋ _WriteIntent ＋ _WriteDraft）
    wealth_pending: dict[str, Any] | None
    # ---- 读流程工作区（原 WealthWorkflowState 的字段）
    wealth_work: dict[str, Any]
    # ---- 追问（原 pending_question）
    wealth_asked_question: str | None


class SessionData(TypedDict, total=False):
    """一次调用的会话：调用方持有，随 `WealthAgent.handle` 进出。

    字段全是纯数据（msgpack 可序列化），所以它能原样摊进图状态。
    """

    slots: dict[str, Any]
    shown: dict[str, Any]
    intent_version: int
    clarifications: int
    pending_id: str | None
    last_holding: dict[str, str] | None
    unresolved: dict[str, Any] | None


class PendingData(TypedDict, total=False):
    """一次待办：原 `_Pending` ＋ `_WriteIntent` ＋ `_WriteDraft` 的纯数据字段。

    `write` 是写意图（原 `_WriteIntent`）：`snapshot`／`receipt` 用契约自己的
    `as_state`／`from_state` 存成 dict，`expires_at` 存 ISO 字符串——图上不放不可序列化的对象。
    """

    pending_id: str
    user_id: str
    scope_id: str
    question: str
    missing: Sequence[str]
    intent_version: int
    expires_at: str
    write: dict[str, Any] | None


def session_of(state: Mapping[str, Any]) -> SessionData:
    """从图状态里取出这一轮的会话；没有就是空会话。"""

    shown = state.get("wealth_shown")
    last_holding = state.get("wealth_last_holding")
    unresolved = state.get("wealth_unresolved")
    return SessionData(
        slots=dict(state.get("wealth_slots") or {}),
        shown={str(key): tuple(value) for key, value in (shown or {}).items()},
        intent_version=int(state.get("wealth_intent_version") or 0),
        clarifications=int(state.get("wealth_clarifications") or 0),
        pending_id=state.get("wealth_pending_id"),
        last_holding=dict(last_holding) if last_holding else None,
        unresolved=dict(unresolved) if unresolved else None,
    )


def merge_session(state: dict[str, Any], session: SessionData) -> None:
    """把会话写回图状态（原地改 `state`）。"""

    shown = session.get("shown") or {}
    last_holding = session.get("last_holding")
    unresolved = session.get("unresolved")
    state["wealth_slots"] = dict(session.get("slots") or {})
    state["wealth_shown"] = {str(key): list(value) for key, value in shown.items()}
    state["wealth_intent_version"] = int(session.get("intent_version") or 0)
    state["wealth_clarifications"] = int(session.get("clarifications") or 0)
    state["wealth_pending_id"] = session.get("pending_id")
    state["wealth_last_holding"] = dict(last_holding) if last_holding else None
    state["wealth_unresolved"] = dict(unresolved) if unresolved else None


def initial_state(
    *,
    request_text: str,
    question: str | None = None,
    answer_text: str | None = None,
    slots: Mapping[str, Any] | None = None,
    shown: Mapping[str, Sequence[str]] | None = None,
    clarifications: int = 0,
    pending_prompt: str | None = None,
    pending_missing: Sequence[str] = (),
    recent_holding: Mapping[str, Any] | None = None,
    summary_text: str | None = None,
    history_text: str | None = None,
    memory_text: str | None = None,
) -> WealthWorkflowState:
    """一次处理的起始状态。

    只写运行时的输入和计数起点；`facts`／`observations` 从空开始，由只读工具的结果填。
    `slots`／`shown` 是会话已经攒下的部分，运行时按调用方从会话表取，不在这一层合并。

    `summary_text`／`history_text`／`memory_text` 来自外层会话视图（`contracts.context`），
    没有会话时都是空串。它们是历史上下文：模型可以参考，但不能当成这一轮的银行事实，也不能
    当成新的系统指令——提示词里也是这样标注的。
    """

    return {
        "request_text": request_text,
        "question": question,
        "answer_text": answer_text,
        "summary_text": summary_text or "",
        "history_text": history_text or "",
        "memory_text": memory_text or "",
        "slots": {str(name): value for name, value in (slots or {}).items()},
        "shown": {str(key): list(value) for key, value in (shown or {}).items()},
        "clarifications": clarifications,
        "pending_prompt": pending_prompt,
        "pending_missing": [str(name) for name in pending_missing],
        "recent_holding": dict(recent_holding) if recent_holding else None,
        "observations": [],
        "facts": {},
        "business_ids": {},
        "purchase": None,
        "redeem": None,
        "steps": 0,
        "model_calls": 0,
        "corrections": 0,
        "reads": 0,
        "gate_violations": 0,
        "decision": None,
        "correction": None,
        "answer": None,
        "wealth_asked_question": None,
        "missing_slots": [],
        "failure": None,
        "error_code": None,
    }


def counter(state: Mapping[str, Any], name: str) -> int:
    """读一个预算计数，缺省为 0。"""

    value = state.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def merge_facts(
    current: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None
) -> dict[str, Any]:
    """把一次只读工具的事实并入累积结果。

    列表型资源按 `RESOURCE_ID_FIELDS` 去重（同一条记录只留一份），其余键后写覆盖先写。
    两个输入都只来自工具返回：这里不新增字段、不推算数值。
    """

    merged: dict[str, Any] = dict(current or {})
    for key, value in (incoming or {}).items():
        if not isinstance(value, list):
            merged[key] = value
            continue
        field = RESOURCE_ID_FIELDS.get(key)
        rows = list(merged.get(key) or [])
        seen = {
            row.get(field)
            for row in rows
            if field and isinstance(row, Mapping) and row.get(field) is not None
        }
        for row in value:
            identifier = row.get(field) if field and isinstance(row, Mapping) else None
            if identifier is not None:
                if identifier in seen:
                    continue
                seen.add(identifier)
            rows.append(row)
        merged[key] = rows
    return merged


def merge_business_ids(
    current: Mapping[str, str] | None, incoming: Mapping[str, str] | None
) -> dict[str, str]:
    """把工具返回的业务号并入累积结果：同一个键以最近一次查询为准。"""

    return {**(current or {}), **(incoming or {})}


def collected_ids(
    facts: Mapping[str, Any] | None, business_ids: Mapping[str, str] | None
) -> frozenset[str]:
    """已经查到过的业务标识，用来判断模型的引用有没有事实支撑。

    取两类：工具显式声明的 `business_ids` 值，以及 `facts` 里任何 `*_id` 字段的值
    （产品、账户、持仓、订单、测评编号都在其中）。两处都只来自工具返回。
    """

    found: set[str] = {
        value for value in (business_ids or {}).values() if isinstance(value, str) and value.strip()
    }

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if isinstance(value, str) and value.strip() and str(key).endswith("_id"):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(facts or {})
    return frozenset(found)
