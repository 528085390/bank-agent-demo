"""账单处理协调器：（压缩包才解压）→ 解析 → 入库 → 分析 → 完成。

真实调用 B 的 ``process_attachment`` 提取内容；解析与分析走 C 的接入缝（bill_parsing）。
起手状态按文件真实类型给——只有压缩包才进"解压中"，其余直接进"解析中"。
密码错误回到 WAITING_PASSWORD 并扣减剩余尝试次数；用尽落 FAILED；其余失败落到 FAILED。

``process_task`` 是同步函数，测试可直接调用；``start_processing`` 用 daemon 线程包装，
供 API 在密码提交后异步推进。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select

from app.analysis import register as register_c_parsers
from app.billing import DEFAULT_SANDBOX_ROOT, BillingError, TaskSandbox, process_attachment
from app.billing.detect import ARCHIVE_TYPES, detect_type
from app.billing.errors import PASSWORD_INVALID, PASSWORD_REQUIRED
from app.core.bill_errors import FILE_NOT_FOUND, PARSE_FAILED
from app.core.errors import AppError
from app.core.types import utcnow
from app.services.bill_audit import write_audit
from app.services.bill_content_hash import content_file_hash
from app.services.bill_import import BillImportService
from app.services.bill_parsing import compute_summary, parse_statement
from app.services.bill_task import backfill_period
from app.storage.bill_models import BillTask

logger = logging.getLogger(__name__)

# 启动即注册 C 的解析器与分析器（幂等；测试用 bill_parsing.reset_parsers 隔离）。
register_c_parsers()


def start_processing(task_id: str, password: str | None, database) -> threading.Thread:
    thread = threading.Thread(target=process_task, args=(task_id, password, database), daemon=True)
    thread.start()
    return thread


def process_task(task_id: str, password: str | None, database) -> None:
    file_path, uploaded_hash = _load_upload(database, task_id)
    if not file_path:
        _fail(database, task_id, FILE_NOT_FOUND, "未找到已上传的文件")
        return

    import_service = BillImportService(database)
    # 只有压缩包才有"解压"这一步。PDF / Excel / CSV 只是提取文本，对它们报"解压中"是假的，
    # 所以起手状态按文件真实类型给：压缩包先进 EXTRACTING，其余直接进 PARSING。
    extracting = _is_archive(file_path)
    _set_state(
        database, task_id, "EXTRACTING" if extracting else "PARSING", "PROCESSING_STARTED", {}
    )

    try:
        with TaskSandbox(DEFAULT_SANDBOX_ROOT, task_id) as sandbox:
            result = process_attachment(path=Path(file_path), password=password, sandbox=sandbox)
        text = result.text
        if not text:
            _fail(database, task_id, PARSE_FAILED, "未能提取文本内容")
            return

        if extracting:
            # 压缩包解压完了，接下来才是解析。
            _set_state(
                database, task_id, "PARSING", "EXTRACTION_DONE", {"type": result.detected_type}
            )
        parse_result = parse_statement(text)
        # pypdf 的表格重建对“单元格跨视觉行”的 PDF 会失败；只在主结果的失败行
        # 多于有效行时才用 pdfplumber 再试一次，避免影响已经能正常解析的文件。
        parse_result = _improve_pdf_parse(parse_result, result, file_path, password)
        # 哈希取 save_upload 存下的那个，**不用** B 返回的 result.sha256：这样
        # bill_task.uploaded_file_hash 与 statement_document.source_file_hash 同源，
        # latest_document_for 的回退查找才按构造成立。文件名仍来自 B（它知道真实来源名）。
        file_hash = uploaded_hash or content_file_hash(Path(file_path).read_bytes())
        parse_result = replace(
            parse_result,
            document_info=replace(
                parse_result.document_info,
                file_hash=file_hash,
                filename=result.source_name,
            ),
        )

        _set_state(database, task_id, "SAVING", "PARSING_DONE", {})
        import_service.save_statement(task_id, parse_result)
        # 上传时不再要求填日期：任务周期在解析前是空的，这里用文档的真实起止补上。
        # 解析结果里的期间作为兜底 —— 整份内容此前已导入时不会建新文档。
        backfill_period(
            database,
            task_id,
            parsed_start=parse_result.document_info.period_start,
            parsed_end=parse_result.document_info.period_end,
        )

        summary = compute_summary(task_id, database)
        import_service.save_summary(task_id, summary)

    except BillingError as exc:
        if exc.code in {PASSWORD_REQUIRED, PASSWORD_INVALID}:
            _password_failed(database, task_id, exc)
        else:
            _fail(database, task_id, exc.code, exc.message)
    except AppError as exc:
        _fail(database, task_id, exc.code, exc.message)
    except Exception:
        logger.exception("处理账单任务失败 task_id=%s", task_id)
        _fail(database, task_id, "INTERNAL", "处理过程中发生错误")


def _is_archive(file_path: str) -> bool:
    """是不是压缩包。按内容判、不看扩展名，与 B 处理附件时用的是同一套识别。"""

    try:
        return detect_type(Path(file_path).read_bytes()) in ARCHIVE_TYPES
    except OSError:
        return False


def _load_upload(database, task_id: str) -> tuple[str | None, str | None]:
    """任务的文件路径与内容指纹。指纹从任务行读回，不让 B 那边再算一个。"""

    with database.sessions() as db:
        task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
        if task is None:
            return None, None
        return task.uploaded_file_path, task.uploaded_file_hash


def _parse_problems(parse_result) -> int:
    """How many rows/headers the parser could not make sense of.

    The parser records these as human-readable quality issues. They are the only signal the
    processing layer has that the *extraction* (not the statement) may be wrong, so this is
    deliberately narrow: a footer row skipped as "无法解析" counts, an informational preamble
    line does not.
    """

    return sum(
        1
        for issue in parse_result.quality_issues
        if "无法解析" in issue
        or "未解析到任何交易记录" in issue
        or "缺少日期或金额列" in issue
    )


def _needs_pdf_fallback(parse_result, detected_type: str) -> bool:
    """Only a PDF whose parse is worse than half-broken gets a second extraction attempt."""

    if detected_type != "pdf":
        return False
    return _parse_problems(parse_result) > len(parse_result.transactions)


def _is_better_parse(candidate, current) -> bool:
    """Only replace the primary result when the fallback is better, never merely longer.

    More transactions is better, but only if it does not come with more parse problems --
    otherwise a bad extraction can win just by inventing rows. Ties keep the primary result.
    """

    candidate_count = len(candidate.transactions)
    current_count = len(current.transactions)
    candidate_problems = _parse_problems(candidate)
    current_problems = _parse_problems(current)

    if candidate_count > current_count:
        return candidate_problems <= current_problems
    if candidate_count == current_count:
        return candidate_problems < current_problems
    return False


def _improve_pdf_parse(parse_result, attachment, file_path: str, password: str | None):
    """Retry a poor PDF parse with pdfplumber, but never replace a better first result."""

    if not _needs_pdf_fallback(parse_result, attachment.detected_type):
        return parse_result
    try:
        from app.billing.handlers.pdf import extract_pdf_tables

        table_text = extract_pdf_tables(Path(file_path).read_bytes(), password=password)
    except Exception:
        logger.warning("pdf table fallback could not read the file", exc_info=True)
        return parse_result
    if not table_text:
        return parse_result
    try:
        candidate = parse_statement(table_text)
    except Exception:
        logger.warning("pdf table fallback could not be parsed", exc_info=True)
        return parse_result
    if not _is_better_parse(candidate, parse_result):
        return parse_result
    logger.info(
        "pdf table fallback improved parse: %s -> %s transactions",
        len(parse_result.transactions),
        len(candidate.transactions),
    )
    return candidate


def _set_state(database, task_id: str, state: str, step: str, detail: dict) -> None:
    with database.sessions.begin() as db:
        task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
        if task is None:
            return
        task.state = state
        write_audit(db, task.id, task.user_id, step, detail)


def _password_failed(database, task_id: str, exc: BillingError) -> None:
    with database.sessions.begin() as db:
        task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
        if task is None:
            return
        # 001 存的是「剩余次数」，递减；用尽即落 FAILED，用户不会停在还能再试的假象里。
        task.password_attempts_left = max(0, task.password_attempts_left - 1)
        task.state = "FAILED" if task.password_attempts_left <= 0 else "WAITING_PASSWORD"
        task.failure_code = exc.code
        task.failure_message = exc.message
        write_audit(
            db, task.id, task.user_id, "PASSWORD_FAILED",
            {"remaining": task.password_attempts_left, "code": exc.code},
            result="FAILURE",
            error_code=exc.code,
        )


def _fail(database, task_id: str, code: str, message: str) -> None:
    with database.sessions.begin() as db:
        task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
        if task is None:
            return
        task.state = "FAILED"
        task.failure_code = code
        task.failure_message = message
        task.completed_at = utcnow()
        write_audit(
            db, task.id, task.user_id, "TASK_FAILED",
            {"code": code, "message": message},
            result="FAILURE",
            error_code=code,
        )
