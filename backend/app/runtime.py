"""The application's long-lived services.

This is the composition root's data holder and nothing more. It used to carry a control
plane, a task repository and an in-memory conversation buffer; the multi-agent runtime
replaced all three, so what remains is the database, the identity service, the email
notification service and the LangGraph agent runtime that owns the conversation.

The graph itself is built per invocation inside `AgentRuntime`, because a domain
subgraph binds one caller's trusted identity into its tool handle. The gateway and its
catalog are therefore stateless and shared.

The gateway is assembled from the module registry, so the tools the model may call are the
ones the modules declared. The bank client it carries is held here as well, because the
connection pool it owns has to be closed when the application stops.
"""

from __future__ import annotations

from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime, TurnContext
from app.audit.context import summary_context
from app.audit.fund_writes import build_fund_write_audit_hook
from app.audit.service import AuditService, RunEventType
from app.context.budget import Budget
from app.context.view import ContextAssembler
from app.conversation.service import ConversationService
from app.conversation.turns import ConversationTurns
from app.identity.pool import PoolRefiller
from app.identity.service import Identity
from app.integration.bank_client import BankClient
from app.integration.model_client import ModelClient, UnconfiguredModelClient
from app.integration.write_facade import WriteFacade
from app.notification.service import NotificationService
from app.storage.database import Database
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext


def build_tool_gateway(registry=None, client=None, audit=None) -> ToolGateway:
    """The production gateway: every registered module's tools, behind the bank adapter."""

    from app.modules.registry import build_registry
    from app.tools.bank_adapter import BankToolAdapter

    resolved = registry or build_registry()
    return ToolGateway(
        resolved.catalog,
        BankToolAdapter(resolved, client or BankClient.from_settings()),
        audit=audit,
    )


def agent_graph_factory(
    gateway: ToolGateway,
    *,
    client: BankClient | None = None,
    roster=None,
):
    """Bind a caller's identity into the graph LangGraph will run for that caller.

    The assembler and the turn are built per invocation by `AgentRuntime._graph_for`; this
    factory only carries them the rest of the way, because `app/context` must not import
    `app.agents` and the two values the assembler needs live on the turn.
    """

    def factory(
        model: ModelClient,
        context: TrustedContext,
        assembler: ContextAssembler | None = None,
        turn: TurnContext | None = None,
    ):
        return build_agent_graph(
            model,
            gateway,
            context,
            assembler=assembler,
            turn=turn,
            client=client,
            roster=roster,
        )

    return factory


