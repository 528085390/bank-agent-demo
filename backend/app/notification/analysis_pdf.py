"""深度分析报告PDF生成器。

将深度分析结果转换为格式化的PDF文档，包含图表、表格和风控批注。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.clock import CST

if TYPE_CHECKING:
    # reportlab 是可选依赖，只在真正生成 PDF 时才 import（见各函数内部）。
    # 但函数签名里用到了这两个名字，静态检查需要能解析到它们。
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import TableStyle


#: 每个模块的导语，取自网页报告里卡片标题下的那行小字 —— 纸上没有章节注释会显得干巴。
MODULE_CAPTIONS = {
    'customer_risk_profile': (
        '命中风控关键词的交易归类，按出现次数排序；证据列为该标签下最早的几笔交易单号'
    ),
    'hourly_activity': (
        '作息规律的人交易集中在白天三段；长期夜间（22:00–06:00）交易频繁是需要留意的信号'
    ),
    'account_usage': '资金集中在哪个账户、各账户的收支与净额',
    'transaction_type_analysis': '长期集中的某一类交易（如红包、理财）本身就是特征',
    'monthly_cashflow': (
        '仅统计对外收支（剔除理财申赎、信用卡还款等内部资金）；'
        '净额为负说明当月入不敷出'
    ),
    'risk_transactions': '命中平台风控规则的交易',
    'frequent_counterparties': '资金集中度：与少数对手反复往来，是判断资金用途的重要线索',
}


#: 报告唯一使用的字体：随代码入库的 Noto Sans SC（见同目录 ``fonts/README.md``）。
#: 以前读的是 ``C:/Windows/Fonts/...``——那条路径在 Linux 容器里不存在，注册失败又被
#: ``except: pass`` 吞掉，于是每个样式都在 ``doc.build`` 时抛「Can't map determine
#: family/bold/italic」，在真机上表现为「发邮件失败」。字体进仓库后，Windows、Linux、
#: macOS、容器与 CI 用的是同一份字形，排版也一致。
FONT_DIR = Path(__file__).parent / "fonts"
FONT_PATH = FONT_DIR / "NotoSansSC-Regular.ttf"

#: 报告里的样式、表格与图表都写着 ``fontName='SimHei'`` / ``'SimSun'``（历史命名）。
#: 两者现在都指向仓库里的同一份字体 —— 不必改几十处样式，也不再依赖系统字体。
FONT_NAMES = ("NotoSansSC", "SimHei", "SimSun")

_fonts_registered = False


def _register_report_fonts() -> None:
    """注册内置字体（进程内只做一次）。

    Raises:
        RuntimeError: 字体文件缺失。**必须响亮地失败**：以前这里静默吞掉异常，错误被
            推迟到 ``doc.build`` 里，用户只看到一句含糊的「生成报告失败」。
    """

    global _fonts_registered
    if _fonts_registered:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if not FONT_PATH.is_file():
        raise RuntimeError(
            f"缺少内置字体文件 {FONT_PATH}，PDF 无法生成；"
            "重新生成方式见 app/notification/fonts/README.md"
        )
    for name in FONT_NAMES:
        pdfmetrics.registerFont(TTFont(name, str(FONT_PATH)))
    _fonts_registered = True


def build_analysis_report_pdf(analysis_result: dict) -> bytes:
    """将深度分析结果渲染为PDF字节流。

    Args:
        analysis_result: analyze_bill_comprehensive 工具返回的完整结果

    Returns:
        PDF文件的字节流
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            CondPageBreak,
            HRFlowable,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError("生成 PDF 需要安装 reportlab（uv add reportlab）") from exc

    _register_report_fonts()

    # 创建PDF文档。页脚要页码，所以左右/下边距在这里统一定好。
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=1.8 * cm,
        bottomMargin=2.2 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
    )

    # 定义样式
    styles = getSampleStyleSheet()

    # 标题样式：放在深墨绿标题带里，白色字。
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Title'],
        fontName='SimHei',
        fontSize=19,
        textColor=colors.white,
        leading=24,
        spaceAfter=0,
        alignment=0,
    )

    # 章节标题样式
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading1'],
        fontName='SimHei',
        fontSize=14,
        textColor=colors.HexColor('#24312b'),
        spaceBefore=12,
        spaceAfter=2,
        leading=18,
        leftIndent=0,
    )

    # 正文样式
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['BodyText'],
        fontName='SimSun',
        fontSize=10,
        textColor=colors.HexColor('#333333'),
        leading=16,
        spaceAfter=6
    )

    # 批注样式。三种类型各用一种颜色：事实是绿、推断是琥珀、建议是蓝绿。
    # 全部涂成红色会让「推断」看起来像错误，网页上也不是这个语义。
    insight_style = ParagraphStyle(
        'InsightText',
        parent=styles['BodyText'],
        fontName='SimSun',
        fontSize=9,
        textColor=colors.HexColor('#2e8763'),
        leading=14,
        leftIndent=12,
        spaceAfter=4
    )
    insight_styles = {
        '事实': insight_style,
        '推断': ParagraphStyle(
            'InsightInfer', parent=insight_style, textColor=colors.HexColor('#b07a1f')
        ),
        '建议': ParagraphStyle(
            'InsightAdvice', parent=insight_style, textColor=colors.HexColor('#2f5f7a')
        ),
    }

    stat_label = ParagraphStyle(
        'StatLabel',
        parent=styles['BodyText'],
        fontName='SimSun',
        fontSize=9,
        textColor=colors.HexColor('#7a867e'),
        leading=12,
    )
    stat_value = ParagraphStyle(
        'StatValue',
        parent=styles['BodyText'],
        fontName='SimHei',
        fontSize=15,
        textColor=colors.HexColor('#24312b'),
        leading=20,
        alignment=2,
    )
    stat_header = ParagraphStyle(
        'StatHeader',
        parent=stat_label,
        fontName='SimHei',
        fontSize=9,
        textColor=colors.white,
    )

    # 副标题：报告头只写期间，标题本身留给报告名。
    subtitle_style = ParagraphStyle(
        'Subtitle',
        parent=styles['BodyText'],
        fontName='SimSun',
        fontSize=9.5,
        textColor=colors.HexColor('#d8c37a'),
        alignment=2,
        leading=14,
        spaceAfter=0,
    )

    # 风险标签摘要条：报告头一眼看到命中了什么。
    tag_strip_style = ParagraphStyle(
        'TagStrip',
        parent=styles['BodyText'],
        fontName='SimSun',
        fontSize=9.5,
        textColor=colors.HexColor('#8a5a1f'),
        leading=15,
        backColor=colors.HexColor('#faf4e6'),
        borderPadding=6,
        spaceBefore=2,
    )

    # 章节导语：与网页每个卡片下的小字说明对应。
    caption_style = ParagraphStyle(
        'Caption',
        parent=styles['BodyText'],
        fontName='SimSun',
        fontSize=8.5,
        textColor=colors.HexColor('#8c9990'),
        leading=13,
        spaceAfter=5,
    )

    # 表下脚注。
    footnote_style = ParagraphStyle(
        'Footnote',
        parent=styles['BodyText'],
        fontName='SimSun',
        fontSize=8.5,
        textColor=colors.HexColor('#8c9990'),
        leading=13,
        spaceBefore=3,
    )

    # 构建PDF内容
    story = []

    modules = analysis_result.get('modules', [])
    module_of = {module.get('module_id'): module for module in modules}
    meta = analysis_result.get('report_meta', {})
    date_range = meta.get('date_range', {})
    accounts = meta.get('accounts') or []

    risk_data = module_of.get('customer_risk_profile', {}).get('data') or {}
    risk_tags = risk_data.get('risk_tags') or []
    completeness_score = risk_data.get('completeness_score')
    monthly_data = (module_of.get('monthly_cashflow', {}).get('data') or {}).get(
        'monthly_data'
    ) or []

    # 核心指标：报告同时展示两套口径，避免和银行账单的借贷方合计对不上。
    # 新报告直接读 report_meta.cashflow；旧报告从模块回退汇总。
    cashflow_meta = meta.get('cashflow') or {}
    all_flow = cashflow_meta.get('all') or {}
    external_flow = cashflow_meta.get('external') or {}
    account_data = (module_of.get('account_usage', {}).get('data') or {}).get(
        'accounts'
    ) or []

    def _flow_value(flow: dict, key: str, fallback: int) -> int:
        value = flow.get(key)
        return int(value) if value is not None else fallback

    all_income_fallback = sum(int(acc.get('income_minor', 0) or 0) for acc in account_data)
    all_expense_fallback = sum(int(acc.get('expense_minor', 0) or 0) for acc in account_data)
    external_income_fallback = sum(month.get('income_minor', 0) for month in monthly_data)
    external_expense_fallback = sum(month.get('expense_minor', 0) for month in monthly_data)
    all_income = _flow_value(all_flow, 'income_minor', all_income_fallback)
    all_expense = _flow_value(all_flow, 'expense_minor', all_expense_fallback)
    external_income = _flow_value(external_flow, 'income_minor', external_income_fallback)
    external_expense = _flow_value(external_flow, 'expense_minor', external_expense_fallback)

    # ── 报告头：报告名 / 区间 / 元信息 / 风险标签摘要 ─────────────────
    period = f"{date_range.get('start', '-')} ~ {date_range.get('end', '-')}"

    # 整年区间就是年报：标题换成年度报告，内容多一段年度总览（见 modules/annual_summary）。
    annual = meta.get('report_type') == 'annual'
    year = str(date_range.get('start', ''))[:4]
    report_title = f'{year} 年度账单报告' if annual and year else '账户流水深度分析报告'

    title_band = Table(
        [[Paragraph(report_title, title_style), Paragraph(period, subtitle_style)]],
        colWidths=[12.2 * cm, 4.8 * cm],
    )
    title_band.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#2e3c34')),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 14),
                ('RIGHTPADDING', (0, 0), (-1, -1), 14),
                ('TOPPADDING', (0, 0), (-1, -1), 14),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
                ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ]
        )
    )
    story.append(title_band)
    story.append(Spacer(1, 0.5 * cm))

    account_text = f"{len(accounts)} 个"
    if accounts:
        account_text += f"（{'、'.join(str(account) for account in accounts)}）"

    meta_data = [
        ['分析区间:', period],
        ['交易总数:', f"{meta.get('total_transactions', 0)} 笔"],
        ['涉及账户:', account_text],
        ['生成时间:', format_report_time(meta.get('generated_at'))],
    ]

    meta_table = Table(meta_data, colWidths=[3*cm, 12*cm])
    meta_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'SimSun'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#666666')),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#1a1a1a')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))

    story.append(meta_table)

    # 风险标签摘要：网页报告头是标签条，纸上换成一行文字。
    story.append(Spacer(1, 0.4 * cm))
    if risk_tags:
        chips = '　'.join(
            f"{tag.get('tag', '-')} {tag.get('hit_count', 0)} 笔" for tag in risk_tags
        )
        story.append(Paragraph(f'<b>风险标签</b>　{chips}', tag_strip_style))
    else:
        story.append(Paragraph('风险标签：未识别到明显风险标签', caption_style))

    # 核心指标：同时展示银行流水合计与对外收支两套口径。
    if any([all_income, all_expense, external_income, external_expense]):
        all_net = all_income - all_expense
        external_net = external_income - external_expense
        score_text = f"{completeness_score} 分" if completeness_score is not None else '未评估'

        def net_text(value: int) -> str:
            return f"{'+' if value >= 0 else '-'}{_money(abs(value))}"

        stats = Table(
            [
                [
                    Paragraph('口径', stat_header),
                    Paragraph('收入', stat_header),
                    Paragraph('支出', stat_header),
                    Paragraph('净额', stat_header),
                ],
                [
                    Paragraph(
                        '流水合计<br/><font size="7" color="#6b756e">含内部资金</font>',
                        stat_label,
                    ),
                    Paragraph(_money(all_income), stat_value),
                    Paragraph(_money(all_expense), stat_value),
                    Paragraph(net_text(all_net), stat_value),
                ],
                [
                    Paragraph(
                        '对外收支<br/><font size="7" color="#6b756e">剔除内部资金</font>',
                        stat_label,
                    ),
                    Paragraph(_money(external_income), stat_value),
                    Paragraph(_money(external_expense), stat_value),
                    Paragraph(net_text(external_net), stat_value),
                ],
            ],
            colWidths=[4.4 * cm, 4.2 * cm, 4.2 * cm, 4.2 * cm],
        )
        stats.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2e3c34')),
                    ('BACKGROUND', (0, 1), (-1, 1), colors.white),
                    ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#f7f9f8')),
                    ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#dfe5e2')),
                    ('INNERGRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#e1e7e3')),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                    ('TOPPADDING', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
                    ('LEFTPADDING', (0, 0), (-1, -1), 12),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 12),
                ]
            )
        )
        story.append(Spacer(1, 0.6 * cm))
        story.append(stats)
        story.append(Spacer(1, 0.15 * cm))
        story.append(Paragraph(f'账单完整性：{score_text}', caption_style))
        story.append(
            Paragraph(
                '流水合计与银行账单的本期借贷方合计一致；对外收支剔除理财申赎、'
                '信用卡还款、内部转账等内部资金往来。',
                footnote_style,
            )
        )

    text_styles = {
        'body': body_style,
        'caption': caption_style,
        'footnote': footnote_style,
    }

    # ── 逐模块展开 ───────────────────────────────────────────────
    for index, module in enumerate(modules):
        # 只保证「章节标题别落在页尾」，不再每个模块后硬分页 —— 硬分页会让
        # 「只有一张两行表」或「只剩一条解读」的模块独占一整页。
        story.append(Spacer(1, 0.8 * cm))
        story.append(CondPageBreak(5 * cm))

        story.append(Paragraph(f"{index + 1}. {module.get('title', '未命名模块')}", heading_style))
        story.append(
            HRFlowable(
                width='100%',
                thickness=0.7,
                color=colors.HexColor('#c9a94f'),
                spaceBefore=1,
                spaceAfter=7,
            )
        )

        caption = MODULE_CAPTIONS.get(module.get('module_id'))
        if caption:
            story.append(Paragraph(caption, caption_style))

        for item in _format_module_data(module, text_styles):
            story.append(item)

        insights = module.get('insights', [])
        if insights:
            story.append(Spacer(1, 0.25 * cm))
            story.append(Paragraph('<b>风控解读：</b>', body_style))

            for insight in insights:
                insight_type = insight.get('type', '事实')
                prefix = {'事实': '■', '推断': '▲', '建议': '●'}.get(insight_type, '•')
                story.append(
                    Paragraph(
                        f"{prefix} {insight.get('text', '')}",
                        insight_styles.get(insight_type, insight_style),
                    )
                )

    # 页脚：左边报告名，右边页码。页脚不占正文流，业务内容不变。
    def _draw_page(canvas, document):
        page_width, _page_height = A4
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#dfe5e2'))
        canvas.setLineWidth(0.5)
        canvas.line(2 * cm, 1.45 * cm, page_width - 2 * cm, 1.45 * cm)
        canvas.setFont('SimSun', 8)
        canvas.setFillColor(colors.HexColor('#8c9990'))
        canvas.drawString(2 * cm, 1.0 * cm, report_title)
        canvas.drawRightString(page_width - 2 * cm, 1.0 * cm, f'第 {document.page} 页')
        canvas.restoreState()

    # 生成PDF
    doc.build(story, onFirstPage=_draw_page, onLaterPages=_draw_page)

    return buffer.getvalue()


