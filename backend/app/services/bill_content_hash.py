"""账单文件的内容指纹：把「视图元数据」从摘要里拿掉。

同一份账单经 WPS「另存」或反复下载后，字节会变而数据不变——变的全是视图状态：
保存时间、窗口尺寸、当前选中的单元格。按原始字节算 sha256 会把它们判成两份不同的
账单，重复导入便绕过了幂等。

对 OOXML 工作簿（xlsx 等），这里只把**数据相关**的条目喂进摘要，跳过上面那些易变项。
另有两点是刻意的：

- **``xl/styles.xml`` 必须保留。** 解析走 openpyxl，它靠 styles 判断数值单元格是不是
  日期；剔掉它会让指纹与解析结果脱钩。所以 styles 真不同时指纹**照样不同**——本函数
  是纵深防御，不是万能药，那类重复由入库层按 ``raw_record_hash`` 的用户级去重兜底。
- **不解析 XML，只做整块正则删除。** 指纹在解析**之前**就要算，输入是不可信文件；
  ``xml.etree`` 对实体扩展（billion laughs）没有防护，而整块删除天然不受属性顺序影响。

指纹只由「条目名 + 归一化内容」决定，因此 zip 里的时间戳、压缩方式、条目顺序、以及
WPS 会写而 openpyxl 不写的目录项，都不影响结果。

非 zip（含加密的 CFB 版 xlsx）或非 OOXML 的 zip，退回原始字节 sha256 —— 与引入本模块
之前的行为一致。
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile

_WORKBOOK = "xl/workbook.xml"
_DOCPROPS_PREFIX = "docProps/"
_WORKSHEET_RE = re.compile(r"xl/worksheets/[^/]+\.xml\Z")

#: 整块删除 <bookViews>…</bookViews>（含自闭合变体）。命名空间前缀可有可无。
_BOOK_VIEWS = re.compile(
    rb"<(?:\w+:)?bookViews\b[^>]*>.*?</(?:\w+:)?bookViews\s*>"
    rb"|<(?:\w+:)?bookViews\b[^>]*/\s*>",
    re.DOTALL,
)

#: 整块删除 <sheetViews>…</sheetViews>（当前选中单元格、滚动位置都在这里）。
_SHEET_VIEWS = re.compile(
    rb"<(?:\w+:)?sheetViews\b[^>]*>.*?</(?:\w+:)?sheetViews\s*>"
    rb"|<(?:\w+:)?sheetViews\b[^>]*/\s*>",
    re.DOTALL,
)


def content_file_hash(data: bytes) -> str:
    """账单文件的内容指纹，恒为 64 位十六进制。"""

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if _WORKBOOK not in names:
                return _fallback(data)
            digest = hashlib.sha256()
            for name in sorted(names):
                # 目录项是 WPS 会写、openpyxl 不写的差异来源，也一并跳过。
                if name.endswith("/") or name.startswith(_DOCPROPS_PREFIX):
                    continue
                digest.update(name.encode("utf-8"))
                digest.update(b"\x00")
                digest.update(_without_views(name, archive.read(name)))
                digest.update(b"\x00")
            return digest.hexdigest()
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError, NotImplementedError):
        # 不是 zip、条目损坏、或加密读不出来 —— 退回字节摘要，不让指纹环节把上传打挂。
        return _fallback(data)


def _fallback(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _without_views(name: str, raw: bytes) -> bytes:
    if name == _WORKBOOK:
        return _BOOK_VIEWS.sub(b"", raw)
    if _WORKSHEET_RE.match(name):
        return _SHEET_VIEWS.sub(b"", raw)
    return raw
