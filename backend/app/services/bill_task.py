"""账单任务服务：状态机、审计、文件落盘与查询。

这是账单流水线的核心服务。所有写操作都在单事务内完成，状态转移严格按序；
B / C 不直接改任务状态，只能通过这里（或入库 / 处理协调器）推进。

对外标识符（方法参数 ``task_id``、返回 dict 的键）沿用既有契约不变；只有 ORM 属性名跟随
001 契约的列名（``task_no`` / ``failure_code`` / ``password_attempts_left`` 等）。
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from app.core.bill_config import get_bill_settings
from app.core.bill_errors import (
    FILE_TOO_LARGE,
    INVALID_TASK_STATE,
    NOT_FOUND,
    PASSWORD_ATTEMPTS_EXCEEDED,
    PASSWORD_REQUIRED,
    PERMISSION_DENIED,
    VALIDATION,
    raise_bill_error,
)
from app.core.types import new_id, utcnow
from app.services.bill_audit import write_audit
from app.services.bill_content_hash import content_file_hash
from app.services.bill_import import latest_document_for
from app.storage.bill_models import (
    BillSummary,
    BillTask,
    BillTaskAudit,
    Transaction,
)

#: 当前支持的银行。
SUPPORTED_BANKS = frozenset({"CMB"})

#: 密码相关的错误码：这类失败允许重新提交密码，而非重新上传文件。
PASSWORD_ERROR_CODES = frozenset({"PASSWORD_INVALID", "PASSWORD_REQUIRED"})

#: 状态 → 展示用进度。
PROGRESS = {
    "WAITING_APPLICATION": {"current_step": "等待申请流水", "percentage": 0},
    "WAITING_UPLOAD": {"current_step": "等待上传文件", "percentage": 15},
    "WAITING_PASSWORD": {"current_step": "等待输入密码", "percentage": 30},
    "EXTRACTING": {"current_step": "解压中", "percentage": 45},
    "PARSING": {"current_step": "解析中", "percentage": 60},
    "SAVING": {"current_step": "入库中", "percentage": 75},
    "ANALYZING": {"current_step": "分析中", "percentage": 90},
    "COMPLETED": {"current_step": "已完成", "percentage": 100},
    "FAILED": {"current_step": "处理失败", "percentage": 100},
    "CANCELLED": {"current_step": "已取消", "percentage": 100},
}


def allowed_actions(state: str, error_code: str | None = None) -> list[str]:
    if state == "WAITING_APPLICATION":
        return ["confirm_application", "cancel"]
    if state == "WAITING_UPLOAD":
        return ["upload_file", "cancel"]
    if state == "WAITING_PASSWORD":
        return ["submit_password", "cancel"]
    if state in {"EXTRACTING", "PARSING", "SAVING", "ANALYZING"}:
        return ["cancel"]
    if state == "COMPLETED":
        return []
    if state == "FAILED":
        if error_code in PASSWORD_ERROR_CODES:
            return ["submit_password", "cancel"]
        return ["upload_file", "cancel"]
    if state == "CANCELLED":
        return []
    return []


def backfill_period(
    database,
    task_id: str,
    *,
    parsed_start: date | None = None,
    parsed_end: date | None = None,
) -> None:
    """解析完成后，用文档的真实起止回填任务周期。

    上传时用户不再填日期（迁移 0018），任务周期在解析前是 NULL。这里的「真实起止」是
    解析器从交易日期上算出来的 ``statement_document.period_start/period_end`` —— 它才是
    这份文件实际覆盖的区间。任务视图与 ``latest_document_for`` 的回退都用它，所以解析
    一成功就立刻补上，而不是等前端来问。

    **这次没有建文档时（整份内容此前已导入）用解析结果里的期间兜底。** 否则任务会停在
    「没有周期」，而 ``latest_document_for`` 的"期间重叠"回退需要周期才能把已有文档找
    回来 —— 结果是这份流水明明在库里，任务却查不到（压缩包与解压后的 PDF 内容相同但文件
    哈希不同，就会走到这里）。
    """

    with database.sessions.begin() as db:
        task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
        if task is None:
            return
        doc = latest_document_for(db, task)
        if doc is not None:
            task.period_start = doc.period_start
            task.period_end = doc.period_end
        elif parsed_start is not None and parsed_end is not None:
            task.period_start = parsed_start
            task.period_end = parsed_end


def _tx_view(tx) -> dict:
    # 键名沿用 ``*_minor``（对外契约，明示单位是分）；列名是 001 的 amount / balance_after。
    return {
        "booked_date": tx.booked_date.isoformat(),
        "amount_minor": tx.amount,
        "direction": tx.direction,
        "transaction_type": tx.transaction_type,
        "counterparty": tx.counterparty,
        "balance_after_minor": tx.balance_after,
        "category": tx.category,
    }


class BillTaskService:
    def __init__(self, database):
        self.database = database

    # ------------------------------------------------------------------ 创建与转移

    def create_task(
        self,
        user_id: str,
        bank_code: str,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> dict:
        if bank_code not in SUPPORTED_BANKS:
            raise_bill_error(VALIDATION, f"暂不支持的银行：{bank_code}")
        # 周期可以不给：上传文件时真正的范围来自解析结果，解析完由 backfill_period 回填。
        # 两个都给时才校验顺序；只给一个没有意义，按格式错误拒绝。
        if (period_start is None) != (period_end is None):
            raise_bill_error(VALIDATION, "开始日期与结束日期必须同时提供或同时省略")
        # 起止相同是合法的一天：账单周期只要求结束不早于开始。
        if period_start is not None and period_end is not None and period_end < period_start:
            raise_bill_error(VALIDATION, "结束日期不能早于开始日期")
        now = utcnow()
        with self.database.sessions.begin() as db:
            task = BillTask(
                task_no=new_id("task"),
                user_id=user_id,
                bank_code=bank_code,
                period_start=period_start,
                period_end=period_end,
                state="WAITING_APPLICATION",
                source_mode="manual_upload",
                created_at=now,
            )
            db.add(task)
            # 审计表的 task_id 指向 bill_task.id（uuid），要等主键生成后才能写。
            db.flush()
            write_audit(
                db, task.id, user_id, "TASK_CREATED",
                {"bank": bank_code, "period": f"{period_start} ~ {period_end}"},
            )
            return self._task_view(task)

    def confirm_application(self, task_id: str, user_id: str) -> dict:
        with self.database.sessions.begin() as db:
            task = self._get_task(db, task_id, user_id)
            self._require_state(task, "WAITING_APPLICATION", "请先创建任务")
            task.state = "WAITING_UPLOAD"
            write_audit(db, task.id, user_id, "APPLICATION_CONFIRMED", {"next_step": "等待上传文件"})
            return self._task_view(task)

    def save_upload(self, task_id: str, user_id: str, data: bytes, filename: str) -> dict:
        settings = get_bill_settings()
        limit = settings.bill_max_file_size_mb * 1024 * 1024
        if len(data) > limit:
            raise_bill_error(FILE_TOO_LARGE, f"文件过大，最大 {settings.bill_max_file_size_mb}MB")

        # 算的是「内容」指纹而非原始字节：同一份账单经 WPS 另存后字节会变而数据不变，
        # 按字节算会让重复上传绕过幂等。见 app.services.bill_content_hash。
        file_hash = content_file_hash(data)
        suffix = Path(filename).suffix or ""
        safe_name = f"{uuid.uuid4().hex}{suffix}"
        task_dir = Path(settings.bill_upload_dir) / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        file_path = task_dir / safe_name
        file_path.write_bytes(data)

        try:
            with self.database.sessions.begin() as db:
                task = self._get_task(db, task_id, user_id)
                allowed = task.state == "WAITING_UPLOAD" or (
                    task.state == "FAILED" and task.failure_code not in PASSWORD_ERROR_CODES
                )
                if not allowed:
                    raise_bill_error(INVALID_TASK_STATE, "请先确认已申请流水")
                self._remove_file(task.uploaded_file_path)
                task.uploaded_file_path = str(file_path)
                task.uploaded_file_hash = file_hash
                task.uploaded_filename = filename
                task.state = "WAITING_PASSWORD"
                task.failure_code = None
                task.failure_message = None
                # 重新上传即重置尝试预算（001 的语义是「剩余次数」，递减）。
                task.password_attempts_left = settings.bill_max_password_attempts
                write_audit(
                    db, task.id, user_id, "FILE_UPLOADED",
                    {"filename": filename, "size": len(data), "hash": file_hash},
                )
                return self._task_view(task)
        except Exception:
            file_path.unlink(missing_ok=True)
            raise

    def submit_password(self, task_id: str, user_id: str, password: str) -> dict:
        if not password or not password.strip():
            raise_bill_error(PASSWORD_REQUIRED, "密码不能为空")
        return self._enter_extracting(
            task_id, user_id, "PASSWORD_SUBMITTED", {"has_password": True}
        )

    def begin_processing(self, task_id: str, user_id: str) -> dict:
        """不提供密码直接进入处理（一次性上传接口用）。

        PDF / 压缩包未必加密，此时无需密码；若实际需要密码，附件处理器会抛
        PASSWORD_REQUIRED，任务回到 WAITING_PASSWORD，前端再走分步的密码接口即可。
        """

        return self._enter_extracting(
            task_id, user_id, "PROCESSING_STARTED", {"has_password": False}
        )

    def _enter_extracting(self, task_id: str, user_id: str, step: str, detail: dict) -> dict:
        with self.database.sessions.begin() as db:
            task = self._get_task(db, task_id, user_id)
            if task.state != "WAITING_PASSWORD":
                raise_bill_error(INVALID_TASK_STATE, "请先上传文件")
            if task.password_attempts_left <= 0:
                raise_bill_error(PASSWORD_ATTEMPTS_EXCEEDED, "密码尝试次数已达上限")
            task.state = "EXTRACTING"
            task.failure_code = None
            task.failure_message = None
            write_audit(db, task.id, user_id, step, detail)
            return self._task_view(task)

    def cancel_task(self, task_id: str, user_id: str) -> dict:
        with self.database.sessions.begin() as db:
            task = self._get_task(db, task_id, user_id)
            if task.state in {"COMPLETED", "FAILED", "CANCELLED"}:
                raise_bill_error(INVALID_TASK_STATE, "任务已结束，无法取消")
            task.state = "CANCELLED"
            task.completed_at = utcnow()
            write_audit(db, task.id, user_id, "TASK_CANCELLED", {"by_user": True})
            path = task.uploaded_file_path
            view = self._task_view(task)
        self._remove_file(path)
        return view

    # ------------------------------------------------------------------ 查询

    def get_task(self, task_id: str, user_id: str) -> dict:
        with self.database.sessions() as db:
            task = self._get_task(db, task_id, user_id)
            return self._task_view(task)

    def get_audit(self, task_id: str, user_id: str) -> dict:
        with self.database.sessions() as db:
            task = self._get_task(db, task_id, user_id)
            rows = db.scalars(
                select(BillTaskAudit)
                .where(BillTaskAudit.task_id == task.id)
                .order_by(BillTaskAudit.created_at)
            ).all()
            return {
                "task_id": task_id,
                "events": [
                    {
                        "timestamp": r.created_at.isoformat(),
                        "event_type": r.step,
                        "details": r.detail,
                    }
                    for r in rows
                ],
            }

    def get_summary(self, task_id: str, user_id: str) -> dict:
        with self.database.sessions() as db:
            task = self._get_task(db, task_id, user_id)
            summary = db.scalar(select(BillSummary).where(BillSummary.task_id == task.id))
            if summary is None:
                raise_bill_error(NOT_FOUND, "摘要尚未生成")
            return {
                "task_id": task_id,
                "period_start": summary.period_start.isoformat(),
                "period_end": summary.period_end.isoformat(),
                "currency": summary.currency,
                "credit_minor": summary.credit_total,
                "debit_minor": summary.debit_total,
                "net_minor": summary.net_total,
                "record_count": summary.record_count,
                # 支出分类的**金额聚合**（消费口径，只有 DEBIT）。
                "categories": summary.categories,
                # **全部出现过的分类名**（含收入）。它是明细筛选的选项来源：`categories`
                # 只聚合支出，用它做下拉会漏掉只在收入上出现的分类，用户便筛不到。
                # 这里现算而不进 BillSummary 表，是为了避免为一个筛选选项加列与迁移。
                "category_names": self._category_names(db, latest_document_for(db, task)),
                "quality_issues": summary.quality_issues,
            }

    @staticmethod
    def _category_names(db, doc) -> list[str]:
        """该账单里出现过的分类名（去重、排序）。走 (statement_document_id, ...) 索引。"""

        if doc is None:
            return []
        rows = db.scalars(
            select(Transaction.category)
            .where(
                Transaction.statement_document_id == doc.id,
                Transaction.category.is_not(None),
            )
            .distinct()
            .order_by(Transaction.category)
        ).all()
        return [name for name in rows if name]

    def get_transactions(
        self,
        task_id: str,
        user_id: str,
        page: int,
        page_size: int,
        *,
        category: str | None = None,
        direction: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict:
        """分页查询交易明细，可选按分类 / 方向 / 日期区间筛选。

        日期区间是在账单周期内再收窄，不是替换它：起止都按闭区间比较。
        `total` 与 `items` 用同一组筛选条件，否则翻页到末页会出现空页。
        """

        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        if direction is not None and direction not in {"debit", "credit"}:
            raise_bill_error(VALIDATION, "方向只支持 debit 或 credit")
        if date_from and date_to and date_to < date_from:
            raise_bill_error(VALIDATION, "结束日期不能早于开始日期")

        with self.database.sessions() as db:
            task = self._get_task(db, task_id, user_id)
            doc = latest_document_for(db, task)
            if doc is None:
                return {"total": 0, "page": page, "page_size": page_size, "items": []}

            filters = [Transaction.statement_document_id == doc.id]
            if category:
                filters.append(Transaction.category == category)
            if direction:
                filters.append(Transaction.direction == direction)
            if date_from:
                filters.append(Transaction.booked_date >= date_from)
            if date_to:
                filters.append(Transaction.booked_date <= date_to)

            total = db.scalar(select(func.count()).select_from(Transaction).where(*filters))
            rows = db.scalars(
                select(Transaction)
                .where(*filters)
                .order_by(Transaction.booked_date.desc(), Transaction.source_row_index)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "total": total or 0,
                "page": page,
                "page_size": page_size,
                "items": [_tx_view(tx) for tx in rows],
            }

    # ------------------------------------------------------------------ 内部

    def _get_task(self, db, task_id: str, user_id: str):
        task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
        if task is None:
            raise_bill_error(NOT_FOUND, "任务不存在")
        if task.user_id != user_id:
            raise_bill_error(PERMISSION_DENIED, "无权访问此任务")
        return task

    def _require_state(self, task, expected: str, message: str) -> None:
        if task.state != expected:
            raise_bill_error(INVALID_TASK_STATE, message)

    @staticmethod
    def _task_view(task) -> dict:
        settings = get_bill_settings()
        return {
            "task_id": task.task_no,
            "state": task.state,
            "bank_code": task.bank_code,
            # 解析完成前是 None：前端据此显示「待解析」，而不是编一个日期。
            "period_start": task.period_start.isoformat() if task.period_start else None,
            "period_end": task.period_end.isoformat() if task.period_end else None,
            "source_mode": task.source_mode,
            # Exposed raw rather than pre-computed: the client shows "剩余 N 次" from the pair,
            # and the limit is configuration, so neither side has to guess the other's default.
            # 库里存的是「剩余」，对外仍报「已用」以保持既有契约。
            "password_attempts": settings.bill_max_password_attempts - task.password_attempts_left,
            "max_password_attempts": settings.bill_max_password_attempts,
            "progress": PROGRESS.get(task.state, {"current_step": task.state, "percentage": 0}),
            "allowed_actions": allowed_actions(task.state, task.failure_code),
            "error": {"code": task.failure_code, "message": task.failure_message}
            if task.failure_code
            else None,
        }

    @staticmethod
    def _remove_file(path: str | None) -> None:
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
