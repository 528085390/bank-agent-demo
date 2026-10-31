"""The tool boundary's vocabulary.

Nothing in this package knows a business domain. A tool is a name, a description and
a JSON Schema for its arguments; domain names and domain fields arrive from the
caller's own configuration, never from these modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol


class ToolStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"


class ErrorCode(StrEnum):
    """The codes a tool failure can carry.

    These are the contract the frontend, the Supervisor and the tests branch on. They are
    stable: changing one is a breaking change, so they are spelled out here rather than
    written as literals at the call sites.
    """

    # Identity (spec 4.1.3)
    NO_CUSTOMER_BINDING = "NO_CUSTOMER_BINDING"
    # One-to-one with BankErrorKind, so a bank failure never loses its meaning
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    # 银行明确判定"验证码不对"（线路码 1008）。它与 CONFIRMATION_REQUIRED 是一对映射里的两个
    # 不同结论：前者是"这次提交的码错了"，后者只是"缺码/还需要确认"。混在一起会让运行时把一次
    # 验证失败当成"还没确认"而重走确认流程。
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    # 资金工具因验证码连续错误被锁定（线路码 1009）。它不是 INVALID_STATE：锁定是一个有明确
    # 原因、有时限、可用 /security/reset-lock 解除的状态，混进通用状态错误会让运行时丢掉原因。
    LOCKED = "LOCKED"
    VALIDATION = "VALIDATION"
    CLIENT = "CLIENT"
    PROTOCOL = "PROTOCOL"
    # Transient. It is True-retryable by construction: this code exists precisely to say
    # "this failure was worth retrying", and `run_tool` is the only thing that reads it.
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    # The tool's own misbehaviour
    OUTPUT_SCHEMA_VIOLATION = "OUTPUT_SCHEMA_VIOLATION"
    INTERNAL = "INTERNAL"
    # Write safety (spec 4.5.2)
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    # A fund write whose outcome the bank confirmed neither way, and which the lookup by the
    # original idempotency key could not resolve. It is a verdict of its own: not a success
    # (nothing may be reported as done) and not a refusal (the money may already have moved).
    # The only correct response is to say so and to re-check by the original key, never to
    # resend the write.
    TRANSACTION_UNKNOWN = "TRANSACTION_UNKNOWN"
    # Refused before reaching an adapter
    TOOL_DENIED = "TOOL_DENIED"


class SideEffect(StrEnum):
    """Whether calling a tool changes anything.

    This decides whether a transient failure may be retried. It defaults to READ, which is
    the *unsafe* direction for a forgotten declaration -- a write would silently regain its
    retry -- so the registry refuses any value outside this enum and the per-tool contract
    requires every write to say so explicitly.
    """

    READ = "READ"
    WRITE = "WRITE"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One declared tool: its existence plus the argument contract the Gateway enforces."""

    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    side_effect: str = SideEffect.READ
    # Constrains `facts`, and only `facts`: the other three envelope fields are pinned by
    # the seam. `None` means "not declared yet" -- modules adopt this incrementally, and a
    # tool that declares nothing must not be blocked because of it. Appended after
    # `side_effect` for the same reason `error_code` was appended to `ToolResult`: the
    # dataclass is frozen, so a field inserted earlier would shift every positional
    # construction site without a word.
    output_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("tool name must be a non-empty string")


@dataclass(frozen=True, slots=True)
class TrustedContext:
    """Identity the Gateway injects. It is never read from a model's output.

    `bank_customer_id` is the identity the bank knows this user by. It is `None` until the
    binding exists, and a tool must fail closed on `None` rather than omit the bank's
    `user_id` parameter -- the bank would then answer with its own default customer.
    """

    user_id: str
    session_id: str
    bank_customer_id: str | None = None


# The names the base injects, and which a model's output must therefore never be able to
# set. `call_id` and `idempotency_key` are minted by the Supervisor, `agent_id` by the
# Gateway, and `confirmed` is the seam's own refusal to let a model confirm a write on the
# user's behalf (contract 3.3 check 9). A tool that declares one as an argument invites the
# model to forge the very value that makes a write safe, so `build_registry` refuses the
# declaration rather than trusting each handler to ignore it.
RESERVED_PARAMETER_NAMES: frozenset[str] = frozenset(
    {"confirmed", "idempotency_key", "call_id", "agent_id"}
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A request that already passed the Gateway's checks."""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    # Minted once per tool call by the Supervisor, outside its retry loop, so a retry
    # reuses it. It must never come from the model: the model's output becomes
    # `arguments`, and the registry refuses a tool that declares this name as a parameter.
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_name: str
    status: ToolStatus
    call_id: str | None = None
    data: dict[str, Any] | None = None
    error: str | None = None
    # Whether this failure is transient. It defaults to False on purpose: an adapter has to
    # say a failure is retryable, because retrying one that is not can repeat its effect.
    retryable: bool = False
    # A stable, machine-readable identifier for the failure. `error` is for the person
    # reading the screen; this is for the code that has to branch on it. Appended last on
    # purpose: the dataclass is frozen, so a field inserted earlier would shift every
    # positional construction site without a word.
    error_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ToolStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """What an adapter is handed: the bound agent, the call, and the trusted identity."""

    agent_id: str
    call: ToolCall
    context: TrustedContext


class ToolAdapter(Protocol):
    """Where a verified call goes.

    This phase ships one in-memory adapter for tests. A real transport, and any
    business tool behind it, is a separate piece of work.
    """

    def invoke(self, invocation: ToolInvocation) -> ToolResult: ...
