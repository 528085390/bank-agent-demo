"""把模型的一轮输出读成理财工作流的结构化决策。

模型边界是原生 tool calling：一轮流式答复里可以带多个只读工具调用（`ToolCall`），但控制动作
仍是一次一个；一个调用都没有时那段散文就是一次回答。解析对"模型只想说话"宽容、对形状严格
（复用基座的 `DecisionError`），所以一个爱聊天的模型不会变成静默失败。

工具表（`llm/toolcalls.py`）是这一层唯一的词表来源：表里有七个只读工具和八个控制类动作。
**表里没有资金写**——没有 `purchase`、没有 `redeem`、没有 `confirm`、没有 `verify`。所以"模型
不能直接下单"不是靠提示词劝阻，而是解析阶段就无此工具可表达。

这一层做四件事，全部是机械检查：

- **一轮一组只读调用**：同一轮里给多个调用时，它们必须全部是只读工具，逐个校验、逐个执行；
  只要其中有一个控制动作，仍按"一次只能调用一个工具"拒绝——这一层不做控制动作调度。
- **工具白名单**：工具名必须在调用方给出的表里。写一个不存在的工具名（`purchase`、`confirm`
  这类）是一次越门禁的尝试，按 `GateViolation` 计。
- **参数 schema**：`arguments` 必须过该工具自己声明的 `parameters`（同一条 `jsonschema` 路径，
  与 `infrastructure.tools.check_arguments` 一致）。多写一个没声明的字段同样是越门禁。
- **动作派生**：工具名映射成工作流的一个动作（`call_read`、`set_slots`、`ask_user`、
  `submit_risk`、`recommend`、`compare`、`prepare_purchase`、`prepare_redeem`、`finish`）。
  派生只做一次，工作流那一层因此不必认识工具名。
- **引用有据**：`finish` 的 `cited_ids` 与 `shown` 是模型声称"这句话基于这些记录"的编号，
  由 `workflow` 层核对它们是否真的来自工具返回（`ungrounded_citations`）。

形状（对象、非空、键是非空字符串）在这里校验；槽位词表、金额是整数分、编号必须来自查到的记录、
序号按最近展示顺序解析，都是 `workflow/state.py` 的 `normalize_slots` 的事——这一层不导入
`workflow`。问卷答案是不是齐全的 q1..q5、对比的编号是不是查到的产品，同样由 `workflow` 层判定。
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.agents.decisions import DecisionError
from app.integration.model_client import ToolCall
from app.modules.wealth.llm.toolcalls import ToolEntry

__all__ = [
    "ACTIONS",
    "ACTION_BY_TOOL",
    "GateViolation",
    "WealthDecision",
    "parse_decision",
    "render_call",
    "ungrounded_citations",
]

# 工具名 → 工作流动作。工作流只认识右边这一列；工具表换名字不会波及节点。
ACTION_BY_TOOL: Mapping[str, str] = {
    "set_slots": "set_slots",
    "ask_user": "ask_user",
    "submit_risk": "submit_risk",
    "recommend": "recommend",
    "compare": "compare",
    "prepare_purchase": "prepare_purchase",
    "prepare_redeem": "prepare_redeem",
    "finish": "finish",
}

# 工作流能执行的全部动作。资金写不在其中；`submit_risk` 是非资金写，由完成问卷的流程调用；
# `prepare_purchase`／`prepare_redeem` 只发起预览，待确认与资金执行由运行时负责。
ACTIONS: tuple[str, ...] = (
    "call_read",
    "set_slots",
    "ask_user",
    "submit_risk",
    "recommend",
    "compare",
    "prepare_purchase",
    "prepare_redeem",
    "finish",
)

_VALIDATORS: dict[str, Draft202012Validator] = {}


class GateViolation(DecisionError):
    """模型这一次输出试图越过一道门禁，而不只是把格式写错了。

    它**不是**另一套拒绝机制：形状不合法的输出本来就抛 `DecisionError`，由决策节点纠正一次。
    这个子类只是把"越过门禁"的几种情况标出来，好让运行时能按用户累计它们（计划任务11：
    "累计 3 次绕过门禁事件后锁定本进程写操作"）：

    - 写了工具表里不存在的工具（`purchase`／`redeem`／`confirm`／`verify` 这类资金写或授权动作）；
    - 在 `arguments` 里塞了工具没有声明的字段（身份、确认、验证码、幂等键）。

    反面同样重要：**格式错误、缺字段、含糊金额、编号没查到、槽位名不合法都不是门禁事件**——
    它们是正常的模型犯错或用户表达不清，只纠正或追问，不累计。
    """


@dataclass(frozen=True, slots=True)
class WealthDecision:
    """一条可执行的决策；只带业务槽位，不带任何授权字段。"""

    action: str
    tool: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    # 同一轮里模型并行请求的多个只读工具；空元组表示只有上面那一个 tool/arguments。
    # 旧路径与单调用决策的形状保持不变，只有真正的并行只读请求才填这里。
    read_calls: tuple[Mapping[str, Any], ...] = ()
    question: str = ""
    answer: str = ""
    # 回答所引用的业务编号（产品／持仓／订单／测评），必须来自工具返回。
    cited_ids: tuple[str, ...] = ()
    # 从用户这句话里读到的业务槽位；值可能是具体值，也可能是 `{"ordinal": n}`。
    slots: Mapping[str, Any] = field(default_factory=dict)
    # 这次追问正在补的槽位名（可选，只用于把候选补进展示顺序）。
    missing: tuple[str, ...] = ()
    # 回答里按展示顺序列出的候选：资源名 → 编号列表。
    shown: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # 问卷答案：题目编号 → 0／1／2 选项。只有这一层校验形状，齐全与否由 `workflow` 判定。
    answers: Mapping[str, int] = field(default_factory=dict)
    # 对比的产品编号：两到三个，必须来自已经查到的产品。
    product_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """存进工作流状态的形状：只有普通数据，没有自定义类型。"""

        return {
            "action": self.action,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "read_calls": [dict(item) for item in self.read_calls],
            "question": self.question,
            "answer": self.answer,
            "cited_ids": list(self.cited_ids),
            "slots": dict(self.slots),
            "missing": list(self.missing),
            "shown": {key: list(value) for key, value in self.shown.items()},
            "answers": dict(self.answers),
            "product_ids": list(self.product_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> WealthDecision:
        """从工作流状态还原；状态是这一层自己写的，形状不对按决策错误处理。"""

        if not isinstance(payload, Mapping):
            raise DecisionError("状态里没有决策")
        action = payload.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise DecisionError(f"action 必须是 {'/'.join(ACTIONS)} 之一")
        arguments = payload.get("arguments")
        slots = payload.get("slots")
        answers = payload.get("answers")
        return cls(
            action=action,
            tool=payload.get("tool") if isinstance(payload.get("tool"), str) else None,
            arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
            read_calls=_read_calls(payload.get("read_calls")),
            question=_text(payload.get("question")),
            answer=_text(payload.get("answer")),
            cited_ids=_ids(payload.get("cited_ids")),
            slots=dict(slots) if isinstance(slots, Mapping) else {},
            missing=_names(payload.get("missing")),
            shown=_shown(payload.get("shown")),
            answers={str(key): value for key, value in dict(answers).items()}
            if isinstance(answers, Mapping)
            else {},
            product_ids=_ids(payload.get("product_ids")),
        )


def parse_decision(
    text: str | None,
    tool_calls: Sequence[ToolCall],
    *,
    table: Mapping[str, ToolEntry],
) -> WealthDecision:
    """读一轮原生工具调用；不可用就抛 `DecisionError`（由调用方决定是否纠正一次）。

    规则按优先级：同一轮里多个调用必须全部是只读工具，逐个校验后一起交给只读节点执行；只要其中
    有一个控制动作，仍按"一次只能调用一个工具"拒绝。恰好一个调用时按工具表判定"执行还是越门禁"；
    一个调用都没有时，模型说的散文就是一次 `finish`。
    """

    if len(tool_calls) > 1:
        return _to_parallel_read_decision(tool_calls, table)

    if tool_calls:
        call = tool_calls[0]
        entry = table.get(call.name)
        if entry is None:
            # 不存在的工具根本不在词表里：写一个 purchase／confirm 不是格式错误，是一次越门禁的尝试。
            raise GateViolation(f"工具不在词表里：{call.name}")
        arguments = _validated_arguments(call.name, entry, call.arguments)
        return _to_decision(call.name, arguments)

    prose = text.strip() if isinstance(text, str) else ""
    if not prose:
        raise DecisionError("模型没有返回内容")
    return WealthDecision(action=ACTION_BY_TOOL["finish"], answer=prose)


def _to_parallel_read_decision(
    tool_calls: Sequence[ToolCall], table: Mapping[str, ToolEntry]
) -> WealthDecision:
    """同一轮多个只读调用：全部校验通过才执行，控制动作仍拒绝。

    模型服务不保证遵守 `parallel_tool_calls=False`，本地模型会在一轮里同时要产品和账户；
    这些请求都是只读的，逐个执行再交给下一轮模型，比整轮失败更接近用户意图。控制动作
    （推荐、预览、确认、回答）仍然一次只能一个：它们会改变工作流状态，不能并行。
    """

    calls: list[dict[str, Any]] = []
    for call in tool_calls:
        entry = table.get(call.name)
        if entry is None:
            raise GateViolation(f"工具不在词表里：{call.name}")
        if call.name in ACTION_BY_TOOL:
            raise DecisionError("一次只能调用一个工具，不能一次给多个")
        calls.append(
            {
                "tool": call.name,
                "arguments": _validated_arguments(call.name, entry, call.arguments),
            }
        )
    first = calls[0]
    return WealthDecision(
        action="call_read",
        tool=str(first["tool"]),
        arguments=dict(first["arguments"]),
        read_calls=tuple(calls),
    )


def _validated_arguments(
    name: str, entry: ToolEntry, call_arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """剔除 strict 的 null 键后校验工具声明，返回可交给工具的参数。"""

    # strict 下模型会显式回 `cited_ids: null`（可选属性被改写成必填可空），而校验器用的是原始
    # 声明。值为 None 的键因此先剔除：在声明里它与"没写这个键"是同一件事。
    arguments = {key: value for key, value in call_arguments.items() if value is not None}
    extra = sorted(str(key) for key in arguments if str(key) not in entry.properties)
    if extra:
        raise GateViolation(f"{name} 的参数里有它没有声明的字段：" + "、".join(extra))
    undeclared = _undeclared_in_closed_objects(entry.parameters, arguments)
    if undeclared is not None:
        # 工具在 schema 里把某个对象声明成"关掉的"（`additionalProperties: false`），
        # 模型却往里写了别的键：那不是一个多余的装饰，而是想把授权字段塞进业务参数。
        raise GateViolation(f"{name} 的参数 {undeclared} 不在工具声明的字段里")
    problem = _schema_problem(entry, arguments)
    if problem is not None:
        raise DecisionError(problem)
    return arguments


def render_call(name: str, arguments: Mapping[str, Any]) -> str:
    """把一次工具调用渲染回文本，用作纠正提示里的"上一次输出"。"""

    return f"{name}({json.dumps(dict(arguments), ensure_ascii=False)})"


def _undeclared_in_closed_objects(
    schema: Mapping[str, Any] | None, value: Any, path: str = ""
) -> str | None:
    """第一个写进了"关掉的对象"的键，形如 `slots.user_id`；没有就返回 `None`。

    只对**声明了 `properties` 且 `additionalProperties: false`** 的对象生效。自由形状的对象
    （问卷答案的题目名、`shown` 的资源名）没有声明属性，所以不受这条规则约束——它们的内容由
    `workflow` 层校验，而不是靠 schema。
    """

    if not isinstance(schema, Mapping) or not isinstance(value, Mapping):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    if schema.get("type") == "object" and schema.get("additionalProperties") is False:
        for key in value:
            if str(key) not in properties:
                return f"{path}{key}"
    for key, item in value.items():
        child = properties.get(str(key))
        if isinstance(child, Mapping):
            nested = _undeclared_in_closed_objects(child, item, f"{path}{key}.")
            if nested is not None:
                return nested
    return None


def _to_decision(name: str, arguments: Mapping[str, Any]) -> WealthDecision:
    """把一次通过校验的工具调用翻译成工作流动作。"""

    action = ACTION_BY_TOOL.get(name)
    if action is None:
        # 只读工具：动作是 call_read，工具名与参数原样交给只读节点。
        return WealthDecision(action="call_read", tool=name, arguments=dict(arguments))

    if action == "set_slots":
        return WealthDecision(action=action, slots=_object(arguments.get("slots"), "slots"))
    if action == "ask_user":
        question = _text(arguments.get("question"))
        if not question:
            raise DecisionError("ask_user 必须给出 question")
        return WealthDecision(
            action=action, question=question, missing=_names(arguments.get("missing"))
        )
    if action == "submit_risk":
        return WealthDecision(action=action, answers=_answers(arguments.get("answers")))
    if action == "recommend":
        # 筛选条件由推荐节点从会话槽位和银行测评读取：模型这一层没有可校验的参数。
        return WealthDecision(action=action)
    if action == "compare":
        return WealthDecision(action=action, product_ids=_product_ids(arguments.get("product_ids")))
    if action in ("prepare_purchase", "prepare_redeem"):
        return WealthDecision(action=action, slots=_object(arguments.get("slots"), "slots"))

    answer = _text(arguments.get("text"))
    if not answer:
        raise DecisionError("finish 必须给出 text")
    return WealthDecision(
        action=action,
        answer=answer,
        cited_ids=_ids(arguments.get("cited_ids")),
        shown=_shown(arguments.get("shown")),
    )


def ungrounded_citations(decision: WealthDecision, known_ids: Collection[str]) -> str | None:
    """回答引用的编号里，第一条不是工具查到的；全部有依据时返回 None。

    这是"关键产品、金额和状态只能来自工具事实"里可机械检查的那一半：散文是模型的，编号不是。
    """

    for identifier in decision.cited_ids:
        if identifier not in known_ids:
            return identifier
    return None


def _validator(entry: ToolEntry) -> Draft202012Validator:
    cached = _VALIDATORS.get(entry.name)
    if cached is None:
        try:
            cached = Draft202012Validator(dict(entry.parameters or {"type": "object"}))
        except SchemaError as error:
            # 声明本身写错是模块作者的 bug，不是模型的错：在构造点抛，不放行。
            raise ValueError(
                f"tool {entry.name} declares an invalid parameters schema: {error}"
            ) from error
        _VALIDATORS[entry.name] = cached
    return cached


def _schema_problem(entry: ToolEntry, arguments: Mapping[str, Any]) -> str | None:
    """第一条不满足的约束，形如 `argument product_id: failed minLength`。

    只说路径与规则，不回报值：`jsonschema` 自己的 message 会带上出错值的 repr，而参数可能含
    业务数据，所以一律不用它。
    """

    errors = sorted(
        _validator(entry).iter_errors(dict(arguments)), key=lambda error: list(error.path)
    )
    if not errors:
        return None
    first = errors[0]
    location = ".".join(str(item) for item in first.path) or "(root)"
    return f"argument {location}: failed {first.validator}"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _object(value: Any, name: str) -> dict[str, Any]:
    """一个非空对象参数；形状不对按决策错误处理。"""

    if not isinstance(value, Mapping) or not value:
        raise DecisionError(f"{name} 必须是一个非空对象")
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise DecisionError(f"{name} 的键必须是非空字符串")
    return {str(key): item for key, item in value.items()}


def _answers(value: Any) -> dict[str, int]:
    """问卷答案的形状：非空对象，题目名是非空字符串，选项是 0／1／2 整数（bool 不算数值）。

    题目是否齐全、是不是 q1..q5 由 `workflow` 层对照问卷定义检查——这一层只认形状，不导入
    `domain`（`llm → contracts` 是既有方向）。
    """

    if not isinstance(value, Mapping) or not value:
        raise DecisionError("submit_risk 必须给出非空的 answers 对象")
    answers: dict[str, int] = {}
    for question, option in value.items():
        if not isinstance(question, str) or not question.strip():
            raise DecisionError("answers 的题目名必须是非空字符串")
        if isinstance(option, bool) or not isinstance(option, int) or option not in (0, 1, 2):
            raise DecisionError("answers 的选项必须是 0/1/2 整数")
        answers[question.strip()] = option
    return answers


def _product_ids(value: Any) -> tuple[str, ...]:
    """对比的产品编号：两到三个、不重复、都是非空字符串。"""

    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise DecisionError("compare 必须给出 product_ids 列表")
    identifiers: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DecisionError("product_ids 必须是产品编号列表")
        identifier = item.strip()
        if identifier in identifiers:
            raise DecisionError("product_ids 不能重复")
        identifiers.append(identifier)
    if not 2 <= len(identifiers) <= 3:
        raise DecisionError("compare 只能对比两个或三个产品")
    return tuple(identifiers)


def _ids(value: Any) -> tuple[str, ...]:
    """`cited_ids` 读成字符串元组；允许模型只写一个字符串。"""

    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        raise DecisionError("cited_ids 必须是字符串列表")
    identifiers: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise DecisionError("cited_ids 必须是字符串列表")
        if item.strip():
            identifiers.append(item.strip())
    return tuple(identifiers)


def _read_calls(value: Any) -> tuple[dict[str, Any], ...]:
    """状态里的并行只读调用；只接受由本层写出的 tool/arguments 形状。"""

    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DecisionError("read_calls 必须是对象列表")
    calls: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise DecisionError("read_calls 的每一项必须是对象")
        name = item.get("tool")
        arguments = item.get("arguments")
        if not isinstance(name, str) or not name.strip():
            raise DecisionError("read_calls 的 tool 必须是非空字符串")
        if not isinstance(arguments, Mapping):
            raise DecisionError("read_calls 的 arguments 必须是对象")
        calls.append({"tool": name.strip(), "arguments": dict(arguments)})
    return tuple(calls)


def _names(value: Any) -> tuple[str, ...]:
    """槽位名列表；允许模型只写一个字符串，其余形状一律拒绝。"""

    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        raise DecisionError("missing 必须是槽位名列表")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DecisionError("missing 必须是槽位名列表")
        names.append(item.strip())
    return tuple(names)


def _shown(value: Any) -> dict[str, tuple[str, ...]]:
    """`shown` 读成"资源名 → 编号列表"；形状不对一律拒绝。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DecisionError("shown 必须是资源名到编号列表的对象")
    shown: dict[str, tuple[str, ...]] = {}
    for resource, identifiers in value.items():
        if not isinstance(resource, str) or not resource.strip():
            raise DecisionError("shown 的资源名必须是非空字符串")
        if isinstance(identifiers, str):
            raise DecisionError("shown 的值必须是编号列表")
        if not isinstance(identifiers, Sequence) or isinstance(identifiers, (bytes, bytearray)):
            raise DecisionError("shown 的值必须是编号列表")
        collected: list[str] = []
        for item in identifiers:
            if not isinstance(item, str) or not item.strip():
                raise DecisionError("shown 的编号必须是非空字符串")
            collected.append(item.strip())
        shown[resource.strip()] = tuple(collected)
    return shown
