"""One LangGraph thread per authenticated session, one invocation at a time.

The process-local lock is the whole concurrency story of the first phase: a single
worker serializes turns of the same session so two requests cannot advance one
checkpoint at once. It is deliberately not a distributed lock and not a run registry.

The graph is built per invocation rather than once at construction. A domain subgraph
is compiled with an `AgentToolHandle` that carries one caller's trusted identity, so the
graph is a function of the caller and cannot be shared across sessions. What stays
long-lived is everything else: the model, the checkpointer and the per-session lock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.types import Command

from app.agents.state import goal_reset_state, new_agent_state
from app.audit.context import AGENT_ID_SUPERVISOR, SOURCE_SESSION_CHAT, AuditContext, audit_context
from app.core.errors import AppError, ConversationErrorCode, conversation_error
from app.integration.model_client import ModelClient, UnconfiguredModelClient, is_configured
from app.tools.protocol import TrustedContext

logger = logging.getLogger(__name__)

THREAD_PREFIX = "chat:"

# The exact payload of every event. A node publishes through the custom stream and this table
# is the only thing the runtime reads, so a field that is not named here is dropped -- and an
# event that is not named here raises at run time. `reply_delta` and `narration` carry the
# text of a turn that is still being written; `final` carries the whole of it, once, at the end.
EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "agent_start": ("agent_id",),
    "tool_call": ("agent_id", "tool_name", "call_id", "arguments"),
    "tool_result": ("agent_id", "tool_name", "call_id", "status", "data", "error", "error_code"),
    "interrupt": ("kind", "question_id", "agent_id", "fields", "display", "attempt"),
    # `turn_status` is the module's own status, alongside the frozen event vocabulary. The
    # event says how the subtask ended (SUCCEEDED/FAILED/DENIED); the turn status is what
    # lets the page say "待核实" for a fund write whose outcome the bank never confirmed,
    # instead of the "FAILED" that vocabulary can only offer. Absent on the publishers that
    # are not a whole-turn module, and read as "not reported" there.
    "subtask_end": ("agent_id", "status", "summary", "turn_status", "grounding_blocked"),
    "plan": ("plan_id", "nodes"),
    "task_update": ("plan_id", "node_id", "status", "artifact", "error"),
    "reply_delta": ("text",),
    "narration": ("text",),
    "final": ("text", "business_ids", "terminal_reason", "error_status"),
}


def thread_id_for_session(session_id: str) -> str:
    """The only place a thread id is derived. The client never sees one."""

    return f"{THREAD_PREFIX}{session_id}"


def resume_value(client_context: Mapping[str, Any] | None) -> tuple[bool, Any]:
    """Read `client_context.resume` as the whole envelope.

    The envelope is handed to the graph intact. `question_id` is what binds the answer to
    the question it belongs to, so unwrapping it here would defeat the check the graph is
    about to make; the graph is where the binding is verified.
    """

    resume = (client_context or {}).get("resume")
    if isinstance(resume, Mapping) and "value" in resume:
        return True, dict(resume)
    return False, None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """An internal event. SSE encoding strips anything the client must not see."""

    event: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TurnContext:
    """One owner-checked turn: which conversation, which thread, which ledger message.

    The runtime no longer derives a thread from a session for this path: the thread is a
    stored fact of the conversation, so a conversation migrated from the old scheme keeps
    the checkpoint it already has. `message_id` and `content` are the ledger's own, and they
    are what make the checkpoint transcript reconcilable with the ledger.

    `verification_code` is here, and deliberately **not** in `AgentState`: it is a secret the
    user typed on a confirmation box, and the graph state is checkpointed after every node, so
    putting it there would persist it. It rides the turn the way `resume` and `question_id`
    do -- the values a node needs and must not checkpoint.

    `auto_cancel` is here for the same reason: it is the server's statement that the bank
    voided this operation, and the module must see it without it entering the checkpointed
    `AgentState` (or, through `client_context`, the request fingerprint).
    """

    conversation_id: str
    thread_id: str
    message_id: str
    content: str
    sequence: int
    run_id: str | None = None
    resume: Any = None
    question_id: str | None = None
    verification_code: str | None = None
    auto_cancel: bool = False


@dataclass(frozen=True, slots=True)
class Admission:
    """What the checkpoint says this request is allowed to be.

    `resume` is the envelope to hand the graph, already bound to the open question -- the
    runtime is the only place that can name it, because only the runtime can read the
    checkpoint.
    """

    resuming: bool
    question_id: str | None = None
    resume: Any = None
    pending: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RecoveryHint:
    """What a conversation's checkpoint can honestly say about itself.

    `pending` is the one question that is provably open. `recovery_required` says there is
    unfinished work the checkpoint cannot prove is a safe interrupt -- a state this phase
    deliberately does not replay, retry or take over.
    """

    pending: dict[str, Any] | None = None
    recovery_required: bool = False


class GraphFactory(Protocol):
    def __call__(
        self,
        model: ModelClient,
        context: TrustedContext,
        assembler: Any | None = None,
        turn: TurnContext | None = None,
    ) -> StateGraph: ...


class CompiledGraph(Protocol):
    def astream(self, input, config=None, **kwargs) -> AsyncIterator[Any]: ...


class AgentRuntime:
    """Owns the graph, the thread derivation and the per-session lock."""

    def __init__(
        self,
        model: ModelClient | None = None,
        *,
        graph: CompiledGraph | None = None,
        graph_factory: GraphFactory | None = None,
        checkpointer: Any | None = None,
        recursion_limit: int = 50,
        customer_resolver: Callable[[str], str | None] | None = None,
        assembler_factory: Callable[[str, TurnContext | None, ModelClient], Any] | None = None,
    ) -> None:
        self._model = model or UnconfiguredModelClient()
        self._owns_checkpointer = checkpointer is None
        self._checkpointer = checkpointer or InMemorySaver()
        if graph is None and graph_factory is None:
            raise ValueError("AgentRuntime needs a compiled graph or a graph factory")
        self._graph = graph
        self._graph_factory = graph_factory
        self._recursion_limit = recursion_limit
        self._customer_resolver = customer_resolver
        self._assembler_factory = assembler_factory
        self._locks: dict[str, asyncio.Lock] = {}

    def _graph_for(self, context: TrustedContext, turn: TurnContext | None = None) -> CompiledGraph:
        """The graph for this caller.

        A fixed graph is shared as-is; a factory is asked for a fresh graph, because
        the identity it binds into the domain handles belongs to one caller only.

        The assembler is built for every invocation the factory serves, `turn is None`
        included: the session path has no conversation, but spec §8.2 still requires that
        degraded assembly to be reported, so the only condition is whether a factory was
        installed at all.

        The factory is handed `context.user_id` because an assembly reads history: since P3
        the ledger and summary reads verify ownership themselves, and the identity they verify
        has to be the caller's, not whatever the previous invocation left behind.
        """

        if self._graph is not None:
            return self._graph
        assembler = (
            self._assembler_factory(context.user_id, turn, self._model)
            if self._assembler_factory is not None
            else None
        )
        return self._graph_factory(self._model, context, assembler, turn).compile(
            checkpointer=self._checkpointer
        )

    @property
    def model(self) -> ModelClient:
        return self._model

    @property
    def model_configured(self) -> bool:
        """Whether a real model is installed. Without one every turn fails loudly.

        The question is about the model, not about the wrapper around it: the audit
        decorator is transparent, so an unconfigured runtime that has been wrapped is still
        unconfigured, and the health endpoint has to say so.
        """

        return is_configured(self._model)

    def set_model(self, model: ModelClient) -> None:
        """Swap the model. The next invocation builds its graph with the new one."""

        self._model = model

    def use_checkpointer(self, checkpointer: Any) -> None:
        """Adopt a checkpointer the deployment opened.

        The caller that opened it closes it, so ownership moves out of this object. The
        graphs are compiled per invocation, so the next turn picks it up with no rebuild.
        """

        self._checkpointer = checkpointer
        self._owns_checkpointer = False

    async def aclose(self) -> None:
        self._model.close()
        if self._owns_checkpointer:
            close = getattr(self._checkpointer, "aclose", None)
            if close is not None:
                await close()

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """Lazily created per-session lock. The lookup itself never awaits."""

        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        """The lock one conversation's turns are serialized on.

        A conversation's turn spans more than one `await`: idempotency lookup, checkpoint
        admission, the ledger write, the graph call and the final write. Whoever owns that
        span owns this lock, which is why `invoke(..., turn=...)` does **not** take it --
        an `asyncio.Lock` is not reentrant, and taking it twice in one turn would deadlock.
        """

        return self._lock_for(f"conversation:{conversation_id}")

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self._recursion_limit,
        }

    async def _thread_graph(
        self, user_id: str, session_id: str, thread_id: str, turn: TurnContext | None = None
    ):
        """A graph bound to this caller, and the config that names this thread."""

        bank_customer_id = await self._resolve_customer(user_id)
        context = TrustedContext(
            user_id=user_id, session_id=session_id, bank_customer_id=bank_customer_id
        )
        return self._graph_for(context, turn), self._config(thread_id)

    @staticmethod
    async def _pending(graph: CompiledGraph, config: dict) -> tuple[tuple, tuple]:
        """What the checkpoint has queued: its interrupts, and the tasks still unfinished."""

        get_state = getattr(graph, "aget_state", None)
        if get_state is None:
            return (), ()
        state = await get_state(config)
        interrupts = tuple(getattr(state, "interrupts", ()) or ())
        unfinished = tuple(getattr(state, "next", ()) or ())
        return interrupts, unfinished

    @staticmethod
    def _one_question(interrupts: tuple) -> dict[str, Any] | None:
        """The single open question, when the pause names exactly one.

        Two open questions cannot be told apart from the outside, and a pause that names no
        question cannot be matched at all, so both read as "not provably a safe interrupt".
        """

        if len(interrupts) != 1:
            return None
        value = getattr(interrupts[0], "value", None)
        if isinstance(value, Mapping) and isinstance(value.get("question_id"), str):
            return dict(value)
        return None

    async def admission(
        self,
        user_id: str,
        session_id: str,
        thread_id: str,
        client_context: Mapping[str, Any] | None,
    ) -> Admission:
        """Whether this request may proceed, read from the checkpoint alone.

        This runs *before* anything is written, which is the whole point: a request that
        cannot be admitted must leave no message, no submission and no sequence behind. The
        checkpoint is the only execution state there is, so this is the only place that can
        decide -- and where it cannot prove the state is safe, it refuses instead of
        guessing, because guessing is what would replay a half-finished turn.
        """

        graph, config = await self._thread_graph(user_id, session_id, thread_id)
        interrupts, unfinished = await self._pending(graph, config)
        resuming, resume = resume_value(client_context)

        if interrupts:
            question = self._one_question(interrupts)
            if question is None:
                # A pause nobody can name is a pause nobody can answer.
                raise conversation_error(ConversationErrorCode.EXECUTION_RECOVERY_REQUIRED)
            open_question = question["question_id"]
            if not resuming:
                raise conversation_error(
                    ConversationErrorCode.INVALID_INTERRUPT_ANSWER,
                    details={"question_id": open_question},
                )
            named = resume.get("question_id") if isinstance(resume, Mapping) else None
            if isinstance(resume, Mapping) and "question_id" in resume:
                # The client made a claim about which question it is answering. A claim that
                # cannot be read, or that names another question, is not an absent one: the
                # key being *present* is what makes it a claim, so an explicit `null` is
                # checked here instead of falling through to the legacy shape below.
                if not isinstance(named, str) or not named.strip() or named != open_question:
                    raise conversation_error(
                        ConversationErrorCode.INVALID_INTERRUPT_ANSWER,
                        details={"question_id": open_question},
                    )
                envelope = dict(resume)
            else:
                # The legacy shape, bound here because only the runtime can name the open
                # question -- and only while exactly one is open, which is checked above.
                envelope = {
                    "question_id": open_question,
                    "value": resume.get("value") if isinstance(resume, Mapping) else None,
                }
            return Admission(
                resuming=True, question_id=open_question, resume=envelope, pending=question
            )

        if unfinished:
            # There is work the checkpoint cannot prove is a safe pause. Replaying it is out
            # of scope for this phase, and pretending otherwise would run a turn twice.
            raise conversation_error(ConversationErrorCode.EXECUTION_RECOVERY_REQUIRED)
        if resuming:
            # Nothing is waiting, so the answer has no question to belong to. Delivering it
            # anyway would start the graph over and silently drop it.
            raise conversation_error(ConversationErrorCode.INVALID_INTERRUPT_ANSWER)
        return Admission(resuming=False)

    async def recovery_hint(self, user_id: str, session_id: str, thread_id: str) -> RecoveryHint:
        """What may be shown about a conversation's checkpoint, and nothing more."""

        graph, config = await self._thread_graph(user_id, session_id, thread_id)
        interrupts, unfinished = await self._pending(graph, config)
        question = self._one_question(interrupts)
        if question is not None:
            return RecoveryHint(pending=question)
        if interrupts or unfinished:
            return RecoveryHint(recovery_required=True)
        return RecoveryHint()

    def _graph_input(
        self,
        message: str,
        client_context: Mapping[str, Any] | None,
        resume: Any = None,
    ):
        del client_context
        if resume is not None:
            # Resume replays the interrupting node; nothing irreversible may precede it.
            return Command(resume=resume)
        return new_agent_state(message)

    @staticmethod
    async def _abandon_goal(graph: CompiledGraph, config: dict) -> None:
        """Drop the current goal, including a pause nobody answered.

        Passing new input to a suspended thread does not consume its interrupt: the graph
        starts again from START and the pause stays pending, ready to swallow the next
        ordinary message as if it were the answer. Replacing the pending task through
        `as_node="reset_goal"` is what actually abandons it.
        """

        update = getattr(graph, "aupdate_state", None)
        if update is None:
            return
        await update(config, goal_reset_state(), as_node="reset_goal")

    async def _bound_resume(
        self, graph: CompiledGraph, config: dict, client_context: Mapping[str, Any] | None
    ) -> Any:
        """The resume envelope, naming the open question for a client that did not.

        A legacy client sends only the value. Naming the question is the runtime's job
        because only the runtime can read which question is open -- and it may do so only
        while exactly one is, since choosing between two would bind an answer to a question
        the user never saw. The graph still verifies whatever binding it is handed.
        """

        _resuming, resume = resume_value(client_context)
        has_pause, open_question = await self._open_pause(graph, config)
        if not has_pause:
            # Nothing is waiting, so the answer has no question to belong to. Delivering it
            # anyway would silently start the graph over and drop it.
            raise AppError("INVALID_INTERRUPT_ANSWER", "当前没有等待回答的问题", 409)

        if isinstance(resume, Mapping) and "question_id" in resume:
            # The client named a question. A name that cannot be read is a protocol error,
            # not an absent one: falling back to the open question would answer something
            # the client never asked about, which is what `question_id` exists to prevent.
            question_id = resume["question_id"]
            if not isinstance(question_id, str) or not question_id.strip():
                raise AppError("INVALID_INTERRUPT_ANSWER", "question_id 无效", 409)
            return resume

        # The field is entirely absent: the legacy shape, which only the runtime can bind.
        if open_question is None:
            # A pause this runtime cannot name, such as a graph whose interrupt carries no
            # question. Hand it on rather than inventing an id.
            return resume
        return {"question_id": open_question, "value": resume.get("value")}

    @staticmethod
    async def _open_pause(graph: CompiledGraph, config: dict) -> tuple[bool, str | None]:
        """Whether this thread is paused, and the id of its one question when it has one."""

        get_state = getattr(graph, "aget_state", None)
        if get_state is None:
            return False, None
        state = await get_state(config)
        interrupts = getattr(state, "interrupts", ()) or ()
        if not interrupts:
            return False, None
        if len(interrupts) != 1:
            return True, None
        value = getattr(interrupts[0], "value", None)
        if isinstance(value, Mapping) and isinstance(value.get("question_id"), str):
            return True, value["question_id"]
        return True, None

    @staticmethod
    async def _active_node(graph: CompiledGraph, config: dict) -> tuple[dict[str, Any], str] | None:
        """The plan the checkpoint holds and the node it is on, or None when it holds neither.

        A thread that never drew a plan has nothing to re-send, and a thread whose plan was
        cleared has nothing to point at. Both are ordinary -- a reply-only turn, or a turn
        that already ended -- so this asks the checkpoint instead of assuming either.
        """

        get_state = getattr(graph, "aget_state", None)
        if get_state is None:
            return None
        state = await get_state(config)
        values = getattr(state, "values", None) or {}
        plan = values.get("task_plan") or {}
        node_id = values.get("active_task_node_id")
        if not plan or node_id is None:
            return None
        return plan, node_id

    @classmethod
    async def _resume_events(cls, graph: CompiledGraph, config: dict) -> list[AgentEvent]:
        """The plan and the running node a resume is about to continue.

        A client that comes back has neither: the plan belongs to a round it is no longer
        streaming. Re-sending the pair first is what lets a refreshed page draw the work
        instead of an empty canvas.
        """

        active = await cls._active_node(graph, config)
        if active is None:
            return []
        plan, node_id = active
        return _custom_event(
            {"event": "plan", "plan_id": plan.get("plan_id"), "nodes": plan.get("nodes")}
        ) + _custom_event(
            {
                "event": "task_update",
                "plan_id": plan.get("plan_id"),
                "node_id": node_id,
                "status": "running",
            }
        )

    @classmethod
    async def _node_update_events(
        cls, graph: CompiledGraph, config: dict, status: str
    ) -> list[AgentEvent]:
        """The node the thread is parked on, as one `task_update` frame.

        A pause suspends the node that would have written the frame, so the checkpoint is the
        only place the id still exists.
        """

        active = await cls._active_node(graph, config)
        if active is None:
            return []
        plan, node_id = active
        return _custom_event(
            {
                "event": "task_update",
                "plan_id": plan.get("plan_id"),
                "node_id": node_id,
                "status": status,
            }
        )

    @staticmethod
    async def _waiting_events(graph: CompiledGraph, config: dict) -> list[AgentEvent]:
        """The pause this thread is sitting on, if any.

        A thread that is suspended has pending tasks and an interrupt per task; a thread
        that is merely finished has neither. Asking is how the runtime keeps a pause
        sticky instead of letting the next ordinary message quietly drop it.
        """

        get_state = getattr(graph, "aget_state", None)
        if get_state is None:
            return []
        state = await get_state(config)
        return _interrupt_events(getattr(state, "interrupts", ()), set())

    async def _resolve_customer(self, user_id: str) -> str | None:
        """The bank customer this user acts as, or None.

        A resolver failure degrades to None rather than ending the turn: the direction is
        fail closed, so a missing binding refuses a bank call instead of guessing one.
        """

        if self._customer_resolver is None:
            return None
        try:
            return await asyncio.to_thread(self._customer_resolver, user_id)
        except Exception as error:
            logger.error(
                "could not resolve the bank customer for %s: %s", user_id, type(error).__name__
            )
            return None

    async def invoke(
        self,
        session_id: str,
        user_id: str,
        message: str,
        client_context: Mapping[str, Any] | None = None,
        *,
        turn: TurnContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn, either on a conversation's thread or on a session's.

        `turn` is the conversation path: the thread and the ledger message come from the
        caller, admission has already happened, and the caller holds the conversation lock
        for the whole turn (see `conversation_lock`). Without it the runtime keeps the
        session-derived behaviour it has always had, which the migration tool and the
        pre-persistence tests still use.
        """

        # The caller's identity never enters the graph state; it reaches a tool only
        # through the handle the Gateway built for it.
        bank_customer_id = await self._resolve_customer(user_id)
        context = TrustedContext(
            user_id=user_id, session_id=session_id, bank_customer_id=bank_customer_id
        )
        thread_id = turn.thread_id if turn is not None else thread_id_for_session(session_id)
        config = self._config(thread_id)
        seen_interrupts: set[Any] = set()
        # A conversation turn is admitted and serialized by its service, which holds the
        # conversation lock across the whole turn; taking a second lock here would deadlock,
        # because an `asyncio.Lock` is not reentrant. The session path serializes here, as it
        # always did.
        guard = self._lock_for(session_id) if turn is None else nullcontext()
        new_goal = turn is None and (client_context or {}).get("mode") == "new_goal"
        # The conversation path arrives with a context the turn lifecycle already set, and it
        # is richer than anything this layer could invent -- so this layer leaves it alone.
        # The session path has none, so it is named here for what it is, with no conversation
        # id invented for it.
        scope = (
            nullcontext()
            if turn is not None
            else audit_context(
                AuditContext(
                    user_id=user_id,
                    source=SOURCE_SESSION_CHAT,
                    agent_id=AGENT_ID_SUPERVISOR,
                )
            )
        )
        with scope:
            async with guard:
                graph = self._graph_for(context, turn)
                if new_goal:
                    # Abandoning the goal is a state write before the turn, not a turn of its
                    # own: the new message still travels the normal `astream()` path below.
                    await self._abandon_goal(graph, config)
                if turn is not None:
                    graph_input = self._turn_input(turn)
                    resumed = turn.resume is not None
                else:
                    # Resolving the resume needs the graph: a legacy envelope has to be named
                    # with the question this thread is actually waiting on.
                    resuming, _value = resume_value(client_context)
                    resume = (
                        await self._bound_resume(graph, config, client_context)
                        if resuming
                        else None
                    )
                    graph_input = self._graph_input(message, client_context, resume)
                    resumed = resuming
                    if not resuming and not new_goal:
                        # An ordinary message during a pause is answered with the pause
                        # itself: the user is told again what is awaited, and nothing is
                        # re-planned.
                        waiting = await self._waiting_events(graph, config)
                        if waiting:
                            for event in waiting:
                                yield event
                            return
                if resumed:
                    # A client that comes back has no plan, and the plan belongs to a round it
                    # is no longer streaming. Re-sending the pair before the continuation is
                    # what lets a refreshed page redraw the graph rather than nothing.
                    for redraw in await self._resume_events(graph, config):
                        yield redraw
                async for chunk in graph.astream(
                    graph_input,
                    config,
                    stream_mode=["updates", "custom"],
                    subgraphs=True,
                    version="v2",
                ):
                    for event in self._adapt(chunk, seen_interrupts):
                        if event.event == "interrupt":
                            # The node is parked, not finished, and it is where the thread is:
                            # the frame belongs immediately before the question it asks.
                            for paused in await self._node_update_events(
                                graph, config, "waiting_confirm"
                            ):
                                yield paused
                        yield event

    @staticmethod
    def _turn_input(turn: TurnContext):
        """What a conversation turn hands the graph.

        A resume carries the ledger message with it: the interrupting node writes that
        message into the transcript under the ledger's own id, which is what keeps the
        checkpoint's history and the user-visible history the same history.
        """

        if turn.resume is None:
            return new_agent_state(turn.content, message_id=turn.message_id)
        envelope = dict(turn.resume) if isinstance(turn.resume, Mapping) else {"value": turn.resume}
        envelope["message_id"] = turn.message_id
        envelope["content"] = turn.content
        return Command(resume=envelope)

    @staticmethod
    def _adapt(chunk: Any, seen_interrupts: set[Any]) -> list[AgentEvent]:
        """LangGraph's stream chunks -> the six internal events SSE knows about.

        A node publishes an event by writing it to the custom stream; a pause arrives
        as an `__interrupt__` update. Nothing is derived from graph state, so the
        adapter never guesses and never re-emits an earlier node's output. The
        namespace is read, never written.
        """

        if not isinstance(chunk, dict):
            return []
        kind = chunk.get("type")
        data = chunk.get("data")
        if kind == "custom":
            return _custom_event(data)
        if kind != "updates" or not isinstance(data, dict):
            return []
        events: list[AgentEvent] = []
        for node, update in data.items():
            if node == "__interrupt__":
                events.extend(_interrupt_events(update, seen_interrupts))
        return events


def _custom_event(payload: Any) -> list[AgentEvent]:
    """A custom payload is trusted to be well-formed: the graph wrote it, not the model."""

    if not isinstance(payload, dict):
        return []
    name = payload.get("event")
    fields = EVENT_FIELDS.get(name) if isinstance(name, str) else None
    if fields is None:
        raise ValueError(f"graph emitted an unknown event: {name!r}")
    return [AgentEvent(event=name, data={field: payload.get(field) for field in fields})]


def _interrupt_events(update: Any, seen: set[Any]) -> list[AgentEvent]:
    """One interrupt is reported once, even though a subgraph's pause surfaces twice."""

    events: list[AgentEvent] = []
    interrupts = update if isinstance(update, (list, tuple)) else (update,)
    for item in interrupts:
        key = getattr(item, "id", None) or id(item)
        if key in seen:
            continue
        seen.add(key)
        value = getattr(item, "value", None)
        if isinstance(value, dict):
            authorization = value.get("authorization")
            confirmation = value.get("confirmation")
            events.append(
                AgentEvent(
                    event="interrupt",
                    data={
                        "kind": value.get("kind"),
                        "question_id": value.get("question_id"),
                        "agent_id": value.get("agent_id"),
                        "fields": list(value.get("fields", [])),
                        "display": value.get("display"),
                        "attempt": value.get("attempt"),
                        **(
                            {"authorization": authorization}
                            if isinstance(authorization, dict)
                            else {}
                        ),
                        # The confirmation box's own projection, carried through unchanged:
                        # it is what the page renders (operation, amount, product, account)
                        # and it is what binds a click to the preview the user saw.
                        **(
                            {"confirmation": confirmation} if isinstance(confirmation, dict) else {}
                        ),
                    },
                )
            )
    return events
