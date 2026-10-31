"""账单报告附件生成（PDF / Excel）。

依赖按需导入：未安装 reportlab / openpyxl 时，只有调用对应生成函数才会报错，
不影响邮件正文发送。
"""

from dataclasses import dataclass
from pathlib import Path

from app.notification.summary import normalize_summary


@dataclass(frozen=True, slots=True)
class Attachment:
    """内存附件：只有字节内容才活得过来回的队列序列化。

    落盘路径附件在异步队列里会失效（worker 与请求可能不同机、临时文件可能已清理），
    所以报告附件的权威表示是「文件名 + 字节」，序列化时编码成 base64 文本。
    """

    filename: str
    content: bytes


_REPORT_FILENAME_PREFIX = "账单报告"


def build_pdf_report(summary: dict) -> bytes:
    """把账单摘要渲染为 PDF 字节流。"""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的可读报错。
        raise RuntimeError("生成 PDF 需要安装 reportlab（uv add reportlab）") from exc

    data = normalize_summary(summary)
    out = _bytes_buffer()
    page = canvas.Canvas(out, pagesize=A4)
    width, height = A4
    y = height - 60
    page.setFont("Helvetica-Bold", 16)
    page.drawString(60, y, f"账单报告（{data['period']}）")
    y -= 30
    page.setFont("Helvetica", 11)
    for label, value in (
        ("总收入", f"¥{data['credit_total']}"),
        ("总支出", f"¥{data['debit_total']}"),
        ("净额", f"¥{data['net_total']}"),
        ("交易笔数", f"{data['record_count']} 笔"),
    ):
        page.drawString(60, y, f"{label}: {value}")
        y -= 20
    y -= 10
    page.setFont("Helvetica-Bold", 12)
    page.drawString(60, y, "支出分类")
    y -= 20
    page.setFont("Helvetica", 11)
    for cat in data["categories"]:
        page.drawString(60, y, f"{cat['category']}: ¥{cat['amount']}（{cat['count']}笔）")
        y -= 18
    page.save()
    return out.getvalue()


def build_excel_report(summary: dict) -> bytes:
    """把账单摘要渲染为 Excel 字节流。"""
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的可读报错。
        raise RuntimeError("生成 Excel 需要安装 openpyxl（uv add openpyxl）") from exc

    data = normalize_summary(summary)
    wb = Workbook()
    ws = wb.active
    ws.title = "账单摘要"
    rows = [
        ("统计周期", data["period"]),
        ("总收入", data["credit_total"]),
        ("总支出", data["debit_total"]),
        ("净额", data["net_total"]),
        ("交易笔数", data["record_count"]),
        ("", ""),
        ("分类", "金额", "笔数"),
    ]
    rows.extend((cat["category"], cat["amount"], cat["count"]) for cat in data["categories"])
    for row in rows:
        ws.append(list(row))
    out = _bytes_buffer()
    wb.save(out)
    return out.getvalue()


def write_report(path: str | Path, summary: dict, fmt: str = "pdf") -> str:
    """把账单报告写入磁盘并返回路径（供作为邮件附件发送）。"""
    fmt = fmt.lower()
    if fmt not in {"pdf", "xlsx"}:
        raise ValueError("附件格式仅支持 pdf / xlsx")
    content = build_pdf_report(summary) if fmt == "pdf" else build_excel_report(summary)
    target = Path(path) if isinstance(path, Path) else Path(path)
    target.write_bytes(content)
    return str(target)


def _bytes_buffer():
    from io import BytesIO

    return BytesIO()
