"""压缩包：安全检查、解密、解压。

**先校验后解压**：ZIP 的解压是流式的，先解压等于已经落盘。所有尺寸 / 路径 /
数量检查都只读中央目录（``ZipInfo`` / ``TarInfo``），不写任何文件。

**成员名编码**：读取时一律传 ``metadata_encoding``（见 ``contracts.ZIP_NAME_ENCODING``），
否则国产压缩软件写的中文文件名会被当成 cp437 解出乱码。读取与解压必须同值。

覆盖方案 §3.1 批次 1 的 ZIP（无密码与 ZipCrypto）、TAR、GZ、BZ2。
AES 加密的 ZIP 与 7z / RAR 不在批次 1 内，按「明确报错」处理。
"""

from __future__ import annotations

import bz2
import gzip
import io
import logging
import re
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

from app.billing import detect
from app.billing.contracts import (
    ZIP_NAME_ENCODING,
    ArchiveLimits,
    AttachmentResult,
    ExtractedEntry,
)
from app.billing.errors import (
    BillingError,
    archive_rejected,
    missing_dependency,
    nesting_too_deep,
    password_invalid,
    password_required,
    unsupported,
)
from app.billing.handlers.registry import HandleContext, result_for
from app.billing.sandbox import allocate_dir

logger = logging.getLogger(__name__)

#: WinZip AES 的压缩方法号；标准库 zipfile 解不开，需要 pyzipper。
_ZIP_AES_METHOD = 99
_ZIP_ENCRYPTED = 0x1
_ZIP_STRONG_ENCRYPTION = 0x40

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")

#: 解压时一次读取的块大小，用于给 GZ / BZ2 套上内存上限。
_CHUNK = 64 * 1024

#: GZ / BZ2 的内层是不是 tar（ustar 魔数落在偏移 257）。
_USTAR_OFFSET = 257


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    """中央目录里的一条成员记录，尚未落盘。"""

    name: str
    size: int
    compressed_size: int
    is_dir: bool
    is_link: bool = False
    is_device: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    """``format`` 是内层结构（zip / tar / single），``compressed`` 是外层压缩。"""

    format: str
    compressed: str | None
    encrypted: bool
    members: list[ArchiveMember]


@dataclass(frozen=True, slots=True)
class ExtractedFile:
    member: ArchiveMember
    path: Path


def inspect_archive(
    data: bytes,
    limits: ArchiveLimits | None = None,
    *,
    detected_type: str,
    filename: str = "",
) -> ArchiveInfo:
    """只读中央目录，判定结构并做全部安全检查；不写任何文件。"""

    limits = limits or ArchiveLimits()
    info = _read_info(data, detected_type, filename, limits)
    _assert_safe(info, limits, total_input=len(data))
    return info


def extract_archive(
    data: bytes,
    info: ArchiveInfo,
    dest: Path,
    *,
    password: str | None = None,
    limits: ArchiveLimits | None = None,
) -> list[ExtractedFile]:
    """把成员解压到 ``dest``（必须已在沙箱内）。失败时清掉本次已写的文件。"""

    limits = limits or ArchiveLimits()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if info.format == "zip":
            return _extract_zip(data, info, dest, password)
        if info.format == "tar":
            return _extract_tar(_payload(data, info, limits), info, dest)
        return _extract_single(_payload(data, info, limits), info, dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


class ArchiveHandler:
    """把压缩包解成成员，并对每个成员递归调用本处理链。

    无状态：上限随 ``ctx.limits`` 走，所以注册一次就能服务所有调用方。
    """

    kind = "archive"

    def handle(self, data: bytes, ctx: HandleContext) -> AttachmentResult:
        if ctx.depth >= ctx.max_depth:
            raise nesting_too_deep(ctx.max_depth)

        info = inspect_archive(
            data, ctx.limits, detected_type=ctx.detected_type, filename=ctx.filename
        )
        extracted = extract_archive(
            data,
            info,
            allocate_dir(ctx.scratch, f"d{ctx.depth}"),
            password=ctx.password,
            limits=ctx.limits,
        )

        entries = []
        table_retries = 0
        for item in extracted:
            member_data = item.path.read_bytes()
            nested = ctx.recurse(member_data, item.member.name)
            # 嵌套 PDF 的第二条提取路径：pypdf 把"单元格跨视觉行"的表格拆碎时，
            # 顶层有 pdfplumber 回退，压缩包里的 PDF 以前没有。成员字节此刻还在手里，
            # 就在这一层补上（详见 _re_extract_pdf）。
            if table_retries < _MAX_TABLE_RETRIES and _needs_table_retry(nested):
                table_retries += 1
                nested = _re_extract_pdf(
                    nested, member_data, password=ctx.password, name=item.member.name
                )
            entries.append(
                ExtractedEntry(
                    name=item.member.name,
                    size=item.member.size,
                    detected_type=nested.detected_type,
                    kind=nested.kind,
                    sha256=nested.sha256,
                    path=item.path,
                    nested=nested,
                )
            )
        return result_for(
            data,
            ctx,
            text=_aggregate_text(entries),
            entries=entries,
            metadata={
                "format": info.format,
                "compression": info.compressed,
                "encrypted": info.encrypted,
                "member_count": len(info.members),
            },
        )


#: 一次导入里最多为几份嵌套 PDF 做二次提取。pdfplumber 比 pypdf 慢得多，
#: 给一个上限，避免一个塞了几十份 PDF 的压缩包把任务拖到超时。
_MAX_TABLE_RETRIES = 5

#: "行首是一个完整日期"。账单的每一行都以日期开头（或日期+时刻），所以这个数量
#: 直接反映"pypdf 有没有把每笔交易还原成一行"。
_DATED_LINE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}(\s|$)")


