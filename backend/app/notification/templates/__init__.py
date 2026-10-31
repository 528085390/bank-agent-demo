"""邮件模板渲染。"""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent

env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)


def render_bill_summary(context: dict[str, Any]) -> str:
    """渲染账单摘要模板。"""
    return env.get_template("bill_summary.html").render(**context)


def render_bill_report(context: dict[str, Any]) -> str:
    """渲染账单详细报告模板。"""
    return env.get_template("bill_report.html").render(**context)


def render_anomaly_alert(context: dict[str, Any]) -> str:
    """渲染异常交易提醒模板。"""
    return env.get_template("anomaly_alert.html").render(**context)
