import asyncio
import importlib
import json
import re
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.agents.runtime import AgentEvent
from app.api.sse import EVENT_NAMES, encode_event, event_stream
from app.core.errors import (
    CONVERSATION_ERROR_MESSAGES,
    CONVERSATION_ERROR_STATUS,
    AppError,
    ConversationErrorCode,
    conversation_error,
)
from app.core.types import json_value, money, new_id
from app.storage.models import Base

CONVERSATION_TABLES = ("conversation", "conversation_message", "conversation_submission")

# The codes `conversation_error` serves. `CONTEXT_LIMIT_EXCEEDED` is the one code whose
# `retryable` is not a constant, so the fixed table cannot build it -- see its own test below.
FIXED_TABLE_CODES = tuple(
    code
    for code in ConversationErrorCode
    if code is not ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED
)


def migration_module():
    """The revision, by its file name -- which starts with a digit and is not an identifier."""

    return importlib.import_module("app.storage.migrations.versions.0007_conversation_persistence")


def constraint_names(table, kind):
    return {
        constraint.name
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, kind)
    }


@pytest.mark.parametrize(
    "value", [200, 0.1, True, "1e3", "-1.00", "1.234", "NaN", "01.00", "1,000"]
)
def test_money_rejects_non_contract_values(value):
    with pytest.raises(ValueError):
        money(value)


def test_money_rejects_trailing_newline():
    with pytest.raises(ValueError):
        money("1000.00\n")


def test_decimal_boundaries_and_json():
    assert money("8320.0") == Decimal("8320.00")
    assert json_value({"amount": Decimal("186.50")}) == {"amount": "186.50"}


def test_an_id_is_a_prefixed_random_hex_string():
    """`new_id` is the only id minting path, so its shape is a contract.

    The property the spec asks for is *unguessable*, and randomness is not something a test
    can assert. What it can assert is the shape and that two draws differ -- which is what
    "not a counter and not derived from anything else" means in practice.
    """

    first = new_id("conv")
    second = new_id("conv")

    assert re.fullmatch(r"conv_[0-9a-f]{32}", first)
    assert first != second


def test_the_stream_contract_contains_base_and_control_events():
    assert EVENT_NAMES == {
        "agent_start",
        "tool_call",
        "tool_result",
        "interrupt",
        "subtask_end",
        "reply_delta",
        "narration",
        "final",
        "plan",
        "task_update",
        "clock",
    }


def test_sse_unicode_and_multiline_are_single_json_data_frame():
    encoded = encode_event(
        AgentEvent(event="tool_result", data={"error": "余额\n不足"}), "req_test"
    )
    assert encoded.startswith("event: tool_result\n") and encoded.endswith("\n\n")
    payload = json.loads(encoded.split("data: ")[1])
    assert payload["data"]["error"] == "余额\n不足" and payload["request_id"] == "req_test"


def test_every_frame_names_its_event_and_there_is_no_unnamed_message():
    for name in sorted(EVENT_NAMES):
        assert encode_event(AgentEvent(event=name, data={}), "r").startswith(f"event: {name}\n")


@pytest.mark.parametrize(
    "removed",
    [
        "message",
        "intent",
        "step_start",
        "step_end",
        "confirmation_required",
        "mfa_required",
        "audit",
        "replan",
    ],
)
def test_retired_and_governance_events_are_not_in_the_stream_contract(removed):
    with pytest.raises(ValueError):
        encode_event(AgentEvent(event=removed, data={}), "r")


def test_the_stream_carries_values_unchanged_without_redaction():
    reference = "6222000000001234"
    encoded = encode_event(AgentEvent(event="tool_result", data={"account_ref": reference}), "r")

    assert reference in encoded


