"""纯文本：按 UTF-8 → GB18030 解码并原样交出正文。"""

from __future__ import annotations

from app.billing.contracts import AttachmentResult
from app.billing.detect import decode_text
from app.billing.errors import unsupported
from app.billing.handlers.registry import HandleContext, result_for


class PlaintextHandler:
    kind = "text"

    def handle(self, data: bytes, ctx: HandleContext) -> AttachmentResult:
        decoded = decode_text(data, declared=ctx.declared_type)
        if decoded is None:
            raise unsupported("无法按 UTF-8 或 GB18030 解码为文本")
        text, encoding = decoded
        return result_for(
            data,
            ctx,
            text=text,
            metadata={
                "encoding": encoding,
                "lines": len(text.splitlines()),
                "characters": len(text),
            },
        )