class Runtime:
    """Database, identity, notifications and the agent runtime."""

    def __init__(self, settings, *, database=None, agent=None, client=None, audit=None) -> None:
        # Imported here, not at module scope, so importing `app.runtime` does not drag every
        # module in; `build_tool_gateway` does the same thing for the same reason.
        from app.modules import production_modules
        from app.modules.registry import build_registry

        self.settings = settings
        self.database = database or Database(settings.database_url)
        # Held here as well as inside the adapter: the pool has to be closed at shutdown,
        # and the gateway does not hand its adapter's client back out.
        self.bank_client = client or BankClient.from_settings()
        # Identity is built after the client because registration now tops the customer pool
        # back up through it. The refiller is best-effort by construction: a registration
        # that cannot reach the bank still succeeds, it just leaves the pool one short.
        self.identity = Identity(
            self.database, settings, refiller=PoolRefiller(self.database, self.bank_client)
        )
        self.notification = NotificationService(self.database, settings)
        # One audit writer for the whole process, shared by the model boundary and the tool
        # gateway: that is what makes a model call and the tool calls it caused share a run.
        self.audit = audit or AuditService(self.database)
        # The write facade is held here, next to the client it wraps: transfer and wealth
        # both receive this one object, so its policy, audit hook and daily-total store exist
        # once per process rather than once per module. The audit hook writes one row per fund
        # write into the business trail; the database goes with the client.
        self.writes = WriteFacade(
            self.bank_client,
            audit_hook=build_fund_write_audit_hook(self.audit),
            database=self.database,
        )
        # Built once and shared: `build_tool_gateway` would otherwise build a second registry,
        # and the catalogue and the roster must come from the same module set.
        # 平台服务在这里注入：`build_registry` 只认识 ModuleSpec，装配归组合根。
        registry = build_registry(
            production_modules(
                notification_service=self.notification,
                database=self.database,
                writes=self.writes,
            )
        )
        self.gateway = build_tool_gateway(
            registry=registry, client=self.bank_client, audit=self.audit
        )
        # An injected runtime keeps its own agent, including an in-memory checkpointer.
        self.agent = agent or AgentRuntime(
            UnconfiguredModelClient(),
            # The gateway registry is the bound roster (production_modules applied
            # each module binder); the import-time DOMAIN_ROSTER is not. Passing it
            # here hands writes and database to a module graph builder -- without it
            # a transfer graph builds a second facade with no audit hook and no
            # daily-total store, so the 1,000-yuan cap silently stops working.
            graph_factory=agent_graph_factory(
                self.gateway, client=self.bank_client, roster=registry.roster
            ),
            customer_resolver=self.identity.bank_customer_id,
            # Built per invocation and never shared: it reads the ledger and may write a
            # summary version. `turn is None` is the session path, which has no conversation
            # to summarize -- the assembler still reports that degraded assembly.
            assembler_factory=lambda user_id, turn, model: self.build_context_assembler(
                user_id, turn, model
            ),
        )
        self.llm = None
        # The ledger and the turn lifecycle. `ConversationTurns` reads `self.agent` per
        # call rather than capturing it, so a test that swaps the agent -- which the API
        # tests do routinely -- dispatches the agent it installed, not the one that happened
        # to be here at construction. The audit writer is built above, before the service
        # that needs it, and is shared: a conversation change is recorded by the same writer
        # that records the model calls a turn causes.
        self.conversations = ConversationService(self.database, audit=self.audit)
        self.turns = ConversationTurns(self.conversations, self)

    # `ConversationTurns` talks to the runtime through the two methods it actually needs,
    # which is also what keeps the dependency one-way: the service layer never imports the
    # runtime module, it is handed something shaped like it.

    def build_context_assembler(
        self,
        user_id: str,
        turn: TurnContext | None,
        model: ModelClient,
        *,
        budget: Budget | None = None,
    ) -> ContextAssembler:
        """The assembler for one invocation, for the conversation the turn belongs to.

        `turn` is not read here: the assembler is handed the conversation id and the
        sequence per `assemble()` call, and `app/context` deliberately does not know about
        `TurnContext`. It stays in the signature because this is what
        `AgentRuntime._graph_for` calls, and the turn is what identifies the invocation.

        `user_id` is the caller, and it goes into the assembler because the reads it makes now
        verify ownership: the conversation id travels per call, so the identity has to be bound
        where the assembler is built -- one assembler, one user (`app.context.ownership`).

        The recording callback passes no `context=`, so `AuditService` resolves the turn's
        own `AuditContext`: the conversation id and the run id are right, and `agent_id`
        stays empty because an assembly belongs to the conversation, not to a domain agent.
        """

        def record(payload: dict) -> None:
            self.audit.record_agent_event(
                event_type=RunEventType.CONTEXT_ASSEMBLED, payload=payload
            )

        return ContextAssembler(
            self.database,
            model,
            budget or Budget.from_settings(self.settings),
            user_id=user_id,
            record=record,
            summary_scope=summary_context,
        )

    async def admission(self, user_id, session_id, thread_id, client_context):
        return await self.agent.admission(user_id, session_id, thread_id, client_context)

    async def recovery_hint(self, user_id, session_id, thread_id):
        return await self.agent.recovery_hint(user_id, session_id, thread_id)

    def conversation_lock(self, conversation_id: str):
        return self.agent.conversation_lock(conversation_id)

    def invoke(self, *args, **kwargs):
        return self.agent.invoke(*args, **kwargs)

    @property
    def model(self) -> ModelClient:
        """The model the next turn would dispatch, unwrapped for the naming call.

        Read per call, like the rest of the turn lifecycle: a test or the settings panel may
        replace the agent after this runtime was built, and naming must use whatever the turn
        itself would have used.
        """

        return self.agent.model

    async def aclose(self) -> None:
        """Release the pool the bank client holds. The database is the caller's to dispose."""

        self.bank_client.close()
