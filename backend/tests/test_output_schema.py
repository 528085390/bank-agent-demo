"""A tool's `facts` shape is a contract with its consumers.

`summary`, `display` and `business_ids` are pinned by the seam. `facts` is the one field a
module designs freely -- so it is the one that needs a schema, because a shape that drifts
fails silently at the consumer rather than loudly at the producer.
"""

import pytest

from app.agents.declaration import DomainDeclaration
from app.integration.bank_client import BankClient
from app.modules.base import ERROR_MESSAGES, ModuleSpec, ToolData
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.protocol import (
    ErrorCode,
    ToolCall,
    ToolInvocation,
    ToolSpec,
    ToolStatus,
    TrustedContext,
)

DECLARATION = DomainDeclaration(agent_id="alpha", display="A", purpose="p")
CONTEXT = TrustedContext(user_id="u", session_id="s", bank_customer_id="u_zhang")

SCHEMA = {
    "type": "object",
    "properties": {"balance_minor": {"type": "integer"}},
    "required": ["balance_minor"],
}


def invoke(spec, handler):
    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=DECLARATION,
                tools={spec.name: spec},
                handlers={spec.name: handler},
            )
        ]
    )
    adapter = BankToolAdapter(registry, BankClient("http://bank.test"))
    return adapter.invoke(
        ToolInvocation(
            agent_id="alpha",
            call=ToolCall(tool_name=spec.name, arguments={}, call_id="c1"),
            context=CONTEXT,
        )
    )


def test_facts_matching_the_schema_pass():
    spec = ToolSpec(name="echo", description="e", output_schema=SCHEMA)
    result = invoke(
        spec,
        lambda i, c: ToolData(
            summary="ok", display={"blocks": []}, facts={"balance_minor": 832000}
        ),
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data["facts"] == {"balance_minor": 832000}


def test_facts_violating_the_schema_fail_loudly():
    """The shape is checked where it is produced, not where it is later consumed."""

    spec = ToolSpec(name="echo", description="e", output_schema=SCHEMA)
    result = invoke(spec, lambda i, c: ToolData(summary="ok", display={"blocks": []}, facts={}))

    assert result.status is ToolStatus.FAILED
    assert result.error_code == ErrorCode.OUTPUT_SCHEMA_VIOLATION
    # The user-facing wording comes from the shared table, not from the schema library.
    assert result.error == ERROR_MESSAGES[ErrorCode.OUTPUT_SCHEMA_VIOLATION]
    # A tool whose own output is malformed will produce the same malformed output on a
    # second attempt, so the Supervisor must not spend its one retry on it.
    assert result.retryable is False
    # The malformed facts are not forwarded alongside the failure.
    assert result.data is None


def test_a_wrong_type_in_facts_fails():
    spec = ToolSpec(name="echo", description="e", output_schema=SCHEMA)
    result = invoke(
        spec,
        lambda i, c: ToolData(
            summary="ok", display={"blocks": []}, facts={"balance_minor": "8320.00"}
        ),
    )

    assert result.error_code == ErrorCode.OUTPUT_SCHEMA_VIOLATION


def test_the_violation_log_names_the_path_and_the_rule_but_not_the_value(caplog):
    """A module's private `facts` must not reach the log either.

    jsonschema's messages embed a `repr` of the offending value, so logging the message
    would publish exactly what `facts` exists to keep in. The log still has to be usable:
    the path says which key, and the keyword says which rule it broke.
    """

    private = "PRIVATE_FACTS_VALUE_7d21"
    spec = ToolSpec(name="echo", description="e", output_schema=SCHEMA)

    with caplog.at_level("WARNING", logger="app.tools.bank_adapter"):
        result = invoke(
            spec,
            lambda i, c: ToolData(
                summary="ok", display={"blocks": []}, facts={"balance_minor": private}
            ),
        )

    assert result.error_code == ErrorCode.OUTPUT_SCHEMA_VIOLATION
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged, "a violation must be logged, or it cannot be debugged"
    assert private not in logged
    # Enough to debug with: which tool, which path, which rule.
    assert "echo" in logged
    assert "facts.balance_minor" in logged
    assert "type" in logged


def test_the_other_envelope_fields_are_not_constrained_by_the_output_schema():
    """The schema is about `facts`, and only `facts`.

    The declared key `raw` exists nowhere in the envelope -- it is inside `facts`. A check
    run against the whole envelope instead of against `facts` would therefore reject this
    perfectly good result.
    """

    spec = ToolSpec(
        name="echo",
        description="e",
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "integer"}},
            "required": ["raw"],
            "additionalProperties": False,
        },
    )
    result = invoke(
        spec,
        lambda i, c: ToolData(
            summary="ok",
            display={"blocks": []},
            facts={"raw": 1},
            business_ids={"account_id": "acc_1"},
        ),
    )

    assert result.status is ToolStatus.SUCCEEDED


def test_no_schema_means_no_check():
    """Incremental adoption: a module can add the schema later without being blocked."""

    spec = ToolSpec(name="echo", description="e")
    result = invoke(
        spec, lambda i, c: ToolData(summary="ok", display={"blocks": []}, facts={"anything": 1})
    )

    assert result.status is ToolStatus.SUCCEEDED


def test_a_bare_dict_return_still_raises_even_with_a_declared_schema():
    """`as_envelope` stays outside the `try`, and the schema check stays after it.

    A handler that returns something other than `ToolData` is a declaration error, not a
    business failure, so it must raise rather than be reported as a readable
    `OUTPUT_SCHEMA_VIOLATION` -- which would hide the declaration bug behind a plausible code.
    """

    spec = ToolSpec(name="echo", description="e", output_schema=SCHEMA)

    with pytest.raises(TypeError, match="expected ToolData"):
        invoke(spec, lambda i, c: {"balance_minor": 832000})


def test_an_invalid_schema_is_a_declaration_bug_and_is_refused_at_registry_build():
    """Same stance as the Gateway takes on `parameters`: a malformed declaration raises
    rather than silently passing everything."""

    with pytest.raises(ValueError, match="invalid output_schema"):
        build_registry(
            [
                ModuleSpec(
                    name="alpha",
                    declaration=DECLARATION,
                    tools={"echo": ToolSpec(name="echo", output_schema={"type": "nonsense"})},
                    handlers={"echo": lambda i, c: None},
                )
            ]
        )