def _dated_rows(text: str | None) -> int:
    return sum(1 for line in (text or "").splitlines() if _DATED_LINE.match(line))


def _needs_table_retry(nested: AttachmentResult) -> bool:
    """嵌套 PDF 的文本要不要用 pdfplumber 再抽一次。

    判据只看"行首完整日期的行数"，不看制表符数量 —— 被拆碎的行同样带好几个制表符，
    数量反而更多（实测 43 行 vs 11 行），拿它判断会选错。至少两行（表头之外还有
    数据行）就认为 pypdf 的表格重建是好的，不再多花一次提取。
    """

    return nested.detected_type == detect.PDF and _dated_rows(nested.text) < 2


def _re_extract_pdf(
    nested: AttachmentResult,
    data: bytes,
    *,
    password: str | None,
    name: str,
) -> AttachmentResult:
    """用 pdfplumber 再抽一次；**只有日期行更多才替换**，否则原样返回。

    二次提取是加分项：任何异常、任何"看起来没更好"的结果都不该影响这次导入。
    """

    # 自己再判一次：调用方（归档循环）也要判，但那样这个函数单独被调用时就会
    # 对一份正常的 PDF 白跑一次 pdfplumber。这个条件才是"要不要修"的唯一依据。
    if not _needs_table_retry(nested):
        return nested

    try:
        from app.billing.handlers.pdf import extract_pdf_tables

        candidate = extract_pdf_tables(data, password=password)
    except Exception:
        logger.warning("嵌套 PDF 的表格二次提取失败：%s", name, exc_info=True)
        return nested

    if candidate and _dated_rows(candidate) > _dated_rows(nested.text):
        return replace(
            nested,
            text=candidate,
            metadata={**nested.metadata, "extractor": "pdfplumber", "view": "table"},
        )
    return nested


def _aggregate_text(entries: list[ExtractedEntry]) -> str | None:
    """汇总压缩包成员的正文：按成员顺序取各自（含嵌套）的非空文本后拼接。

    全部成员都没有正文（纯二进制、图片）时返回 None——不伪造空正文，与
    image / unknown 的「仅登记」语义保持一致。嵌套压缩包的 nested.text 已由本函数
    填充，递归只是兜底（覆盖 ZIP→ZIP→XLSX 这类多层结构）。
    """
    parts = []
    for entry in entries:
        nested = entry.nested
        if nested is None:
            continue
        text = nested.text or _aggregate_text(nested.entries)
        if text:
            parts.append(text)
    return "\n\n".join(parts) if parts else None


# --------------------------------------------------------------------------- 读取


def _read_info(
    data: bytes, detected_type: str, filename: str, limits: ArchiveLimits
) -> ArchiveInfo:
    if detected_type == detect.ZIP:
        return _read_zip(data)
    if detected_type == detect.TAR:
        return _read_tar(data)
    if detected_type in {detect.GZIP, detect.BZIP2}:
        return _read_compressed(data, detected_type, filename, limits)
    if detected_type in {detect.SEVENZIP, detect.RAR}:
        raise unsupported(f"{detected_type} 压缩包不在当前支持的格式内（方案 §3.1 批次 2/3）")
    raise unsupported(f"不是可识别的压缩包：{detected_type}")


