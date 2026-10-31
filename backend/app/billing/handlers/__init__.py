"""附件处理门面：``process_attachment``。

处理链（方案 §5）：

    取字节 → 类型识别 → 分派 → 压缩包则递归 → 汇总 AttachmentResult

**即用即弃**：每次顶层调用在自己的临时子目录里工作，返回前把整个子目录删掉。
所以 ``ExtractedEntry.path`` 记录的是处理期间的落盘位置，返回后即失效——
调用方应依赖 ``nested`` 里的 ``text`` / ``metadata``，而不是回头去读磁盘。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.billing import detect
from app.billing.contracts import (
    DEFAULT_MAX_DEPTH,
    ArchiveLimits,
    AttachmentRequest,
    AttachmentResult,
)
from app.billing.errors import INTERNAL_ERROR, BillingError
from app.billing.handlers.archive import ArchiveHandler
from app.billing.handlers.docx import DocxHandler
from app.billing.handlers.image import ImageHandler
from app.billing.handlers.pdf import PdfHandler
from app.billing.handlers.plaintext import PlaintextHandler
from app.billing.handlers.registry import HandleContext, handler_for, register, result_for
from app.billing.handlers.spreadsheet import SpreadsheetHandler
from app.billing.sandbox import TaskSandbox

# 7z / RAR 也交给压缩包处理器：它会在 _read_info 里明确报「批次 2/3 未支持」，
# 比落到「没有处理器，仅登记」更能说明问题。
register(detect.ARCHIVE_TYPES, ArchiveHandler())
register((detect.PDF,), PdfHandler())
register((detect.DOCX,), DocxHandler())
register((detect.XLSX, detect.CSV), SpreadsheetHandler())
register((detect.TEXT,), PlaintextHandler())
register((detect.JPEG, detect.PNG, detect.GIF, detect.BMP), ImageHandler())

#: 只登记类型与摘要、不提取正文的类型（方案 §5.1 的 OLE 降级 + §3.1 的未知二进制）。
_REGISTERED_ONLY_WARNINGS: dict[str, str] = {
    detect.OLEDOC: "旧版 Office 格式（.doc/.xls）需要外部程序才能提取正文，本次仅登记类型",
    detect.UNKNOWN: "无法识别的二进制类型，仅登记类型与摘要",
}


def process_attachment(
    data: bytes | None = None,
    *,
    path: Path | None = None,
    filename: str = "",
    password: str | None = None,
    sandbox: TaskSandbox,
    max_depth: int = DEFAULT_MAX_DEPTH,
    limits: ArchiveLimits | None = None,
) -> AttachmentResult:
    """识别一个附件的真实类型并提取内容。

    ``data`` 与 ``path`` 二选一：给了 ``path`` 就整块读进内存，不额外落盘——
    本模块从不把输入写进沙箱，只写解压产物，所以不必先复制一份。

    **递归深度从内部固定为 0 起算**，调用方只能通过 ``max_depth`` 设上限。
    ``depth`` 曾是公共参数，而判据是 ``depth >= max_depth``——传负数就能让它
    迟迟不成立，从而穿过任意多层嵌套（见方案 §7 步骤 4）。

    失败时抛 ``BillingError``，错误码见 ``errors.py``；参数本身不合法（``data`` 与
    ``path`` 两个都没给或都给了）抛 ``ValueError``，那属于调用方的 bug。
    """

    request = _build_request(data=data, path=path, filename=filename, password=password)
    scratch = sandbox.new_scratch()
    try:
        return _process(
            request,
            limits=limits or ArchiveLimits(),
            depth=0,
            max_depth=max_depth,
            scratch=scratch,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _build_request(
    *, data: bytes | None, path: Path | None, filename: str, password: str | None
) -> AttachmentRequest:
    if (data is None) == (path is None):
        raise ValueError("data 与 path 必须且只能提供一个")

    if data is None:
        source = Path(path)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise BillingError(INTERNAL_ERROR, f"无法读取文件：{source.name}") from exc
        filename = filename or source.name

    return AttachmentRequest(data=data, filename=filename, password=password)


def _process(
    request: AttachmentRequest,
    *,
    limits: ArchiveLimits,
    depth: int,
    max_depth: int,
    scratch: Path,
) -> AttachmentResult:
    payload = request.data or b""
    detection = detect.detect(payload, request.filename)

    def recurse(member_data: bytes, member_name: str) -> AttachmentResult:
        # 嵌套压缩包沿用同一个密码：一次任务只有一个密码，重新问一遍没有意义。
        return _process(
            AttachmentRequest(data=member_data, filename=member_name, password=request.password),
            limits=limits,
            depth=depth + 1,
            max_depth=max_depth,
            scratch=scratch,
        )

    ctx = HandleContext(
        filename=request.filename,
        declared_type=detection.declared_type,
        detected_type=detection.detected_type,
        scratch=scratch,
        depth=depth,
        max_depth=max_depth,
        recurse=recurse,
        limits=limits,
        password=request.password,
    )

    handler = handler_for(detection.detected_type)
    if handler is None:
        return result_for(payload, ctx, warnings=[_registered_only_warning(ctx.detected_type)])
    return handler.handle(payload, ctx)


def _registered_only_warning(detected_type: str) -> str:
    return _REGISTERED_ONLY_WARNINGS.get(
        detected_type, f"类型 {detected_type} 没有对应的处理器，仅登记类型与摘要"
    )
