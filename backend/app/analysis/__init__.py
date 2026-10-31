"""账单解析与分析（C 交付物）。

独立包，只依赖 A 的契约：``app.services.bill_import``（ParseResult 等）与
``app.services.bill_parsing``（接入缝）。不放 ``app/billing/``（那是 B 的附件处理包）。

``register()`` 把解析器与分析器注册到 A 的接入缝，供 ``bill_processing`` 调用。
"""

from app.analysis.cmb_parser import parse_cmb_statement
from app.analysis.summarizer import compute_summary
from app.services.bill_parsing import register_analyzer, register_parser


def register() -> None:
    """把 C 的解析器与分析器注册到 A 的接入缝（幂等，可重复调用）。"""

    register_parser(parse_cmb_statement)
    register_analyzer(compute_summary)


__all__ = ["parse_cmb_statement", "compute_summary", "register"]
