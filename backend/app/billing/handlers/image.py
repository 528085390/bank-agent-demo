"""图片：只登记类型与摘要，不做 OCR。

方案 §3.1 把图片列在批次 1，但只要求「识别类型并登记」；OCR 见 §10 限制 3，
列为后续扩展。所以这里不产出 ``text``，不上抛异常，也不需要额外依赖。
"""

from __future__ import annotations

from app.billing.contracts import AttachmentResult
from app.billing.handlers.registry import HandleContext, result_for


class ImageHandler:
    kind = "image"

    def handle(self, data: bytes, ctx: HandleContext) -> AttachmentResult:
        return result_for(data, ctx, metadata={"format": ctx.detected_type})