def _format_module_data(module: dict, text_styles: dict[str, ParagraphStyle]) -> list:
    """把单个模块渲染成 PDF 元素列表。

    每个分支对应网页报告里的一张卡片：**网页上出现的列，这里都要有** —— 风险标签的
    证据交易、时段模块的 24 小时分布、风控清单的命中原因，之前都漏掉了。

    Args:
        module: 一个分析模块（`module_id` / `title` / `data`）
        text_styles: `{'body': …, 'caption': …, 'footnote': …}` 三个段落样式
    """

    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer, Table

    body_style = text_styles['body']
    footnote_style = text_styles['footnote']

    module_id = module.get('module_id', '')
    data = module.get('data', {})

    elements = []

    # 根据不同模块类型进行特殊格式化
    if module_id == 'annual_summary':
        # 年度总览：全年收支 + 覆盖范围 + 三张年度榜单。放在所有维度之前。
        savings = data.get('savings_rate_pct')
        summary_rows = [
            ['全年收入', '全年支出', '年度结余', '储蓄率'],
            [
                _money(data.get('income_minor')),
                _money(data.get('expense_minor')),
                _money(data.get('net_minor')),
                f'{savings}%' if savings is not None else '—',
            ],
        ]
        elements.append(_wrapped_table(summary_rows, [4.25 * cm] * 4))

        active = data.get('active_months') or []
        missing = data.get('missing_months') or []
        elements.append(Spacer(1, 0.2 * cm))
        coverage = f'数据覆盖：{len(active)} / 12 个月'
        if missing:
            coverage += '（缺 ' + '、'.join(str(month)[5:] + ' 月' for month in missing) + '）'
            coverage += '；以上年度数字只代表有数据的月份'
        elements.append(Paragraph(coverage, body_style))

        categories = data.get('top_categories') or []
        if categories:
            elements.append(Spacer(1, 0.3 * cm))
            elements.append(Paragraph('<b>年度支出类别 Top 5</b>', body_style))
            rows = [['类别', '笔数', '金额']]
            rows += [
                [str(item.get('category', '-')), str(item.get('count', 0)),
                 _money(item.get('amount_minor'))]
                for item in categories
            ]
            elements.append(_wrapped_table(rows, [7.5 * cm, 3 * cm, 6.5 * cm]))

        counterparts = data.get('top_counterparties') or []
        if counterparts:
            elements.append(Spacer(1, 0.3 * cm))
            elements.append(Paragraph('<b>年度交易对手 Top 5</b>', body_style))
            rows = [['对手', '笔数', '金额']]
            rows += [
                [str(item.get('counterparty', '-')), str(item.get('count', 0)),
                 _money(item.get('amount_minor'))]
                for item in counterparts
            ]
            elements.append(_wrapped_table(rows, [7.5 * cm, 3 * cm, 6.5 * cm]))

        largest = data.get('largest_transactions') or []
        if largest:
            elements.append(Spacer(1, 0.3 * cm))
            elements.append(Paragraph('<b>年度最大单笔</b>', body_style))
            rows = [['时间', '对手', '方向', '金额']]
            rows += [
                [str(item.get('time', '-'))[:16].replace('T', ' '),
                 str(item.get('counterparty', '-')),
                 str(item.get('direction', '-')),
                 _money(item.get('amount_minor'))]
                for item in largest
            ]
            elements.append(_wrapped_table(rows, [4.2 * cm, 6.3 * cm, 2 * cm, 4.5 * cm]))

        risk_count = int(data.get('risk_hit_count') or 0)
        if risk_count:
            elements.append(Spacer(1, 0.2 * cm))
            elements.append(Paragraph(f'全年命中风控规则的交易：{risk_count} 笔', body_style))

    elif module_id == 'customer_risk_profile':
        # 风险标签：data["risk_tags"]，每项 {tag, hit_count, amount_minor, confidence,
        # evidence, detail}
        tags = data.get('risk_tags') or []
        if tags:
            table_data = [
                ['风险标签', '出现次数', '涉及金额', '占总交易', '证据交易（前 3 笔）', '说明']
            ]
            for tag in tags:
                evidence = tag.get('evidence') or []
                table_data.append([
                    str(tag.get('tag', '-')),
                    str(tag.get('hit_count', 0)),
                    _money(tag.get('amount_minor')),
                    str(tag.get('confidence', '-')),
                    '、'.join(str(item) for item in evidence) if evidence else '—',
                    str(tag.get('detail', '-')),
                ])

            elements.append(
                _wrapped_table(
                    table_data,
                    # 证据列放长交易号、说明列放整句，两列都给足宽度，其余列收紧。
                    [2.0*cm, 1.4*cm, 2.2*cm, 1.6*cm, 4.8*cm, 5.0*cm],
                )
            )
        else:
            elements.append(Paragraph('未识别到明显风险标签。', body_style))

        # 完整性评分已经提到报告头的核心指标里，这里只列问题清单，不再重复「- 分」。
        issues = data.get('completeness_issues') or []
        if issues:
            elements.append(Paragraph('<b>账单完整性问题：</b>', body_style))
            for issue in issues:
                elements.append(Paragraph(f'· {issue}', body_style))

    elif module_id == 'hourly_activity':
        # 时段分析：data["hourly_distribution"]（24 项 {hour, count, income, expense}）
        # 与 data["night_stats"] = {count, percentage, amount_minor, amount_percentage,
        # consecutive_days, top_counterparties}
        hourly = data.get('hourly_distribution') or []
        if hourly:
            elements.append(Paragraph('<b>24 小时交易分布</b>', body_style))
            elements.append(_hourly_activity_chart(hourly))
            elements.append(Spacer(1, 0.15 * cm))
            elements.append(
                Paragraph(
                    '柱为交易笔数（左轴），线为交易金额（右轴）；浅色底为夜间 22:00–06:00。',
                    footnote_style,
                )
            )

            peaks = _peak_hours(hourly)
            if peaks:
                elements.append(Paragraph(f"交易高峰时段：{peaks}", body_style))

        stats = data.get('night_stats') or {}
        if stats:
            elements.append(
                Paragraph(
                    f"夜间交易（22:00–06:00）：{stats.get('count', 0)} 笔"
                    f"（占笔数 {stats.get('percentage', 0)}%）；"
                    f"夜间金额：{_money(stats.get('amount_minor'))}"
                    f"（占金额 {stats.get('amount_percentage', 0)}%）；"
                    f"连续夜间交易天数：{stats.get('consecutive_days', 0)} 天",
                    body_style,
                )
            )

            top_night = stats.get('top_counterparties') or []
            if top_night:
                elements.append(
                    Paragraph(
                        '夜间高频对手：'
                        + '、'.join(
                            f"{c.get('name', '-')}({c.get('count', 0)} 笔)" for c in top_night
                        ),
                        body_style,
                    )
                )

    elif module_id == 'account_usage':
        # 账户使用：data["accounts"]，每项 {account_no, count, income_minor,
        # expense_minor, net_minor, net_flow, active_days}
        accounts = data.get('accounts') or []
        if accounts:
            table_data = [['账户', '笔数', '收入', '支出', '净额', '流向', '活跃天数']]
            for acc in accounts:
                table_data.append([
                    str(acc.get('account_no') or '未提供'),
                    str(acc.get('count', 0)),
                    _money(acc.get('income_minor')),
                    _money(acc.get('expense_minor')),
                    _money(acc.get('net_minor')),
                    str(acc.get('net_flow') or '-'),
                    str(acc.get('active_days', 0)),
                ])

            elements.append(
                _wrapped_table(
                    table_data,
                    [3.2*cm, 1.6*cm, 2.8*cm, 2.8*cm, 2.8*cm, 1.6*cm, 2.2*cm],
                )
            )

    elif module_id == 'monthly_cashflow':
        # 月度收支：data["monthly_data"]，每项 {month, income_minor, expense_minor,
        # net_minor, is_deficit}
        monthly_data = data.get('monthly_data') or []
        if monthly_data:
            elements.append(
                _bar_chart(
                    [str(m.get('month', '-')) for m in monthly_data],
                    [
                        ('收入', [m.get('income_minor', 0) / 100 for m in monthly_data]),
                        ('支出', [m.get('expense_minor', 0) / 100 for m in monthly_data]),
                        ('净额', [m.get('net_minor', 0) / 100 for m in monthly_data]),
                    ],
                )
            )

            table_data = [['月份', '收入', '支出', '净额', '状态']]
            for month in monthly_data:
                table_data.append([
                    str(month.get('month', '-')),
                    _money(month.get('income_minor')),
                    _money(month.get('expense_minor')),
                    _money(month.get('net_minor')),
                    '收支倒挂' if month.get('is_deficit') else '正常',
                ])

            elements.append(
                _wrapped_table(table_data, [3*cm, 3.5*cm, 3.5*cm, 3.5*cm, 2.5*cm])
            )

    elif module_id == 'frequent_counterparties':
        # 高频对手：data["time_slices"]["all"]["by_frequency"]，
        # 每项 {counterparty, count, total_amount, income, expense, tag}
        # 另有 data["tag_distribution"]
        by_frequency = ((data.get('time_slices') or {}).get('all') or {}).get(
            'by_frequency', []
        )
        if by_frequency:
            table_data = [['#', '交易对手', '次数', '总收入', '总支出', '总金额', '标签']]
            for rank, cp in enumerate(by_frequency[:10], start=1):
                table_data.append([
                    str(rank),
                    str(cp.get('counterparty', '-')),
                    str(cp.get('count', 0)),
                    _money(cp.get('income')),
                    _money(cp.get('expense')),
                    _money(cp.get('total_amount')),
                    str(cp.get('tag') or '-'),
                ])

            elements.append(
                _wrapped_table(
                    table_data,
                    [0.9*cm, 3.6*cm, 1.5*cm, 2.7*cm, 2.7*cm, 2.7*cm, 2.2*cm],
                )
            )

        tag_distribution = data.get('tag_distribution') or []
        if tag_distribution:
            elements.append(
                Paragraph(
                    '<b>对手标签分布：</b>'
                    + '、'.join(
                        f"{t.get('tag', '-')} {t.get('count_pct', 0)}%"
                        for t in tag_distribution
                    ),
                    body_style,
                )
            )

    elif module_id == 'transaction_type_analysis':
        # 交易类型占比：data["categories"]，每项 {category, count, count_pct,
        # amount_minor, amount_pct}；另有 data["p2p_transfers"]
        categories = data.get('categories') or []
        if categories:
            top = categories[:7]
            elements.append(
                _pie_chart(
                    [str(c.get('category', '-')) for c in top],
                    [c.get('count', 0) for c in top],
                )
            )

            table_data = [['交易类型', '笔数', '笔数占比', '金额', '金额占比']]
            for cat in categories[:10]:
                table_data.append([
                    str(cat.get('category', '-')),
                    str(cat.get('count', 0)),
                    f"{cat.get('count_pct', 0)}%",
                    _money(cat.get('amount_minor')),
                    f"{cat.get('amount_pct', 0)}%",
                ])

            elements.append(
                _wrapped_table(table_data, [3.5*cm, 2*cm, 2.5*cm, 3.5*cm, 2.5*cm])
            )

            if len(categories) > 10:
                elements.append(
                    Paragraph(f"（仅展示前 10 类，共 {len(categories)} 类）", footnote_style)
                )

        p2p = data.get('p2p_transfers') or {}
        if p2p.get('count'):
            elements.append(
                Paragraph(
                    f"其中个人对个人转账 {p2p.get('count')} 笔，"
                    f"合计 {_money(p2p.get('amount_minor'))}",
                    footnote_style,
                )
            )

    elif module_id == 'risk_transactions':
        # 风控交易清单：data["transactions"]，每项 {time, account, summary,
        # counterparty, direction, amount_minor, balance_minor, rules, reasons,
        # risk_level}；另有 data["summary"]{total_count, by_rule}
        transactions = data.get('transactions') or []
        summary = data.get('summary') or {}

        if summary.get('total_count'):
            elements.append(
                Paragraph(f"<b>命中风控的交易共 {summary.get('total_count')} 笔</b>", body_style)
            )
        if summary.get('by_rule'):
            elements.append(
                Paragraph(
                    '按规则：'
                    + '、'.join(f'{rule} {count} 笔' for rule, count in summary['by_rule'].items()),
                    body_style,
                )
            )

        if transactions:
            shown = transactions[:30]
            table_data = [
                ['#', '时间', '对手', '摘要', '金额', '余额', '方向', '等级', '命中规则']
            ]
            for rank, tx in enumerate(shown, start=1):
                table_data.append([
                    str(rank),
                    str(tx.get('time', '-'))[:16].replace('T', ' '),
                    str(tx.get('counterparty', '-'))[:12],
                    str(tx.get('summary', '-'))[:14],
                    _money(tx.get('amount_minor')),
                    _money(tx.get('balance_minor'))
                    if tx.get('balance_minor') is not None
                    else '—',
                    str(tx.get('direction', '-')),
                    str(tx.get('risk_level', '-')),
                    str(tx.get('reasons', '-'))[:20],
                ])

            elements.append(
                _wrapped_table(
                    table_data,
                    [
                        0.8*cm, 2.4*cm, 2.3*cm, 2.4*cm, 2.1*cm,
                        1.9*cm, 1.1*cm, 1.1*cm, 2.9*cm,
                    ],
                )
            )

            if len(transactions) > len(shown):
                elements.append(
                    Paragraph(
                        f"仅展示前 {len(shown)} 笔，共 {len(transactions)} 笔。", footnote_style
                    )
                )

    else:
        # 通用数据展示（键值对形式）
        if isinstance(data, dict):
            items = []
            for key, value in data.items():
                if isinstance(value, (str, int, float, bool)):
                    items.append(f"<b>{key}:</b> {value}")

            if items:
                elements.append(Paragraph('<br/>'.join(items), body_style))

    return elements


