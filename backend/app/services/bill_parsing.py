"""C 的解析器 / 分析器接入缝。

C 交付 ``parse_cmb_statement`` 与 ``compute_summary`` 后，调用 ``register_parser`` /
``register_analyzer`` 一次即可接入；A 的编排器（bill_processing）通过
``parse_statement`` / ``compute_summary`` 调用。未注册时抛 ``PARSE_FAILED``，让流程
明确停在解析阶段，而不是静默跳过。

契约（给 C）：
- parser:   ``parse_cmb_statement(text: str) -> ParseResult``
- analyzer: ``compute_summary(task_id: str, database) -> dict``
  （摘要字段见 ``bill_import.BillImportService.save_summary`` 的参数说明）
"""

from __future__ import annotations

from typing import Any, Callable

from app.core.bill_errors import PARSE_FAILED, raise_bill_error

_parser: Callable[[str], Any] | None = None
_analyzer: Callable[..., dict] | None = None


def register_parser(fn: Callable[[str], Any]) -> None:
    global _parser
    _parser = fn


def register_analyzer(fn: Callable[..., dict]) -> None:
    global _analyzer
    _analyzer = fn


def reset_parsers() -> None:
    """测试用：清空注册，避免跨测试串扰。"""

    global _parser, _analyzer
    _parser = None
    _analyzer = None


def parse_statement(text: str):
    if _parser is None:
        raise_bill_error(PARSE_FAILED, "解析器尚未接入（C 未注册 parse_cmb_statement）")
    return _parser(text)


def compute_summary(task_id: str, database) -> dict:
    if _analyzer is None:
        raise_bill_error(PARSE_FAILED, "分析器尚未接入（C 未注册 compute_summary）")
    return _analyzer(task_id, database)


def is_registered() -> bool:
    return _parser is not None and _analyzer is not None
