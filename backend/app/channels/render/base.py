"""渲染器之间共用的那一层：状态怎么说、业务操作怎么称呼、金额怎么写成人话。

把这三样抽出来，是因为它们都不属于某一个平台。「待确认用暖色、失败用红色」是渠道无关的判断，
飞书把它翻译成 header 的 `template`，别的渠道翻译成别的颜色名；「transfer = 转账汇款」同样是
业务措辞，不是飞书措辞。放在 base 里，第二个渠道接入时就不必重新发明这套说法。

刻意不含的部分：任何 JSON 结构、任何平台字段名、任何时钟。渲染器因此是纯函数——同样的入参永远
产出同样的输出，卡片可以被逐字节比对、被回放、被断言。时间戳与流水号属于调用方（引擎在收尾时
补），渲染器不知道现在是几点，也就不会把"渲染时刻"混进按钮回传值里（那会让同一笔操作的两次
点击变成两次出账）。

调用方必须做的事：

1. 只传**展示字段**。要素是按白名单投影的，未知键会被静默丢弃——这是为了挡住误传进来的验证码、
   令牌或快照原文。反过来说，想让一个新字段出现在卡片上，得先在这个模块里给它一个标签。
2. 状态一律用 `CardStatus` 里的值。写错就 `ValueError`：颜色和标题是用户判断"这笔钱办没办成"
   的唯一线索，宁可当场炸掉，也不要渲染成一张颜色随便的卡片。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class CardStatus(StrEnum):
    """一张卡片此刻代表的状态：前三个是"在办"，后三个是结果。"""

    STREAMING = "streaming"  # 回答正在生成（打字机）
    PENDING = "pending"  # 等待用户点确认
    PROCESSING = "processing"  # 已经点了，正在核验并执行
    SUCCESS = "success"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CardTone(StrEnum):
    """卡片色调，平台无关；具体叫什么颜色名由各渠道渲染器翻译。"""

    BLUE = "blue"
    ORANGE = "orange"
    GREEN = "green"
    GREY = "grey"
    RED = "red"


#: 状态 → 色调（设计规格 §3.4.1：待确认橙色、处理中蓝色、成功绿色、取消灰色、失败红色）。
STATUS_TONES: dict[CardStatus, CardTone] = {
    CardStatus.STREAMING: CardTone.BLUE,
    CardStatus.PENDING: CardTone.ORANGE,
    CardStatus.PROCESSING: CardTone.BLUE,
    CardStatus.SUCCESS: CardTone.GREEN,
    CardStatus.CANCELLED: CardTone.GREY,
    CardStatus.FAILED: CardTone.RED,
}

#: 状态 → 卡片页脚的一句话。用户扫一眼就知道这张卡是"在办"还是"已办完"。
STATUS_NOTES: dict[CardStatus, str] = {
    CardStatus.STREAMING: "正在生成…",
    CardStatus.PENDING: "等待您的确认",
    CardStatus.PROCESSING: "正在核验并执行…",
    CardStatus.SUCCESS: "已完成",
    CardStatus.CANCELLED: "已取消",
    CardStatus.FAILED: "未完成",
}


def resolve_status(value: str) -> CardStatus:
    """把外部传进来的状态串收窄成 `CardStatus`；不认识的值当场拒绝。

    状态来自引擎，而引擎的分支是从事件里读的：一个拼错的状态（`"done"`）必须炸在这里，
    不能悄悄渲染成一张"看起来正常"的卡片。
    """

    try:
        return CardStatus(value)
    except ValueError as exc:
        known = "、".join(status.value for status in CardStatus)
        raise ValueError(f"未知的卡片状态 {value!r}；只认 {known}") from exc


@dataclass(frozen=True, slots=True)
class OperationWording:
    """一种业务操作在卡片上的说法：标题怎么说、两个按钮各写什么。"""

    title: str
    confirm_label: str
    cancel_label: str = "取消操作"


#: 业务操作的措辞表。键是模块给出的 `operation`（`TurnConfirmation.operation`）。
#: 表里没有的操作会退回模块自己的标题，再没有就写"待确认操作"——不认识不等于不能显示。
OPERATION_WORDING: dict[str, OperationWording] = {
    "transfer": OperationWording("转账汇款", "确认转账"),
    "cancel_scheduled_transfer": OperationWording("取消定时转账", "确认取消"),
    "send_report": OperationWording("发送账单报告", "确认发送"),
    "birthday_order": OperationWording("生日订单", "确认下单"),
    "cancel_birthday_order": OperationWording("撤销生日订单", "确认撤销"),
    "purchase_product": OperationWording("购买理财", "确认购买"),
    "redeem_product": OperationWording("赎回理财", "确认赎回"),
    "subscribe": OperationWording("开通订阅", "确认开通"),
    "charge": OperationWording("订阅扣款", "确认扣款"),
    "pause": OperationWording("暂停订阅", "确认暂停"),
    "resume": OperationWording("恢复订阅", "确认恢复"),
    "cancel": OperationWording("取消订阅", "确认取消"),
    "freeze": OperationWording("冻结卡片", "确认冻结"),
    "unfreeze": OperationWording("解冻卡片", "确认解冻"),
    "update_limit": OperationWording("调整卡片限额", "确认调整"),
    "update_restrictions": OperationWording("调整卡片限制", "确认调整"),
}

#: 兜底措辞：表里没有的 operation、且模块也没给 title 时用它。
DEFAULT_WORDING = OperationWording("待确认操作", "确认操作")

#: 模块常给的两个"万能标签"。它们不携带任何业务信息，所以让位给操作措辞表。
_GENERIC_LABELS = frozenset({"", "确认", "取消", "确认操作", "取消操作"})

#: 确认卡的标题前缀；与 `TurnConfirmation.title` 自带的"确认"前缀不重复叠加。
CONFIRM_TITLE_PREFIX = "操作确认："


def confirmation_title(operation: str, title: str | None = None) -> str:
    """确认卡标题：`操作确认：转账汇款`。

    优先操作措辞表（同一笔业务在不同模块里叫法才会一致），其次用模块给的标题，最后兜底。
    """

    wording = OPERATION_WORDING.get((operation or "").strip())
    if wording is not None:
        return CONFIRM_TITLE_PREFIX + wording.title

    text = (title or "").strip()
    if text.startswith(CONFIRM_TITLE_PREFIX):
        return text
    if text.startswith("确认"):
        text = text[len("确认") :].strip()
    return CONFIRM_TITLE_PREFIX + (text or DEFAULT_WORDING.title)


def confirmation_labels(
    operation: str,
    confirm_label: str | None = None,
    cancel_label: str | None = None,
) -> tuple[str, str]:
    """两个按钮的文字。模块给的标签优先，但它只是"确认"/"取消"这种万能词时让位给操作措辞。"""

    wording = OPERATION_WORDING.get((operation or "").strip(), DEFAULT_WORDING)
    confirm = (confirm_label or "").strip()
    cancel = (cancel_label or "").strip()
    return (
        confirm if confirm not in _GENERIC_LABELS else wording.confirm_label,
        cancel if cancel not in _GENERIC_LABELS else wording.cancel_label,
    )


@dataclass(frozen=True, slots=True)
class FieldLabel:
    """一个允许出现在确认卡上的载荷字段：键名、中文标签、是否金额。"""

    key: str
    label: str
    money: bool = False


#: 确认卡要素的**白名单**，顺序就是卡片上的阅读顺序。
#: 不在这张表里的键（验证码、令牌、快照原文……）永远不上卡片，也不进任何日志。
CONFIRMATION_FIELDS: tuple[FieldLabel, ...] = (
    FieldLabel("payee_name", "收款人"),
    FieldLabel("product_name", "业务对象"),
    FieldLabel("payee_account", "收款账号"),
    FieldLabel("account_display", "账户"),
    FieldLabel("amount", "金额", money=True),
    FieldLabel("amount_minor", "金额", money=True),
    FieldLabel("fee", "手续费", money=True),
    FieldLabel("fee_waived", "手续费减免"),
    FieldLabel("expires_at", "有效期至"),
)


def confirmation_fields(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """把一份确认载荷投影成 `[(标签, 值)]`：按白名单取，同名的只取第一个，空值跳过。

    `amount` 与 `amount_minor` 都叫"金额"，所以同一张卡片上只会出现其中一个——两份金额并排
    显示过，用户会以为有两笔钱。
    """

    fields: list[tuple[str, str]] = []
    seen: set[str] = set()
    for spec in CONFIRMATION_FIELDS:
        if spec.label in seen:
            continue
        text = display_value(payload.get(spec.key), money=spec.money)
        if text is None:
            continue
        seen.add(spec.label)
        fields.append((spec.label, text))
    return fields


def display_value(value: Any, *, money: bool = False) -> str | None:
    """一个载荷值的卡片写法；没东西可显示时返回 `None`。

    复合值（dict / list）一律不显示：要素应该是人能一眼读完的东西，而复合值里最可能藏着不该
    出现在卡片上的东西（快照原文、请求指纹）。布尔值写成"是 / 否"，因为 `True` 在中文卡片上
    读起来就是个 bug。
    """

    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, bool):
        # 金额位置上的布尔值是上游给错了：宁可空着，也不要在"金额"旁边显示"是"。
        return None if money else ("是" if value else "否")
    if money:
        return _money_text(value)
    text = str(value).strip()
    return text or None


def _money_text(value: Any) -> str | None:
    """金额：整数按"分"换算，浮点按"元"补两位，字符串只补一个货币符号。

    三种形状都来自真实载荷：引擎手里的整数分（权威）、上游漏乘 100 的浮点元（"500.0" 直接
    印成 `¥500.0` 会被读成五毛钱，所以固定两位），以及模块自己写好的中文字符串（"¥500.00"，
    它已经带符号，别再加一个）。非有限浮点是没有意义的金额，宁可空着也不要在"金额"旁边
    印一个 nan。
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return format_minor(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return f"¥{value:,.2f}"
    text = str(value).strip()
    if not text:
        return None
    return text if text.startswith("¥") else f"¥{text}"


def format_minor(minor: int) -> str:
    """`500000` -> `¥5,000.00`。整数分是唯一权威金额单位，换算只在这一处做。

    分开写两份（卡片一份、回执一份）的话，两边迟早对不上，而用户只会相信自己先看到的那一份。
    """

    if isinstance(minor, bool) or not isinstance(minor, int):
        raise TypeError(f"金额必须是整数分，收到 {type(minor).__name__}")
    sign = "-" if minor < 0 else ""
    yuan, fen = divmod(abs(minor), 100)
    return f"{sign}¥{yuan:,}.{fen:02d}"


@runtime_checkable
class BaseRenderer(Protocol):
    """渠道渲染器必须提供的三张卡片。引擎只认这三个方法，换渠道不该改引擎。

    `render_text_card`：一段问答正文（流式刷新与最终结果共用，靠 `status` 变色）。
    `render_confirmation_card`：待确认操作的交互卡片，按钮要带 q/p/d 回传值。
    `render_intermediate_card`：卡片被点下之后 3 秒内返回的中间态，必须不可再点。
    """

    def render_text_card(
        self, title: str, text: str, status: str = "streaming"
    ) -> dict[str, Any]: ...

    def render_confirmation_card(
        self,
        pending_dict: Mapping[str, Any],
        question_id: str,
        pending_id: str,
        mfa_url: str | None = None,
    ) -> dict[str, Any]: ...

    def render_intermediate_card(self, title: str, text: str) -> dict[str, Any]: ...
