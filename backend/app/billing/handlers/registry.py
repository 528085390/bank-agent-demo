"""处理器契约与类型 → 处理器注册表。

本模块是叶子：只依赖 contracts / detect / errors，不 import 任何具体处理器，
所以具体处理器可以自由地从这里取契约，不会形成循环 import。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.billing.contracts import ArchiveLimits, AttachmentResult, ExtractedEntry, Kind, sha256_of
from app.billing.detect import Detection, kind_of


@dataclass(frozen=True, slots=True)
class HandleContext:
    """处理器需要的全部外部信息。

    ``password`` 不进 ``repr``，也永远不参与路径构造。
    ``recurse`` 由门面注入，用于压缩包成员「递归调用本处理链」。
    处理器本身无状态，上限随 ``limits`` 逐次传入。
    """

    filename: str
    declared_type: str
    detected_type: str
    scratch: Path
    depth: int
    max_depth: int
    recurse: Callable[[bytes, str], AttachmentResult]
    limits: ArchiveLimits = field(default_factory=ArchiveLimits)
    password: str | None = field(default=None, repr=False)

    @property
    def detection(self) -> Detection:
        return Detection(
            detected_type=self.detected_type,
            declared_type=self.declared_type,
            kind=kind_of(self.detected_type),
        )


class FormatHandler(Protocol):
    """一种格式的处理器。``kind`` 必须与 ``detect.kind_of`` 对同一类型的结论一致。"""

    kind: Kind

    def handle(self, data: bytes, ctx: HandleContext) -> AttachmentResult: ...


HANDLERS: dict[str, FormatHandler] = {}


def register(detected_types: Iterable[str], handler: FormatHandler) -> None:
    """把一个处理器挂到若干类型上，并校验它声明的 ``kind`` 没写错。"""

    for detected_type in detected_types:
        expected = kind_of(detected_type)
        if handler.kind != expected:
            raise ValueError(
                f"{type(handler).__name__}.kind={handler.kind!r} 与类型 "
                f"{detected_type!r} 的 kind={expected!r} 不一致"
            )
        HANDLERS[detected_type] = handler


def handler_for(detected_type: str) -> FormatHandler | None:
    return HANDLERS.get(detected_type)


def result_for(
    data: bytes,
    ctx: HandleContext,
    *,
    text: str | None = None,
    metadata: dict[str, Any] | None = None,
    entries: list[ExtractedEntry] | None = None,
    warnings: Iterable[str] = (),
) -> AttachmentResult:
    """补齐 size / sha256 / kind 与类型不符告警，产出统一的 ``AttachmentResult``。"""

    collected = list(warnings)
    mismatch = ctx.detection.warning
    if mismatch:
        collected.append(mismatch)
    return AttachmentResult(
        source_name=ctx.filename,
        detected_type=ctx.detected_type,
        declared_type=ctx.declared_type,
        kind=kind_of(ctx.detected_type),
        size=len(data),
        sha256=sha256_of(data),
        entries=list(entries or []),
        text=text,
        metadata=dict(metadata or {}),
        warnings=collected,
    )
