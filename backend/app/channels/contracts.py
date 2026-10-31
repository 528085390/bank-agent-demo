"""渠道层的入站数据契约：把飞书原始事件压成与平台无关的两个不可变结构。

只依赖标准库，且刻意不 import 任何 `app.*`：契约要被调度核心、渲染器、绑定管理器和
离线验收探针同时引用，一旦让它反向依赖运行时，就会在 `app.main` 的 import 链上打结。

两者都是 `frozen=True, slots=True`——事件一旦解析出来就只读：白名单、去重、绑定、引擎
依次消费同一份对象，谁都不许就地改，否则排查「这条消息为什么被拦」时看到的就不是当时
进来的那条了。

但 frozen 只挡得住字段重绑，挡不住容器内部的改动（`click.raw_value["d"] = "cancel"` 会成
功）。按钮回传值恰恰是个嵌套字典，改它等于伪造「用户当时点的到底是什么」。所以构造时把
可变的容器递归复制成只读结构（映射换成 `MappingProxyType`，列表换成元组），`to_dict()`
再反过来给出一份可以随便改、也 JSON 得出来的普通对象。两个方向都不共享内存。

字段名与顺序本身就是接口（后续 Task 按名字构造，日志里也按这个顺序展开），别重排。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Literal

#: 卡片按钮只认这两个决定；飞书回传的值原样落在 CardActionClick.decision 上，
#: 越界取值由引擎拒绝（契约不负责校验不可信输入，也不该在构造时抛异常）。
Decision = Literal["confirm", "cancel"]


def _frozen_copy(value: Any) -> Any:
    """递归复制容器并冻住每一层；标量本身就是不可变的，原样返回。

    只认三类容器：映射（含 `dict` / `MappingProxyType`）、序列（`list` / `tuple`）、
    集合。飞书按钮 value 的 JSON 结构只可能落在前两类里，集合是无害的全集收口。
    """

    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen_copy(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_copy(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_frozen_copy(item) for item in value)
    return value


def _thawed_copy(value: Any) -> Any:
    """`_frozen_copy` 的逆：给调用方一份可改、可序列化的普通对象。

    日志、审计旁证与离线探针要的是一份快照，不是契约本体；每次调用都新建，两次
    `to_dict()` 之间不共享任何可变结构，改返回值污染不到原对象。
    """

    if isinstance(value, Mapping):
        return {key: _thawed_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thawed_copy(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return {_thawed_copy(item) for item in value}
    return value


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """一条进站的私聊消息。

    `create_time_ms` 保持飞书给的 epoch **毫秒**原值，不在解析时折算成秒：水位比较
    是整型算术，越早落到浮点上，越容易在毫秒边界上判错一条真消息。
    `event_id` 与 `message_id` 都要留：飞书重投时事件 ID 会重复，而同一消息也可能
    以不同事件形态再来一次，两个键分别喂给去重缓存。
    """

    channel: str
    sender_id: str
    sender_name: str
    message_id: str
    event_id: str
    text: str
    create_time_ms: int

    def to_dict(self) -> dict[str, Any]:
        """展开成 plain dict：日志、审计旁证与离线探针都直接序列化它。

        用显式字段遍历而不是 `asdict()`——这里全是标量，深拷贝没有意义，而 `asdict()`
        会悄悄把将来可能加进来的嵌套 dataclass 也复制一遍。容器仍然走 `_thawed_copy`：
        返回值必须是调用方的，不是本对象的。
        """

        return {field.name: _thawed_copy(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class CardActionClick:
    """一次卡片按钮点击（确认 / 取消 / 后续可能新增的按钮）。

    `action_id` 是飞书给这次点击的标识，`question_id` + `pending_id` 定位到共享授信状态机
    里的那个挂起断点。`raw_value` 保留按钮回传的原始 value（不裁剪、不重命名）——新增按钮
    字段时不必改契约，出问题时也能看到飞书当时究竟送来了什么。

    防重键**不用** `action_id`：卡片重发一次它和 `pending_id` 就都换了，用它做键等于
    「每点一次都是新动作」。键由 `ChannelDedup.action_key` 从 `question_id` + `decision` 拼，
    见 `app/channels/dedup.py`。

    `event_id` 与 `create_time_ms` 是后补的事件字段（放末尾、给默认值，不动已有
    字段顺序）：卡片回调同样是事件推送，Task 8 的 dispatcher **必须**填这两个，否则 Task 7
    只能退化成「按 question_id + decision 防重」，启动水位数也用不上——重启前的堆积回调
    会被当成刚发生的点击。
    """

    channel: str
    sender_id: str
    action_id: str
    question_id: str
    pending_id: str
    decision: Decision
    raw_value: Mapping[str, Any]
    event_id: str = ""
    create_time_ms: int = 0

    def __post_init__(self) -> None:
        """构造时就把 `raw_value` 换成只读副本：调用方之后改原字典影响不到这里。"""

        object.__setattr__(self, "raw_value", _frozen_copy(self.raw_value))

    def to_dict(self) -> dict[str, Any]:
        """展开成 plain dict；`raw_value` 原样带出，便于回放与排障。

        返回的是副本：改它不会污染契约，也不会影响下一次 `to_dict()`。`raw_value` 在这里被
        还原成普通 dict / list，所以 `json.dumps(click.to_dict())` 仍然成立。
        """

        return {field.name: _thawed_copy(getattr(self, field.name)) for field in fields(self)}
