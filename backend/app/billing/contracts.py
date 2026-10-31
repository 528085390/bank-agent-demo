"""附件处理器的输入 / 输出数据结构。

只依赖标准库。所有结构都是 ``frozen=True, slots=True``：处理链只产出、不就地修改。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

#: 产物大类，决定下游按什么方式消费。取值来自方案 §4.2。
Kind = Literal["archive", "document", "spreadsheet", "text", "image", "unknown"]

KINDS: frozenset[str] = frozenset(
    {"archive", "document", "spreadsheet", "text", "image", "unknown"}
)

#: 压缩包内出现即拒绝的可执行 / 脚本扩展名。判定与拒绝共用这一份。
FORBIDDEN_SUFFIXES: frozenset[str] = frozenset(
    {".exe", ".bat", ".cmd", ".sh", ".js", ".vbs", ".scr", ".dll", ".msi", ".ps1", ".jar"}
)

#: 压缩包嵌套的最大层数。默认允许 2 层，超过抛 NESTING_TOO_DEEP。
DEFAULT_MAX_DEPTH = 2

#: 读取 ZIP 时，用于解码**未置 UTF-8 flag** 的成员名。
#:
#: 国产压缩软件（WinRAR / 好压 / 360 压缩等）默认用 GBK 写中文文件名且不置
#: flag bit 11，而 `zipfile` 的默认回落是 cp437——会把「招商银行流水.xlsx」
#: 解成 `µï¢σòåΘô╢Φíîµ╡üµ░┤.xlsx` 之类的乱码。银行附件的成员名几乎必然是中文，
#: 下游按名字选取成员会直接失效。
#:
#: gb18030 是 GBK 的超集且对 ASCII 兼容；置了 UTF-8 flag 的成员不受影响
#: （`zipfile` 优先按 flag 走 UTF-8）。读取与解压**必须用同一个值**，
#: 否则 `archive.open(member.name)` 会因名字对不上而找不到成员。
ZIP_NAME_ENCODING = "gb18030"


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """压缩包安全检查的上限。默认值取自方案 §7 步骤 2。"""

    max_files: int = 200
    max_single_size: int = 50 * 1024 * 1024
    max_total_size: int = 200 * 1024 * 1024
    max_ratio: float = 100.0
    forbidden_suffixes: frozenset[str] = FORBIDDEN_SUFFIXES


def sha256_of(data: bytes) -> str:
    """附件内容摘要；范围只在本模块内使用，不作为安全边界。"""

    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class AttachmentRequest:
    """一次处理的输入。``data`` 与 ``path`` 二选一。

    ``data`` 与 ``password`` 都不进 ``repr``：前者可能很大，后者是明文密码。
    """

    data: bytes | None = field(default=None, repr=False)
    path: Path | None = None
    filename: str = ""
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ExtractedEntry:
    """压缩包内的一个成员。

    ``path`` 是处理期间的落盘位置。本模块「即用即弃」：``process_attachment``
    返回前会连同临时目录一起删除，所以返回后的 ``path`` 只可用于追溯，
    调用方应依赖 ``nested`` 中的 ``text`` / ``metadata``。
    """

    name: str
    size: int
    detected_type: str
    kind: Kind
    sha256: str
    path: Path | None = None
    nested: AttachmentResult | None = None


@dataclass(frozen=True, slots=True)
class AttachmentResult:
    """处理结果。``text`` 与 ``metadata`` 是给下游解析用的正文与旁证。"""

    source_name: str
    detected_type: str
    declared_type: str
    kind: Kind
    size: int
    sha256: str
    entries: list[ExtractedEntry] = field(default_factory=list)
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
