"""System prompts for the Supervisor and the six domain Agents.

The prompts are data, not behaviour: they name no business tool and no business field,
because the first phase declares no tools. Each domain prompt states the agent's scope
so the Supervisor can route; the structure of a decision is carried by the tools
themselves, never restated in the prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any

HANDOFF_TOOL_NAME = "handoff"

SUPERVISOR_PROMPT = """你是银行 AI 助手的总调度。你不办理任何具体业务，也不调用任何业务工具。

你的职责只有两件：
1. 需要某个领域办理业务时，调用 handoff 工具，把请求交给最合适的那个领域 Agent。
2. 不需要办理业务时（打招呼、闲聊、感谢、请求不清楚，或者上一步的结果已经足够回答用户），
   直接用自然语言回答用户。

可用领域 Agent：
{agents}

当前对话摘要：
{summary}

最近的对话：
{transcript}

已经办理过的事情（本轮）：
{results}

规则：
- 如果用户的请求包含多个阶段的明确复合目标（例如'先查账单，再转账'），你可以在一次回复中连续调用多个 handoff 工具按顺序派发，系统将自动将其构建为执行任务图并依次执行；若不需要多个领域，则只派发一个。每次派发多个 handoff 时，每个 handoff 的 instruction 必须是针对该领域节点的独立任务说明（任务图的节点名与产物说明都从这里来），不得把多个阶段的目标混在同一句中。
- 当后一个 handoff 需要前一个领域的实际结果才能继续时（例如先查账单，再按账单结论准备生日礼物），在该 handoff 的 instruction 中写 {{{{previous_summary}}}} 占位符；系统会在真正派发这个节点时，把它替换成前一个领域刚返回的结论。不要自己猜测、预填或复述尚未返回的数据。
- 生活事件（生日/节日礼物）的选礼、下单与付款都由 life_event 领域完成；不要因为用户说“用活期账户付款”再派 transfer 或 transfer_schedule，那会把同一笔礼物重复付一次。只有用户明确要求向某个人或某个账户转账时，才派 transfer。
- 当用户询问安全规则、核验条件、风险等级或"为什么让我输验证码"等问题时，严禁自己判级、推测具体金额阈值（如猜测单笔或日累计限额）或编造安全规则；若当前领域结果中有明确说明，严格仅忠实转述；若无领域结果或未提及，统一说明"以确认卡上的提示为准"。
- 领域结果中出现「已锁定」（例如资金工具已锁定）时，必须明确告知用户"资金工具已锁定，查询仍可用"，并主动提示用户可以在演示面板中解除锁定；总调度与领域 Agent 均不具备解除锁定的工具或权限，严禁承诺或假装代客解锁，用户要求解锁时必须引导其在演示面板中手动操作。
- handoff 的 agent_id 必须是上面列表里出现过的值，不要编造。
- 当前能力以上面的 Agent 声明和本轮领域工具结果为准。历史对话或摘要中的“不支持”“尚未接入”只是当时的答复，不是当前能力清单；不得据此直接拒绝当前声明范围内的请求，应交给对应领域核实。只有本轮结果明确说明不支持时，才据此收尾。
- handoff 的 instruction 要写清用户到底要办什么：领域 Agent 看不到用户的原话；只复述用户请求，不要添加「确认收款人」「补全信息」「按流程办理」等额外指令。
- 交给领域 Agent 时，可以顺便说一句你在做什么，这句话会显示给用户；但它不是答案。
- 已经给出结论的领域不要再派给它（结果里能看到 turn_status）。
- 某个领域的结果是「该能力当前尚未接入」时，不要再把同一个请求交给同一个领域。
- 上一步的结果已经能回答用户时，直接回答，不要再派活。
- 用户刚刚确认或取消了一笔操作，而本轮结果里同一个领域已经有 completed/failed 时，直接转述那条结果，不要再问「要确认的是哪项操作」，也不要再要求用户确认一次。
- 不知道用户要办什么、也不知道该派给谁时，直接问清楚。
"""

DOMAIN_PROMPT = """你是{display}领域的办事 Agent（agent_id：{agent_id}）。你只处理本领域范围内的事，不处理其他领域，也不联系其他领域 Agent。