def _read_zip(data: bytes) -> ArchiveInfo:
    try:
        with zipfile.ZipFile(io.BytesIO(data), metadata_encoding=ZIP_NAME_ENCODING) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise unsupported("压缩包已损坏或不是有效的 ZIP") from exc

    for info in infos:
        encrypted = info.flag_bits & _ZIP_ENCRYPTED
        if info.compress_type == _ZIP_AES_METHOD or (
            encrypted and info.flag_bits & _ZIP_STRONG_ENCRYPTION
        ):
            raise missing_dependency("pyzipper", "解压 AES 加密的 ZIP")

    return ArchiveInfo(
        format="zip",
        compressed=None,
        encrypted=any(info.flag_bits & _ZIP_ENCRYPTED for info in infos),
        members=[
            ArchiveMember(
                name=info.filename,
                size=info.file_size,
                compressed_size=info.compress_size,
                is_dir=info.is_dir(),
                is_link=stat.S_ISLNK(info.external_attr >> 16),
            )
            for info in infos
        ],
    )


def _read_tar(payload: bytes) -> ArchiveInfo:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            infos = archive.getmembers()
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise unsupported("压缩包已损坏或不是有效的 TAR") from exc
    return ArchiveInfo(
        format="tar", compressed=None, encrypted=False, members=[_tar_member(i) for i in infos]
    )


def _read_compressed(
    data: bytes, detected_type: str, filename: str, limits: ArchiveLimits
) -> ArchiveInfo:
    """GZ / BZ2 可能只是单文件，也可能是 tar 的外层。

    解压本身是有上限的：压缩比检查必须发生在「已经解出几十 GB」之前。
    """

    inner = _decompress(data, detected_type, limits.max_total_size)
    if _looks_like_tar(inner):
        info = _read_tar(inner)
        return ArchiveInfo(
            format="tar",
            compressed=detected_type,
            encrypted=False,
            members=info.members,
        )
    name = _strip_archive_suffix(filename) or "content"
    member = ArchiveMember(name=name, size=len(inner), compressed_size=len(data), is_dir=False)
    return ArchiveInfo(format="single", compressed=detected_type, encrypted=False, members=[member])


def _tar_member(info: tarfile.TarInfo) -> ArchiveMember:
    return ArchiveMember(
        name=info.name,
        size=info.size,
        compressed_size=0,
        is_dir=info.isdir(),
        is_link=info.issym() or info.islnk(),
        is_device=info.ischr() or info.isblk() or info.isfifo(),
    )


def _strip_archive_suffix(filename: str) -> str:
    lowered = filename.lower()
    for suffix in (".tgz", ".tbz2", ".tar"):
        if lowered.endswith(suffix):
            return filename[: -len(suffix)]
    suffix = Path(filename).suffix
    return filename[: -len(suffix)] if suffix else filename


def _looks_like_tar(payload: bytes) -> bool:
    return (
        len(payload) >= _USTAR_OFFSET + 5 and payload[_USTAR_OFFSET : _USTAR_OFFSET + 5] == b"ustar"
    )


def _decompress(data: bytes, detected_type: str, limit: int) -> bytes:
    """分块解压，超过 ``limit`` 立即拒绝——不让 zip bomb 进到内存里。"""

    stream = (
        gzip.GzipFile(fileobj=io.BytesIO(data))
        if detected_type == detect.GZIP
        else bz2.BZ2File(io.BytesIO(data))
    )
    chunks: list[bytes] = []
    total = 0
    try:
        with stream:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise archive_rejected(f"解压后已超过 {limit} 字节，判定为构造包并中止")
                chunks.append(chunk)
    except BillingError:
        raise
    except (OSError, EOFError, ValueError) as exc:
        raise unsupported("压缩包已损坏或不是有效的压缩流") from exc
    return b"".join(chunks)


def _payload(data: bytes, info: ArchiveInfo, limits: ArchiveLimits) -> bytes:
    """取出内层字节；外层没有压缩时就是原字节。"""

    if info.compressed is None:
        return data
    return _decompress(data, info.compressed, limits.max_total_size)


# --------------------------------------------------------------------------- 安全检查


def _assert_safe(info: ArchiveInfo, limits: ArchiveLimits, *, total_input: int) -> None:
    files = [member for member in info.members if not member.is_dir]
    if len(files) > limits.max_files:
        raise archive_rejected(f"成员数量 {len(files)} 超过上限 {limits.max_files}")

    for member in info.members:
        reason = _unsafe_name(member.name)
        if reason:
            raise archive_rejected(reason)
        if member.is_link:
            raise archive_rejected(f"成员是符号链接或硬链接：{member.name}")
        if member.is_device:
            raise archive_rejected(f"成员是设备文件：{member.name}")
        if Path(member.name).suffix.lower() in limits.forbidden_suffixes:
            raise archive_rejected(f"成员扩展名在禁止清单内：{member.name}")

    for member in files:
        if member.size > limits.max_single_size:
            raise archive_rejected(f"成员 {member.name} 解压后 {member.size} 字节，超过单文件上限")
    total = sum(member.size for member in files)
    if total > limits.max_total_size:
        raise archive_rejected(f"解压总量 {total} 字节，超过上限 {limits.max_total_size}")

    _assert_ratio(
        files, limits, total=total, total_input=total_input, per_member=info.format == "zip"
    )


