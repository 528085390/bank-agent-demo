"""bill 模块的工具声明（`bill_` 前缀）与 handler 装配。

搬来的三组声明（邮件 / 明细查询 / 深度分析）在这里收敛成一个 `SPECS`，工具名统一加
`bill_` 前缀；发信那组拆成 prepare 与 execute 两件（前者进模型菜单、后者隐藏），所以
`SPECS` 是四件。`TOOLS` 与 `build_handlers` 是这一层对外的全部接口，
`app/modules/bill/__init__.py` 拿它们构造 `ModuleSpec`。

依赖（数据库句柄、`NotificationService`）一律在函数内 import 后注入：`tools.py` 在
import 期只认识 `app.modules.base` 与 `app.tools.protocol`，否则
`registry → modules → bill → services` 会在 import 期成环。
"""

from __future__ import annotations

import json

from app.modules.base import ToolHandler
from app.tools.protocol import SideEffect, ToolSpec


def spec(
    name, description, properties=None, required=(), *, write=False, output, **constraints
):
    """声明一个 bill 工具。

    与 card / subscription 的 `spec` 同形（描述尾部内嵌参数 JSON Schema），只有 `output`
    不同：那两家的 `facts` 都是「单键包一层」，账单这三个不是 —— 明细是分页对象、
    深度分析是整份报告、发信是投递回执 —— 所以这里直接收 `facts` 的 schema。
    """
    schema = {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
        **constraints,
    }
    return ToolSpec(
        name=f"bill_{name}",
        description=f"{description}。参数 JSON Schema：{json.dumps(schema, ensure_ascii=False)}",
        parameters=schema,
        side_effect=SideEffect.WRITE if write else SideEffect.READ,
        output_schema=output,
    )


def date_param(label: str) -> dict:
    """日期参数：模型可以直接传用户原话，解析在 handler 侧（app/modules/dates.py）。"""
    return {
        "type": "string",
        "description": (
            f"{label}。YYYY-MM-DD，也可以直接用用户原话里的写法："
            "9月13日、今年5月1日、五月一日、今天、昨天、前天；"
            "整年（年报）可以只写年份：2025年、今年、去年、前年。"
            "「今年」原样传下来，不要自己换算年份。"
        ),
    }


# `facts` 的形状（output_schema 只约束 facts，见 app/tools/bank_adapter.py:132-165）。
# 三份都要与 handler 实际返回的 facts 一一对应，否则调用会被判 OUTPUT_SCHEMA_VIOLATION。

