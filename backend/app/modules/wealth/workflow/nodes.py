"""理财对话工作流的节点：理解请求 → 结构化决策 → 补槽位／只读工具／问卷／推荐／对比／申购预览／赎回预览 → 继续决策 → 回答／追问。

每个节点只读写 `state`；调用方身份不在 state 里，需要银行的节点由图的构建者闭包持有 `context`
（图在每次 `handle` 时按调用方重建，见 `graph.build_read_graph`）。

七处硬边界：

1. **决策节点**：预算（8 次模型调用、12 次节点推进）在调用模型**之前**检查；动作词表、工具
   白名单、参数 schema、槽位名与值、追问只问缺的、引用有据、问卷答案齐全、对比编号有据八项不
   合法时最多纠正 1 次，第二次仍不合法就以 failed 收尾，绝不"带着坏决策往下走"。其中"动作不在
   词表／工具不在只读白名单／只带业务输入的决策里塞了授权字段"三种是**越门禁尝试**：它们除了
   被拒绝，还会按 `gate_violations` 计数（计划任务11），由运行时按用户累计后锁定本进程写操作。
2. **补槽位节点**：只把决策阶段已经校验并解析过的槽位并进 state，不重算、不猜。
3. **只读节点**：参数 schema 在决策阶段已经过一遍，这里再校验 facts；工具失败不伪造成功，
   失败事实原样进 `observations`，让模型据此改口或追问。
4. **问卷节点**：把模型从用户回答里读到的五题选项交给银行保存。它不评分、不建本地档案：等级、
   分数和有效期只来自银行返回；保存失败就以失败收尾，不在本地造一个"有效测评"继续推荐。
5. **推荐与对比节点**：筛选条件由程序从会话槽位和银行测评读取，模型只说"要推荐"或"对比哪两个"。
   没有有效测评就先开五题问卷，缺条件就追问，筛不出来就返回空候选——**不放宽风险凑数**。
6. **申购／赎回预览节点**（任务9–10）：模型只提取业务槽位（申购：产品、金额、账户；赎回：持仓、
   金额、到账账户）；程序向银行查事实（申购还查测评，赎回不查），跑硬约束预检，通过才把预览原文与
   业务事实冻结成 `PurchaseSnapshot`／`RedeemSnapshot` 写进 `purchase`／`redeem` 通道。预检不过就
   用一条 `blocked` 事实交回模型如实解释，**不生成待确认快照**。写请求不在这张图里。
7. **回答／追问节点**：回答文本来自模型；事实与业务号只有银行调用和 `domain` 规则会写，所以
   "关键产品、金额和状态只能来自工具事实"不靠模型自律。连续澄清到上限时停止追问（计划任务7）。

会话相关的文本（已知槽位、缺哪些槽位、最近展示顺序、上一轮没被回答的问题、五题问卷）由
`decide_node` 用 `state.py` 与 `domain.rules` 的渲染器渲染后交给提示词：`llm` 层不导入
`workflow`／`domain`，方向保持单向。

依赖方向：`workflow → infrastructure/llm/domain/contracts`。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from typing import Any

from app.agents.decisions import DecisionError
from app.agents.toolcalls import StreamTruncated, collect_stream
from app.core.errors import AppError
from app.integration.model_client import ModelClient, ModelClientError, ToolCall
from app.modules.base import ToolData, ToolFailure
from app.modules.wealth.contracts.bank import (
    Product,
    PurchaseSnapshot,
    RedeemSnapshot,
    RiskProfile,
    WealthBank,
)
from app.modules.wealth.domain.recommendation import (
    Recommendation,
    Scored,
    compare,
    recommend,
    rows,
)
from app.modules.wealth.domain.rules import (
    QUESTION_IDS,
    RISK_LABELS,
    SIMULATION_NOTE,
    PurchaseCheck,
    RedeemAmountError,
    RedeemCheck,
    check_purchase,
    check_redeem,
    parse_redeem_amount,
    purchase_preview,
    questionnaire_lines,
    redeem_preview,
)
from app.modules.wealth.infrastructure.tools import (
    ReadTool,
    check_arguments,
    check_facts,
    extra_arguments,
    read_with_one_retry,
)
from app.modules.wealth.llm.decisions import (
    GateViolation,
    WealthDecision,
    parse_decision,
    render_call,
    ungrounded_citations,
)
from app.modules.wealth.llm.prompts import correction_notice, read_messages, resume_notice
from app.modules.wealth.llm.toolcalls import ToolEntry, function_schemas, tool_table
from app.modules.wealth.workflow.state import (
    ASKABLE_SLOTS,
    MAX_CLARIFICATIONS,
    MAX_CORRECTIONS,
    MAX_MODEL_CALLS,
    MAX_STEPS,
    PURCHASE_SLOTS,
    RECOMMENDATION_SLOTS,
    REDEEM_SLOTS,
    RESOURCE_ID_FIELDS,
    SLOT_LABELS,
    SlotError,
    WealthWorkflowState,
    candidate_lines,
    collected_ids,
    counter,
    merge_business_ids,
    merge_facts,
    missing_slot_lines,
    normalize_redeem_slots,
    normalize_slots,
    slot_lines,
)
from app.tools.protocol import ErrorCode, TrustedContext

__all__ = [
    "ASSESSMENT_FAILURE_SUMMARY",
    "CLARIFY_FAILURE_SUMMARY",
    "DECISION_FAILURE_SUMMARY",
    "MODEL_FAILURE_SUMMARY",
    "TOOL_FAILURE_SUMMARY",
    "ask_user_node",
    "call_read_node",
    "closing_summary",
    "compare_node",
    "decide_node",
    "finish_node",
    "prepare_purchase_node",
    "prepare_redeem_node",
    "recommend_node",
    "route_after_decide",
    "route_after_evidence",
    "route_after_prepare",
    "set_slots_node",
    "submit_risk_node",
    "understand_node",
]

logger = logging.getLogger(__name__)

# 五条收尾话术：都以证据为准，不说"已经办理"，也不把失败说成成功。
MODEL_FAILURE_SUMMARY = "模型服务暂时不可用，本次没有完成查询。"
DECISION_FAILURE_SUMMARY = "模型没有给出可用的查询指令，本次处理已结束。"
TOOL_FAILURE_SUMMARY = "只读工具没有返回可用结果，本次处理已结束。"
CLARIFY_FAILURE_SUMMARY = "同一个问题已经追问多次，本次处理已结束，请重新说明您的需求。"
ASSESSMENT_FAILURE_SUMMARY = "风险测评没有保存成功，本次没有继续推荐。"

# 需要银行返回事实的非只读节点在 `observations` 里用的名字（只影响提示词与日志，不是工具）。
SUBMIT_RISK_STEP = "wealth_submit_risk_assessment"
RECOMMEND_STEP = "wealth_recommend"
COMPARE_STEP = "wealth_compare"
PREPARE_PURCHASE_STEP = "wealth_prepare_purchase"
PREPARE_REDEEM_STEP = "wealth_prepare_redeem"

# 一条"被规则拦住"的事实用的状态：它既不是银行失败（那会让整次响应变成 failed），也不是成功
# （那会让模型以为可以往下走）。申购预检不过时用它，模型据此如实解释，用户不会被误导成"买好了"。
BLOCKED_STATUS = "blocked"

# 一条"缺证据"的事实用的状态：节点告诉模型还缺哪一类资源，由模型决定去查还是去问。
# 与 `blocked` 分开是因为含义不同：`blocked` 是"条件不满足"，`needs_evidence` 是"我还没有数据"。
NEEDS_EVIDENCE_STATUS = "needs_evidence"


def understand_node():
    """理解请求：把运行时的输入归一化成提示词用的任务描述，并起算节点步数。"""

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        return {"task": _task_text(state), "steps": counter(state, "steps") + 1}

    return run


def decide_node(model: ModelClient, *, tools: Mapping[str, ReadTool]):
    """问模型下一步做什么，并把它的输出校验成一条可执行的决策。

    模型看到的是一张**工具表**，这次是以原生工具定义（`function_schemas`）送出去的；它回一轮
    流式答复，里面最多带一个工具调用。工具表由 `llm.toolcalls` 从模块自己的 `ToolSpec` 生成，
    所以"模型能填的字段"与"程序会校验的字段"是同一份定义。

    模型调用次数与节点步数都在这里计入；纠正只发生在这一节点内部，所以一次"格式错误"不会
    变成一次额外的节点推进。
    """

    table = tool_table(tools)
    schemas = function_schemas(table)

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        calls = counter(state, "model_calls")
        if steps >= MAX_STEPS:
            return _budget_stop(state, steps=steps, calls=calls)
        if calls >= MAX_MODEL_CALLS:
            return _budget_stop(state, steps=steps, calls=calls)

        corrections = 0
        # 这一次决策里模型越过门禁的次数（计划任务11）：它随每次返回一起写进状态，由运行时按用户
        # 累计。坏 JSON、缺字段这类普通格式错误**不**计入——只有 `GateViolation` 才计。
        gate_events = 0
        correction = state.get("correction")
        while True:
            if calls >= MAX_MODEL_CALLS:
                return _gated(
                    _budget_stop(state, steps=steps, calls=calls, corrections=corrections),
                    state,
                    gate_events,
                )
            messages = read_messages(
                task=str(state.get("task") or ""),
                observations=list(state.get("observations") or []),
                slots=slot_lines(state.get("slots")),
                missing=missing_slot_lines(state.get("slots")),
                candidates=candidate_lines(state.get("shown")),
                questionnaire=questionnaire_lines(),
                resume=resume_notice(
                    state.get("pending_prompt"), state.get("pending_missing") or ()
                ),
                correction=correction,
                summary=str(state.get("summary_text") or ""),
                history=str(state.get("history_text") or ""),
                memory=str(state.get("memory_text") or ""),
            )
            try:
                reply, tool_calls = _model_decision(model, messages, schemas)
            except (AppError, ModelClientError, StreamTruncated):
                # 模型边界失败：诚实失败，不拿旧事实拼一个"成功"的回答。
                logger.warning("wealth read flow: model call failed")
                return _gated(
                    {
                        "failure": "model",
                        "error_code": ErrorCode.UPSTREAM_UNAVAILABLE,
                        "answer": MODEL_FAILURE_SUMMARY,
                        "model_calls": calls + 1,
                        "corrections": corrections,
                        "steps": steps + 1,
                    },
                    state,
                    gate_events,
                )
            calls += 1

            decision, problem = _read_decision(
                reply, tool_calls, table=table, tools=tools, state=state
            )
            if problem is None and decision is not None:
                return _gated(
                    {
                        "decision": decision.as_dict(),
                        "correction": None,
                        "corrections": corrections,
                        "model_calls": calls,
                        "steps": steps + 1,
                    },
                    state,
                    gate_events,
                )
            if isinstance(problem, GateViolation):
                gate_events += 1
            if corrections >= MAX_CORRECTIONS:
                logger.warning("wealth read flow: unusable decision, corrections exhausted")
                return _gated(
                    {
                        "failure": "decision",
                        "error_code": ErrorCode.PROTOCOL,
                        "answer": DECISION_FAILURE_SUMMARY,
                        "corrections": corrections,
                        "model_calls": calls,
                        "steps": steps + 1,
                    },
                    state,
                    gate_events,
                )
            corrections += 1
            previous = (
                render_call(tool_calls[0].name, tool_calls[0].arguments) if tool_calls else reply
            )
            correction = correction_notice(str(problem), previous)

    return run


def call_read_node(bank: WealthBank, context: TrustedContext, *, tools: Mapping[str, ReadTool]):
    """执行模型选中的只读工具，把结果并入事实。

    同一轮里的多个只读调用按顺序逐个执行；每个都产生一条 observation，事实合并后交给下一轮
    决策。身份只从闭包里的 `context` 来；`arguments` 里就算混进 `user_id` 也到不了银行——
    参数 schema 的 `additionalProperties: false` 在决策阶段已经把它拒了。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        if steps >= MAX_STEPS:
            # 节点推进已经到上限：不再发起银行调用，直接用已有事实收尾。
            return _budget_stop(state, steps=steps, calls=counter(state, "model_calls"))
        steps += 1
        decision = WealthDecision.from_dict(state.get("decision"))
        calls = decision.read_calls or (
            {"tool": decision.tool or "", "arguments": decision.arguments},
        )
        reads = counter(state, "reads") + len(calls)
        observations: list[dict[str, Any]] = []
        merged_facts = state.get("facts")
        merged_business_ids = state.get("business_ids")

        for call in calls:
            name = str(call.get("tool") or "")
            arguments = call.get("arguments") or {}
            tool = tools.get(name)
            if tool is None:
                # 决策阶段已经保证工具在白名单里；到这里说明状态被改坏了。失败关闭，不换一个工具执行。
                logger.error("wealth read flow: state names an unknown read tool")
                return _tool_stop(steps=steps, reads=reads)

            try:
                # 只读工具：边界说"值得再发一次"的失败最多再试一次（计划任务11）。重试用的是同一个
                # 参数、同一个端点，所以它不会变成另一次业务动作，也不会变成一次资金写。
                data = read_with_one_retry(
                    lambda tool=tool, arguments=arguments: tool.run(bank, context, arguments)
                )
            except ToolFailure as failure:
                # 业务失败（含银行不可用、未绑定客户号）：作为一条失败事实交给模型，不伪造成功。
                observations.append(_failed_observation(tool.spec.name, arguments, failure))
                continue

            facts = dict(data.facts) if isinstance(data.facts, Mapping) else {}
            violation = check_facts(tool, facts)
            if violation is not None:
                # 处理器产出的事实不符合自己的声明：实现 bug，不是业务失败。只把路径与规则写日志。
                logger.error(
                    "wealth read tool %s violated its facts schema: %s", tool.spec.name, violation
                )
                return _tool_stop(
                    steps=steps,
                    reads=reads,
                    error_code=ErrorCode.OUTPUT_SCHEMA_VIOLATION,
                )

            observations.append(_succeeded_observation(tool, arguments, data))
            merged_facts = merge_facts(merged_facts, facts)
            merged_business_ids = merge_business_ids(merged_business_ids, data.business_ids)

        return {
            "observations": observations,
            "facts": merged_facts,
            "business_ids": merged_business_ids,
            "reads": reads,
            "steps": steps,
        }

    return run


