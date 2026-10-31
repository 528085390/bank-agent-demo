"""附件真实类型识别。

**魔数优先，扩展名只作提示**：银行附件常被改名（`.zip` 实为 `.rar`、`账单.pdf`
实为 `.xls`），类型判错会让后续所有处理失效。所以判定只看字节，扩展名只用来
（a）在纯文本家族内部区分 CSV 与普通文本，（b）产出「扩展名与实际不符」的告警。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.billing.contracts import ZIP_NAME_ENCODING, Kind

# 类型标识。只覆盖方案 §3.1 批次 1 与「登记但不解析」的格式。
ZIP = "zip"
TAR = "tar"
GZIP = "gzip"
BZIP2 = "bzip2"
SEVENZIP = "7z"
RAR = "rar"
OLEDOC = "oledoc"
PDF = "pdf"
DOCX = "docx"
XLSX = "xlsx"
CSV = "csv"
TEXT = "text"
JPEG = "jpeg"
PNG = "png"
GIF = "gif"
BMP = "bmp"
UNKNOWN = "unknown"

ARCHIVE_TYPES: frozenset[str] = frozenset({ZIP, TAR, GZIP, BZIP2, SEVENZIP, RAR})

_KIND_BY_TYPE: dict[str, Kind] = {
    ZIP: "archive",
    TAR: "archive",
    GZIP: "archive",
    BZIP2: "archive",
    SEVENZIP: "archive",
    RAR: "archive",
    OLEDOC: "document",
    PDF: "document",
    DOCX: "document",
    XLSX: "spreadsheet",
    CSV: "spreadsheet",
    TEXT: "text",
    JPEG: "image",
    PNG: "image",
    GIF: "image",
    BMP: "image",
    UNKNOWN: "unknown",
}

#: 扩展名 → 提示类型。仅用于比对与纯文本家族的细分，从不覆盖魔数结论。
_DECLARED_BY_SUFFIX: dict[str, str] = {
    ".zip": ZIP,
    ".tar": TAR,
    ".gz": GZIP,
    ".tgz": GZIP,
    ".bz2": BZIP2,
    ".tbz2": BZIP2,
    ".7z": SEVENZIP,
    ".rar": RAR,
    ".pdf": PDF,
    ".docx": DOCX,
    ".doc": OLEDOC,
    ".xlsx": XLSX,
    ".xls": OLEDOC,
    ".csv": CSV,
    ".tsv": CSV,
    ".txt": TEXT,
    ".text": TEXT,
    ".log": TEXT,
    ".md": TEXT,
    ".json": TEXT,
    ".xml": TEXT,
    ".yaml": TEXT,
    ".yml": TEXT,
    ".jpg": JPEG,
    ".jpeg": JPEG,
    ".png": PNG,
    ".gif": GIF,
    ".bmp": BMP,
}


@dataclass(frozen=True, slots=True)
class Detection:
    """一次类型判定的结果。"""

    detected_type: str
    declared_type: str
    kind: Kind

    @property
    def mismatched(self) -> bool:
        """扩展名与魔数是否互相矛盾。

        两边都识别出来才叫「不符」；`unknown`（无扩展名或未知扩展名）不构成本条告警。
        """

        return (
            self.declared_type != UNKNOWN
            and self.detected_type != UNKNOWN
            and self.declared_type != self.detected_type
        )

    @property
    def warning(self) -> str | None:
        if not self.mismatched:
            return None
        return f"扩展名与实际类型不符：声明为 {self.declared_type}，实际为 {self.detected_type}"


def detect(data: bytes, filename: str = "") -> Detection:
    """识别 ``data`` 的真实类型，并用 ``filename`` 补出提示类型。"""

    declared = declared_from_filename(filename)
    detected = detect_type(data, declared=declared)
    return Detection(detected_type=detected, declared_type=declared, kind=kind_of(detected))


def detect_type(data: bytes, *, declared: str = "") -> str:
    """只看字节判定类型。``declared`` 只在纯文本家族内部用于区分 CSV 与文本。"""

    if not data:
        return TEXT if declared in {TEXT, CSV} else UNKNOWN

    if data[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}:
        return _detect_zip_family(data)
    if data.startswith(b"%PDF-"):
        return PDF
    if data[:2] == b"\x1f\x8b":
        return GZIP
    if data[:3] == b"BZh" and data[3:4].isdigit():
        return BZIP2
    if len(data) >= 263 and data[257:262] == b"ustar":
        return TAR
    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        return SEVENZIP
    if data[:5] == b"Rar!\x1a" and len(data) > 6:
        return RAR
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return OLEDOC
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return PNG
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return GIF
    if data[:2] == b"BM" and len(data) >= 14 and data[6:8] == b"\x00\x00":
        return BMP

    decoded = decode_text(data, declared=declared)
    if decoded is None:
        return UNKNOWN
    text, _encoding = decoded
    if declared == CSV:
        return CSV
    if declared == TEXT:
        return TEXT
    return CSV if looks_like_csv(text) else TEXT


def declared_from_filename(filename: str) -> str:
    """按扩展名给出的提示类型；识别不出来时返回 ``unknown``。"""

    suffix = Path(filename).suffix.lower()
    return _DECLARED_BY_SUFFIX.get(suffix, UNKNOWN)


def kind_of(detected_type: str) -> Kind:
    return _KIND_BY_TYPE.get(detected_type, "unknown")


def decode_text(data: bytes, *, declared: str = "") -> tuple[str, str] | None:
    """按 UTF-8 → GB18030 的顺序解码；不是文本时返回 ``None``。

    第二段是有条件的：GB18030 相当宽松，随机二进制也能被它「解」出成片的汉字，
    所以只有在（a）扩展名已经说是文本，或（b）字节里有足够多的 ASCII 时才尝试。
    真随机字节的 ASCII 占比稳定在 0.5 附近，够不到这个门槛。
    """

    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass

    if declared in {TEXT, CSV} or _ascii_ratio(data) >= 0.6:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError:
            return None
        if _printable_ratio(text) >= 0.9:
            return text, "gb18030"
    return None


def looks_like_csv(text: str) -> bool:
    """没有扩展名提示时，用「多行分隔符数一致」判断是不是表格文本。"""

    lines = [line for line in text.splitlines() if line.strip()][:5]
    if len(lines) < 2:
        return False
    for delimiter in (",", ";", "\t"):
        counts = [line.count(delimiter) for line in lines]
        if counts[0] >= 1 and len(set(counts)) == 1:
            return True
    return False


def _detect_zip_family(data: bytes) -> str:
    """OOXML（docx / xlsx）本身就是 ZIP，必须看中央目录才能区分。"""

    try:
        # 与 archive.py 同值：判定只看 ASCII 的 OOXML 目录名，但仍统一编码约定。
        with zipfile.ZipFile(io.BytesIO(data), metadata_encoding=ZIP_NAME_ENCODING) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        # 魔数像 ZIP 但读不出目录：仍归为 zip，由 archive 处理器报 ATTACHMENT_UNSUPPORTED。
        return ZIP
    if "word/document.xml" in names:
        return DOCX
    if "xl/workbook.xml" in names:
        return XLSX
    return ZIP


def _ascii_ratio(data: bytes) -> float:
    return sum(1 for byte in data if byte < 0x80) / len(data)


def _printable_ratio(text: str) -> float:
    if not text:
        return 1.0
    printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    return printable / len(text)