#: 明细查询：BillQueryService.get_transactions 的分页结果（handlers.py:68-77）。
TRANSACTION_PAGE = {
    "type": "object",
    "properties": {
        "total": {"type": "integer"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "items": {"type": "array"},
        # 按文件查询时非空：表格恢复后靠它翻页（日期区间那一路则为 null）。
        "task_id": {"type": ["string", "null"]},
    },
    "required": ["total", "page", "page_size", "items", "task_id"],
    "additionalProperties": False,
}

#: 深度分析：整份报告（AnalysisReport.to_dict，analysis_handlers.py:62-66）。
ANALYSIS_REPORT = {
    "type": "object",
    "properties": {
        "report_meta": {"type": "object"},
        "modules": {"type": "array"},
        "text_report": {"type": "string"},
        "audit": {"type": "object"},
    },
    "required": ["report_meta", "modules", "text_report", "audit"],
    "additionalProperties": False,
}

#: 发信冻结：prepare 只回这四个键（email_handlers.py 的 prepare_send_report）。
#: 它不含 `format` / `queued` / `notification_id` —— 那些是投递回执，冻结时还不存在。
REPORT_PREPARATION = {
    "type": "object",
    "properties": {
        "action_id": {"type": "string"},
        "period_start": {"type": "string"},
        "period_end": {"type": "string"},
        "recipient": {"type": "string"},
        # 按文件发信时非空：执行阶段靠它定位同一份账单。
        "task_id": {"type": ["string", "null"]},
    },
    "required": ["action_id", "period_start", "period_end", "recipient", "task_id"],
    "additionalProperties": False,
}

#: 发信回执：send_email 的返回（email_handlers.py 的 execute_send_report）。队列模式下没有
#: `notification_id`（只有 job_id），所以它可空。
REPORT_DELIVERY = {
    "type": "object",
    "properties": {
        "recipient": {"type": "string"},
        "period": {"type": "string"},
        "format": {"type": "string"},
        "notification_id": {"type": ["string", "null"]},
        "queued": {"type": "boolean"},
    },
    "required": ["recipient", "period", "format", "queued"],
    "additionalProperties": False,
}


#: 账单查询 / 深度分析工具声明（bill agent 使用，纯读取）。
#:
#: 职责边界（见《修正方案》）：agent 只查询与分析已入库数据；上传、解压、解析、入库
#: 属于后台服务，不经过 agent。查询类工具都是 SideEffect.READ。
#:
#: `query_bill_summary` 已删除：它返回的是「收入/支出/净额/笔数」—— 那是**统计**，不是
#: 分析。账单分析这条线统一走 `bill_analyze_comprehensive`，只保留明细查询作为补充。
#:
#: 可选参数必须声明成可空（`["string", "null"]`）—— 这不是啰嗦，是两边必须一致：
#: `app/tools/schema.py` 的 `normalize_parameters` 会把**每一个**非 required 的属性改成
#: `["<type>", "null"]` 并加进 required，模型手上没有内容时就传 `null`；而网关是拿
#: **这份原始声明**去校验的，声明里只写 `"string"` 的话，模型传 `null` 会被判
#: `failed type`，整个调用判 DENIED、handler 一行都不执行。
SPECS = [
    spec(
        "query_transactions",
        (
            "查询指定时间段的交易明细，支持按分类和方向筛选、分页。"
            "**用户说「查 / 看流水」「看看某笔交易」时用这个** —— 那是翻看交易，不是分析，"
            "不要在这条路上给他「上传新的 / 查询已导入」的选项。"
            "**注意别被字面带偏**：用户要「分析」（包括他刚选了「查询已导入的账单」这个选项）"
            "时，用 `bill_analyze_comprehensive` —— 那个选项里虽然有「查询」二字，"
            "但它指的是「在已导入的数据里做分析」。"
            "**只用于查看/列明细，不要用于分析或统计；用户说「分析 / 统计 / 报告」时"
            "改用 `bill_analyze_comprehensive`，并且不要和它同时调用。**"
            "**当前这条消息**里带任务号（「查询刚导入的流水（任务 task_…）」）时把任务号原样放进 "
            "`task_id`、不要问日期：查的就是这份账单的全部流水；没有任务号时才按日期查。"
            "历史消息里的任务号不算。"
            "账单数据来自用户上传的流水文件；若返回未找到数据，说明该周期还没上传流水，"
            "应提示用户点击界面右上角「上传流水」按钮上传后重试。"
        ),
        {
            "period_start": date_param("开始日期（给了 task_id 时不需要）"),
            "period_end": date_param("结束日期（给了 task_id 时不需要）"),
            "task_id": {
                "type": ["string", "null"],
                "description": (
                    "账单任务号（形如 task_…），只查这一份账单解析出来的流水；"
                    "只在用户消息里本来就带着时原样传下来，**不要向用户索要**"
                ),
            },
            "category": {"type": ["string", "null"], "description": "分类筛选（可选）"},
            "direction": {
                "type": ["string", "null"],
                # enum 里也要有 null：类型允许了，枚举不放行照样判失败。
                "enum": ["debit", "credit", None],
                "description": "方向筛选（可选）",
            },
            "page": {"type": ["integer", "null"], "description": "页码，默认 1"},
            "page_size": {
                "type": ["integer", "null"],
                "description": "每页条数。固定按 20 返回，翻页由前端表格负责，不要传大值",
            },
        },
        (),
        output=TRANSACTION_PAGE,
    ),
    spec(
        "analyze_comprehensive",
        (
            "对指定时间段的账单进行**深度分析**并返回完整报告，含 7 个维度：\n"
            "1. 风险标签识别（网贷、租机、先用后付等）\n"
            "2. 交易时段分析（夜间交易、活跃时段）\n"
            "3. 账户使用统计（活跃度、资金流向）\n"
            "4. 交易类型占比（消费、转账、理财等）\n"
            "5. 月度收支趋势（收支倒挂识别）\n"
            "6. 风控交易清单（命中风控规则的交易）\n"
            "7. 高频交易对手分析（资金集中度）\n\n"
            "**只有用户明确要「分析 / 看分析结果」时才调用本工具。**\n"
            "**如果用户只是要「把（账单）分析报告发到邮箱」，不要调用本工具** —— 句子里"
            "的「分析报告」是报告的名字，不代表现在要分析；直接调用 `bill_send_report`，"
            "服务端会按这段时间生成报告。\n"
            "只有用户明确说「先分析、再发送」（如「分析一下并发送到邮箱」）时，才先调用"
            "本工具显示结果，再调用 `bill_send_report`。\n"
            "注意区分动作：这句话指的是「分析」这件事本身 ——\n"
            "  · 用户要「**把（分析）报告发到我的邮箱**」→ 用 `bill_send_report`，"
            "即使句子里带「分析」二字（「把分析报告发我邮箱」）也照样是发信，不是重新分析。\n"
            "  · 用户只是「**查 / 看**流水」「某笔交易是什么」→ 用 `bill_query_transactions`。\n\n"
            "两种分析范围：\n"
            "- **当前这条用户消息**里带任务号（「分析刚导入的流水（任务 task_…）」）→ "
            "传入 `task_id`（把任务号原样抄下来），**不要再问日期**："
            "分析的就是这份账单解析出来的全部交易。**历史消息里的任务号不算**；\n"
            "- 否则按日期：用户明确提到日期（如'9月15日到今天'）就直接用；"
            "没有提到日期（如只说'分析流水'）必须先询问日期再调用；"
            "**不要使用历史对话中的日期或期间**，除非用户在当前消息中明确提到。"
            "用户换说法（「我要分析流水」）而上一轮已经成功时，不要照搬上一轮的任务号或日期。\n"
            "- 例外：用户说「重新 / 重来 / 重做」**且上一轮分析失败**时，可以沿用上一轮的参数。\n\n"
            "**同一轮里只调用一次。** 拿到结果就是拿到了 —— 不要为了「再确认一下」「再看看」"
            "而重复调用同一个区间，用户会连着看到好几份一样的结果。要汇报的是上一份结果。\n"
            "**但跨轮次不同**：用户在新的一轮里再次明确要求分析时，必须重新调用本工具，"
            "不要拿上一轮的结论复述 —— 期间可能已经导入了新的流水。只有用户只是要求"
            "「看看 / 再给我看看已有的结果」时，才复述。"
            "**调用本工具时不要再调用 `bill_query_transactions`**：它内部已经读取了流水，"
            "同时请求两个工具会被工作流拒绝。"
        ),
        {
            "period_start": date_param("开始日期（与结束日期一起用；给了 task_id 时不需要）"),
            "period_end": date_param("结束日期（与开始日期一起用；给了 task_id 时不需要）"),
            "task_id": {
                "type": ["string", "null"],
                "description": (
                    "账单任务号（形如 task_…），只分析这一份账单解析出来的流水；"
                    "给了它就不需要日期。只在**当前这条**用户消息里本来就带着任务号时原样传下来"
                    "（历史消息里的不算）；**不要向用户索要任务号**——他记不住，消息里没有就问日期"
                ),
            },
            "modules": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "要执行的分析模块（可选，不指定则执行全部；传 null 等同于不指定）",
            },
        },
        (),
        output=ANALYSIS_REPORT,
    ),
    spec(
        "send_report",
        (
            "把账单深度分析报告发到用户邮箱。"
            "**用户说「把（分析）报告发到我的邮箱」时用这个工具** —— 那是发信这个动作，"
            "不是「分析」，即使句子里带「分析」二字也照样用这个。"
            "**用户只要求发送时直接调用本工具，不要先调用 `bill_analyze_comprehensive`** ——"
            "发送前会按时间段生成报告，用户不需要先在页面上看到它。"
            "只有用户明确要求「先分析 / 先看分析结果」时，才先分析、再调用本工具；"
            "两种情况都不要在同一次模型输出里同时请求两个工具。不要询问收件邮箱或「要不要发送」。"
            "收件人留空即可，工具会自动使用用户已绑定的邮箱；只有未绑定时才提示用户去设置里绑定。**"
            "只需给出**范围**，报告由系统按这个范围生成 —— **当前这条消息**里带任务号（形如"
            "「任务 task_…」）时把任务号原样放进 `task_id`，不要问日期，那样发出去的就是这份账单；"
            "没有任务号时才用时间段，**不要向用户索要任务号**。历史消息里的任务号不算。"
            "**不要**试图传入分析结果对象，你拿不到它，也不需要它。\n\n"
            "**这一步只是冻结，不会发信。** 调用后系统会把收件人和时间段摆给用户确认，"
            "用户点确认才会真正发出。所以拿到结果后照实说明「已准备好，请确认」，"
            "**不要**说「已发送」—— 这时候一封信都没发出去。"
            "这个确认卡是代码的安全步骤，不需要你在对话里再问一遍。"
        ),
        {
            "period_start": date_param("开始日期（给了 task_id 时不需要）"),
            "period_end": date_param("结束日期（给了 task_id 时不需要）"),
            "task_id": {
                "type": ["string", "null"],
                "description": (
                    "账单任务号（形如 task_…）。消息里带「任务 …」时原样传下来，"
                    "发送的就是这份账单；与起止日期二选一。"
                    "**不要向用户索要任务号**，消息里没有就按日期发送"
                ),
            },
            "recipient": {
                "type": ["string", "null"],
                "description": "收件人邮箱地址（可选）。不指定则使用用户绑定的邮箱",
            },
        },
        (),
        write=True,
        output=REPORT_PREPARATION,
    ),
    spec(
        "send_report_execute",
        (
            "真正生成 PDF 并发出账单深度分析报告 —— 只由用户在确认卡上点击后调用。"
            "**模型不要调用这个工具**：它不在你的工具菜单里，冻结那一步（"
            "`bill_send_report`）已经把收件人与时间段摆给用户了，这里只是把那份冻结"
            "执行掉。参数与冻结时一致，收件人必须与用户确认的是同一个。"
        ),
        {
            "period_start": date_param("开始日期（给了 task_id 时不需要）"),
            "period_end": date_param("结束日期（给了 task_id 时不需要）"),
            "task_id": {
                "type": ["string", "null"],
                "description": "与冻结时相同的任务号（可选）",
            },
            "recipient": {
                "type": ["string", "null"],
                "description": "收件人邮箱地址（可选）。不指定则使用用户绑定的邮箱",
            },
        },
        (),
        write=True,
        output=REPORT_DELIVERY,
    ),
]

