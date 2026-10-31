-- 账单分析模块初始化 DDL（PostgreSQL 16）
-- 版本：v0.1｜日期：2026-09-16｜维护：A（后端基础与数据）
-- 说明：此基线由 backend/app/storage/migrations/versions/0001_billing_baseline.py 读取。
-- 数据库结构变更应通过 Agent 服务的 Alembic 迁移执行，不要依赖此文件中的旧库名或旧文档路径。

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. bill_task：账单任务与状态机
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_task (
    id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    task_no                 text        NOT NULL UNIQUE,
    user_id                 uuid        NOT NULL,
    session_id              text,
    bank_code               text        NOT NULL,
    period_start            date        NOT NULL,
    period_end              date        NOT NULL,
    source_mode             text        NOT NULL,
    state                   text        NOT NULL,
    password_attempts_left  smallint    NOT NULL DEFAULT 3,
    password_secret_ref     text,
    password_expires_at     timestamptz,
    failure_code            text,
    failure_message         text,
    completed_steps         jsonb       NOT NULL DEFAULT '[]'::jsonb,
    progress                jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bill_task_source_mode_check
        CHECK (source_mode IN ('EMAIL', 'LOCAL_SAMPLE')),
    CONSTRAINT bill_task_state_check
        CHECK (state IN (
            'WAITING_APPLICATION', 'WAITING_PASSWORD', 'FETCHING',
            'WAITING_EMAIL', 'WAITING_SELECTION', 'EXTRACTING',
            'PARSING', 'SAVING', 'ANALYZING',
            'COMPLETED', 'FAILED', 'CANCELLED'
        )),
    CONSTRAINT bill_task_password_attempts_check
        CHECK (password_attempts_left BETWEEN 0 AND 3),
    CONSTRAINT bill_task_period_check
        CHECK (period_start <= period_end)
);

COMMENT ON TABLE  bill_task IS '账单任务与状态机；state 取值与状态机契约一致';
COMMENT ON COLUMN bill_task.password_secret_ref IS '临时密码内存句柄，绝不存密码明文';
COMMENT ON COLUMN bill_task.user_id IS '任务归属；用户表由账户模块另行定义，本模块只保存标识';