def _bar_chart(
    categories: list[str],
    series: list[tuple[str, list[float]]],
    *,
    width: float = 460,
    height: float = 190,
):
    """分组柱状图：给「月度收支」这类逐项对比用。

    纯数字列在报告里要一行行读，柱状图一眼能看出高低和趋势。
    """

    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors

    drawing = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 36
    chart.y = 42
    chart.width = width - 110
    chart.height = height - 62
    chart.data = [values for _, values in series]
    chart.categoryAxis.categoryNames = categories
    chart.categoryAxis.labels.fontName = 'SimSun'
    chart.categoryAxis.labels.fontSize = 8
    chart.valueAxis.labels.fontName = 'SimSun'
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeColor = colors.HexColor('#e3e8e5')
    chart.valueAxis.gridStrokeWidth = 0.4
    chart.valueAxis.labels.dx = -8
    chart.categoryAxis.labels.dy = -2
    # 净额可能是负的（收支倒挂），下界跟着最小值走，否则倒挂的柱子会被裁掉。
    chart.valueAxis.valueMin = min([0.0] + [value for _, values in series for value in values])
    chart.barSpacing = 2
    chart.groupSpacing = 8

    palette = [colors.HexColor('#2e8763'), colors.HexColor('#a2514b'), colors.HexColor('#c9a94f')]
    for index in range(len(series)):
        chart.bars[index].fillColor = palette[index % len(palette)]
        chart.bars[index].strokeColor = None

    drawing.add(chart)

    legend = Legend()
    legend.x = width - 70
    legend.y = height - 16
    legend.fontName = 'SimSun'
    legend.fontSize = 8
    legend.dxTextSpace = 4
    legend.dy = 6
    legend.alignment = 'right'
    legend.colorNamePairs = [
        (palette[index % len(palette)], name) for index, (name, _) in enumerate(series)
    ]
    drawing.add(legend)

    return drawing