def set_slots_node():
    """补槽位：把模型从用户这句话里读到的业务槽位并进状态。

    槽位名、金额单位、编号来源和序号引用都在决策阶段用 `normalize_slots` 校验并解析过了，
    这里只做合并——不重算金额、不替用户挑账户、不补一个"看起来合理"的默认值。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        if steps >= MAX_STEPS:
            # 节点推进已经到上限：不再累积槽位，直接用已有事实收尾。
            return _budget_stop(state, steps=steps, calls=counter(state, "model_calls"))
        decision = WealthDecision.from_dict(state.get("decision"))
        slots = {**(state.get("slots") or {}), **dict(decision.slots)}
        return {"slots": slots, "steps": steps + 1}

    return run


def submit_risk_node(bank: WealthBank, context: TrustedContext):
    """保存风险问卷：把模型从用户回答里读到的五题选项交给银行，等级以银行返回为准。

    这是非资金写（需求 §6.2）：由"问卷已经答完"的流程调用，需要审计但不需要交易验证码。它不评分、
    不建本地档案——保存失败就以 failed 收尾，绝不在本地造一个"有效测评"继续推荐或购买。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        if steps >= MAX_STEPS:
            return _budget_stop(state, steps=steps, calls=counter(state, "model_calls"))
        steps += 1
        decision = WealthDecision.from_dict(state.get("decision"))
        try:
            profile = bank.submit_risk_assessment(context, decision.answers)
        except ToolFailure as failure:
            # 银行的失败原因不翻译成"我们这边保存成功了"：没有回执就没有测评。
            logger.warning("wealth flow: risk assessment was not saved")
            return {
                "failure": "assessment",
                "error_code": failure.code,
                "answer": ASSESSMENT_FAILURE_SUMMARY,
                "steps": steps,
            }
        return {
            "observations": [_assessment_observation(profile)],
            "facts": merge_facts(state.get("facts"), {"risk_profile": asdict(profile)}),
            "business_ids": merge_business_ids(
                state.get("business_ids"), {"assessment_id": profile.assessment_id}
            ),
            "steps": steps,
        }

    return run