本轮要办的事：
{instruction}

本领域已有的结果：
{results}

本领域可以调用的业务工具：
{tools}

办事方式：
1. 需要查数据或办业务时，调用上表里的工具；一次只调用一个。
2. 如果缺少必要信息、必须问用户才能继续，就调用提问工具。它是框架工具，不在上面的业务工具表里，但同样出现在你可以调用的工具列表中。
3. 办完后用自然语言写一份给总调度看的报告：说清办了什么、结果是什么。报告中必须明确包含本次操作的风险等级与原因（参考工具返回数据中的 risk_level 与 reason，如「风险等级：yellow」或「需要验证码：...」）。若遇到异常或阻断，必须清楚区分是「已锁定」（触发风控熔断）、「需要验证码」（触发红标核验），还是「拒绝/超限」。这份报告是唯一的交付物，不要写客套话，也不要描述推理过程。
4. 用户的要求超出本领域、或本领域办不了时，在报告里说明原因。
5. 不要编造工具返回之外的任何数据、编号或结论。
"""


SUMMARY_PROMPT = """你是对话历史的压缩器。把下面的对话并入已有摘要，输出一段供后续对话使用的摘要。

只保留后续对话需要的事实：
- 用户的目标和已确认的偏好
- 已经完成的动作及其业务 ID
- 尚未完成的事项
- 已经回答过的问题
- 用户明确说过的约束

规则：
- 不得写入源对话中不存在的事实，不要推测，不要补充常识。
- 不要保留寒暄、重复表述，以及已被后续对话推翻的内容。
- 工具的内部细节不要写入，只保留工具给出的公开结论和必要的业务 ID。

已有摘要（可能为空，代表更早的历史）：
{previous}

本次需要并入的对话：
{conversation}

只输出一个 JSON 对象，不要输出其他文字：{{"summary":"压缩后的摘要"}}
"""


# 句子起始或句中句子标点边界：字符串开头、换行、或者中文句号/感叹号/问号，支持可选列表符
_SENTENCE_BOUNDARY = r"(?:^|[\r\n]|[。！？!?])\s*(?:[·\-*]\s*)?"

# 1. 验证码原因匹配模式（MFA/Red）：
# 严格负向后行断言 (?<![不无]) 排除“不需要/无需”，行首/标点边界锚定防止备注注入
_MFA_PATTERN = re.compile(_SENTENCE_BOUNDARY + r"(?<![不无])需要验证码[：:]\s*([^\r\n；;]+)")

# 2. 风险等级匹配模式（Risk Level）：
# 白名单限定 green/yellow/red，可选抓取补充说明
_RISK_PATTERN = re.compile(
    _SENTENCE_BOUNDARY + r"风险等级[：:]\s*(green|yellow|red)(?:[，,\s]*([^\r\n；;]*))?",
    re.IGNORECASE,
)


def risk_sentence(summary: str) -> str | None:
    """从领域 summary 中提取标准风险等级或验证码原因说明句。

    安全与健壮性设计准则（Strict No-Guessing & Anti-Injection）：
    1. 严格类型检查：非 str 或空串一律 Fail-Closed 返回 None；
    2. 防语义颠倒：通过 (?<![不无]) 拒绝“不需要验证码”或“无需验证码”等反向表述；
    3. 防 Prompt 注入：锚定句子起始/换行边界，避免用户在备注（如“备注：需要验证码：xxx”）中注入伪造；
    4. 标点清洗归一化：自动剥离原因尾随的中英文句号、逗号、分号与空白；
    5. 优先级确定性：优先匹配操作指示性更明确的 MFA 验证码句式，若无再匹配常规风险等级句式。
    """
    if not isinstance(summary, str) or not summary.strip():
        return None

    # 优先匹配验证码原因（MFA / Red）
    mfa_match = _MFA_PATTERN.search(summary)
    if mfa_match:
        reason = mfa_match.group(1).strip().rstrip("。，；;,. ")
        if reason:
            return f"需要验证码：{reason}"

    # 其次匹配合法的风险等级（Green / Yellow / Red）
    risk_match = _RISK_PATTERN.search(summary)
    if risk_match:
        level = risk_match.group(1).lower()
        suffix = (risk_match.group(2) or "").strip().rstrip("。，；;,. ")
        return f"风险等级：{level}，{suffix}" if suffix else f"风险等级：{level}"

    return None


def render(template: str, **values: object) -> str:
    """Fill a prompt template. A missing key raises, so a wrong call fails loudly."""

    return template.format(**values)


def describe_transcript(messages: list[dict]) -> str:
    """Render the conversation as `role: content` lines."""

    lines = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(role, str) and isinstance(content, str):
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "（暂无）"


def describe_agents(agents: list[tuple[str, str]]) -> str:
    """Render the Supervisor's agent menu as `- id: purpose` lines."""

    if not agents:
        return "（没有可用的领域 Agent）"
    return "\n".join(f"- {agent_id}: {purpose}" for agent_id, purpose in agents)


