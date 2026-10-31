"""理财消费的外层会话视图：只承载记忆，不承载授权。

外层（`app.context.handoff`）在调用整轮处理器时发布一份视图，理财把它翻译成这个契约再交给
`WealthAgent`。它存在的原因只有一个：**理财内部的状态住在图 state 里**（`wealth_*` 字段，
由基座的检查点保存），而"用户上一轮说了什么"是外层账本里已经持久化的事实，理财没有理由自己
再记一份。

三条结构性约束：

- **只有外层能构造它。** 它不是模型可以填的参数，也不是客户端可以自己声明的对象：没有
  `WealthRequest` 字段指向它，模型的动作词表里也没有"提供会话"这种动作。构造点是领域图
  （`workflow/graph.py` 的 `_conversation`），输入是外层发布的 `DomainView`。
- **它不携带授权。** `domain_context` 只接受外层白名单里的语义字段；确认、验证码、幂等键在这份
  契约里没有字段，`__post_init__` 会把它们当作契约错误拒绝，而不是忽略。恢复出来的东西只能用来
  理解用户想干什么，不能用来执行任何一笔交易。
- **历史是历史。** `summary_text` 与 `history_text` 明确标为历史上下文，不是系统指令；理财把它们
  当参考，不当命令。

依赖方向：`contracts` 不导入 `workflow`／`llm`／`infrastructure`，也不导入 `app.context`
（两个包之间只通过 `workflow/graph.py` 的翻译层相连）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MEMORY_FIELDS",
    "ConversationContext",
    "ConversationMessage",
]

# 外层允许理财带回、也允许外层交给理财的语义字段。它与 `app.context.handoff.CONTEXT_UPDATE_KEYS`
# 是同一张表，但**故意各写一份**：两个包不能互相导入，而一张共享的表会让任一侧悄悄放宽边界。
# `tests/wealth/test_context_handoff.py` 断言两者相等，所以它们不会各自漂移。
MEMORY_FIELDS = frozenset({"goal", "answered", "shown", "references"})

# 这些名字一旦出现在 `domain_context` 里就是契约错误，而不是"被忽略的字段"：它们是授权凭据，
# 记忆里出现它们意味着某一层把授权当成了可以恢复的东西。
FORBIDDEN_FIELDS = frozenset(
    {
        "confirmed",
        "confirmation",
        "verification_code",
        "mfa_code",
        "idempotent_key",
        "idempotency_key",
        "pending_id",
        "user_id",
        "bank_customer_id",
    }
)


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """一条已经发生过的对话，按外层账本的顺序。

    `message_id` 与 `sequence` 是外层的来源引用：理财拿它说明"这句话是从哪条记录来的"，
    不拿它做身份判断——归属检查在外层，已经做过了。
    """

    role: str
    content: str
    message_id: str | None = None
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError("role 必须是 user 或 assistant")
        if not isinstance(self.content, str):
            raise ValueError("content 必须是字符串")
        if self.sequence is not None and (
            isinstance(self.sequence, bool) or not isinstance(self.sequence, int)
        ):
            raise ValueError("sequence 必须是整数或 None")


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """一个会话视图：当前原话、历史、摘要，以及外层记下的语义线索。

    `conversation_id` 是隔离键。它不进入模型提示词——模型不需要知道自己在哪个会话里，
    而且把它写进提示词等于给模型一个可以引用的内部编号。
    """

    conversation_id: str
    current_message_id: str | None = None
    current_sequence: int = 0
    current_user_text: str | None = None
    summary_text: str | None = None
    history_text: str = ""
    recent_messages: tuple[ConversationMessage, ...] = ()
    domain_context: Mapping[str, Any] = field(default_factory=dict)
    context_sources: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_id, str) or not self.conversation_id.strip():
            raise ValueError("conversation_id 必须是非空字符串")
        if isinstance(self.current_sequence, bool) or not isinstance(self.current_sequence, int):
            raise ValueError("current_sequence 必须是整数")
        for name in ("summary_text", "history_text", "current_user_text", "current_message_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} 必须是字符串或 None")
        if not isinstance(self.domain_context, Mapping):
            raise ValueError("domain_context 必须是映射")
        if not isinstance(self.context_sources, Mapping):
            raise ValueError("context_sources 必须是映射")

        # 先看授权字段：它也是"白名单外"，但原因完全不同——那不是字段名写错，而是有人把授权
        # 当成了可以恢复的记忆。分开报出来，日志里才看得出是哪一类问题。
        forbidden = sorted(name for name in FORBIDDEN_FIELDS if name in self.domain_context)
        if forbidden:
            raise ValueError("会话线索不得携带授权字段：" + "、".join(forbidden))

        unknown = sorted(str(key) for key in self.domain_context if key not in MEMORY_FIELDS)
        if unknown:
            raise ValueError("会话线索含有不允许的字段：" + "、".join(unknown))

    @property
    def has_history(self) -> bool:
        """有没有比当前这句话更早的东西可讲。"""

        return bool(self.summary_text or self.history_text or self.recent_messages)
