"""账单分析域的数据模型。

表名与列名以 ``docs/contracts/sql/001_init.sql``（由迁移 0001 执行）为基准：001 是这些
表的权威定义，本模块的 ORM 向它靠拢。金额一律用 ``BigInteger`` 整数分，方向（收/支）用
``debit``/``credit`` 小写；完整账号与密码不进表，只存脱敏版本或哈希。

与 ``app.storage.models`` 共享同一个 ``Base``，因此随 Alembic 迁移链一起建表。

有三张表（``bill_email_candidate`` / ``bill_category_rule`` / ``bill_idempotency_key``）
本模块只声明、不写业务代码：``alembic/env.py`` 用 ``Base.metadata`` 做 ``target_metadata``，
不声明它们会让 ``--autogenerate`` 建议删表。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.models import Base, Record

#: 账单任务的合法状态。001 的 12 态 + 我方流程必需的 WAITING_UPLOAD。
BILL_STATE_CONSTRAINT = (
    "state IN ('WAITING_APPLICATION','WAITING_UPLOAD','WAITING_PASSWORD','FETCHING',"
    "'WAITING_EMAIL','WAITING_SELECTION','EXTRACTING','PARSING','SAVING','ANALYZING',"
    "'COMPLETED','FAILED','CANCELLED')"
)

#: 审计事件的三种结果（001 的 CHECK 取值）。
AUDIT_RESULT_CONSTRAINT = "result IN ('SUCCESS','FAILURE','SKIPPED')"


class BillTask(Record, Base):
    __tablename__ = "bill_task"
    __table_args__ = (
        CheckConstraint(BILL_STATE_CONSTRAINT),
        CheckConstraint("source_mode IN ('EMAIL','LOCAL_SAMPLE','manual_upload')"),
        CheckConstraint("password_attempts_left BETWEEN 0 AND 3"),
        CheckConstraint("period_start <= period_end"),
        Index("ix_bill_task_user_created", "user_id", "created_at"),
    )
    task_no: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    bank_code: Mapped[str] = mapped_column(String(16))
    #: 账单周期。上传时不再要求用户填写（见迁移 0018）：解析完成后由
    #: ``bill_processing`` 用 ``statement_document`` 的真实起止回填。未解析完的任务
    #: 这两个值是 NULL —— 分析、明细都按文档取数，不依赖它们。
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(32), default="WAITING_APPLICATION")
    source_mode: Mapped[str] = mapped_column(String(32), default="manual_upload")
    #: 剩余尝试次数，递减。001 的列名与语义，初值见 BILL_MAX_PASSWORD_ATTEMPTS。
    password_attempts_left: Mapped[int] = mapped_column(SmallInteger, default=3)
    uploaded_file_path: Mapped[str | None] = mapped_column(Text)
    uploaded_file_hash: Mapped[str | None] = mapped_column(String(64))
    uploaded_filename: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    #: 由 001 的 trg_bill_task_set_updated_at 触发器维护，Python 侧不赋值。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StatementDocument(Record, Base):
    __tablename__ = "statement_document"
    __table_args__ = (
        # 文件层去重是「每个用户一份」：bill_import 的查询按 BillTask.user_id 过滤，
        # 全局唯一会让第二个用户上传同一份文件时直接撞约束。
        UniqueConstraint("user_id", "source_file_hash", name="uq_statement_document_user_file"),
        Index("ix_statement_document_task_id", "task_id"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bill_task.id"))
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bank_code: Mapped[str] = mapped_column(String(16))
    source_file_hash: Mapped[str] = mapped_column(String(64))
    original_filename: Mapped[str] = mapped_column(Text)
    account_holder_masked: Mapped[str] = mapped_column(Text)
    account_no_masked: Mapped[str] = mapped_column(String(32))
    account_type: Mapped[str] = mapped_column(String(20))
    statement_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sub_branch: Mapped[str | None] = mapped_column(Text)
    verification_code_present: Mapped[bool] = mapped_column(Boolean, default=False)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    parser_name: Mapped[str] = mapped_column(String(64), default="")
    parser_version: Mapped[str] = mapped_column(String(32), default="")
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Transaction(Record, Base):
    __tablename__ = "bill_transaction"
    __table_args__ = (
        UniqueConstraint("statement_document_id", "raw_record_hash"),
        CheckConstraint("amount >= 0"),
        CheckConstraint("direction IN ('debit','credit')"),
        CheckConstraint("category_source IS NULL OR category_source IN ('RULE','MANUAL','DEFAULT')"),
        Index("ix_transaction_doc_date", "statement_document_id", "booked_date"),
    )
    statement_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statement_document.id")
    )
    #: 001 要求该列 NOT NULL：写入时必须提供所属任务。
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bill_task.id"))
    booked_date: Mapped[date] = mapped_column(Date)
    #: 完整时刻（含时分秒），给「交易活跃时段」「夜间交易」这类分析用。
    #: `booked_date` 是按天聚合的键，这个是按小时聚合的键。账单原文就是本地时间、
    #: 不带时区，所以这里也用 naive。老数据没有这一列的值，会是 NULL —— 时段分析
    #: 对 NULL 的按 00:00 处理，但那种数据本来就是「不知道几点」，别当成真值。
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    amount: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(6))
    balance_after: Mapped[int | None] = mapped_column(BigInteger)
    transaction_type: Mapped[str] = mapped_column(Text)
    counterparty: Mapped[str] = mapped_column(Text, default="")
    #: 商品/摘要列。对手方往往只是个笼统的商户名，「这是干什么的」常在这一列
    #: （「单车」「食堂」「12306消费」）。分类会读它，所以它得落库。老数据为空串。
    product: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(Text, default="未分类")
    category_source: Mapped[str | None] = mapped_column(String(16))
    source_page: Mapped[int] = mapped_column(Integer)
    source_row_index: Mapped[int] = mapped_column(Integer)
    raw_record_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BillTaskAudit(Base):
    """审计事件。001 的主键是 ``bigserial``，所以本模型不继承 ``Record``（那是 Uuid 主键）。

    001 的字段语义是「分步执行 + 结果」，我方原有的「事件类型 + 详情」映射过去：
    ``step`` 存原事件名（001 的 ``step`` 是 text，无 CHECK），``result`` 按语义取
    ``SUCCESS`` / ``FAILURE`` / ``SKIPPED``。
    """

    __tablename__ = "bill_audit_event"
    __table_args__ = (
        CheckConstraint(AUDIT_RESULT_CONSTRAINT),
        Index("ix_bill_audit_event_task_created", "task_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bill_task.id"))
    request_id: Mapped[str | None] = mapped_column(Text)
    step: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BillSummary(Record, Base):
    __tablename__ = "bill_summary"
    __table_args__ = (
        CheckConstraint("record_count >= 0"),
        CheckConstraint("credit_total - debit_total = net_total"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bill_task.id"), unique=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    credit_total: Mapped[int] = mapped_column(BigInteger, default=0)
    debit_total: Mapped[int] = mapped_column(BigInteger, default=0)
    net_total: Mapped[int] = mapped_column(BigInteger, default=0)
    record_count: Mapped[int] = mapped_column(Integer)
    categories: Mapped[list] = mapped_column(JSONB, default=list)
    quality_issues: Mapped[list] = mapped_column(JSONB, default=list)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 001 独有、本模块保留不用的三张表。声明它们只为让 autogenerate 保持干净。
# ---------------------------------------------------------------------------


class BillEmailCandidate(Record, Base):
    __tablename__ = "bill_email_candidate"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bill_task.id"))
    candidate_no: Mapped[str] = mapped_column(Text)
    mail_uid: Mapped[str] = mapped_column(Text)
    subject_masked: Mapped[str | None] = mapped_column(Text)
    sender_masked: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attachment_name: Mapped[str | None] = mapped_column(Text)
    attachment_size: Mapped[int | None] = mapped_column(BigInteger)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BillCategoryRule(Record, Base):
    __tablename__ = "bill_category_rule"
    priority: Mapped[int] = mapped_column(Integer, default=100)
    match_field: Mapped[str] = mapped_column(Text)
    pattern: Mapped[str] = mapped_column(Text)
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BillIdempotencyKey(Record, Base):
    __tablename__ = "bill_idempotency_key"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bill_task.id"))
    scope: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    response_snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
