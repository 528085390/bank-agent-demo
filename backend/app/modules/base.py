"""What a domain module declares, and what its handlers must return.

A module owns one domain: its declaration, its tools and the handlers behind them. The
registry collects modules; nothing here knows another module exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from app.agents.declaration import DomainDeclaration
from app.tools.protocol import ErrorCode, ToolInvocation, ToolSpec

if TYPE_CHECKING:  # runtime imports would drag the transport into every module import
    from app.integration.bank_client import BankClient
    from app.integration.write_facade import WriteFacade
    from app.notification.service import NotificationService
    from app.storage.database import Database


@dataclass(frozen=True, slots=True)
class ToolData:
    """A verified tool result, in the one shape every consumer reads.

    `summary` is what the Supervisor and `subtask_end` show; `display` is what the client
    renders; `facts` holds exact values (`*_minor`, ids); `business_ids` names the records
    the turn created or read, and is what `final.business_ids` is built from.
    """

    summary: str
    display: dict[str, Any]
    facts: dict[str, Any]
    business_ids: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("ToolData.summary must be a non-empty string")
        for name in ("display", "facts", "business_ids"):
            if not isinstance(getattr(self, name), dict):
                raise ValueError(f"ToolData.{name} must be a dict")

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "display": dict(self.display),
            "facts": dict(self.facts),
            "business_ids": dict(self.business_ids),
        }


def as_envelope(value: Any, *, tool_name: str) -> dict[str, Any]:
    """Validate a handler's return value as a result envelope.

    A handler returning a bare dict is a programming error, not a business failure, so it
    raises rather than being coerced -- the same stance the Gateway takes on a malformed
    tool schema.
    """

    if not isinstance(value, ToolData):
        raise TypeError(f"tool {tool_name} returned {type(value).__name__}, expected ToolData")
    return value.as_dict()


ERROR_MESSAGES: Mapping[ErrorCode, str] = {
    ErrorCode.NO_CUSTOMER_BINDING: "当前用户未绑定银行客户号。",
    ErrorCode.NOT_FOUND: "没有找到对应的银行记录。",
    ErrorCode.INVALID_STATE: "当前业务状态不支持该操作。",
    ErrorCode.INSUFFICIENT_BALANCE: "账户余额不足。",
    ErrorCode.CONFIRMATION_REQUIRED: "已拦截：该高风险操作必须通过确认卡完成安全验证。",
    ErrorCode.VERIFICATION_FAILED: "验证码不正确。",
    ErrorCode.LOCKED: "该资金工具已锁定，请稍后重试。",
    ErrorCode.VALIDATION: "已拦截：请求参数不符合安全规范或银行要求。",
    ErrorCode.CLIENT: "银行拒绝了该请求。",
    ErrorCode.PROTOCOL: "银行返回了无法识别的响应。",
    ErrorCode.UPSTREAM_UNAVAILABLE: "银行服务暂时不可用，请稍后重试。",
    ErrorCode.OUTPUT_SCHEMA_VIOLATION: "工具内部错误。",
    ErrorCode.INTERNAL: "工具内部错误。",
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: "系统繁忙，请稍后重试。",
    ErrorCode.TRANSACTION_UNKNOWN: "这笔交易的银行结果暂时未知，请稍后按原单核实。",
    ErrorCode.TOOL_DENIED: "已拦截：该操作未获授权或已被安全策略网关阻断。",
}


class ToolFailure(Exception):
    """A business failure a module wants reported as a readable tool result.

    `retryable` defaults to False on purpose: retrying a tool that is not transient can
    repeat its effect, so an adapter has to say so explicitly.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        if code not in {member.value for member in ErrorCode}:
            # A typo here would produce a code the frontend can never branch on, and the
            # failure would look like a generic one. Fail at the raise site instead.
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


ToolHandler = Callable[["ToolInvocation", "BankClient"], ToolData]


@dataclass(frozen=True, slots=True)
class ModuleDeps:
    """The platform services a module may be handed when it is assembled.

    One process owns exactly one of each (`app/runtime.py`: one database, one
    `NotificationService`, one `WriteFacade`), and a module says it needs one by declaring a
    `binder`. The services live here rather than on the module so a module never learns where
    they came from -- and `base.py` imports none of the module packages, which is what lets
    every module package import this one back without a cycle.
    """

    notification_service: "NotificationService"
    database: "Database"
    writes: "WriteFacade"


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """One domain module. `declaration=None` means it only contributes shared tools."""

    name: str
    declaration: DomainDeclaration | None = None
    tools: Mapping[str, ToolSpec] = field(default_factory=dict)
    handlers: Mapping[str, ToolHandler] = field(default_factory=dict)
    grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # How this module turns itself into its production shape once the platform services exist.
    # `None` means it needs none and is used exactly as declared. A binder describes assembly,
    # not capability, so it stays out of the tool/handler checks in `__post_init__`.
    binder: Callable[[ModuleSpec, ModuleDeps], ModuleSpec] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("module name must be a non-empty string")
        # A tool's name is its mapping key here and `ToolSpec.name` in the catalogue, the
        # gateway, the prompt menu and the adapter. Nothing else compares the two, so a
        # mismatch would build cleanly and then fail at call time -- the catalogue would
        # offer one name while the handlers were filed under another.
        mismatched = sorted(key for key, spec in self.tools.items() if key != spec.name)
        if mismatched:
            raise ValueError(
                f"{self.name} files a tool under a name it does not carry: " + ", ".join(mismatched)
            )
        missing = sorted(set(self.tools) - set(self.handlers))
        if missing:
            raise ValueError(f"{self.name} declares tools without handlers: " + ", ".join(missing))
        extra = sorted(set(self.handlers) - set(self.tools))
        if extra:
            raise ValueError(f"{self.name} has handlers without tools: " + ", ".join(extra))
        if self.declaration is None and self.tools and not any(self.grants.values()):
            # `any(...)`, not just "is grants empty": `grants={"a": ()}` is a non-empty mapping
            # that still reaches nothing, so the tool would be declared and unreachable.
            raise ValueError(
                f"{self.name} declares tools nobody can reach: no declaration and no grants"
            )

    @property
    def effective_grants(self) -> dict[str, tuple[str, ...]]:
        """Who may call this module's tools.

        The module's own agent always reaches its own tools. `grants` exists to express
        cross-domain sharing, so declaring one is an addition and never a replacement --
        otherwise a module that shares a single tool would silently lose every tool it owns.
        """

        granted: dict[str, list[str]] = {agent: list(names) for agent, names in self.grants.items()}
        if self.declaration is not None:
            own = granted.setdefault(self.declaration.agent_id, [])
            for name in self.tools:
                if name not in own:
                    own.append(name)
        return {agent: tuple(names) for agent, names in granted.items()}


def bind_writes(module: ModuleSpec, deps: ModuleDeps) -> ModuleSpec:
    """Bind the composition root's one write facade into a module's own graph builder.

    The base (`agents/supervisor/graph.py`'s `build_domain_graph`) calls a builder by keyword
    only, so `functools.partial` takes effect directly: a module package never learns where the
    facade came from, and cannot be handed a second one.

    It lives here rather than in `modules/__init__.py` because that package imports every
    module package -- a module importing this helper back from it would be a cycle. Nothing in
    this file imports a module package.

    `dataclasses.replace`, never mutation: `ModuleSpec` is frozen and slotted, so the bound
    copy is a new object and the import-time one stays exactly as declared.
    """

    declaration = module.declaration
    return replace(
        module,
        declaration=replace(
            declaration, graph_builder=partial(declaration.graph_builder, writes=deps.writes)
        ),
    )