def test_stream_exception_is_an_unnamed_error_frame_never_a_final():
    async def failing():
        yield AgentEvent(event="agent_start", data={"agent_id": "transfer"})
        raise RuntimeError("sensitive backend text")

    async def collect():
        return [s async for s in event_stream(failing(), "req_stream")]

    frames = asyncio.run(collect())
    assert len(frames) == 2
    assert frames[0].startswith("event: agent_start\n")
    # The failure frame carries no event name: it is an error, not a turn result.
    assert not frames[-1].startswith("event:")
    assert "INTERNAL_ERROR" in frames[-1]
    assert "sensitive" not in frames[-1]


def test_money_columns_and_timestamps_keep_their_sql_types():
    for name in ["ledger_entry", "account"]:
        sql = str(CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
        assert "NUMERIC(18, 2)" in sql
    sql = str(
        CreateTable(Base.metadata.tables["orchestration_task"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "TIMESTAMP WITH TIME ZONE" in sql


# --- The conversation model: three tables, and deliberately no fourth -------------------


def test_the_conversation_tables_exist_with_their_documented_columns():
    expected = {
        "conversation": {
            "conversation_id",
            "user_id",
            "title",
            "pinned",
            "archived_at",
            "deleted_at",
            "created_at",
            "updated_at",
            "last_message_at",
            "next_message_sequence",
            "thread_id",
        },
        "conversation_message": {
            "message_id",
            "conversation_id",
            "run_id",
            "sequence",
            "role",
            "content",
            "message_kind",
            "question_id",
            "created_at",
            "metadata",
        },
        "conversation_submission": {
            "submission_id",
            "user_id",
            "operation_type",
            "idempotency_key",
            "request_hash",
            "conversation_id",
            "message_id",
            "run_id",
            "created_at",
        },
    }

    for table, columns in expected.items():
        assert columns <= set(Base.metadata.tables[table].columns.keys()), table


def test_no_run_projection_table_is_added():
    """The non-goal, pinned: execution state is the checkpoint's and there is no second one."""

    forbidden = {"conversation_run", "conversation_run_event", "conversation_attempt"}
    assert forbidden & set(Base.metadata.tables) == set()
    created = [call[1] for call in _upgrade_calls() if call[0] == "create_table"]
    assert set(created) == set(CONVERSATION_TABLES)


def test_the_message_sequence_and_the_submission_key_are_unique_in_the_schema():
    assert "uq_conversation_message_sequence" in constraint_names(
        "conversation_message", UniqueConstraint
    )
    assert "uq_conversation_submission_key" in constraint_names(
        "conversation_submission", UniqueConstraint
    )
    assert "ck_conversation_message_question" in constraint_names(
        "conversation_message", CheckConstraint
    )
    assert "ck_conversation_message_role" in constraint_names(
        "conversation_message", CheckConstraint
    )
    # `thread_id` is the join between a conversation and its checkpoint, so it is unique.
    assert Base.metadata.tables["conversation"].columns["thread_id"].unique is True


def test_the_ledger_sequence_is_a_bigint_and_the_metadata_a_jsonb_document():
    table = Base.metadata.tables["conversation_message"]

    assert isinstance(table.columns["sequence"].type, BigInteger)
    assert table.columns["metadata"].type.__class__.__name__ == "JSONB"
    assert table.columns["run_id"].nullable is True, "a migrated message may have no run"
    assert "updated_at" not in table.columns, "an append-only ledger has nothing to update"


def test_run_id_is_not_a_foreign_key():
    """It is a correlation id: making it one would create the run table this phase refuses."""

    for table in ("conversation_message", "conversation_submission"):
        column = Base.metadata.tables[table].columns["run_id"]
        assert column.foreign_keys == set()


def _upgrade_calls():
    """Replay `upgrade()` against a recorder, so the order is read from the migration."""

    module = migration_module()
    calls: list[tuple] = []

    class Recorder:
        def create_table(self, name, *args, **kwargs):
            calls.append(("create_table", name, args, kwargs))

        def create_index(self, name, table, *args, **kwargs):
            calls.append(("create_index", name, table))

    original = module.op
    module.op = Recorder()
    try:
        module.upgrade()
    finally:
        module.op = original
    return calls


def test_the_migration_creates_the_three_tables_in_dependency_order():
    created = [name[1] for name in _upgrade_calls() if name[0] == "create_table"]

    assert created == list(CONVERSATION_TABLES)


def test_the_migration_declares_the_same_uniques_and_checks_as_the_models():
    """A model and a migration that disagree leave a schema nobody tested."""

    by_table: dict[str, list] = {}
    for call in _upgrade_calls():
        if call[0] != "create_table":
            continue
        _, name, args, kwargs = call
        by_table[name] = list(args) + list(kwargs.get("args", []))

    declared_uniques = {
        item.name for item in by_table["conversation_message"] if isinstance(item, UniqueConstraint)
    }
    assert "uq_conversation_message_sequence" in declared_uniques
    declared_submission = {
        item.name
        for item in by_table["conversation_submission"]
        if isinstance(item, UniqueConstraint)
    }
    assert "uq_conversation_submission_key" in declared_submission
    checks = {
        item.name for item in by_table["conversation_message"] if isinstance(item, CheckConstraint)
    }
    assert {"ck_conversation_message_role", "ck_conversation_message_question"} <= checks


def test_the_migration_downgrades_in_reverse_dependency_order():
    """A submission points at a message, both point at the conversation: reverse that."""

    module = migration_module()
    calls: list[tuple] = []

    class Recorder:
        def drop_table(self, name):
            calls.append(("drop_table", name))

        def drop_index(self, name, table_name=None):
            calls.append(("drop_index", name))

    original = module.op
    module.op = Recorder()
    try:
        module.downgrade()
    finally:
        module.op = original

    assert [name[1] for name in calls if name[0] == "drop_table"] == [
        "conversation_submission",
        "conversation_message",
        "conversation",
    ]


# --- The conversation error vocabulary --------------------------------------------------


def test_every_conversation_error_code_has_exactly_one_message_and_status():
    """Both directions, as the tool layer does: a missing key is wording nobody sees, a stale
    one is a code nothing can carry."""

    assert set(CONVERSATION_ERROR_MESSAGES) == set(ConversationErrorCode)
    assert set(CONVERSATION_ERROR_STATUS) == set(ConversationErrorCode)
    for code in ConversationErrorCode:
        assert CONVERSATION_ERROR_MESSAGES[code].strip()
        assert 400 <= CONVERSATION_ERROR_STATUS[code] < 600


def test_the_conversation_codes_are_the_frozen_eight():
    """The client branches on these strings, so the set is pinned rather than derived.

    The eighth is not drift: 持久化 spec §0.1 修订记录 records "错误码由七个扩为八个", its §4
    now reads 错误码固定为八个, and context-management spec §12 calls this 唯一需要改动"已冻结"
    文档的地方. The frozen property is that the set is written down here, not that it never grew.
    """

    assert {code.value for code in ConversationErrorCode} == {
        "IDEMPOTENCY_KEY_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "CONVERSATION_NOT_WRITABLE",
        "INVALID_INTERRUPT_ANSWER",
        "EXECUTION_RECOVERY_REQUIRED",
        "PERSISTENCE_FAILED",
        "NEW_CONVERSATION_REQUIRED",
        "CONTEXT_LIMIT_EXCEEDED",
    }


@pytest.mark.parametrize("code", FIXED_TABLE_CODES)
def test_the_error_factory_carries_the_code_its_message_and_its_status(code):
    error = conversation_error(code)

    assert isinstance(error, AppError)
    assert error.code == code.value
    assert error.message == CONVERSATION_ERROR_MESSAGES[code]
    assert error.status == CONVERSATION_ERROR_STATUS[code]
    assert error.retryable is (code is ConversationErrorCode.PERSISTENCE_FAILED)


def test_the_error_factory_refuses_the_code_whose_retryability_is_not_a_constant():
    """`CONTEXT_LIMIT_EXCEEDED` carries `retryable` in `details.reason`, so the fixed table
    cannot build it -- it must be refused loudly, not built with the wrong retryability."""

    with pytest.raises(ValueError, match="context_limit_error"):
        conversation_error(ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED)