def _pie_chart(labels: list[str], values: list[float], *, size: float = 190):
    """饼图：给「交易类型占比」这类构成分析用。"""

    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors

    drawing = Drawing(size + 200, size)
    pie = Pie()
    pie.x = 6
    pie.y = 8
    pie.width = size - 16
    pie.height = size - 16
    pie.data = values
    pie.labels = None
    pie.slices.strokeWidth = 0.5
    pie.slices.strokeColor = colors.white

    palette = [
        colors.HexColor('#2e8763'),
        colors.HexColor('#4a7c6a'),
        colors.HexColor('#c9a94f'),
        colors.HexColor('#a2514b'),
        colors.HexColor('#8fa89b'),
        colors.HexColor('#6b8f7f'),
        colors.HexColor('#d8c37a'),
    ]
    for index in range(len(values)):
        pie.slices[index].fillColor = palette[index % len(palette)]

    drawing.add(pie)

    legend = Legend()
    legend.x = size + 6
    legend.y = size - 14
    legend.fontName = 'SimSun'
    legend.fontSize = 8
    legend.dxTextSpace = 4
    legend.dy = 7
    legend.alignment = 'right'
    total = sum(values) or 1
    legend.colorNamePairs = [
        (
            palette[index % len(palette)],
            f'{label} {value / total * 100:.1f}%',
        )
        for index, (label, value) in enumerate(zip(labels, values))
    ]
    drawing.add(legend)

    return drawing


