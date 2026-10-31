"""给模型看的工具表：名字、说明、参数 schema、副作用和输出概要。

计划 §3.4 的旧协议让模型输出 `{"action": "...", ...}`——九种动作各自有一套定制字段，加一种问法
就要加一条解析规则。这一层把它换成一份**工具表**：只读工具直接复用模块自己的 `ToolSpec`（参数
schema 本来就是给校验用的），控制类动作也声明成同样的形状，于是模型看到的和程序校验的是同一份
定义，而"模型能做什么"变成了一张可以逐项检查的表，而不是散在解析器里的分支。

三条边界：

- **渲染器只在 wealth 内。** 公共 `describe_tools` 渲染的是 `(名字, 说明)` 菜单，主平台各域共用；
  给模型看完整 schema 是理财这次改造的需要，所以渲染放在这里，不动公共函数。
- **schema 是执行依据，不只是提示。** 表里每个工具的 `parameters` 都会在决策阶段用同一份
  `jsonschema` 校验（`infrastructure.tools.check_arguments` 用的是同一条路径），模型多写一个字段
  就是一次越门禁的尝试，而不是被忽略的装饰。
- **这里没有资金写。** 表里没有任何能直接下单、确认或提交验证码的工具：`prepare_purchase` 只
  发起预览，确认与资金执行在运行时的受控节点里，模型无论被怎么诱导都拿不到那条路径。

依赖方向：`llm → contracts`（外加复用公共 `ToolSpec`／`SideEffect`）。这一层不导入 `workflow`：
槽位名与金额规则由 `workflow` 层在解析之后校验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.modules.wealth.infrastructure.tools import READ_TOOLS, ReadTool
from app.tools.protocol import SideEffect
from app.tools.schema import normalize_parameters

__all__ = [
    "CONTROL_TOOLS",
    "ToolEntry",
    "function_schema",
    "function_schemas",
    "read_arguments",
    "tool_table",
]


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """工具表里的一项：模型看到的字段与程序校验的字段是同一份。

    `side_effect` 与 `output` 是**给模型的说明**，不是门禁：真正的门禁是"表里根本没有资金写工具"
    以及运行时自己的受控节点。写清楚它们是为了让模型不必靠猜来决定要不要再查一次。
    """

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    side_effect: str = SideEffect.READ
    output: str = ""

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(str(name) for name in (self.parameters or {}).get("required") or ())

    @property
    def properties(self) -> Mapping[str, Any]:
        return (self.parameters or {}).get("properties") or {}

    @property
    def additional_properties(self) -> bool:
        """未声明的参数是否被接受。理财的每个工具都声明为 `False`。"""

        return bool((self.parameters or {}).get("additionalProperties", True))


def _parameters(properties: Mapping[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------- 控制类工具

# 除了七个只读工具，模型还需要能表达"问用户""记下信息""保存问卷""要推荐""要对比""发起预览""回答"。
# 它们与只读工具共用同一张表、同一套 schema 校验，所以模型不必学两种输出形状。
CONTROL_TOOLS: tuple[ToolEntry, ...] = (
    ToolEntry(
        name="set_slots",
        description=(
            "把用户这句话里的业务信息记进本次会话。金额一律用整数分（两千元＝200000）。"
            "只写用户真的说过的信息，不要替用户猜。"
        ),
        parameters=_parameters(
            {"slots": {"type": "object", "minProperties": 1}},
            ["slots"],
        ),
        output="记下的槽位并进会话；不合法的槽位名或值会被拒绝，并告诉你原因。",
    ),
    ToolEntry(
        name="ask_user",
        description=(
            "向用户提问。问题由你自己组织：可以问槽位，也可以问问卷里的某一题或几题。"
            "一次只问最必要的，已经知道的信息不要再问。"
        ),
        parameters=_parameters(
            {
                "question": {"type": "string", "minLength": 1},
                # **必填**，可以是空数组。它回答"这次提问是在补哪几个槽位"：程序用两件事——
                # 把"这一轮摆在用户眼前的候选"记进展示顺序，以及机械地拒绝重复追问已经知道的
                # 槽位。可选就等于可以省略，一省略这两件事都无从检查（"已知信息不重复问"会退化
                # 成一句纯提示词建议），所以它必须由模型显式声明：不在补槽位（例如问问卷的某一题）
                # 就写 `[]`。
                "missing": {"type": "array", "items": {"type": "string"}},
            },
            ["question", "missing"],
        ),
        output="把问题交给用户，本次处理到此结束，等用户回答。",
    ),
    ToolEntry(
        name="submit_risk",
        description=(
            "把用户明确说过的五题问卷选项交给银行保存。只能填用户说过的选项，"
            "等级、分数和有效期由银行计算，你自己写的等级不算数。"
        ),
        parameters=_parameters(
            {"answers": {"type": "object", "minProperties": 1}},
            ["answers"],
        ),
        side_effect=SideEffect.WRITE,
        output="银行保存的测评：等级、分数、有效期与测评编号。",
    ),
    ToolEntry(
        name="recommend",
        description=(
            "按用户的条件给出推荐。筛选条件由程序从会话槽位和银行测评读取，你不要自己填。"
            "调用之前必须已经查到银行的有效测评、产品列表，以及金额／用钱时间／流动性三个条件；"
            "缺哪一样，工具会告诉你缺什么，你再决定去查还是去问。"
        ),
        parameters=_parameters({}),
        output="候选产品与落选产品（含落选原因）。",
    ),
    ToolEntry(
        name="compare",
        description=(
            "对比两到三只产品。编号必须来自本轮已经查到的产品记录；缺记录时工具会告诉你缺哪一只，"
            "先查它再对比。"
        ),
        parameters=_parameters(
            {
                "product_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 3,
                }
            },
            ["product_ids"],
        ),
        output="每只产品的客观参数与适配结论。",
    ),
    ToolEntry(
        name="prepare_purchase",
        description=(
            "发起一次申购**预览**（不是下单）。只填产品、金额、账户三个业务槽位。"
            "缺哪个槽位，工具会告诉你缺什么，你去问用户；程序负责预检并冻结预览，"
            "确认与资金提交由用户在预览上点击完成。"
        ),
        parameters=_parameters(
            {
                "slots": {
                    "type": "object",
                    "minProperties": 1,
                    "properties": {
                        "product_id": {"type": "string"},
                        "amount_minor": {"type": "integer"},
                        "account_id": {"type": "string"},
                    },
                    # 关掉的：这三个槽位就是全部。身份、确认、验证码和幂等键不在其中，
                    # 写进来是一次越门禁的尝试（`decisions.parse_decision` 按声明逐个核对）。
                    "additionalProperties": False,
                }
            },
            ["slots"],
        ),
        side_effect=SideEffect.WRITE,
        output="一份待用户确认的申购预览，或预检不通过的原因。",
    ),
    ToolEntry(
        name="prepare_redeem",
        description=(
            "发起一次赎回**预览**（不是下单）。只填持仓、金额、到账账户三个业务槽位；"
            '金额可以是整数分、{"all": true}（全部）或 {"half": true}（一半），'
            '持仓可以写具体编号或 {"recent": true}（最近一次成功申购的新仓）。'
            "缺哪个槽位，工具会告诉你缺什么，你去问用户。"
        ),
        parameters=_parameters(
            {
                "slots": {
                    "type": "object",
                    "minProperties": 1,
                    "properties": {
                        # 持仓可以是具体编号，也可以是 `{"recent": true}`（最近一次成功申购的
                        # 新仓）；金额可以是整数分、`{"all": true}` 或 `{"half": true}`。
                        # 形状在这里放行，**含义**由 `workflow.normalize_redeem_slots` 判定，
                        # 所以 schema 用宽类型而不是把两种形式写成 anyOf——那会让错误信息变成
                        # "两个分支都不匹配"，模型看不出到底哪里不对。
                        "holding_id": {"type": ["string", "object"]},
                        "amount_minor": {"type": ["integer", "object"]},
                        "account_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                }
            },
            ["slots"],
        ),
        side_effect=SideEffect.WRITE,
        output="一份待用户确认的赎回预览，或预检不通过的原因。",
    ),
    ToolEntry(
        name="finish",
        description=(
            "把回答交给用户，本次处理结束。回答里用到的业务编号要列进 cited_ids；"
            "如果给用户列出了候选（产品、账户、持仓），要在 shown 里按展示顺序写出编号。"
        ),
        parameters=_parameters(
            {
                "text": {"type": "string", "minLength": 1},
                "cited_ids": {"type": "array", "items": {"type": "string"}},
                "shown": {"type": "object"},
            },
            ["text"],
        ),
        output="回答展示给用户；没有工具返回过的编号会被拒绝。",
    ),
)

_CONTROL_BY_NAME: Mapping[str, ToolEntry] = {entry.name: entry for entry in CONTROL_TOOLS}


def tool_table(
    tools: Mapping[str, ReadTool] = READ_TOOLS,
) -> dict[str, ToolEntry]:
    """完整工具表：七个只读工具（复用它们自己的 `ToolSpec`）加控制类动作。

    只读工具的说明后面会补上它们的输出概要，因为模型决定"要不要再查一次"靠的是"这次能拿到什么"。
    """

    table: dict[str, ToolEntry] = {}
    for name, tool in tools.items():
        spec = tool.spec
        table[name] = ToolEntry(
            name=name,
            description=spec.description,
            parameters=dict(spec.parameters or {}),
            side_effect=str(spec.side_effect),
            output=_output_summary(spec.output_schema),
        )
    table.update(_CONTROL_BY_NAME)
    return table


def _output_summary(output_schema: Mapping[str, Any] | None) -> str:
    """只读工具的输出概要：它返回哪些资源。这是给模型看的，不是校验。"""

    properties = (output_schema or {}).get("properties") or {}
    if not properties:
        return ""
    return "返回：" + "、".join(sorted(str(name) for name in properties))


def read_arguments(entry: ToolEntry, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """只读工具的参数原样交给处理器；这里只做一次浅拷贝。"""

    del entry
    return dict(arguments)


# strict 模式（结构化输出）只认 JSON Schema 的一个子集；这些关键词必须从送出去的 schema 里
# 删掉，否则 provider 会直接拒绝这次请求。删掉之后改写成 description 里的一句话，模型仍然看得见。
_NON_STRICT_KEYWORDS = (
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
    "pattern",
    "format",
)

_KEYWORD_NOTES: Mapping[str, str] = {
    "minLength": "{name} 至少 {value} 个字符",
    "maxLength": "{name} 至多 {value} 个字符",
    "minItems": "{name} 至少 {value} 项",
    "maxItems": "{name} 至多 {value} 项",
    "minProperties": "{name} 至少 {value} 个字段",
    "maxProperties": "{name} 至多 {value} 个字段",
    "pattern": "{name} 必须匹配 {value}",
    "format": "{name} 必须是 {value} 格式",
}


def _drop_keywords(schema: dict[str, Any], keywords: Sequence[str]) -> list[str]:
    """从 schema 顶层的每个属性里删掉 strict 不认的关键词，返回可读的说明。

    只处理顶层 properties（与 normalize_parameters 同层）：嵌套对象是声明者自己写死的形状，
    这一层不递归改写别人的 schema。返回空列表表示什么都没删。
    """

    dropped: list[str] = []
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return dropped
    for name, declared in properties.items():
        if not isinstance(declared, dict):
            continue
        for keyword in keywords:
            if keyword not in declared:
                continue
            value = declared.pop(keyword)
            dropped.append(_KEYWORD_NOTES[keyword].format(name=name, value=value))
    return dropped


def _wire_parameters(entry: ToolEntry, *, strict: bool) -> tuple[dict[str, Any], str, bool]:
    """把一个工具声明成模型看到的原生函数：参数 schema、说明、能不能声称 strict。

    normalize_parameters 负责"闭合对象、把可选属性变成必填的可空属性"；这里再把 strict 不认的
    约束删掉，并把删掉的约束写进说明。entry.parameters 本身**不动**：决策层的校验仍然用它。
    """

    schema, expressible = normalize_parameters(dict(entry.parameters))
    description = entry.description
    if strict:
        dropped = _drop_keywords(schema, _NON_STRICT_KEYWORDS)
        if dropped:
            description = description + "\n" + "；".join(dropped)
    return schema, description, bool(strict and expressible)


def function_schema(entry: ToolEntry, *, strict: bool = True) -> dict[str, Any]:
    """一个工具的原生函数定义（tools 参数里的一项）。"""

    parameters, description, can_be_strict = _wire_parameters(entry, strict=strict)
    return {
        "type": "function",
        "function": {
            "name": entry.name,
            "description": description,
            "parameters": parameters,
            "strict": can_be_strict,
        },
    }


def function_schemas(
    table: Mapping[str, ToolEntry], *, strict: bool = True
) -> tuple[dict[str, Any], ...]:
    """整张工具表的原生函数定义；顺序与工具表的插入顺序一致。"""

    return tuple(function_schema(entry, strict=strict) for entry in table.values())