def recommend_node(bank: WealthBank, context: TrustedContext):
    """按银行测评和用户条件推荐产品：**消费本轮已经查到的证据**，缺什么就如实说缺什么。

    这个节点不自己拉起问卷、也不自己查产品列表：模型决定什么时候查、查什么，节点只做程序该做的
    判断——有没有有效测评、三个条件齐不齐、候选怎么排、风险硬约束怎么卡。缺证据时返回一条
    `needs_evidence` 事实，由模型决定是去查还是去问，然后回到决策节点。

    硬条件一条都不放宽：没有有效测评就没有个性化推荐，筛不出来就是空候选。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        if steps >= MAX_STEPS:
            return _budget_stop(state, steps=steps, calls=counter(state, "model_calls"))
        steps += 1
        facts = state.get("facts") or {}

        profile = _risk_profile_from_facts(facts)
        if profile is None:
            return _needs_evidence(
                state,
                steps=steps,
                evidence="risk_profile",
                reason=(
                    "本轮还没有查到银行的风险测评。先调用 wealth_risk_profile；"
                    "没有保存过或已经过期时，向用户问完五题并 submit_risk。"
                ),
            )
        if not profile.valid:
            return _needs_evidence(
                state,
                steps=steps,
                evidence="risk_profile",
                reason=(
                    "银行里的测评已经过期。向用户问完五题并 submit_risk 之后再 recommend；"
                    "不要用过期等级推荐。"
                ),
            )

        slots = dict(state.get("slots") or {})
        missing = [name for name in RECOMMENDATION_SLOTS if slots.get(name) is None]
        if missing:
            return _needs_evidence(
                state,
                steps=steps,
                evidence="slots",
                reason=(
                    "推荐还缺这些条件，需要向用户追问："
                    + "、".join(SLOT_LABELS.get(name, name) for name in missing)
                ),
                missing=missing,
            )

        products = _products_from_facts(facts)
        if not products:
            return _needs_evidence(
                state,
                steps=steps,
                evidence="products",
                reason="本轮还没有查到产品列表。先调用 wealth_products，再 recommend。",
            )

        result = recommend(
            products,
            risk_level=profile.risk_level,
            amount_minor=slots["plan_amount_minor"],
            horizon_months=slots["horizon_months"],
            liquidity=slots["liquidity"],
        )
        facts = {
            # 产品事实原样进 facts：模型引用候选编号、以及后续对比，都以此为依据。
            "products": [asdict(product) for product in products],
            "recommendation": {
                "assessment_id": profile.assessment_id,
                "risk_level": profile.risk_level,
                "amount_minor": slots["plan_amount_minor"],
                "horizon_months": slots["horizon_months"],
                "liquidity": slots["liquidity"],
                "candidates": rows(result.candidates),
                "not_recommended": rows(result.not_recommended),
                "note": SIMULATION_NOTE,
            },
        }
        return {
            "observations": [_recommendation_observation(result)],
            "facts": merge_facts(state.get("facts"), facts),
            "reads": counter(state, "reads"),
            "steps": steps,
        }

    return run


def compare_node(bank: WealthBank, context: TrustedContext):
    """对比两三个**本轮已经查到的**产品：不重新查银行，缺记录就说缺哪一只。

    对比是分析，不是交易依据（计划任务3 §3.2 第 9 条）：用本轮成功查询的结果就够了。所以这个
    节点不再逐只重新拉取，缺哪只就返回 `needs_evidence`，由模型决定去查它还是换个问法。

    没有测评也能对比（需求 §7.2）：这时只列客观参数并标注"无法判断是否适合"，既不因此允许购买，
    也不因此强制用户先做问卷。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        if steps >= MAX_STEPS:
            return _budget_stop(state, steps=steps, calls=counter(state, "model_calls"))
        steps += 1
        decision = WealthDecision.from_dict(state.get("decision"))
        facts = state.get("facts") or {}
        found = {product.product_id: product for product in _products_from_facts(facts)}

        unknown = [identifier for identifier in decision.product_ids if identifier not in found]
        if unknown:
            return _needs_evidence(
                state,
                steps=steps,
                evidence="products",
                reason=(
                    "这些产品编号本轮还没有查到，先调用 wealth_product 查它们："
                    + "、".join(unknown)
                ),
            )

        profile = _risk_profile_from_facts(facts)
        risk_level = profile.risk_level if (profile is not None and profile.valid) else None
        slots = dict(state.get("slots") or {})
        products = [found[identifier] for identifier in decision.product_ids]
        scored = compare(
            products,
            risk_level=risk_level,
            amount_minor=slots.get("plan_amount_minor"),
            horizon_months=slots.get("horizon_months"),
            liquidity=slots.get("liquidity"),
        )
        facts = {
            "products": [asdict(product) for product in products],
            "comparison": {
                "risk_level": risk_level,
                "assessment_valid": risk_level is not None,
                "rows": rows(scored),
                "note": SIMULATION_NOTE,
            },
        }
        return {
            "observations": [_comparison_observation(scored)],
            "facts": merge_facts(state.get("facts"), facts),
            "reads": counter(state, "reads"),
            "steps": steps,
        }

    return run