CREATE INDEX IF NOT EXISTS idx_bill_task_user_created
    ON bill_task (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bill_task_active_state
    ON bill_task (state)
    WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED');

-- ---------------------------------------------------------------------------
-- 2. bill_email_candidate：一次取件命中的候选邮件
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_email_candidate (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id          uuid        NOT NULL REFERENCES bill_task(id) ON DELETE CASCADE,
    candidate_no     text        NOT NULL,
    mail_uid         text        NOT NULL,
    subject_masked   text,
    sender_masked    text,
    received_at      timestamptz,
    attachment_name  text,
    attachment_size  bigint,
    selected         boolean     NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bill_email_candidate_task_mail_uid_key
        UNIQUE (task_id, mail_uid),
    CONSTRAINT bill_email_candidate_task_candidate_no_key
        UNIQUE (task_id, candidate_no),
    CONSTRAINT bill_email_candidate_size_check
        CHECK (attachment_size IS NULL OR attachment_size >= 0)
);

CREATE INDEX IF NOT EXISTS idx_bill_email_candidate_task_received
    ON bill_email_candidate (task_id, received_at DESC);

-- ---------------------------------------------------------------------------
-- 3. statement_document：解析后的账单文件元数据
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS statement_document (
    id                         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                    uuid        NOT NULL REFERENCES bill_task(id) ON DELETE CASCADE,
    bank_code                  text        NOT NULL,
    source_file_hash           text        NOT NULL,
    original_filename          text,
    account_holder_masked      text,
    account_no_masked          text,
    account_type               text,
    statement_requested_at     timestamptz,
    sub_branch                 text,
    verification_code_present  boolean     NOT NULL DEFAULT false,
    period_start               date,
    period_end                 date,
    parser_name                text,
    parser_version             text,
    record_count               integer     NOT NULL DEFAULT 0,
    parse_issues               jsonb       NOT NULL DEFAULT '[]'::jsonb,
    imported_at                timestamptz NOT NULL DEFAULT now(),

    -- 注意：此全局唯一键已被 0015_bill_doc_tenant_unique 取代（它 drop 本约束、改建
    -- uq_statement_document_user_file (user_id, source_file_hash)，去重键按 owner 作用域）。
    -- 本基线保持原样，只作历史 schema 记录，勿据此判断当前去重语义。
    CONSTRAINT statement_document_source_file_hash_key
        UNIQUE (source_file_hash),
    CONSTRAINT statement_document_record_count_check
        CHECK (record_count >= 0),
    CONSTRAINT statement_document_period_check
        CHECK (period_start IS NULL OR period_end IS NULL OR period_start <= period_end)
);

COMMENT ON COLUMN statement_document.verification_code_present IS '仅记录验证码是否存在，不存验证码值';
COMMENT ON COLUMN statement_document.source_file_hash IS '附件内容 SHA-256，文件层去重键';

-- source_file_hash 的去重索引由唯一约束自带，无需重复创建。
CREATE INDEX IF NOT EXISTS idx_statement_document_task
    ON statement_document (task_id);

-- ---------------------------------------------------------------------------
-- 4. bill_transaction：标准化交易流水
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_transaction (
    id                     uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    statement_document_id  uuid          NOT NULL REFERENCES statement_document(id) ON DELETE CASCADE,
    task_id                uuid          NOT NULL REFERENCES bill_task(id) ON DELETE CASCADE,
    booked_date            date          NOT NULL,
    currency               char(3)       NOT NULL,
    amount                 numeric(18,2) NOT NULL,
    raw_amount_text        text,
    direction              text          NOT NULL,
    balance_after          numeric(18,2),
    transaction_type       text,
    counterparty           text,
    category               text          NOT NULL DEFAULT '未分类',
    category_source        text,
    source_page            integer       NOT NULL,
    source_row_index       integer       NOT NULL,
    raw_record_hash        text          NOT NULL,
    created_at             timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT bill_transaction_record_key
        UNIQUE (statement_document_id, raw_record_hash),
    CONSTRAINT bill_transaction_amount_check
        CHECK (amount >= 0),
    CONSTRAINT bill_transaction_direction_check
        CHECK (direction IN ('CREDIT', 'DEBIT')),
    CONSTRAINT bill_transaction_category_source_check
        CHECK (category_source IS NULL OR category_source IN ('RULE', 'MANUAL', 'DEFAULT')),
    CONSTRAINT bill_transaction_source_page_check
        CHECK (source_page >= 1),
    CONSTRAINT bill_transaction_source_row_check
        CHECK (source_row_index >= 0)
);

COMMENT ON COLUMN bill_transaction.amount IS '绝对值金额；方向由 direction 表达';
COMMENT ON COLUMN bill_transaction.raw_amount_text IS '原始有符号金额文本，仅用于追溯';

CREATE INDEX IF NOT EXISTS idx_bill_transaction_task_order
    ON bill_transaction (task_id, booked_date, source_page, source_row_index);

CREATE INDEX IF NOT EXISTS idx_bill_transaction_task_direction
    ON bill_transaction (task_id, direction);

CREATE INDEX IF NOT EXISTS idx_bill_transaction_task_category
    ON bill_transaction (task_id, category);

-- ---------------------------------------------------------------------------
-- 5. bill_summary：任务级摘要
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_summary (
    id              uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         uuid          NOT NULL UNIQUE REFERENCES bill_task(id) ON DELETE CASCADE,
    currency        char(3)       NOT NULL,
    period_start    date          NOT NULL,
    period_end      date          NOT NULL,
    record_count    integer       NOT NULL,
    credit_total    numeric(18,2) NOT NULL,
    debit_total     numeric(18,2) NOT NULL,
    net_total       numeric(18,2) NOT NULL,
    categories      jsonb         NOT NULL DEFAULT '[]'::jsonb,
    quality_issues  jsonb         NOT NULL DEFAULT '[]'::jsonb,
    generated_at    timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT bill_summary_record_count_check
        CHECK (record_count >= 0),
    CONSTRAINT bill_summary_net_total_check
        CHECK (credit_total - debit_total = net_total)
);

-- ---------------------------------------------------------------------------
-- 6. bill_category_rule：规则式分类配置
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_category_rule (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    priority     integer     NOT NULL DEFAULT 100,
    match_field  text        NOT NULL,
    pattern      text        NOT NULL,
    is_regex     boolean     NOT NULL DEFAULT false,
    category     text        NOT NULL,
    enabled      boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bill_category_rule_match_field_check
        CHECK (match_field IN ('TRANSACTION_TYPE', 'COUNTERPARTY', 'BOTH'))
);

CREATE INDEX IF NOT EXISTS idx_bill_category_rule_priority
    ON bill_category_rule (priority)
    WHERE enabled;

-- ---------------------------------------------------------------------------
-- 7. bill_audit_event：脱敏处理轨迹
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_audit_event (
    id           bigserial   PRIMARY KEY,
    task_id      uuid        NOT NULL REFERENCES bill_task(id) ON DELETE CASCADE,
    request_id   text,
    step         text        NOT NULL,
    result       text        NOT NULL,
    error_code   text,
    duration_ms  integer,
    detail       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bill_audit_event_result_check
        CHECK (result IN ('SUCCESS', 'FAILURE', 'SKIPPED')),
    CONSTRAINT bill_audit_event_duration_check
        CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

COMMENT ON TABLE bill_audit_event IS '禁止写入密码、邮件正文、附件原文、完整账号与完整验证码';

CREATE INDEX IF NOT EXISTS idx_bill_audit_event_task_created
    ON bill_audit_event (task_id, created_at);

-- ---------------------------------------------------------------------------
-- 8. bill_idempotency_key：幂等键与首次响应
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_idempotency_key (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id            uuid        NOT NULL REFERENCES bill_task(id) ON DELETE CASCADE,
    scope              text        NOT NULL,
    key                text        NOT NULL,
    response_snapshot  jsonb       NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bill_idempotency_key_scope_key
        UNIQUE (task_id, scope, key)
);

-- ---------------------------------------------------------------------------
-- 9. updated_at 触发器
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bill_task_set_updated_at ON bill_task;
CREATE TRIGGER trg_bill_task_set_updated_at
    BEFORE UPDATE ON bill_task
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

COMMIT;

-- ---------------------------------------------------------------------------
-- 可选：分类规则初始数据（合成示例，可按需调整或删除）
-- ---------------------------------------------------------------------------
-- INSERT INTO bill_category_rule (priority, match_field, pattern, category) VALUES
--     (10, 'TRANSACTION_TYPE', '快捷支付', '消费'),
--     (20, 'TRANSACTION_TYPE', '转账',     '转账'),
--     (30, 'COUNTERPARTY',     '示例商户', '消费');
