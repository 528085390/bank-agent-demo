"""PDF statement extraction: page transforms, wrapped cells, date+time, and fallback choice."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from app.analysis.cmb_parser import parse_cmb_statement
from app.billing.handlers.pdf import _clean_table_cell, _page_lines
from app.services.bill_processing import (
    _improve_pdf_parse,
    _is_better_parse,
    _needs_pdf_fallback,
)


class _FakePage:
    """A page whose text is positioned with the page transform, not the text matrix."""

    def extract_text(self, visitor_text=None):
        # Same local tm for both blocks; only cm moves them to different visual rows.
        visitor_text("A", (1, 0, 0, 1, 10, 100), (1, 0, 0, 1, 0, 2.5), None, 10)
        visitor_text("B", (1, 0, 0, 1, 10, 50), (1, 0, 0, 1, 0, 2.5), None, 10)
        return ""


def test_page_lines_applies_the_page_transform_to_the_text_matrix():
    lines = _page_lines(_FakePage())

    assert len(lines) == 2
    # The two blocks have the same local tm; only cm moves them to different rows.
    # Rows are emitted top-to-bottom, so A (higher) comes before B.
    assert [row[0][1] for row in lines] == ["A", "B"]
    assert [row[0][0] for row in lines] == [10.0, 10.0]


def test_clean_table_cell_joins_wrapped_lines():
    assert _clean_table_cell("2026-05-\n01") == "2026-05-01"
    assert _clean_table_cell("08:32:1\n5") == "08:32:15"
    # A complete date followed by a time keeps the separator; otherwise it is one datetime.
    assert _clean_table_cell("2026-10-18\n10:06:00") == "2026-10-18 10:06:00"
    assert _clean_table_cell("深圳市星辰科技有\n限公司") == "深圳市星辰科技有限公司"
    assert _clean_table_cell(None) == ""


def test_separate_date_and_time_columns_form_one_datetime():
    text = "\n".join(
        [
            "# sheet: pdf",
            "交易日期\t交易时间\t业务摘要\t交易金额(元)\t账户余额(元)",
            "2026-05-01\t08:32:15\t工资代发\t+8,500.00\t20,845.67",
        ]
    )

    parsed = parse_cmb_statement(text)

    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].booked_at == datetime(2026, 5, 1, 8, 32, 15)
    assert parsed.transactions[0].amount == Decimal("8500.00")


def test_a_single_transaction_time_column_is_still_a_full_datetime():
    text = "\n".join(
        [
            "# sheet: pdf",
            "交易时间\t交易类型\t金额\t收/支",
            "2026-05-01 08:32:15\t消费\t-32.50\t支出",
        ]
    )

    parsed = parse_cmb_statement(text)

    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].booked_at == datetime(2026, 5, 1, 8, 32, 15)


def _result(transactions: int, problems: int):
    return SimpleNamespace(
        transactions=list(range(transactions)),
        quality_issues=["无法解析"] * problems,
    )


def test_fallback_is_needed_only_when_problems_outnumber_transactions():
    assert _needs_pdf_fallback(_result(0, 1), "pdf") is True
    assert _needs_pdf_fallback(_result(16, 1), "pdf") is False
    assert _needs_pdf_fallback(_result(0, 1), "csv") is False


def test_more_transactions_win_only_without_more_problems():
    # More rows, no more problems: clearly better.
    assert _is_better_parse(_result(3, 0), _result(1, 0)) is True
    assert _is_better_parse(_result(3, 1), _result(1, 2)) is True
    # More rows but more parse problems: keep the primary, do not let a bad extraction win.
    assert _is_better_parse(_result(3, 5), _result(1, 0)) is False
    assert _is_better_parse(_result(3, 1), _result(1, 0)) is False
    # Same row count, fewer problems: better.
    assert _is_better_parse(_result(3, 1), _result(3, 4)) is True
    assert _is_better_parse(_result(3, 4), _result(3, 1)) is False
    # Perfect tie: the primary result stays.
    assert _is_better_parse(_result(3, 1), _result(3, 1)) is False


def test_improve_pdf_parse_keeps_a_better_primary_result(monkeypatch, tmp_path):
    primary = parse_cmb_statement("# sheet: pdf\n交易日期\t交易金额\n")
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(
        "app.billing.handlers.pdf.extract_pdf_tables",
        lambda data, password=None: "# sheet: pdf\n交易日期\t交易金额\n",
    )

    improved = _improve_pdf_parse(
        primary, SimpleNamespace(detected_type="pdf"), str(path), None
    )

    assert improved is primary


def test_improve_pdf_parse_uses_a_better_fallback(monkeypatch, tmp_path):
    primary = parse_cmb_statement("# sheet: pdf\n交易日期\t交易金额\n")
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(
        "app.billing.handlers.pdf.extract_pdf_tables",
        lambda data, password=None: (
            "# sheet: pdf\n交易日期\t交易金额\n2026-05-01\t100.00\n"
        ),
    )

    improved = _improve_pdf_parse(
        primary, SimpleNamespace(detected_type="pdf"), str(path), None
    )

    assert len(improved.transactions) == 1


# --- 压缩包内 PDF：pypdf 拆碎时补一次表格提取 ---------------------------------------


def _nested_pdf(text: str):
    from app.billing.contracts import AttachmentResult

    return AttachmentResult(
        source_name="bill.pdf",
        detected_type="pdf",
        declared_type="pdf",
        kind="document",
        size=1,
        sha256="x",
        text=text,
        metadata={"extractor": "pypdf", "view": "text"},
    )


def test_a_fragmented_nested_pdf_is_replaced_by_the_table_extraction(monkeypatch):
    """pypdf 把每笔交易拆成几行时，用 pdfplumber 的结果替换。"""

    from app.billing.handlers import archive

    primary = "\n".join([
        "# sheet: pdf",
        "2026-05- 08:32:1\t\t深圳市星辰科技有\t",
        "\t工资代发\t贷 +8,500.00\t20,845.67",
        "01\t5\t限公司\t7766",
    ])
    candidate = "\n".join([
        "# sheet: pdf",
        "交易日期\t交易时间\t业务摘要\t金额\t余额",
        "2026-05-01\t08:32:15\t工资代发\t+8,500.00\t20,845.67",
    ])
    monkeypatch.setattr(
        "app.billing.handlers.pdf.extract_pdf_tables",
        lambda data, password=None: candidate,
    )

    nested = _nested_pdf(primary)
    assert archive._needs_table_retry(nested) is True

    improved = archive._re_extract_pdf(nested, b"%PDF", password=None, name="bill.pdf")

    assert improved.text == candidate
    assert improved.metadata["extractor"] == "pdfplumber"


def test_a_healthy_nested_pdf_is_left_alone(monkeypatch):
    """pypdf 已经还原出成行的日期时，不再多跑一次 pdfplumber。"""

    from app.billing.handlers import archive

    healthy = "\n".join([
        "# sheet: pdf",
        "交易日期\t摘要\t金额",
        "2026-05-01\t工资\t8500.00",
        "2026-05-02\t超市\t-32.50",
    ])
    nested = _nested_pdf(healthy)
    assert archive._needs_table_retry(nested) is False

    called = []
    monkeypatch.setattr(
        "app.billing.handlers.pdf.extract_pdf_tables",
        lambda *a, **k: called.append(1) or "2026-05-01\tx",
    )
    assert archive._re_extract_pdf(nested, b"%PDF", password=None, name="bill.pdf") is nested
    assert called == []


def test_a_failed_re_extraction_keeps_the_original_text(monkeypatch):
    """二次提取出错不能影响导入：原文本照常返回。"""

    from app.billing.handlers import archive

    def boom(*_args, **_kwargs):
        raise RuntimeError("pdfplumber 读不了")

    monkeypatch.setattr("app.billing.handlers.pdf.extract_pdf_tables", boom)
    nested = _nested_pdf("没有日期的碎片")

    assert archive._re_extract_pdf(nested, b"%PDF", password=None, name="bill.pdf") is nested


def test_a_pdf_inside_a_zip_benefits_from_the_retry(monkeypatch):
    """端到端：ZIP 里的 PDF 也走这条二次提取，下游能解析出交易。"""

    import io
    import tempfile
    import zipfile
    from pathlib import Path

    from reportlab.pdfgen import canvas

    from app.billing import TaskSandbox, process_attachment

    buf = io.BytesIO()
    page = canvas.Canvas(buf)
    page.drawString(72, 720, "fragmented statement")
    page.save()

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as zipped:
        zipped.writestr("bill.pdf", buf.getvalue())

    candidate = "\n".join([
        "# sheet: pdf",
        "交易日期\t摘要\t金额\t余额",
        "2026-05-01\t工资\t8500.00\t9000.00",
    ])
    monkeypatch.setattr(
        "app.billing.handlers.pdf.extract_pdf_tables",
        lambda data, password=None: candidate,
    )

    with tempfile.TemporaryDirectory() as tmp, TaskSandbox(Path(tmp), "nested") as sandbox:
        result = process_attachment(data=archive_bytes.getvalue(), filename="bill.zip", sandbox=sandbox)

    parsed = parse_cmb_statement(result.text or "")
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].amount == Decimal("8500.00")
