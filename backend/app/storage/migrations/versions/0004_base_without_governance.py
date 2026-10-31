"""Remove the governance tables the base no longer owns.

Confirmation, MFA, scheduled jobs, idempotency keys, audit events, the demo clock
and the daily risk total all belong to the deferred governance extension modules.
This revision drops them along with the columns they were the only writer of, and
narrows the task and step state constraints to the states the base can produce.

Constraint names are discovered rather than assumed: 0002 created them through
`Base.metadata.create_all`, so PostgreSQL chose the names.
"""

from alembic import op

revision = "0004_base_without_governance"
down_revision = "0003_audit_sequence"
branch_labels = None
depends_on = None

DROPPED_TABLES = (
    "audit_event",
    "idempotency_key",
    "mfa_challenge",
    "confirmation",
    "scheduled_job",
    "risk_daily_total",
    "clock_state",
)

TASK_STATES = "state IN ('RUNNING','WAITING_USER','REPLANNING','SUCCEEDED','FAILED','CANCELLED')"
STEP_STATES = "state IN ('READY','STARTED','SUCCEEDED','FAILED','CANCELLED')"

_DROP_CHECKS = """
DO $$
DECLARE constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE contype = 'c' AND conrelid = '{table}'::regclass
    LOOP
        EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;
"""

# Match the unique constraint by the exact column set it covers, so the
# single-column unique constraint on the same table is left alone.
_DROP_LEDGER_IDEMPOTENCY_UNIQUE = """
DO $$
DECLARE constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE contype = 'u' AND conrelid = 'ledger_entry'::regclass
      AND (
        SELECT array_agg(attname::text ORDER BY attname::text)
        FROM pg_attribute
        WHERE attrelid = 'ledger_entry'::regclass AND attnum = ANY(conkey)
      ) = ARRAY['idempotency_key', 'user_id'];
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE ledger_entry DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;
"""


def upgrade():
    op.execute(_DROP_CHECKS.format(table="orchestration_task"))
    op.drop_column("orchestration_task", "wait_reason")
    op.drop_column("orchestration_task", "domain_ref")
    op.create_check_constraint("orchestration_task_state_check", "orchestration_task", TASK_STATES)
    op.create_check_constraint(
        "orchestration_task_plan_version_check", "orchestration_task", "plan_version > 0"
    )

    op.execute(_DROP_CHECKS.format(table="orchestration_step"))
    op.drop_column("orchestration_step", "type")
    op.drop_column("orchestration_step", "idempotency_key")
    op.create_check_constraint("orchestration_step_state_check", "orchestration_step", STEP_STATES)
    op.create_check_constraint(
        "orchestration_step_side_effect_check",
        "orchestration_step",
        "side_effect IN ('READ','WRITE')",
    )

    op.execute(_DROP_LEDGER_IDEMPOTENCY_UNIQUE)
    op.drop_column("ledger_entry", "idempotency_key")

    op.drop_column("app_user", "failure_count")
    op.drop_column("app_user", "failure_window_start")
    op.drop_column("app_user", "locked_reason")

    for table in DROPPED_TABLES:
        op.drop_table(table)


def downgrade():
    raise NotImplementedError(
        "Governance tables are recreated by their own extension module, not by this base"
    )