def _wrapped_table(rows: list[list[object]], col_widths: list[float]):
    """构造一张**会换行**的表格。

    单元格若直接放字符串，reportlab 不会给它换行：长文本（比如风险标签的证据交易号）
    会原样画出去，压到右边那一列上，两列的字叠在一起。每一格包成 `Paragraph` 之后，
    内容在格子宽度内折行、行高跟着长，相邻列不再互相遮盖。

    `repeatRows=1` 让跨页的表重复表头；内容里的 `&` / `<` 会先转义，否则会被当成标签。
    """

    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table

    head_style = ParagraphStyle(
        'TableHead',
        fontName='SimHei',
        fontSize=8.5,
        leading=11,
        textColor=colors.white,
        wordWrap='CJK',
    )
    cell_style = ParagraphStyle(
        'TableCell',
        fontName='SimSun',
        fontSize=8.5,
        leading=11,
        wordWrap='CJK',
    )

    wrapped = []
    for index, row in enumerate(rows):
        style = head_style if index == 0 else cell_style
        wrapped.append([
            cell if not isinstance(cell, str) else Paragraph(escape(cell), style)
            for cell in row
        ])

    table = Table(wrapped, colWidths=col_widths, repeatRows=1)
    table.setStyle(_get_table_style())
    return table