def _assert_ratio(
    files: list[ArchiveMember],
    limits: ArchiveLimits,
    *,
    total: int,
    total_input: int,
    per_member: bool,
) -> None:
    """压缩比上限，防 zip bomb。

    TAR 的压缩发生在内层，逐成员没有压缩尺寸可比，所以只算整体比；
    ZIP 两者都算，逐成员的检查更早命中。
    """

    overall = total / max(1, total_input)
    if total and overall > limits.max_ratio:
        raise archive_rejected(f"整体压缩比 {overall:.1f} 超过上限 {limits.max_ratio}")
    if not per_member:
        return
    for member in files:
        if member.size == 0:
            continue
        if member.compressed_size == 0:
            raise archive_rejected(f"成员 {member.name} 声称有内容却零压缩体积，疑似构造包")
        ratio = member.size / member.compressed_size
        if ratio > limits.max_ratio:
            raise archive_rejected(
                f"成员 {member.name} 压缩比 {ratio:.1f} 超过上限 {limits.max_ratio}"
            )


def _unsafe_name(name: str) -> str | None:
    if not name or not name.strip():
        return "成员名为空"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return f"成员名是绝对路径：{name}"
    if _DRIVE_LETTER.match(normalized):
        return f"成员名包含盘符：{name}"
    if any(part == ".." for part in normalized.split("/")):
        return f"成员名包含上级目录引用：{name}"
    return None


def _safe_join(root: Path, name: str) -> Path:
    """按 realpath 前缀比对落点，防 zip slip。

    ``Path.resolve()`` 会展开符号链接，所以前缀比对必须发生在 resolve 之后。
    """

    target = (root / name.replace("\\", "/")).resolve()
    if not target.is_relative_to(root.resolve()):
        raise archive_rejected(f"成员落点越出目标目录：{name}")
    return target


# --------------------------------------------------------------------------- 解压


def _extract_zip(
    data: bytes, info: ArchiveInfo, dest: Path, password: str | None
) -> list[ExtractedFile]:
    # metadata_encoding 必须与 _read_zip 一致：这里的 member.name 正是那边的解码结果，
    # 两边不同就会因名字对不上而找不到成员。
    with zipfile.ZipFile(io.BytesIO(data), metadata_encoding=ZIP_NAME_ENCODING) as archive:
        secret = _resolve_zip_password(archive, info, password)
        written: list[ExtractedFile] = []
        for member in info.members:
            if member.is_dir:
                continue
            target = _safe_join(dest, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member.name, pwd=secret) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
            except RuntimeError as exc:
                # ZipCrypto 的校验字节不符，zipfile 在 open() 时抛这个。
                raise password_invalid() from exc
            except NotImplementedError as exc:
                raise unsupported(f"不支持成员 {member.name} 的压缩方法") from exc
            except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
                raise unsupported(f"压缩包已损坏（成员 {member.name}）") from exc
            written.append(ExtractedFile(member=member, path=target))
        return written


def _resolve_zip_password(
    archive: zipfile.ZipFile, info: ArchiveInfo, password: str | None
) -> bytes | None:
    """不加密返回 ``None``；加密时把密码错误挡在落盘之前。"""

    if not info.encrypted:
        return None
    if password is None:
        raise password_required()

    secret = password.encode("utf-8")
    first = next((member for member in info.members if not member.is_dir), None)
    if first is None:
        return secret
    try:
        # zipfile 在 open() 里就比对 ZipCrypto 的校验字节，正确性无需读满整个成员。
        with archive.open(first.name, pwd=secret):
            pass
    except RuntimeError as exc:
        raise password_invalid() from exc
    except NotImplementedError as exc:
        raise unsupported(f"不支持成员 {first.name} 的压缩方法") from exc
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise unsupported("压缩包已损坏") from exc
    return secret


def _extract_tar(payload: bytes, info: ArchiveInfo, dest: Path) -> list[ExtractedFile]:
    written: list[ExtractedFile] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in info.members:
            if member.is_dir:
                continue
            source = archive.extractfile(member.name)
            if source is None:
                raise unsupported(f"无法读取成员 {member.name}")
            target = _safe_join(dest, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            written.append(ExtractedFile(member=member, path=target))
    return written


def _extract_single(payload: bytes, info: ArchiveInfo, dest: Path) -> list[ExtractedFile]:
    """GZ / BZ2 的单文件形态。"""

    member = info.members[0]
    target = _safe_join(dest, member.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return [ExtractedFile(member=member, path=target)]