def prepare_purchase_node(bank: WealthBank, context: TrustedContext):
    """把模型提取的三个业务槽位变成一份**待确认的申购预览**（任务9）。

    这里做的四件事都是程序的判断，不是提示词建议：

    1. **凑齐槽位**：产品、金额、账户缺一个就把缺项交回模型（金额的整数分规则在决策阶段已经过
       `normalize_slots`），绝不默认挑一个账户去付款。
    2. **向银行查事实**：产品详情、本人账户列表、最近测评——预检用银行刚返回的事实，不复用上一轮
       查询结果（写之前的判断要以当前事实为准）。
    3. **预检**：`domain.rules.check_purchase` 的在售、风险、金额、账户、余额、流动性硬约束。
       不过就交回模型如实解释（`blocked` 事实），**不生成待确认快照**。
    4. **冻结快照**：预览原文由程序模板生成，连同产品、账户、金额、期限一起进 `purchase` 通道；
       运行时据此铸待办、铸幂等键并等用户确认。模型没有通道往快照里写任何东西。

    **为什么这三／四个读取是固定的，而不是交给模型选？** 因为它们是交易预检，不是用户意图路由。
    计划 §3.2 第 6 条把这条边界写得很清楚："`prepare_purchase`／`prepare_redeem` 由模型决定何时
    申请。缺业务槽位时返回缺项供模型追问；**必要的交易预检与确认后的写前刷新可以由程序固定执行**。
    这些是操作不变量，不是用户意图路由。" 第 7 条同时要求"资金写、确认授权、幂等键、身份……不能
    交由模型自由生成"。

    换句话说：**模型决定要不要申请这一次预览**（它自己选择调用哪个工具、什么时候调用），
    而"申请一次申购预览必须先看产品、账户和测评"是这笔操作的定义，不是可选的查询计划。让模型
    来挑要读哪几项，等于让它有机会跳过风险与余额校验——那正是预检存在的理由。

    还有一层区分值得写清楚：**这里的读取根本不走模型那一层。** 模型选的是"工具"（`wealth_product`
    这类，见 `llm/toolcalls.py`），而预检直接调银行协议（`bank.get_product` 等）。两层是分开的，
    所以"工具由模型选择"与"预检由程序固定执行"之间没有冲突：前者是模型能表达什么，后者是程序在
    一笔交易前必须自己核实什么。

    所以这里的固定调用是**有意的**，它不违反"工具由模型选择"，也不与 `recommend`／`compare` 那种
    "缺证据就交回模型"的处理矛盾：那两处是分析，读什么由需求决定；这里是交易前置条件，读什么
    是操作不变量。

    写请求不在这里发出：这个节点只到"待确认"为止。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        if steps >= MAX_STEPS:
            return _budget_stop(state, steps=steps, calls=counter(state, "model_calls"))
        steps += 1
        decision = WealthDecision.from_dict(state.get("decision"))
        slots = {**(state.get("slots") or {}), **dict(decision.slots)}
        missing = [name for name in PURCHASE_SLOTS if slots.get(name) is None]
        if missing:
            # 缺业务槽位：把缺项交回模型，由它决定怎么问、什么时候问——节点不替它组织问题，
            # 也不再拉起一次"必答问卷"式的固定追问链。
            return {
                "slots": slots,
                "observations": [
                    _needs_evidence_observation(
                        PREPARE_PURCHASE_STEP,
                        {
                            name: slots[name]
                            for name in PURCHASE_SLOTS
                            if slots.get(name) is not None
                        },
                        evidence="slots",
                        reason=(
                            "申购预览还缺这些业务槽位，需要向用户追问："
                            + "、".join(SLOT_LABELS.get(name, name) for name in missing)
                        ),
                        missing=missing,
                    )
                ],
                "missing_slots": list(missing),
                "steps": steps,
            }

        reads = counter(state, "reads") + 3
        arguments = {
            "product_id": slots["product_id"],
            "account_id": slots["account_id"],
            "amount_minor": slots["amount_minor"],
        }
        try:
            product = read_with_one_retry(
                lambda: bank.get_product(context, str(slots["product_id"]))
            )
            accounts = read_with_one_retry(lambda: bank.list_accounts(context))
            profile = read_with_one_retry(lambda: bank.get_risk_profile(context))
        except ToolFailure as failure:
            return {
                "slots": slots,
                "observations": [_failed_observation(PREPARE_PURCHASE_STEP, arguments, failure)],
                "reads": reads,
                "steps": steps,
            }

        account = next(
            (item for item in accounts if item.account_id == str(slots["account_id"])), None
        )
        risk_level = profile.risk_level if (profile is not None and profile.valid) else None
        check = check_purchase(
            product,
            account=account,
            risk_level=risk_level,
            amount_minor=int(slots["amount_minor"]),
            liquidity=slots.get("liquidity"),
            user_id=context.bank_customer_id,
        )
        facts: dict[str, Any] = {
            "products": [asdict(product)],
            "accounts": [asdict(item) for item in accounts],
            "risk_profile": None if profile is None else asdict(profile),
        }
        if not check.allowed or account is None:
            # 预检不过：事实与原因照实交给模型解释，不生成待确认快照，也没有任何资金写。
            return {
                "slots": slots,
                "facts": merge_facts(state.get("facts"), facts),
                "observations": [_blocked_purchase_observation(arguments, check)],
                "reads": reads,
                "steps": steps,
            }

        snapshot = PurchaseSnapshot(
            product_id=product.product_id,
            product_name=product.name,
            from_account_id=account.account_id,
            amount_minor=int(slots["amount_minor"]),
            # 首版不允许自选缩短期限：缺省就是产品期限，银行还会复核一次。
            term_months=product.duration_months,
            risk_level=product.risk_level,
            redeem_rule=product.redeem_rule,
            balance_minor=account.available_minor,
            preview=purchase_preview(
                product,
                account,
                risk_level=risk_level,
                amount_minor=int(slots["amount_minor"]),
                term_months=product.duration_months,
            ),
        )
        facts["purchase_preview"] = snapshot.as_state()
        return {
            "slots": slots,
            "facts": merge_facts(state.get("facts"), facts),
            "purchase": snapshot.as_state(),
            "observations": [_purchase_preview_observation(snapshot)],
            "reads": reads,
            "steps": steps,
        }

    return run


def prepare_redeem_node(bank: WealthBank, context: TrustedContext):
    """把模型提取的赎回槽位变成一份**待确认的赎回预览**（任务10）。

    与申购预览同一套门禁，差别只在"赎回取自己的持仓"，所以这里做的事是：

    1. **凑齐槽位**：持仓、金额、到账账户缺一个就把缺项交回模型（缺持仓时提醒它先查持仓、把候选
       列给用户挑），绝不默认赎回第一笔（计划任务7／10）。金额可以是具体整数分，也可以是"全部"
       或"一半"。
    2. **解析金额**：`{"all": true}` 取银行的可赎回额，`{"half": true}` 按它向下取整到分；
       **预览前一定是一个确定的整数分**。
    3. **向银行查事实**：持仓详情、产品、本人账户列表——预检用银行刚返回的事实。
    4. **预检**：`domain.rules.check_redeem` 的持仓归属、可赎回额、到账账户硬约束。**不查风险
       测评**：赎回是取回自己的持仓，不要求重新做问卷。
    5. **冻结快照**：预览原文由程序模板生成，连同持仓、金额、到账账户一起进 `redeem` 通道；
       运行时据此铸待办、铸幂等键并等用户确认。写请求不在这里发出。

    第 3 步的三个读取是**固定的**，理由与申购预览完全相同：它们是交易预检（操作不变量），不是
    用户意图路由（计划 §3.2 第 6 条）。**模型决定要不要申请这一次预览**，而"申请一次赎回预览必须
    先看这笔持仓、它的产品和到账账户"是这笔操作的定义。特别地，**不查风险测评**这一条同样是
    不变量：赎回是取回自己的持仓，不因为模型没查就不做，也不因为模型查了就要求重做问卷。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps")
        if steps >= MAX_STEPS:
            return _budget_stop(state, steps=steps, calls=counter(state, "model_calls"))
        steps += 1
        decision = WealthDecision.from_dict(state.get("decision"))
        slots = {**(state.get("slots") or {}), **dict(decision.slots)}
        missing = [name for name in REDEEM_SLOTS if slots.get(name) is None]
        if missing:
            return _ask_which_holding(state, bank, context, slots, missing, steps=steps)
        reads = counter(state, "reads") + 3
        arguments = {
            "holding_id": slots["holding_id"],
            "amount_minor": slots["amount_minor"],
            "account_id": slots["account_id"],
        }
        try:
            holding = read_with_one_retry(
                lambda: bank.get_holding(context, str(slots["holding_id"]))
            )
            product = read_with_one_retry(lambda: bank.get_product(context, holding.product_id))
            accounts = read_with_one_retry(lambda: bank.list_accounts(context))
        except ToolFailure as failure:
            return {
                "slots": slots,
                "observations": [_failed_observation(PREPARE_REDEEM_STEP, arguments, failure)],
                "reads": reads,
                "steps": steps,
            }

        account = next(
            (item for item in accounts if item.account_id == str(slots["account_id"])), None
        )
        try:
            amount_minor = parse_redeem_amount(slots["amount_minor"]).resolve(
                holding.redeemable_minor
            )
        except RedeemAmountError:
            # 金额形状在决策阶段已经校验过；到这里说明状态被改坏了。失败关闭，不猜一个金额。
            logger.error("wealth flow: the redeem amount in state is unusable")
            return _tool_stop(steps=steps, reads=reads, error_code=ErrorCode.PROTOCOL)

        check = check_redeem(
            holding,
            account=account,
            amount_minor=amount_minor,
            user_id=context.bank_customer_id,
        )
        facts: dict[str, Any] = {
            "holdings": [asdict(holding)],
            "accounts": [asdict(item) for item in accounts],
        }
        # 金额解析成具体整数分之后再进槽位：会话里不留 `{"all": true}` 这种待解析形式。
        resolved_slots = {**slots, "amount_minor": amount_minor}
        if not check.allowed or account is None:
            return {
                "slots": resolved_slots,
                "facts": merge_facts(state.get("facts"), facts),
                "observations": [_blocked_redeem_observation(arguments, check)],
                "reads": reads,
                "steps": steps,
            }

        snapshot = RedeemSnapshot(
            holding_id=holding.holding_id,
            product_id=holding.product_id,
            product_name=product.name,
            to_account_id=account.account_id,
            amount_minor=amount_minor,
            redeemable_minor=holding.redeemable_minor,
            redeem_rule=product.redeem_rule,
            full=amount_minor == holding.redeemable_minor,
            preview=redeem_preview(
                holding,
                account,
                amount_minor=amount_minor,
                redeemable_minor=holding.redeemable_minor,
            ),
        )
        facts["redeem_preview"] = snapshot.as_state()
        return {
            "slots": resolved_slots,
            "facts": merge_facts(state.get("facts"), facts),
            "redeem": snapshot.as_state(),
            "observations": [_redeem_preview_observation(snapshot)],
            "reads": reads,
            "steps": steps,
        }

    return run


