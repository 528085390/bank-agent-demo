"""飞书 Interactive Card (Schema 2.0) 渲染器：把一次回答或一笔待确认操作变成一张卡片 JSON。

这个模块保证三件事：

1. **确定**。同样的入参产出逐字节相同的 JSON。卡片会被重渲染（节流刷新、确认之后改成回执、
   会话恢复时重放），渲染器不读时钟、不生成随机数、不带序号，所以两次渲染的按钮回传值完全一致。
   防重缓存的键与点击绑定的 q/p 都建在这份值上——它一漂移，同一笔操作就能被点两次，那是重复出账。
2. **回传值只有一个构造点**：`confirmation_action_value(q, p, d)`，键固定为 `q`/`p`/`d`。
   飞书 2.0 把按钮回传值放在 `behaviors[type=callback].value`，dispatcher 就该从那里读回来
   （不要另造一套顶层 value：同一份值两处存放，迟早对不上）。
3. **用户可见文本只来自白名单与措辞表**。`pending_dict` 来自引擎，里面可能有验证码、令牌或
   快照原文；要素经 `confirmation_fields()` 投影，未知键不会出现在卡片上；标题与按钮文字分别
   来自操作措辞表和 `_label_text()`（后者拒收任何含数字的"标签"）。本模块也不打任何日志。

调用方必须做的事：把要显示给用户的东西一次给全（标题、要素、是否需要验证码、签名短链）。
不要指望渲染器去查库、算时间或判断"这笔要不要验证码"——它不知道用户在哪个会话里，也不该知道。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.channels.render.base import (
    STATUS_NOTES,
    STATUS_TONES,
    CardTone,
    confirmation_fields,
    confirmation_labels,
    confirmation_title,
    resolve_status,
)

#: 色调 → 飞书 header `template` 的取值。平台名只在这张表里出现。
TEMPLATES: dict[CardTone, str] = {
    CardTone.BLUE: "blue",
    CardTone.ORANGE: "orange",
    CardTone.GREEN: "green",
    CardTone.GREY: "grey",
    CardTone.RED: "red",
}

#: 单张卡片正文的字符上限。飞书的卡片消息有大小限制，超了整条 PATCH 会被拒，用户就永远停在
#: 半句话上；截断并留一句提示，至少最终结果可见。取值留了余量：正常回答远小于它。
MAX_TEXT_CHARS = 8000

#: 截断提示。它出现在卡片正文里，用户应当看得出"这条回答不全"。
TRUNCATED_NOTICE = "\n\n…（内容过长，已截断）"

#: 卡片按钮回传的两种决定。与 `app.channels.contracts.Decision` 同值，这里是线上格式。
DECISION_CONFIRM = "confirm"
DECISION_CANCEL = "cancel"
DECISIONS = (DECISION_CONFIRM, DECISION_CANCEL)

#: 要验证码时，卡片上那条警示；两种措辞分别对应"给了短链"和"没给短链"。
CODE_NOTICE_WITH_LINK = (
    "**【安全核身】**本笔操作涉及风控，需要短信验证码。"
    "请点「安全核验并确认」，在银行安全页面输入验证码后提交。"
)
CODE_NOTICE_NO_LINK = (
    "**【安全核身】**本笔操作需要短信验证码；当前渠道未配置安全核验链接，请到网银页面完成确认。"
)

#: 跳转短链按钮的文字。
MFA_BUTTON_LABEL = "安全核验并确认"

#: 载荷自带的按钮文字最长几位。按钮是窄的，超过这个长度的"标签"其实是句子。
_MAX_LABEL_CHARS = 8

#: 中间态卡片上那个置灰按钮的文字：让用户知道"点过了，正在办"，而不是"没反应"。
INTERMEDIATE_BUTTON_LABEL = "正在核验并执行…"


def confirmation_action_value(question_id: str, pending_id: str, decision: str) -> dict[str, str]:
    """按钮回传值的唯一构造点：`{"q": ..., "p": ..., "d": ...}`。

    三个键的名字是契约的一部分（dispatcher 按名字读回 `CardActionClick`），`d` 只认
    confirm / cancel。这里刻意不接受任何额外字段：多一个键，这份值就不再是"这一笔操作的这
    一个决定"，而防重正是按值判等的。
    """

    if decision not in DECISIONS:
        raise ValueError(f"决定只能是 {DECISIONS}，收到 {decision!r}")
    return {"q": question_id, "p": pending_id, "d": decision}


class FeishuCardRenderer:
    """飞书 Schema 2.0 卡片渲染器；无状态、无时钟，可以当单例被所有会话共用。"""

    __slots__ = ()

    def render_text_card(self, title: str, text: str, status: str = "streaming") -> dict[str, Any]:
        """一段问答卡片。流式刷新与最终结果共用它，靠 `status` 换色换页脚。

        页脚写出这张卡当前处于什么状态（"正在生成…" / "已完成" / "未完成"）：用户是靠颜色和
        这句话判断"要不要继续等"的，卡片上只有正文会被误读成"说完了"。
        """

        state = resolve_status(status)
        elements: list[dict[str, Any]] = [self._markdown(_fit(text))]
        elements.append(self._markdown(_grey(STATUS_NOTES[state])))
        return self._card(title, STATUS_TONES[state], elements)

    def render_confirmation_card(
        self,
        pending_dict: Mapping[str, Any],
        question_id: str,
        pending_id: str,
        mfa_url: str | None = None,
    ) -> dict[str, Any]:
        """待确认操作的交互卡片（橙色头部 + 要素双列 + 确认/取消）。

        `pending_dict` 可以是引擎手里的整封信（`{"kind": "confirmation", "confirmation": {...}}`），
        也可以直接是内层那份 `confirmation`——两种形状都在引擎的真实调用路径上出现。

        需要验证码时，**确认入口只能是跳转签名短链的按钮**：飞书按钮的回传值带不回验证码，
        一个普通的回调确认按钮在那种卡片上点了必然被拒（422 VERIFICATION_CODE_REQUIRED），
        用户会以为是自己按错了。没有短链时索性不给确认入口，只留取消。
        """

        if not isinstance(pending_dict, Mapping):
            raise TypeError(f"pending_dict 必须是映射，收到 {type(pending_dict).__name__}")
        payload = _confirmation_payload(pending_dict)
        _refuse_contradictory_ids(pending_dict, question_id, pending_id)
        operation = str(payload.get("operation") or "").strip()
        title = confirmation_title(operation, _optional_text(payload.get("title")))
        confirm_label, cancel_label = confirmation_labels(
            operation,
            _label_text(payload.get("confirm_label")),
            _label_text(payload.get("cancel_label")),
        )
        requires_code = bool(payload.get("requires_code") or pending_dict.get("requires_code"))
        link = _checked_url(mfa_url) if mfa_url is not None else None

        elements: list[dict[str, Any]] = [
            self._markdown("请确认以下信息。确认后将立即执行，且不能撤销。")
        ]
        fields = confirmation_fields(payload)
        if fields:
            elements.append(self._columns(fields))
        if requires_code:
            notice = CODE_NOTICE_WITH_LINK if link else CODE_NOTICE_NO_LINK
            elements.append(self._markdown(notice))

        if link is not None and requires_code:
            elements.append(self._url_button(MFA_BUTTON_LABEL, link))
        elif not requires_code:
            elements.append(
                self._callback_button(
                    confirm_label,
                    confirmation_action_value(question_id, pending_id, DECISION_CONFIRM),
                    "primary",
                )
            )
        elements.append(
            self._callback_button(
                cancel_label,
                confirmation_action_value(question_id, pending_id, DECISION_CANCEL),
                "default",
            )
        )
        return self._card(title, CardTone.ORANGE, elements)

    def render_intermediate_card(self, title: str, text: str) -> dict[str, Any]:
        """卡片被点下之后 3 秒内同步返回的中间态：蓝色、置灰、说清在干什么。

        它刻意**没有任何 behaviors**：飞书要求卡片回调 3 秒内必有响应，而这 3 秒里授信还没跑完。
        如果这张卡片还能再点，用户连击第二次就是第二次真实出账。
        """

        elements: list[dict[str, Any]] = [
            self._markdown(_fit(text)),
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": INTERMEDIATE_BUTTON_LABEL},
                "type": "default",
                "disabled": True,
            },
        ]
        return self._card(title, CardTone.BLUE, elements)

    # --- 内部拼装 -------------------------------------------------------------------

    @staticmethod
    def _card(title: str, tone: CardTone, elements: list[dict[str, Any]]) -> dict[str, Any]:
        """卡片骨架。`update_multi` 必须为真：流式刷新与 3 秒响应都靠同一个消息原地更新。"""

        return {
            "schema": "2.0",
            "config": {"update_multi": True, "width_mode": "fill"},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": TEMPLATES[tone],
            },
            "body": {"elements": elements},
        }

    @staticmethod
    def _markdown(content: str) -> dict[str, Any]:
        return {"tag": "markdown", "content": content}

    @staticmethod
    def _columns(fields: Sequence[tuple[str, str]]) -> dict[str, Any]:
        """要素双列展示：左列比右列多一个，每格是「**标签** + 值」。"""

        cells = [{"tag": "markdown", "content": f"**{label}**\n{value}"} for label, value in fields]
        half = (len(cells) + 1) // 2
        groups = (cells[:half], cells[half:])
        columns = [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": list(group)}
            for group in groups
            if group
        ]
        return {"tag": "column_set", "flex_mode": "bisect", "columns": columns}

    @staticmethod
    def _callback_button(label: str, value: dict[str, str], kind: str) -> dict[str, Any]:
        """回调按钮：用户点它，飞书把 `value` 原样送回 `card.action.trigger`。"""

        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": kind,
            "behaviors": [{"type": "callback", "value": value}],
        }

    @staticmethod
    def _url_button(label: str, url: str) -> dict[str, Any]:
        """跳转按钮：打开银行自己的签名短链页面，不在飞书里输入任何秘密。"""

        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "primary",
            "behaviors": [{"type": "open_url", "default_url": url}],
        }


def _confirmation_payload(pending_dict: Mapping[str, Any]) -> Mapping[str, Any]:
    """取出内层那份展示载荷。引擎递来的可能是带 kind / confirmation 的信封，也可能已经拆开。"""

    inner = pending_dict.get("confirmation")
    return inner if isinstance(inner, Mapping) else pending_dict


def _label_text(value: Any) -> str | None:
    """载荷自带的按钮文字：只接受短短的、不含数字的标签。

    按钮文字会原样上卡，所以它是**用户可见文本里唯一一处完全由载荷决定的位置**——正文走要素
    白名单，标题走操作措辞表，只有这里还留着"上游说了算"。而最危险的上游文本恰好带数字：
    模块如果把"用 999111 确认"当确认标签传进来，验证码就被印在了卡片上，还要跟着卡片一起被
    转发、截图、留在会话记录里。含数字的标签一律退回操作措辞表（"确认转账"），那本来就够用了。
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_LABEL_CHARS:
        return None
    if any(char.isdigit() for char in text):
        return None
    return text


