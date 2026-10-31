"""PDF：用 pypdf 提取文本层。

依赖按需导入（沿用 ``app/notification/attachments.py`` 的惯例）：没装 pypdf 时
只有 PDF 这一条路报错并给出安装命令，其他格式不受影响。

扫描件没有文本层——本版不承诺 OCR（方案 §10 限制 3），提取为空时记一条 warning。
"""

from __future__ import annotations

import io
import re

from app.billing.contracts import AttachmentResult
from app.billing.errors import (
    missing_dependency,
    password_invalid,
    password_required,
    unsupported,
)
from app.billing.handlers.registry import HandleContext, result_for


class PdfHandler:
    kind = "document"

    def handle(self, data: bytes, ctx: HandleContext) -> AttachmentResult:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise missing_dependency("pypdf", "解析 PDF") from exc

        try:
            reader = PdfReader(io.BytesIO(data))
        except Exception as exc:
            raise unsupported("PDF 已损坏或无法解析") from exc

        if reader.is_encrypted:
            # 大量正规 PDF 在规范里有加密字典、但用户密码为空（银行、政府导出常见），
            # pypdf 对这种文件同样报 ``is_encrypted=True``。先用空密码试一次：能打开就
            # 说明它不需要用户提供密码，不该拦人。
            if not _opens_with_empty_password(reader):
                _decrypt(reader, ctx.password)

        try:
            table = _render_tables(reader)
            if table is None:
                text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
            else:
                text = table
        except Exception as exc:
            raise unsupported("PDF 已损坏或无法解析") from exc

        warnings = []
        if not text:
            warnings.append("PDF 没有可提取的文本层（可能是扫描件），本版不做 OCR")
        return result_for(
            data,
            ctx,
            text=text or None,
            metadata={
                "pages": len(reader.pages),
                "extractor": "pypdf",
                "view": "table" if table else "text",
            },
            warnings=warnings,
        )


#: 同一视觉行的 y 容差（PDF 坐标单位 pt）。块与块之间落在这个距离内即视为同一行。
_ROW_TOLERANCE = 2.5


def _render_tables(reader) -> str | None:
    """把 PDF 的表格重建成 ``# sheet:`` 风格的 TSV；不是表格就返回 None（由调用方退回纯文本）。

    PDF 里没有"表格"，只有"字画在哪个坐标"。所以按每个文本块的 y 聚成视觉行、再用行内
    的 x 相对位置分列。下游的解析器认的是 TSV 视图——纯文本它一列都切不开，整份账单会
    解析成 0 条。

    列基准取**块数最多的那一行**：最"规整"的一行最像表格的行（表头或数据行），这样不用
    知道表头是什么、也不掺任何银行特定的词。
    """

    lines: list[list[tuple[float, str]]] = []
    for page in reader.pages:
        lines.extend(_page_lines(page))
    if not lines:
        return None

    anchor = max(lines, key=len)
    if len(anchor) < 2:
        # 每行只有一个文本块——这是普通段落，不是表格，别硬凑。
        return None
    centers = [x for x, _ in anchor]

    rows = []
    for line in lines:
        cells = [""] * len(centers)
        for x, text in line:
            index = min(range(len(centers)), key=lambda k: abs(centers[k] - x))
            cells[index] = f"{cells[index]} {text}".strip() if cells[index] else text
        rows.append(cells)
    return "# sheet: pdf\n" + "\n".join("\t".join(row) for row in rows)


def _page_lines(page) -> list[list[tuple[float, str]]]:
    """一页里的视觉行：按 y 把文本块聚成行，行内按 x 从左到右排。"""

    blocks: list[tuple[float, float, str]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text.strip():
            # `tm` 是文字矩阵，`cm` 是当前的页面变换矩阵。很多生成器（包括这份微众
            # 流水）先用 `cm` 把原点挪到单元格，再用 `Tm` 在局部坐标里排版；只看
            # `tm` 会把整页文字都当成同一个 y，表格被压成几行。这里把文字原点真正
            # 映射到页面坐标（按 row-vector 约定 tm × cm）。
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            blocks.append((round(y, 1), round(x, 1), text.strip()))

    page.extract_text(visitor_text=visitor)

    rows: dict[float, list[tuple[float, str]]] = {}
    for y, x, text in blocks:
        key = next((k for k in rows if abs(k - y) <= _ROW_TOLERANCE), y)
        rows.setdefault(key, []).append((x, text))
    return [sorted(rows[key]) for key in sorted(rows, reverse=True)]


def _opens_with_empty_password(reader) -> bool:
    """空密码能否解开。需要密码的 PDF 返回 False，且 reader 仍可继续尝试其它密码。"""

    try:
        return bool(reader.decrypt(""))
    except Exception:
        return False


def _decrypt(reader, password: str | None) -> None:
    if password is None:
        raise password_required()
    try:
        opened = reader.decrypt(password)
    except Exception as exc:
        raise unsupported("PDF 已加密，但加密方式不受支持") from exc
    if not opened:
        raise password_invalid()


def extract_pdf_tables(data: bytes, password: str | None = None) -> str | None:
    """A second, table-aware extraction used only when the primary parse is poor.

    pypdf gives text blocks; pdfplumber understands table geometry and, importantly, keeps a
    cell that wraps across visual lines together (as a newline inside the cell). The caller
    decides whether to use this result, so this function must not change the primary path.

    Returns ``# sheet:``-prefixed TSV in the same shape the statement parser consumes, or
    ``None`` when pdfplumber is unavailable, the file cannot be opened, or no table is found.
    """

    try:
        import pdfplumber
    except ImportError:
        return None

    try:
        kwargs = {"password": password} if password else {}
        with pdfplumber.open(io.BytesIO(data), **kwargs) as pdf:
            blocks: list[str] = []
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    rows = []
                    for row in table:
                        cells = [_clean_table_cell(cell) for cell in row]
                        if any(cells):
                            rows.append(cells)
                    # A table without a header and at least one row cannot be a statement.
                    if len(rows) < 2:
                        continue
                    blocks.append(
                        "# sheet: pdf\n" + "\n".join("\t".join(row) for row in rows)
                    )
            return "\n\n".join(blocks) if blocks else None
    except Exception:
        # Wrong password, unsupported encryption, damaged file: the primary result stands.
        return None


_CELL_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
_CELL_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def _clean_table_cell(value: object) -> str:
    """Join a wrapped cell without losing the boundary between a date and a time.

    ``2026-05-\\n01`` is one date fragment and must join to ``2026-05-01``; but
    ``2026-10-18\\n10:06:00`` is a date followed by a time and must join with a space, or the
    statement parser sees an impossible datetime. Anything else (CJK wraps, account fragments)
    joins without a space.
    """

    if value is None:
        return ""
    parts = [part.strip() for part in str(value).replace("\r", "").split("\n") if part.strip()]
    if not parts:
        return ""
    joined = parts[0]
    for part in parts[1:]:
        joined = f"{joined} {part}" if _CELL_DATE.match(joined) and _CELL_TIME.match(part) else joined + part
    return joined
