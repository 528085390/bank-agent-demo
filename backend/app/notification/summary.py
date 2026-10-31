"""账单摘要数据归一化：把 bill_summary 工具输出规整为邮件模板所需的上下文。"""

from decimal import Decimal, InvalidOperation


def money(value) -> str:
    """把任意金额归一化为两位小数字符串，解析失败回落为 0.00。"""
    try:
        return format(Decimal(str(value)), ".2f")
    except (InvalidOperation, TypeError, ValueError):
        return "0.00"


def normalize_summary(summary: dict) -> dict:
    """规整账单摘要，补齐净额并统一分类字段。

    输入是 bill_summary 工具的返回结构（period / debit_total / credit_total /
    record_count / categories），输出是模板可直接渲染的上下文。
    """
    credit = Decimal(money(summary.get("credit_total")))
    debit = Decimal(money(summary.get("debit_total")))
    net = credit - debit
    categories = [
        {
            "category": item.get("category", "未分类"),
            "amount": money(item.get("amount")),
            "count": item.get("count", 0),
        }
        for item in (summary.get("categories") or [])
    ]
    return {
        "period": summary.get("period") or "—",
        "credit_total": money(credit),
        "debit_total": money(debit),
        "net_total": money(net),
        "record_count": summary.get("record_count", 0),
        "categories": categories,
        "quality_issues": summary.get("quality_issues") or [],
    }