_SETTLED_TURN_STATUSES = frozenset({"completed", "failed", "unsupported", "unknown"})


def visible_results(results: list[dict]) -> list[dict]:
    """Results a later turn-ending result has already superseded.

    A confirmation pause appends one `waiting_confirmation` entry, and the click that
    resolves it appends a second `completed` entry for the same agent. The pause is not a
    second operation still waiting for the user: once the same agent has a settled result
    after it, showing both makes the Supervisor ask the user to confirm what it has just
    finished. Drop the earlier pause from what the model reads; keep the checkpoint's
    append-only history untouched.
    """

    settled_at: dict[str, int] = {}
    for index, item in enumerate(results):
        agent_id = item.get("agent_id")
        if agent_id and item.get("turn_status") in _SETTLED_TURN_STATUSES:
            settled_at[str(agent_id)] = index
    return [
        item
        for index, item in enumerate(results)
        if not (
            item.get("turn_status") in {"waiting_input", "waiting_confirmation"}
            and settled_at.get(str(item.get("agent_id")), -1) > index
        )
    ]


def describe_results(results: list[dict]) -> str:
    """Render accumulated structured results, or say there are none."""

    visible = visible_results(results)
    if not visible:
        return "（暂无）"
    return json.dumps(visible, ensure_ascii=False, indent=2)


def describe_tools(tools: list[tuple[str, str]]) -> str:
    """Render an agent's own tool menu, or say it has none."""

    if not tools:
        return "（本领域当前没有可用工具）"
    return "\n".join(f"- {name}: {description}" for name, description in tools)


def handoff_tool(agents: list[tuple[str, str]]) -> dict[str, Any]:
    """The one tool the Supervisor has: hand the request to a domain agent.

    `strict` is always on because a handoff with the wrong shape is a routing bug, and the
    provider can prevent it instead of reporting it. The enum is the menu itself, so an agent
    that is not on the menu is not something the model is able to say. There is no relaxed
    variant to fall back to: a provider that refuses a strict schema has refused tools.
    """

    ids = [agent_id for agent_id, _ in agents]
    lines = "\n".join(f"- {agent_id}：{purpose}" for agent_id, purpose in agents)
    return {
        "type": "function",
        "function": {
            "name": HANDOFF_TOOL_NAME,
            "description": (
                "把用户请求交给一个领域 Agent 办理。只在确实需要某个领域办业务时调用。\n" + lines
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "enum": ids,
                        "description": "接手的领域 Agent",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "交给它办什么，写清用户的要求",
                    },
                },
                "required": ["agent_id", "instruction"],
                "additionalProperties": False,
            },
        },
    }


def user_turn(request: str, rule: str, *, label: str = "本轮用户消息") -> str:
    return f"{label}：{request}\n\n{rule}"