TOOLS = {item.name: item for item in SPECS}


def build_handlers(*, database=None, notification_service=None) -> dict[str, ToolHandler]:
    """按依赖装配 handler：键必须与 `TOOLS` 同名（modules/base.py:112-135 强制相等）。

    搬来的两个工厂返回的键还是旧名（`query_transactions` / `analyze_bill_comprehensive`），
    在这里映射成 `bill_` 前缀 —— 不改名构造 `ModuleSpec` 时就直接 `ValueError`。发信
    那一组在任务 9 拆成两个键：`bill_send_report`（冻结，进模型菜单）与
    `bill_send_report_execute`（执行，隐藏），邮件工厂直接按这两个新名返回；两个都要
    登记，Gateway 的绑定校验要求每个工具都有 handler，隐藏的也不例外。

    依赖非空才登记：`database=None` 时只有邮件工具，`notification_service=None` 时
    只有查询与分析。三个工厂本身也在函数内 import，避免 import 期把服务层拖进来。
    """
    handlers: dict[str, ToolHandler] = {}

    if notification_service is not None:
        from app.modules.email_handlers import create_email_handlers

        email_handlers = create_email_handlers(notification_service)
        handlers["bill_send_report"] = email_handlers["bill_send_report"]
        handlers["bill_send_report_execute"] = email_handlers["bill_send_report_execute"]

    if database is not None:
        from app.modules.bill.analysis_handlers import create_analysis_handlers
        from app.modules.bill.handlers import create_bill_tool_handlers

        handlers["bill_query_transactions"] = create_bill_tool_handlers(database)[
            "query_transactions"
        ]
        handlers["bill_analyze_comprehensive"] = create_analysis_handlers(database)[
            "analyze_bill_comprehensive"
        ]

    return handlers