def _ask_which_holding(
    state: WealthWorkflowState,
    bank: WealthBank,
    context: TrustedContext,
    slots: Mapping[str, Any],
    missing: Sequence[str],
    *,
    steps: int,
) -> dict[str, Any]:
    """缺槽位时的缺项事实：说清缺什么，由模型决定怎么问。

    这个函数**不再自己拉取持仓**（计划任务3）：要不要把候选摆到用户眼前是模型的判断，它可以先
    调 `wealth_holdings`，也可以直接问一句"要赎回哪一笔"。节点只负责如实报出缺项，并在缺的是
    持仓时提醒一句"先查持仓再问，别替用户挑一笔"。

    仍然坚持的硬规则只有一条：**绝不默认赎回第一笔**。它由参数校验保证（`holding_id` 只能来自
    查到的记录），而不是由这里猜一个值。
    """

    del bank, context  # 只读调用由模型选择，节点不再自己发起
    question_hint = _missing_slots_question(missing, purpose="完成这笔赎回预览")
    if "holding_id" in missing:
        question_hint += "（先调用 wealth_holdings 拿到本人可赎回的持仓，再把它们列给用户挑；不要替用户选一笔。）"
    return {
        "slots": dict(slots),
        "observations": [
            _needs_evidence_observation(
                PREPARE_REDEEM_STEP,
                {name: slots[name] for name in REDEEM_SLOTS if slots.get(name) is not None},
                evidence="slots",
                reason=question_hint,
                missing=list(missing),
            )
        ],
        "missing_slots": list(missing),
        "steps": steps,
    }