def _get_table_style() -> TableStyle:
    """获取表格的统一样式。"""
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    return TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'SimHei'),  # 表头字体
        ('FONTNAME', (0, 1), (-1, -1), 'SimSun'),  # 数据字体
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        # 深墨绿表头 + 白色字，和报告头的标题带同一套颜色。
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2e3c34')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, colors.HexColor('#c9a94f')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#e1e7e3')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9f8')]),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ])


#: 金额符号用全角 ￥(U+FFE5)，不要用半角 ¥(U+00A5)。
#:
#: 当初这么定，是因为系统黑体 SimHei 没有 U+00A5 的字形（simhei.ttf 的 cmap 里没有
#: 这个码位），半角 ¥ 会印成空心方框（□4,697.73）。现在报告改用内置的 Noto Sans SC，
#: 两种符号都有；仍保留全角写法：既有报告的排版不必跟着变，而且它与中文数字同宽。
CURRENCY_SIGN = '￥'


def _money(amount_minor: float | int | None) -> str:
    """金额：负数把符号放在 ￥ 前面（`-￥160.67`，而不是 `￥-160.67`）。"""

    value = (amount_minor or 0) / 100
    if value < 0:
        return f'-{CURRENCY_SIGN}{abs(value):,.2f}'
    return f'{CURRENCY_SIGN}{value:,.2f}'