def _refuse_contradictory_ids(
    pending_dict: Mapping[str, Any], question_id: str, pending_id: str
) -> None:
    """信封里若自带 question_id / pending_id，必须与调用方传进来的一致。

    按钮回传值里的 q / p 是点下去之后去授信状态机里找那个断点的**唯一**线索。信封带着另一组
    id、按钮却按入参写：用户点下去得到一个 409，卡片上没有任何提示，日志里也只有一条"答案
    不匹配"。这种错在渲染期就能发现，而且只有调用方写错了才会发生，所以当场炸响，不要把它做成
    一张点不动的死卡。
    """

    for key, expected in (("question_id", question_id), ("pending_id", pending_id)):
        declared = _optional_text(pending_dict.get(key))
        if declared is not None and declared != expected:
            raise ValueError(
                f"信封里的 {key}={declared!r} 与按钮要写的 {expected!r} 不一致："
                "点下去会 409，请让调用方传同一笔操作的 id"
            )


def _optional_text(value: Any) -> str | None:
    """载荷里那些"可能没有"的字符串字段：非字符串一律当作没有。"""

    return value if isinstance(value, str) and value.strip() else None


def _grey(text: str) -> str:
    """飞书 markdown 支持 `<font color=...>`；页脚用灰色，不跟正文抢注意力。"""

    return f"<font color='grey'>{text}</font>"


def _fit(text: str) -> str:
    """超长正文截断：宁可少显示尾部，也不能让整条 PATCH 被拒。

    提示串本身也算长度：先截到 MAX_TEXT_CHARS - len(TRUNCATED_NOTICE) 再追加提示，最终长度
    不超过 MAX_TEXT_CHARS——与 throttler._visible 的「可见上限」是同一个口径。先截满再追加那种
    写法会让每一次超长回答都稳稳超限一点点，正好落在节流器以为还看得见的那一截外面。
    """

    if len(text) <= MAX_TEXT_CHARS:
        return text
    keep = max(0, MAX_TEXT_CHARS - len(TRUNCATED_NOTICE))
    return text[:keep] + TRUNCATED_NOTICE


def _checked_url(url: str) -> str:
    """短链必须是 http(s)。地址来自配置，但配置也会被人改错：`javascript:` 送到客户端就是注入。"""

    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        raise ValueError("MFA 短链必须是 http(s) 地址")
    return url.strip()
