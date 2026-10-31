"""账单入库服务：把 C 解析出的结果原子保存到数据库。

这是 A 提供给 C 的契约接口：C 调用 ``save_statement(task_id, parse_result)`` 入库，
调用 ``save_summary(task_id, summary)`` 保存摘要。C 的 ``TxRecord.amount`` 是 Decimal 元
（带符号），入库时 ×100 转成 BigInteger 分；方向由符号推导后存小写 debit/credit；
账号与户名脱敏后入表；文件哈希与记录哈希做幂等，防止重复导入。

列名跟随 001 契约（``amount`` / ``credit_total`` / ``generated_at`` 等）；``task_id`` 参数
仍是业务号字符串，落库前先解析出 ``bill_task.id``（uuid），子表用它做外键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.bill_errors import DUPLICATE_IMPORT, NOT_FOUND, raise_bill_error
from app.core.types import fingerprint, utcnow
from app.services.bill_audit import write_audit
from app.storage.bill_models import BillSummary, BillTask, StatementDocument, Transaction


@dataclass(frozen=True)
class DocumentInfo:
    bank_code: str
    file_hash: str
    filename: str
    account_holder: str
    account_no: str
    account_type: str
    period_start: date
    period_end: date
    parser_name: str = ""
    parser_version: str = ""
    statement_requested_at: datetime | None = None
    sub_branch: str | None = None
    verification_code_present: bool = False


@dataclass(frozen=True)
class TxRecord:
    booked_date: date
    amount: Decimal  # 带符号：正为收入（credit），负为支出（debit）；单位是元，入库时 ×100 转分
    currency: str
    balance: Decimal | None
    transaction_type: str
    counterparty: str
    source_page: int
    source_row_index: int
    #: 完整时刻（含时分秒）。日期列本来就带时间，只是以前在解析那一步被 `.date()` 切掉了，
    #: 导致「交易活跃时段」「夜间交易」这些分析全部在「所有交易都是 12 点」上算。
    #: 只有日期的账单这里会是当天 00:00。
    booked_at: datetime | None = None
    #: 商品/摘要列（「单车」「瘦肉汤米粉」「12306消费」）。分类会把它拼进匹配文本 ——
    #: 很多账单的对手方是个笼统的商户名，真正说明「这是干什么的」在这一列。默认空串，
    #: 没有这一列的账单不受影响。
    product: str = ""


@dataclass(frozen=True)
class ParseResult:
    document_info: DocumentInfo
    transactions: list[TxRecord] = field(default_factory=list)
    quality_issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportResult:
    success: int = 0
    skipped: int = 0
    failed: int = 0
    duplicate: bool = False
    document_id: str | None = None


def mask_account(account_no: str) -> str:
    """保留前 4 后 4，中间打码：6222********1234。"""

    value = account_no or ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def mask_name(name: str) -> str:
    """保留首字符，其余打码：张**。"""

    value = name or ""
    if not value:
        return ""
    return value[0] + "*" * (len(value) - 1)


def _to_minor(value: Decimal) -> int:
    """元 → 分：两位小数的元 ×100 后量化到整数分，避免 int() 截断丢精度。"""

    return int((value * 100).quantize(Decimal("1")))


def _record_hash(tx: TxRecord) -> str:
    """这笔交易的内容指纹：哪一刻、多少钱、跟谁。

    这三样都相同，就是同一笔订单。**不看它在文件里的位置**（``page`` / ``row``）——
    同一笔交易在任何两份账单里行号都不同，拿它们判重等于要求两份文件逐字段一致，于是
    「同一笔交易换个文件再传」永远认不出来。

    也不看 ``transaction_type``：那是账单给的分类词，两份导出对同一笔的写法未必一样。

    对手方**必须留着**：它是消费对象，不同就是不同的订单。之前那个「滴滴出行 / 杭州深度
    求索 各一行」的例子，是脱敏时手动改出来的数据差异，不代表真实场景 —— 真实场景里
    对手方不同就该是两笔。

    ``booked_at`` 有值时精确到秒（同一秒内的两笔同额同对手，实际不会发生）；只有日期
    的账单退回日期粒度。
    """

    return fingerprint(
        {
            "at": (tx.booked_at or tx.booked_date).isoformat(),
            "amount": str(tx.amount),
            "counterparty": tx.counterparty,
        }
    )


def _hashes_user_already_has(db, user_id: str, candidates: set[str]) -> set[str]:
    """该用户已入库的交易里，哈希命中 candidates 的那些。

    跨该用户的**全部**文档——不能只看本文档：同一份账单换个任务再传时，这些行早就在
    库里了，只是挂在上一份文档下。
    """

    if not candidates:
        return set()
    return set(
        db.scalars(
            select(Transaction.raw_record_hash)
            .join(BillTask, BillTask.id == Transaction.task_id)
            .where(
                BillTask.user_id == user_id,
                Transaction.raw_record_hash.in_(candidates),
            )
        ).all()
    )


def latest_document_for(db, task) -> StatementDocument | None:
    """本任务的流水文档；本任务没导入过，就回退到该用户已入库的那一份。

    「同一文件重复上传」时**写**是幂等的——不再插行，免得同一份账单被记两次账。但
    **读**不该被牵连：用户要的是那份账单的数据，不是一份 0 条的空账。回退分两步：

    1. 文件哈希命中——``bill_task.uploaded_file_hash`` 与
       ``statement_document.source_file_hash`` 同源（都由 ``content_file_hash`` 算），
       同一份文件换个任务上传时必然对得上。
    2. 上一步落空时，按「同一用户 + 期间重叠」找最近的文档。这一步覆盖内容哈希对不上的
       情形：同一份账单经 WPS 另存后 ``xl/styles.xml`` 变了、指纹跟着变，入库层按
       ``raw_record_hash`` 判为全重复而不建文档——此时只剩业务字段（用户、期间）能把
       那份已有文档找回来。

    两步都限定在 ``task.user_id`` 内：文件哈希相同不代表是同一个人，不能据此读到别人的
    账单。第三步还受第 124 行的早退保护，只对「确实上传过文件、却没建成本文档」的任务
    生效，尚未上传的新任务不会误命中上一份账单。
    """

    doc = db.scalar(
        select(StatementDocument)
        .where(StatementDocument.task_id == task.id)
        .order_by(StatementDocument.imported_at.desc())
    )
    if doc is not None or not task.uploaded_file_hash:
        return doc
    doc = db.scalar(
        select(StatementDocument)
        .join(BillTask, BillTask.id == StatementDocument.task_id)
        .where(
            StatementDocument.source_file_hash == task.uploaded_file_hash,
            BillTask.user_id == task.user_id,
        )
        .order_by(StatementDocument.imported_at.desc())
    )
    if doc is not None:
        return doc
    # 任务周期是可空的（0018）：上传时不再让用户填日期，回填发生在解析成功之后。
    # 没有周期就没有「重叠」这个概念 —— 这时宁可返回 None，也不拿 NULL 去做比较，
    # 让一次意外的命中把别人的账单当成本任务的。
    if task.period_start is None or task.period_end is None:
        return None
    return db.scalar(
        select(StatementDocument)
        .join(BillTask, BillTask.id == StatementDocument.task_id)
        .where(
            BillTask.user_id == task.user_id,
            StatementDocument.period_start <= task.period_end,
            StatementDocument.period_end >= task.period_start,
        )
        .order_by(StatementDocument.imported_at.desc())
    )


def document_for_task(db, task_no: str, user_id: str) -> StatementDocument | None:
    """任务号 → 这份任务对应的账单文档；任务不存在或不属于该用户时返回 ``None``。

    这是「分析刚导入的这份流水」的入口：调用方拿到文档后，按 ``document.id`` 取数、
    按文档自己的真实起止作为报告期间 —— 而不是用户在上传时填/猜的区间。
    任务号来自模型转述的消息文本，所以归属校验必须在这里做：别人的任务号不能读到别人的账单。
    """

    task = db.scalar(select(BillTask).where(BillTask.task_no == task_no))
    if task is None or task.user_id != user_id:
        return None
    return latest_document_for(db, task)


def _require_task(db, task_id: str) -> BillTask:
    task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
    if task is None:
        raise_bill_error(NOT_FOUND, "任务不存在")
    return task


def _document_has_transactions(db, document_id) -> bool:
    """这个文档里是否已经有交易——用来识别“上次解析 0 条留下的空占位”。"""

    return db.scalar(
        select(Transaction.id)
        .where(Transaction.statement_document_id == document_id)
        .limit(1)
    ) is not None


class BillImportService:
    def __init__(self, database):
        self.database = database

    def _existing_document(self, db, task: BillTask) -> StatementDocument | None:
        """该用户已入库的同文件文档——文件级去重的唯一入口。

        ``bill_task.uploaded_file_hash`` 与解析结果里的 ``file_hash`` 同源（都由
        ``content_file_hash`` 算，见 bill_processing 的回填），查它等于查这一次上传的文件。

        这里也是竞态兜底的入口：INSERT 撞上 ``uq_statement_document_user_file`` 之后再查一次，
        命中说明另一个请求刚刚赢了，按重复处理；仍未命中的才是真的重复导入。
        """

        return db.scalar(
            select(StatementDocument)
            .join(BillTask, BillTask.id == StatementDocument.task_id)
            .where(
                StatementDocument.source_file_hash == task.uploaded_file_hash,
                BillTask.user_id == task.user_id,
            )
        )

    def save_statement(self, task_id: str, parse_result: ParseResult) -> ImportResult:
        """原子保存一条流水文档及其交易记录。

        幂等分两层：

        - **文件级**：内容指纹命中该用户已入库的文档——这份文件原样传过了，直接返回。
        - **记录级**：逐行按 ``raw_record_hash`` 与该用户**已有**的交易比对，不只本文档。
          跨文档是必要的：同一份账单换个任务再传时，文件指纹可能因另存而变
          （见 ``bill_content_hash``），但每行的内容哈希不会变。

        全部行都已有 → 不建文档，免得重复上传留下一份空账；部分已有 → 建文档，但**只插
        新增行**，旧行不再入库。
        """

        info = parse_result.document_info
        with self.database.sessions.begin() as db:
            task = _require_task(db, task_id)
            existing = self._existing_document(db, task)
            doc: StatementDocument | None = None
            if existing is not None:
                # 上次解析出 0 条时留下了一个空占位文档；解析器修好后重传同一文件，
                # 不能让空文档继续把它判成“重复”。其余情况仍然是真重复。
                if not parse_result.transactions or _document_has_transactions(db, existing.id):
                    return ImportResult(
                        duplicate=True,
                        skipped=len(parse_result.transactions),
                        document_id=str(existing.id),
                    )
                doc = existing

            pairs = [(_record_hash(tx), tx) for tx in parse_result.transactions]
            batch_hashes = {raw_hash for raw_hash, _ in pairs}
            already_have = _hashes_user_already_has(db, task.user_id, batch_hashes)
            if batch_hashes and batch_hashes <= already_have:
                return ImportResult(
                    duplicate=True, skipped=len(parse_result.transactions)
                )
            if doc is not None:
                # 复用空占位：把文档元数据更新为这次解析的结果，并让它归到当前任务。
                doc.task_id = task.id
                doc.bank_code = info.bank_code
                doc.original_filename = info.filename
                doc.account_holder_masked = mask_name(info.account_holder)
                doc.account_no_masked = mask_account(info.account_no)
                doc.account_type = info.account_type
                doc.statement_requested_at = info.statement_requested_at
                doc.sub_branch = info.sub_branch
                doc.verification_code_present = info.verification_code_present
                doc.period_start = info.period_start
                doc.period_end = info.period_end
                doc.parser_name = info.parser_name
                doc.parser_version = info.parser_version
                doc.imported_at = utcnow()
            else:
                doc = StatementDocument(
                    task_id=task.id,
                    user_id=task.user_id,
                    bank_code=info.bank_code,
                    source_file_hash=info.file_hash,
                    original_filename=info.filename,
                    account_holder_masked=mask_name(info.account_holder),
                    account_no_masked=mask_account(info.account_no),
                    account_type=info.account_type,
                    statement_requested_at=info.statement_requested_at,
                    sub_branch=info.sub_branch,
                    verification_code_present=info.verification_code_present,
                    period_start=info.period_start,
                    period_end=info.period_end,
                    parser_name=info.parser_name,
                    parser_version=info.parser_version,
                    imported_at=utcnow(),
                )
                try:
                    # 唯一约束是 (user_id, source_file_hash)：去重查询与 INSERT 之间仍有竞态窗口，
                    # 两个请求可能都查空、都插同一份文件。用 SAVEPOINT 兜住输的那一次——直接吞掉
                    # IntegrityError 会把整个事务留在 aborted 状态，随后的重查会以
                    # InFailedSqlTransaction 收场，又变成 INTERNAL。
                    with db.begin_nested():
                        db.add(doc)
                        db.flush()
                except IntegrityError:
                    existing = self._existing_document(db, task)
                    if existing is not None:
                        return ImportResult(
                            duplicate=True,
                            skipped=len(parse_result.transactions),
                            document_id=str(existing.id),
                        )
                    raise_bill_error(DUPLICATE_IMPORT, "该账单文件已导入过")

            success = 0
            skipped = 0
            for raw_hash, tx in pairs:
                # already_have 装着「库里已有的」，循环里又累积「本批已插入的」——
                # 同一行在一批里出现两次，第二次同样算跳过。
                if raw_hash in already_have:
                    skipped += 1
                    continue
                already_have.add(raw_hash)
                db.add(
                    Transaction(
                        statement_document_id=doc.id,
                        task_id=task.id,
                        booked_date=tx.booked_date,
                        booked_at=tx.booked_at,
                        currency=tx.currency,
                        amount=_to_minor(abs(tx.amount)),
                        direction="credit" if tx.amount >= 0 else "debit",
                        balance_after=_to_minor(tx.balance) if tx.balance is not None else None,
                        transaction_type=tx.transaction_type,
                        counterparty=tx.counterparty,
                        product=tx.product,
                        source_page=tx.source_page,
                        source_row_index=tx.source_row_index,
                        raw_record_hash=raw_hash,
                        created_at=utcnow(),
                    )
                )
                success += 1

            task.state = "ANALYZING"
            write_audit(db, task.id, task.user_id, "IMPORT_DONE", {"records": success})

            return ImportResult(success=success, skipped=skipped, document_id=str(doc.id))

    def save_summary(self, task_id: str, summary: dict) -> dict:
        """保存账单摘要并完成任务（幂等 upsert）。"""

        now = utcnow()
        with self.database.sessions.begin() as db:
            task = _require_task(db, task_id)
            existing = db.scalar(select(BillSummary).where(BillSummary.task_id == task.id))
            # 左边是 001 的列名，右边是本模块对外契约的键名（仍是 *_minor，明示单位是分）。
            values = dict(
                period_start=summary["period_start"],
                period_end=summary["period_end"],
                currency=summary.get("currency", "CNY"),
                credit_total=int(summary["credit_minor"]),
                debit_total=int(summary["debit_minor"]),
                net_total=int(summary["net_minor"]),
                record_count=int(summary["record_count"]),
                categories=summary.get("categories", []),
                quality_issues=summary.get("quality_issues", []),
                generated_at=now,
            )
            if existing is not None:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                db.add(BillSummary(task_id=task.id, **values))

            task.state = "COMPLETED"
            task.completed_at = now
            task.failure_code = None
            task.failure_message = None
            write_audit(db, task.id, task.user_id, "TASK_COMPLETED", {})
        return summary