def format_report_time(value: str | None) -> str:
    """把 ISO 时间串压成**东八区**的 `2026-10-13 18:16:42`；拿不到就退回当前时间。

    ``report_meta.generated_at`` 是带 ``+00:00`` 的 UTC（analyzer 用 ``timezone.utc``
    取的），直接截字符串会让报告上的时间比北京时间早 8 小时。

    这里固定转成项目统一的 ``CST``（app/core/clock.py），**不依赖服务器时区**：本地
    Windows 是 +8、部署容器默认是 UTC，用 ``astimezone()`` 会让同一份报告在两边显示成
    不同的时间——邮件里的 PDF 在线上就是这样错了 8 小时。不带时区的旧值按原样显示。
    """
    text = str(value or '').strip()
    if not text:
        return datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        # 不是 ISO 的旧值：保留原样（压掉过长的部分），至少不编一个时间出来。
        return text[:19].replace('T', ' ')
    if at.tzinfo is not None:
        at = at.astimezone(CST)
    return at.strftime('%Y-%m-%d %H:%M:%S')


def _peak_hours(hourly: list[dict], *, top: int = 3) -> str:
    """按笔数取前几个时段，拼成「18 点、12 点、17 点」。"""
    ranked = sorted(hourly, key=lambda item: item.get('count', 0), reverse=True)
    return '、'.join(
        f"{item.get('hour', 0)} 点" for item in ranked[:top] if item.get('count', 0) > 0
    )


def _nice_max(value: float) -> float:
    """坐标轴上界：取 1 / 2 / 2.5 / 5 / 10 × 10^n，避免 137 这种刻度。"""

    import math

    if value <= 0:
        return 1
    exponent = math.floor(math.log10(value))
    base = 10 ** exponent
    for factor in (1, 2, 2.5, 5, 10):
        if value <= factor * base:
            return factor * base
    return 10 * base


def _count_axis_max(value: int) -> int:
    """笔数轴用整数刻度：上界取 4 的倍数，横线才不会落在 1.25 笔上。"""

    import math

    if value <= 0:
        return 4
    return max(1, math.ceil(value / 4)) * 4


def _axis_amount(value: float) -> str:
    """右轴金额标签：一万以上用「万」，一千以上用「k」。"""

    if value >= 10000:
        return f'{value / 10000:.1f}万'
    if value >= 1000:
        return f'{value / 1000:.1f}k'
    return f'{value:.0f}'


