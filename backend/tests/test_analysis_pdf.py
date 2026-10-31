"""The PDF report's charts must survive real, empty and partial hourly data."""

import io
import os

import pytest
from pypdf import PdfReader

from app.notification.analysis_pdf import (
    FONT_PATH,
    _hourly_activity_chart,
    _register_report_fonts,
    build_analysis_report_pdf,
    format_report_time,
)


def test_the_report_font_ships_with_the_code_and_registers():
    """字体必须来自仓库，而不是操作系统 —— 否则 Linux 容器里发邮件必然失败。

    这条用例同时防止「有人把字体文件删了/被 .dockerignore 排除了」：它不依赖
    Windows 的 C:/Windows/Fonts，在任何平台上都成立。
    """

    from reportlab.pdfbase import pdfmetrics

    assert FONT_PATH.is_file(), f"内置字体缺失：{FONT_PATH}"
    _register_report_fonts()
    for name in ("NotoSansSC", "SimHei", "SimSun"):
        assert pdfmetrics.getFont(name).face.name, f"字体 {name} 未注册"


def test_generated_at_is_shown_in_beijing_time_to_the_second():
    """analyzer 写的是带 +00:00 的 UTC；报告上固定显示东八区时间。

    不能按"机器本地时区"换算：部署容器默认是 UTC，那样邮件里的 PDF 会比北京时间
    早 8 小时（本地 Windows 却是对的），同一份报告在两处显示不一致。
    """

    shown = format_report_time("2026-10-20T15:30:45+00:00")

    assert shown == "2026-10-20 23:30:45"


def test_a_timestamp_without_an_offset_keeps_its_wall_clock():
    assert format_report_time("2026-10-20T15:30:45") == "2026-10-20 15:30:45"


def _hourly() -> list[dict]:
    return [
        {
            'hour': hour,
            'count': (hour % 5) * 3,
            'income': (hour % 7) * 10000,
            'expense': (hour % 3) * 8000,
        }
        for hour in range(24)
    ]


def _report(hourly: list[dict]) -> dict:
    return {
        'report_meta': {
            'date_range': {'start': '2026-09-01', 'end': '2026-09-30'},
            'accounts': ['****1234'],
            'total_transactions': 92,
            'generated_at': '2026-10-20T15:30:00',
            'cashflow': {
                'all': {
                    'income_minor': 1108888,
                    'expense_minor': 485875,
                    'net_minor': 623013,
                },
                'external': {
                    'income_minor': 908888,
                    'expense_minor': 57830,
                    'net_minor': 851058,
                },
            },
        },
        'modules': [
            {
                'module_id': 'hourly_activity',
                'title': '交易活跃时段',
                'data': {
                    'hourly_distribution': hourly,
                    'night_stats': {
                        'count': 6,
                        'percentage': 8.5,
                        'amount_minor': 320000,
                        'amount_percentage': 12.3,
                        'consecutive_days': 3,
                        'top_counterparties': [],
                    },
                },
                'insights': [],
            }
        ],
        'text_report': '示例报告',
    }


def test_hourly_chart_handles_zero_and_partial_data():
    empty = _hourly_activity_chart(
        [{'hour': hour, 'count': 0, 'income': 0, 'expense': 0} for hour in range(24)]
    )
    assert (empty.width, empty.height) == (470, 215)

    # A missing hour is normal (the analyzer may omit empty hours); the chart fills them.
    partial = _hourly_activity_chart([{'hour': 8, 'count': 4, 'income': 100, 'expense': 200}])
    assert (partial.width, partial.height) == (470, 215)


@pytest.mark.skipif(os.name != 'nt', reason='the report registers Windows fonts')
def test_pdf_builds_with_the_hourly_chart():
    pdf = build_analysis_report_pdf(_report(_hourly()))

    assert pdf.startswith(b'%PDF')
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) >= 1
    text = '\n'.join((page.extract_text() or '') for page in reader.pages)
    assert '24 小时交易分布' in text
    assert '交易高峰时段' in text
    assert '流水合计' in text
    assert '对外收支' in text
    assert '第 1 页' in text


def _risk_report(evidence: str, detail: str) -> dict:
    report = _report(_hourly())
    report['modules'].insert(0, {
        'module_id': 'customer_risk_profile',
        'title': '1. 客户基础信息与风险标签',
        'data': {
            'risk_tags': [
                {
                    'tag': '民间借贷',
                    'hit_count': 1,
                    'amount_minor': 80000,
                    'confidence': 0.02,
                    'evidence': [evidence],
                    'detail': detail,
                }
            ],
        },
        'insights': [],
    })
    return report


def test_long_cells_wrap_inside_their_column_instead_of_overlapping():
    """证据交易号比列还长：单元格必须是会折行的 Paragraph。

    裸字符串在 reportlab 里不换行，会整串画出去压到右边一列上 —— 邮件里看到的就是
    两列的字叠在一起。这里直接盯住单元格的类型和折行后的高度。
    """

    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph

    from app.notification.analysis_pdf import _wrapped_table

    long_id = '8fa4c5d3-02a7-4a08-9197-b15dcc7e9a01'
    table = _wrapped_table(
        [['风险标签', '证据交易'], ['民间借贷', '、'.join([long_id] * 3)]],
        [2.0 * cm, 4.8 * cm],
    )

    cell = table._cellvalues[1][1]
    assert isinstance(cell, Paragraph)
    width, height = cell.wrap(4.8 * cm, 100 * cm)
    assert width <= 4.8 * cm + 0.5
    assert height > 11 * 1.5, '长内容没有折行'


def test_cells_with_xml_characters_do_not_break_the_report():
    """对手名里出现 & 或 < 时，Paragraph 会当成标签解析 —— 必须先转义。"""

    pdf = build_analysis_report_pdf(
        _risk_report('8fa4c5d3-02a7', '交易对手 A&B <测试>，金额 800.00 元')
    )
    reader = PdfReader(io.BytesIO(pdf))
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)

    assert '民间借贷' in text
    assert 'A&B' in text


def test_an_annual_report_gets_its_own_title_and_summary_section():
    """整年区间生成的报告，PDF 封面写年度报告，正文含年度总览与覆盖范围。"""

    report = _report(_hourly())
    report['report_meta']['date_range'] = {'start': '2025-01-01', 'end': '2025-12-31'}
    report['report_meta']['report_type'] = 'annual'
    report['modules'].insert(0, {
        'module_id': 'annual_summary',
        'title': '2025 年度总览',
        'data': {
            'year': 2025,
            'transaction_count': 128,
            'income_minor': 1200000,
            'expense_minor': 900000,
            'net_minor': 300000,
            'savings_rate_pct': 25.0,
            'active_months': ['2025-01', '2025-02'],
            'missing_months': [f'2025-{month:02d}' for month in range(3, 13)],
            'top_categories': [{'category': '餐饮', 'count': 12, 'amount_minor': 30000}],
            'top_counterparties': [{'counterparty': '某商户', 'count': 8, 'amount_minor': 20000}],
            'largest_transactions': [
                {'time': '2025-01-10T09:30:00', 'counterparty': '某商户',
                 'direction': '支出', 'amount_minor': 50000},
            ],
            'risk_hit_count': 3,
        },
        'insights': [],
    })

    reader = PdfReader(io.BytesIO(build_analysis_report_pdf(report)))
    text = "\n".join(page.extract_text() for page in reader.pages)

    assert '2025 年度账单报告' in text
    assert '年度总览' in text
    assert '数据覆盖' in text