def ask_user_node():
    """追问：把模型的问题交给运行时，由运行时铸 `pending_id` 并等下一次输入。

    问题由模型自由组织（可以是问卷的某一题、可以是金额、可以是任何澄清），这里不再限制问题必须
    对应槽位词表里的名字——限制在 `_read_decision` 里只保留一条："已经知道的信息不要重复问"。

    连续追问的次数由运行时按会话保存并在下一次 `handle` 时写回 state；到上限就停止追问，
    不把用户拖进无限问答（计划任务7）。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        steps = counter(state, "steps") + 1
        stop = _clarification_stop(state, steps=steps)
        if stop is not None:
            return stop
        decision = WealthDecision.from_dict(state.get("decision"))
        return {
            "wealth_asked_question": decision.question,
            "missing_slots": list(decision.missing),
            "steps": steps,
        }

    return run


def finish_node():
    """回答：把模型的回答交给运行时；事实与业务号已经由只读节点写好。

    `shown` 是模型声明"我按这个顺序给用户列了这些候选"，运行时按它解析后续的"第几个"。
    编号与资源名在决策阶段已经核对过工具返回，这里只搬运。
    """

    def run(state: WealthWorkflowState) -> dict[str, Any]:
        decision = WealthDecision.from_dict(state.get("decision"))
        update: dict[str, Any] = {
            "answer": decision.answer,
            "steps": counter(state, "steps") + 1,
        }
        if decision.shown:
            update["shown"] = {key: list(value) for key, value in decision.shown.items()}
        return update

    return run


def route_after_decide(state: WealthWorkflowState) -> str:
    """决策之后去哪：失败就结束，否则按动作走。"""

    if state.get("failure"):
        return "end"
    action = (state.get("decision") or {}).get("action")
    if action in (
        "call_read",
        "set_slots",
        "ask_user",
        "submit_risk",
        "recommend",
        "compare",
        "prepare_purchase",
        "prepare_redeem",
        "finish",
    ):
        return str(action)
    return "end"


def route_after_prepare(state: WealthWorkflowState) -> str:
    """申购／赎回预览之后去哪。

    有快照（等用户确认）、有追问、或已经失败，都结束；否则说明预检没过或银行调用失败，交回模型
    让它如实解释或改口。
    """

    if (
        state.get("failure")
        or state.get("wealth_asked_question")
        or state.get("purchase")
        or state.get("redeem")
    ):
        return "end"
    return "decide"


def route_after_evidence(state: WealthWorkflowState) -> str:
    """问卷／推荐／对比之后去哪：已经有结论（追问或失败）就结束，否则交回模型继续决策。"""

    if state.get("failure") or state.get("wealth_asked_question"):
        return "end"
    return "decide"


def closing_summary(state: WealthWorkflowState) -> str:
    """预算用尽时的收尾：只复述已经查到的事实，不补一句模型没说过的话。"""

    succeeded = [
        observation
        for observation in state.get("observations") or []
        if observation.get("status") == "succeeded"
    ]
    if not succeeded:
        return "本次处理已达到上限，暂时没有可以回答的查询结果。"
    lines = "；".join(
        str(observation.get("summary") or observation.get("tool") or "")
        for observation in succeeded
    )
    return f"本次处理已达到上限，以下是已经查到的结果：{lines}"


def _read_decision(
    reply: str,
    tool_calls: Sequence[ToolCall],
    *,
    table: Mapping[str, ToolEntry],
    tools: Mapping[str, ReadTool],
    state: WealthWorkflowState,
) -> tuple[WealthDecision | None, DecisionError | None]:
    """把一轮模型输出读成决策；返回 `(决策, 问题)`，问题非空表示不可用。

    工具名、参数 schema 与"多个调用只能全是只读工具"由 `parse_decision` 把住（用的是工具表里
    那一份 schema）。这里补三件只有拿得到 state 才能做的检查：槽位名与槽位值、问卷答案是否齐全、
    以及"引用／展示的编号必须来自工具返回"。

    第二个返回值是 `DecisionError` 本身而不是一句话：调用方要区分"格式写错了"（纠正一次就好）
    与"想越门禁"（`GateViolation`，要按用户累计，计划任务11）。
    """

    try:
        decision = parse_decision(reply, tool_calls, table=table)
    except DecisionError as error:
        return None, error
    if decision.action == "call_read":
        calls = decision.read_calls or (
            {"tool": decision.tool or "", "arguments": decision.arguments},
        )
        for call in calls:
            name = str(call.get("tool") or "")
            arguments = call.get("arguments") or {}
            tool = tools.get(name)
            if tool is None:
                return None, GateViolation(f"call_read 的工具不在白名单里：{name}")
            extra = extra_arguments(tool, arguments)
            if extra:
                # 只读工具的参数 schema 里没有身份、确认、验证码和幂等键：模型顺手带上它们是一次
                # 越门禁的尝试，而不是一次普通的参数写错。
                return None, GateViolation(
                    "call_read 的参数里有工具没有声明的字段：" + "、".join(extra)
                )
            problem = check_arguments(tool, arguments)
            if problem is not None:
                return None, DecisionError(problem)
        return decision, None
    if decision.action == "set_slots":
        try:
            resolved = normalize_slots(
                decision.slots,
                facts=state.get("facts"),
                shown=state.get("shown"),
                recent_holding=_recent_holding_id(state),
            )
        except SlotError as error:
            return None, DecisionError(str(error))
        return replace(decision, slots=resolved), None
    if decision.action == "ask_user":
        # 问题本身自由（问卷的某一题、金额、任何澄清），但 `missing` 是**必填**的声明：它说明这次
        # 提问在补哪几个槽位。声明必须能被程序检查，所以只收词表里的槽位名——一个程序不认识的名字
        # 既不能驱动候选展示，也不能用来判断"是不是在重复问已知信息"，等于没声明。
        #
        # 两条机械规则：
        #   - 名字必须在 `ASKABLE_SLOTS` 里（不在补槽位就写 `[]`）；
        #   - 不能点名已经知道的槽位（"已知信息不重复问"，计划 §3.2 第 3 条）。
        #
        # 这条边界的反面也要说清楚：程序**不能**从自由文本里读出"这句话是不是又在问金额"，所以它
        # 保证的是"模型必须声明、且声明里不能有已知信息"，而不是"任何措辞都不会重复问"。
        filled = set(state.get("slots") or {})
        for name in decision.missing:
            if name not in ASKABLE_SLOTS:
                return None, DecisionError(
                    f"未知的槽位 {name}：missing 只能列 "
                    + "、".join(ASKABLE_SLOTS)
                    + "，不在补槽位就写 []"
                )
            if name in filled:
                return None, DecisionError(f"槽位 {name} 已经知道，不要再追问")
        return decision, None
    if decision.action == "submit_risk":
        problem = _questionnaire_problem(decision)
        return (decision, None) if problem is None else (None, DecisionError(problem))
    if decision.action == "recommend":
        # 筛选条件由推荐节点从会话槽位和银行测评读取：模型这一层没有可校验的参数。
        return decision, None
    if decision.action == "compare":
        # 产品编号是否本轮已经查到，由 `compare_node` 判定并回报缺哪一只（`needs_evidence`），
        # 而不是在这里纠正一次：纠正只有一次预算，而"去查那一只"是模型该做的下一步。
        return decision, None
    if decision.action == "prepare_purchase":
        # 申购预览只接受产品、金额、账户三个业务槽位，值按会话槽位规则校验并解析
        # （金额是整数分、编号来自查到的记录或最近展示过的序号）。多带一个槽位名就是想把
        # 身份或授权字段塞进来，按越门禁计。
        unknown = sorted(set(decision.slots) - set(PURCHASE_SLOTS))
        if unknown:
            return None, GateViolation(
                "prepare_purchase 只接受 "
                + "、".join(PURCHASE_SLOTS)
                + "，不能带 "
                + "、".join(unknown)
            )
        try:
            resolved = normalize_slots(
                decision.slots,
                facts=state.get("facts"),
                shown=state.get("shown"),
                recent_holding=_recent_holding_id(state),
                allowed=PURCHASE_SLOTS,
            )
        except SlotError as error:
            return None, DecisionError(str(error))
        return replace(decision, slots=resolved), None
    if decision.action == "prepare_redeem":
        # 赎回预览只接受持仓、金额、到账账户三个业务槽位；金额还允许 `{"all": true}` 与
        # `{"half": true}`（具体金额由赎回节点按银行可赎回额解析，所以预览前一定是确定的数）。
        unknown = sorted(set(decision.slots) - set(REDEEM_SLOTS))
        if unknown:
            return None, GateViolation(
                "prepare_redeem 只接受 "
                + "、".join(REDEEM_SLOTS)
                + "，不能带 "
                + "、".join(unknown)
            )
        try:
            resolved = normalize_redeem_slots(
                decision.slots,
                facts=state.get("facts"),
                shown=state.get("shown"),
                recent_holding=_recent_holding_id(state),
            )
        except SlotError as error:
            return None, DecisionError(str(error))
        return replace(decision, slots=resolved), None

    ungrounded = ungrounded_citations(
        decision, collected_ids(state.get("facts"), state.get("business_ids"))
    )
    if ungrounded is not None:
        return None, DecisionError(f"引用了工具没有返回过的编号 {ungrounded}")
    problem = _ungrounded_shown(decision, state)
    if problem is not None:
        return None, DecisionError(problem)
    return decision, None


def _gated(update: dict[str, Any], state: WealthWorkflowState, gate_events: int) -> dict[str, Any]:
    """把这一次决策累计的越门禁次数写进返回值（计划任务11）。

    运行时按用户累计它并据此锁定本进程的写操作；这里只负责把"这一次处理里发生了几次"如实报上去。
    """

    update["gate_violations"] = counter(state, "gate_violations") + gate_events
    return update


def _questionnaire_problem(decision: WealthDecision) -> str | None:
    """问卷答案必须正好是 q1..q5：少一题不提交，多一个题目名也不提交。

    这一层（而不是 `llm` 层）拿得到问卷定义：题面与银行 `wealth-risk-v1` 对齐的是 `domain.rules`，
    `llm` 只认"对象 + 0／1／2 选项"这个形状。
    """

    given = set(decision.answers)
    missing = [question for question in QUESTION_IDS if question not in given]
    if missing:
        return "问卷答案缺少 " + "、".join(missing)
    unknown = sorted(given - set(QUESTION_IDS))
    if unknown:
        return "问卷答案含未知题目 " + "、".join(unknown)
    return None


def _needs_evidence(
    state: WealthWorkflowState,
    *,
    steps: int,
    evidence: str,
    reason: str,
    missing: Sequence[str] = (),
    step: str = RECOMMEND_STEP,
) -> dict[str, Any]:
    """缺证据时的收尾：把"缺什么、为什么"作为一条事实交回决策节点。

    这是计划 §3.2 第 4 条：推荐与对比不再自己拉起问卷或产品查询，而是返回一条
    `needs_evidence`，由模型决定下一步查哪个工具、还是先问用户。节点因此不再需要"缺条件时的
    固定追问链"，也不需要为每种问法准备一句话术。

    `step` 是这条事实的名字（推荐还是对比）：模型按它分辨"哪一步缺东西"，所以调用方必须传自己
    那一个，不能让它一律报成推荐步骤。

    它**不是**失败：`status` 用 `needs_evidence` 这个独立取值，所以运行时不会把它当成
    "银行不可用"而返回 failed，模型也不会以为已经查到东西了。
    """

    return {
        "observations": [
            _needs_evidence_observation(
                step, {}, evidence=evidence, reason=reason, missing=list(missing)
            )
        ],
        "missing_slots": list(missing),
        "steps": steps,
    }


def _needs_evidence_observation(
    name: str,
    arguments: Mapping[str, Any],
    *,
    evidence: str,
    reason: str,
    missing: Sequence[str] = (),
) -> dict[str, Any]:
    """一条"缺证据"的事实：说清缺哪一类资源、为什么，以及下一步可以做什么。"""

    return {
        "tool": name,
        "arguments": dict(arguments),
        "status": NEEDS_EVIDENCE_STATUS,
        "summary": reason,
        "facts": {
            "needs_evidence": {
                "resource": evidence,
                "reason": reason,
                "missing_slots": list(missing),
            }
        },
    }


def _risk_profile_from_facts(facts: Mapping[str, Any]) -> RiskProfile | None:
    """本轮工具事实里的风险测评；没有查到或形状不对就是 `None`。

    只认**本轮**的查询结果：上一轮的测评可能已经过期，而"这一轮有没有有效测评"正是推荐硬条件
    的判断依据，不能靠记忆里的旧值。
    """

    payload = (facts or {}).get("risk_profile")
    if not isinstance(payload, Mapping):
        return None
    try:
        return RiskProfile.from_bank(payload)
    except (KeyError, TypeError, ValueError):
        logger.warning("wealth flow: the risk profile in facts is unusable")
        return None


def _products_from_facts(facts: Mapping[str, Any]) -> list[Product]:
    """本轮工具事实里的产品记录；形状不对的条目丢掉。

    丢掉而不是报错：一次查询里混进一条读不懂的记录，不应该让整次推荐变成失败。
    """

    rows = (facts or {}).get("products")
    if not isinstance(rows, list):
        return []
    products: list[Product] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            products.append(Product.from_bank(row))
        except (KeyError, TypeError, ValueError):
            continue
    return products


def _ungrounded_shown(decision: WealthDecision, state: WealthWorkflowState) -> str | None:
    """`shown` 的资源名和编号都要有依据；第一条问题或 None。

    `shown` 决定"第几个"怎么解析，所以它不能是模型随手编的一串编号：资源名必须是四种已知资源，
    编号必须是这次已经查到的记录。
    """

    if not decision.shown:
        return None
    known = collected_ids(state.get("facts"), state.get("business_ids"))
    for resource, identifiers in decision.shown.items():
        if resource not in RESOURCE_ID_FIELDS:
            return f"shown 的资源名 {resource} 不可用"
        for identifier in identifiers:
            if identifier not in known:
                return f"shown 里的编号 {identifier} 不是工具返回过的记录"
    return None


def _missing_slots_question(missing: Sequence[str], *, purpose: str = "给您推荐") -> str:
    """缺条件时的追问：只问缺的那几个，名字来自槽位词表。"""

    names = "、".join(SLOT_LABELS.get(name, name) for name in missing)
    return f"为了{purpose}，还需要您补充：{names}。"


def _clarification_stop(state: WealthWorkflowState, *, steps: int) -> dict[str, Any] | None:
    """连续追问到上限就停止追问；返回 None 表示还可以再问一次。

    追问的两个入口（模型的 `ask_user` 和推荐节点自己的问卷／补条件）共用这一条边界：同一个任务
    连续澄清最多 `MAX_CLARIFICATIONS` 次，不把用户拖进无限问答（计划任务7）。
    """

    if counter(state, "clarifications") < MAX_CLARIFICATIONS:
        return None
    logger.info("wealth flow: clarification budget exhausted")
    return {
        "failure": "clarifications",
        "error_code": ErrorCode.INTERNAL,
        "answer": CLARIFY_FAILURE_SUMMARY,
        "steps": steps,
    }


def _purchase_preview_observation(snapshot: PurchaseSnapshot) -> dict[str, Any]:
    """一份待确认的申购预览作为事实：内容与用户看到的那段原文完全一致。"""

    return {
        "tool": PREPARE_PURCHASE_STEP,
        "arguments": {
            "product_id": snapshot.product_id,
            "account_id": snapshot.from_account_id,
            "amount_minor": snapshot.amount_minor,
        },
        "status": "succeeded",
        "summary": f"已生成待确认的模拟申购预览（{snapshot.product_id}）。",
        "facts": {"purchase_preview": snapshot.as_state()},
    }


def _blocked_purchase_observation(
    arguments: Mapping[str, Any], check: PurchaseCheck
) -> dict[str, Any]:
    """预检被拦下的事实：原因逐条来自 `domain` 规则，`status` 既不是 succeeded 也不是 failed。"""

    return {
        "tool": PREPARE_PURCHASE_STEP,
        "arguments": dict(arguments),
        "status": BLOCKED_STATUS,
        "summary": "申购预检没有通过：" + "；".join(check.reasons),
        "facts": {"purchase_check": {"allowed": False, "reasons": list(check.reasons)}},
    }


def _recent_holding_id(state: WealthWorkflowState) -> str | None:
    """会话记下的"最近一次成功申购的新仓"编号；没有就是 None（"刚才买的"没有对应持仓）。"""

    recent = state.get("recent_holding")
    if not isinstance(recent, Mapping):
        return None
    value = recent.get("holding_id")
    return value if isinstance(value, str) and value.strip() else None


def _redeem_preview_observation(snapshot: RedeemSnapshot) -> dict[str, Any]:
    """一份待确认的赎回预览作为事实：内容与用户看到的那段原文完全一致。"""

    return {
        "tool": PREPARE_REDEEM_STEP,
        "arguments": {
            "holding_id": snapshot.holding_id,
            "amount_minor": snapshot.amount_minor,
            "account_id": snapshot.to_account_id,
        },
        "status": "succeeded",
        "summary": f"已生成待确认的模拟赎回预览（{snapshot.holding_id}）。",
        "facts": {"redeem_preview": snapshot.as_state()},
    }


def _blocked_redeem_observation(arguments: Mapping[str, Any], check: RedeemCheck) -> dict[str, Any]:
    """赎回预检被拦下的事实：原因逐条来自 `domain` 规则，`status` 既不是 succeeded 也不是 failed。"""

    return {
        "tool": PREPARE_REDEEM_STEP,
        "arguments": dict(arguments),
        "status": BLOCKED_STATUS,
        "summary": "赎回预检没有通过：" + "；".join(check.reasons),
        "facts": {"redeem_check": {"allowed": False, "reasons": list(check.reasons)}},
    }


def _assessment_observation(profile: RiskProfile) -> dict[str, Any]:
    """问卷保存成功的证据：等级、分数、有效期和测评编号都来自银行返回。"""

    return {
        "tool": SUBMIT_RISK_STEP,
        "arguments": {"answers": dict(profile.answers)},
        "status": "succeeded",
        "summary": (
            f"银行已保存测评 {profile.assessment_id}，等级为"
            f"{_risk_label(profile.risk_level)}风险，有效期至 {profile.expires_at}。"
        ),
        "facts": {"risk_profile": asdict(profile)},
    }


def _recommendation_observation(result: Recommendation) -> dict[str, Any]:
    """推荐结果作为一条事实：候选、落选产品和筛选条件都在 facts 里。"""

    if result.candidates:
        names = "、".join(item.product.name for item in result.candidates)
        summary = f"按您的条件筛出 {len(result.candidates)} 只候选：{names}。"
    else:
        summary = "按您的条件没有筛出合适的产品，候选为空。"
    return {
        "tool": RECOMMEND_STEP,
        "arguments": {},
        "status": "succeeded",
        "summary": summary,
        "facts": {
            "recommendation": {
                "candidates": rows(result.candidates),
                "not_recommended": rows(result.not_recommended),
            }
        },
    }


def _comparison_observation(scored: Sequence[Scored]) -> dict[str, Any]:
    """对比结果作为一条事实：每只产品的客观参数与适配结论都来自银行和 `domain` 规则。"""

    names = "、".join(item.product.name for item in scored)
    return {
        "tool": COMPARE_STEP,
        "arguments": {},
        "status": "succeeded",
        "summary": f"已按银行事实对比 {len(scored)} 只产品：{names}。",
        "facts": {"comparison": {"rows": rows(scored)}},
    }


def _risk_label(value: str) -> str:
    return RISK_LABELS.get(value, value)


def _succeeded_observation(
    tool: ReadTool, arguments: Mapping[str, Any], data: ToolData
) -> dict[str, Any]:
    """一条成功的工具事实：结论加事实，两者都来自工具返回。"""

    return {
        "tool": tool.spec.name,
        "arguments": dict(arguments),
        "status": "succeeded",
        "summary": data.summary,
        "facts": dict(data.facts),
    }


def _failed_observation(
    name: str, arguments: Mapping[str, Any], failure: ToolFailure
) -> dict[str, Any]:
    """一条失败的银行事实：错误码加中文说明；银行原文不会出现在这里。

    `name` 是只读工具名，或推荐／对比这类同样要银行返回的步骤名——两者在提示词里是同一种证据。
    """

    return {
        "tool": name,
        "arguments": dict(arguments),
        "status": "failed",
        "error_code": failure.code,
        "error": failure.message,
    }


def _budget_stop(
    state: WealthWorkflowState, *, steps: int, calls: int, corrections: int | None = None
) -> dict[str, Any]:
    """预算用尽：不调用模型、不调用银行，直接用已查到的事实收尾。"""

    update: dict[str, Any] = {
        "failure": "budget",
        "error_code": ErrorCode.INTERNAL,
        "answer": closing_summary(state),
        "steps": steps,
        "model_calls": calls,
    }
    if corrections is not None:
        update["corrections"] = corrections
    return update


def _tool_stop(*, steps: int, reads: int, error_code: str = ErrorCode.INTERNAL) -> dict[str, Any]:
    """只读节点无法产出事实：以内部错误收尾，不让模型基于残缺状态继续。"""

    return {
        "failure": "tool",
        "error_code": error_code,
        "answer": TOOL_FAILURE_SUMMARY,
        "reads": reads,
        "steps": steps,
    }


def _task_text(state: WealthWorkflowState) -> str:
    """提示词里的"本次请求"：新请求是用户原话，回答待办时是问题加答案。"""

    question = state.get("question")
    answer = state.get("answer_text")
    if question and answer:
        return f"（用户回答了你上一次的提问）问题：{question}\n用户回答：{answer}"
    return str(state.get("request_text") or "")


def _model_decision(
    model: ModelClient,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> tuple[str, list[ToolCall]]:
    """一次模型调用：流式带工具定义，复用基座"只重试一次瞬时失败"的边界。"""

    return collect_stream(model, tuple(dict(m) for m in messages), tuple(tools))