def _hourly_activity_chart(
    hourly: list[dict],
    *,
    width: float = 470,
    height: float = 215,
):
    """24 小时交易分布：笔数柱 + 金额折线，双轴，和网页报告的口径一致。

    网页端用的是一张双 y 轴折线图。纸上以前把笔数和金额拆成两张很小的柱状图，
    既没有网格也没有图例，24 个小时挤在一起几乎看不出规律。这里自己画一张：
    左轴笔数柱、右轴金额线、夜间时段底色、峰值标注，尺寸和正文同宽。
    """

    from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Rect, String
    from reportlab.lib import colors

    gold = colors.HexColor('#c9a94f')
    gold_dark = colors.HexColor('#a5842f')
    green = colors.HexColor('#4a7c6a')
    ink = colors.HexColor('#24312b')
    muted = colors.HexColor('#8c9990')
    grid = colors.HexColor('#e3e8e5')
    night = colors.HexColor('#f2f6f7')

    by_hour = {}
    for item in hourly:
        try:
            hour = int(item.get('hour', 0))
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            by_hour[hour] = item

    counts: list[int] = []
    amounts: list[float] = []
    for hour in range(24):
        item = by_hour.get(hour) or {}
        try:
            count = max(0, int(item.get('count', 0) or 0))
        except (TypeError, ValueError):
            count = 0
        try:
            income = float(item.get('income', 0) or 0)
            expense = float(item.get('expense', 0) or 0)
            amount = max(0.0, (income + expense) / 100)
        except (TypeError, ValueError):
            amount = 0.0
        counts.append(count)
        amounts.append(amount)

    left, right, top, bottom = 36, 44, 30, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    step = plot_w / 24
    y0 = bottom
    y1 = bottom + plot_h

    drawing = Drawing(width, height)

    # 夜间底色 22:00–06:00：网页上也是把夜间单独标出来的。
    for hour in list(range(22, 24)) + list(range(0, 6)):
        drawing.add(Rect(left + hour * step, y0, step, plot_h, fillColor=night, strokeColor=None))

    count_max = _count_axis_max(max(counts) if counts else 0)
    amount_max = _nice_max(max(amounts) if amounts else 0)

    # 网格与左右轴刻度。
    for index in range(5):
        fraction = index / 4
        y = y0 + plot_h * fraction
        drawing.add(
            Line(
                left,
                y,
                left + plot_w,
                y,
                strokeColor=grid,
                strokeWidth=0.4,
                strokeDashArray=[2, 2] if 0 < index < 4 else None,
            )
        )
        drawing.add(
            String(
                left - 6,
                y - 2,
                str(int(round(count_max * fraction))),
                fontName='SimSun',
                fontSize=6.5,
                fillColor=muted,
                textAnchor='end',
            )
        )
        drawing.add(
            String(
                left + plot_w + 6,
                y - 2,
                _axis_amount(amount_max * fraction),
                fontName='SimSun',
                fontSize=6.5,
                fillColor=muted,
                textAnchor='start',
            )
        )

    # 笔数柱。峰值那一根用深金色，并在柱顶标出「几点、几笔」。
    peak_hour = max(range(24), key=lambda hour: counts[hour]) if counts else 0
    peak_count = counts[peak_hour] if counts else 0
    bar_w = step * 0.56
    for hour, count in enumerate(counts):
        if count <= 0:
            continue
        bar_h = plot_h * count / count_max
        drawing.add(
            Rect(
                left + hour * step + (step - bar_w) / 2,
                y0,
                bar_w,
                bar_h,
                fillColor=gold_dark if hour == peak_hour else gold,
                strokeColor=None,
            )
        )

    # 金额折线，点只画在非零小时上。
    points: list[float] = []
    for hour, amount in enumerate(amounts):
        x = left + hour * step + step / 2
        y = y0 + plot_h * (amount / amount_max if amount_max else 0)
        points.extend([x, y])
    if len(points) >= 4:
        drawing.add(PolyLine(points, strokeColor=green, strokeWidth=1.6))
        for hour, amount in enumerate(amounts):
            if amount <= 0:
                continue
            drawing.add(
                Circle(
                    left + hour * step + step / 2,
                    y0 + plot_h * amount / amount_max,
                    1.3,
                    fillColor=green,
                    strokeColor=colors.white,
                    strokeWidth=0.4,
                )
            )

    # 小时刻度：24 个都在，字号压到 5.5，仍能逐小时读。
    for hour in range(24):
        drawing.add(
            String(
                left + hour * step + step / 2,
                y0 - 12,
                f'{hour:02d}',
                fontName='SimSun',
                fontSize=5.5,
                fillColor=muted,
                textAnchor='middle',
            )
        )
    drawing.add(
        String(
            left + plot_w / 2,
            3,
            '小时',
            fontName='SimSun',
            fontSize=6.5,
            fillColor=muted,
            textAnchor='middle',
        )
    )

    if peak_count > 0:
        drawing.add(
            String(
                left + peak_hour * step + step / 2,
                min(y0 + plot_h * peak_count / count_max + 4, y1 - 8),
                f'{peak_hour}时 {peak_count}笔',
                fontName='SimHei',
                fontSize=6.5,
                fillColor=ink,
                textAnchor='middle',
            )
        )

    # 图例放在绘图区上方，不挤占 24 个小时的横向空间。
    legend_y = height - 14
    drawing.add(Rect(left, legend_y, 7, 7, fillColor=gold, strokeColor=None))
    drawing.add(
        String(left + 11, legend_y + 0.5, '交易笔数', fontName='SimSun', fontSize=7, fillColor=ink)
    )
    drawing.add(
        Line(left + 66, legend_y + 3.5, left + 82, legend_y + 3.5, strokeColor=green, strokeWidth=1.6)
    )
    drawing.add(
        String(left + 86, legend_y + 0.5, '交易金额', fontName='SimSun', fontSize=7, fillColor=ink)
    )
    drawing.add(
        Rect(
            left + plot_w - 105,
            legend_y,
            7,
            7,
            fillColor=night,
            strokeColor=grid,
            strokeWidth=0.4,
        )
    )
    drawing.add(
        String(
            left + plot_w - 94,
            legend_y + 0.5,
            '夜间 22:00–06:00',
            fontName='SimSun',
            fontSize=6.5,
            fillColor=muted,
        )
    )

    if not any(counts) and not any(amounts):
        drawing.add(
            String(
                left + plot_w / 2,
                y0 + plot_h / 2,
                '该时段没有交易',
                fontName='SimSun',
                fontSize=9,
                fillColor=muted,
                textAnchor='middle',
            )
        )

    return drawing


def safe_analysis_filename(date_range: dict) -> str:
    """生成安全的文件名。"""
    start = date_range.get('start', '').replace('-', '')
    end = date_range.get('end', '').replace('-', '')

    if start and end:
        return f"流水深度分析结果_{start}_{end}.pdf"

    return f"流水深度分析结果_{datetime.now().strftime('%Y%m%d')}.pdf"
