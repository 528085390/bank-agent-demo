"""The lifecycle of one submitted turn.

This is the only place the order of a submission is written down, and the order *is* the
design:

1. the conversation is the caller's and still writable;
2. a key already spent on this exact request is answered with its receipt, and nothing else
   happens -- no sequence, no graph call;
3. the checkpoint says whether this request is admissible at all, and a refusal here leaves
   no message and no submission behind;
4. the user message, its sequence and its submission commit as one transaction;
5. a dispatch correlation id is attached in a short transaction of its own, and only then is
   the graph called;
6. the assistant's reply is written with that same id, and only after it commits does the
   `final` event reach the client.

Nothing here is atomic across the database and the graph, and nothing pretends to be: a
crash between (4) and (5) leaves a stored message that no run will ever answer, and the same
key replaying it gets the receipt rather than a second call. That is the accepted delivery
limit of this phase, not an oversight -- there is deliberately no retry, no outbox and no
background catch-up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.exc import SQLAlchemyError

from app.agents.runtime import Admission, AgentEvent, TurnContext
from app.audit.context import AGENT_ID_SUPERVISOR, SOURCE_CONVERSATION, AuditContext, audit_context
from app.audit.service import RunEventType
from app.conversation.service import ConversationService, Receipt, request_hash
from app.conversation.titling import derive_title, is_usable_title
from app.core.errors import ConversationErrorCode, conversation_error
from app.core.types import new_id
from app.integration.model_client import ModelClient

logger = logging.getLogger(__name__)

#: The report is stored in the assistant message's JSONB metadata so another device can show it
#: as a downloadable file. The metadata is returned with every message read, so a pathological
#: payload is dropped rather than allowed to bloat the ledger; normal reports are far smaller.
MAX_ANALYSIS_REPORT_BYTES = 512 * 1024


def _analysis_report_of(event: AgentEvent) -> dict[str, Any] | None:
    """The full analysis display carried by a successful ``bill_analyze_comprehensive`` result.

    The envelope is already projected by the seam: ``display`` is the analyzer's complete result
    (``report_meta`` / ``modules`` / ``text_report``), which is exactly what the PDF builder and
    the frontend chart renderer consume. Only that shape is persisted; other tools' displays are
    progress or small summaries and stay out of the ledger.
    """

    if event.event != "tool_result":
        return None
    data = event.data
    if data.get("tool_name") != "bill_analyze_comprehensive" or data.get("error") is not None:
        return None
    envelope = data.get("data")
    if not isinstance(envelope, Mapping):
        return None
    display = envelope.get("display")
    if not isinstance(display, Mapping):
        return None
    if not isinstance(display.get("report_meta"), Mapping):
        return None
    if not isinstance(display.get("modules"), list):
        return None
    if not isinstance(display.get("text_report"), str):
        return None
    return dict(display)


def _report_fingerprint(report: Mapping[str, Any]) -> str:
    """Identity used to collapse a retried tool call into one stored report."""

    meta = report.get("report_meta")
    meta = meta if isinstance(meta, Mapping) else {}
    date_range = meta.get("date_range")
    date_range = date_range if isinstance(date_range, Mapping) else {}
    return "|".join(
        str(value or "")
        for value in (
            meta.get("generated_at"),
            date_range.get("start"),
            date_range.get("end"),
            meta.get("total_transactions"),
        )
    )


def _bounded_analysis_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate and cap what one turn may add to its message metadata."""

    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for report in reports:
        fingerprint = _report_fingerprint(report)
        if fingerprint in seen:
            continue
        try:
            size = len(json.dumps(report, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            logger.warning("dropping analysis report that is not JSON-serializable")
            continue
        if total + size > MAX_ANALYSIS_REPORT_BYTES:
            logger.warning(
                "dropping analysis report: metadata budget exceeded (%s bytes)", size
            )
            continue
        seen.add(fingerprint)
        kept.append(report)
        total += size
    return kept


#: A query result is only the current page, and only enough to keep the table visible after a
#: reload. The full detail is still fetched from the read-only endpoint when the user re-queries.
MAX_QUERY_RESULT_ITEMS = 20
MAX_QUERY_RESULT_ENTRIES = 4
MAX_QUERY_RESULT_BYTES = 256 * 1024


def _query_result_of(event: AgentEvent) -> dict[str, Any] | None:
    """The current page of a successful ``bill_query_transactions`` result."""

    if event.event != "tool_result":
        return None
    data = event.data
    if data.get("tool_name") != "bill_query_transactions" or data.get("error") is not None:
        return None
    envelope = data.get("data")
    if not isinstance(envelope, Mapping):
        return None
    display = envelope.get("display")
    if not isinstance(display, Mapping) or not isinstance(display.get("items"), list):
        return None
    items = [dict(item) for item in display["items"][:MAX_QUERY_RESULT_ITEMS] if isinstance(item, Mapping)]
    return {
        "period_start": display.get("period_start"),
        "period_end": display.get("period_end"),
        # 按文件查询时带着任务号：恢复后的表格靠它直接翻页，不必重新问模型。
        "task_id": display.get("task_id"),
        "total": display.get("total"),
        "page": display.get("page"),
        "page_size": display.get("page_size"),
        "items": items,
    }


def _query_result_fingerprint(result: Mapping[str, Any]) -> str:
    # 任务号必须在键里：两份不同账单的同一页、同一总数，否则会被当成同一份结果去重掉。
    return "|".join(
        str(result.get(key) or "")
        for key in ("task_id", "period_start", "period_end", "page", "total")
    )


def _bounded_query_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate query pages and keep the metadata small."""

    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for result in results:
        fingerprint = _query_result_fingerprint(result)
        if fingerprint in seen:
            continue
        try:
            size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            continue
        if total + size > MAX_QUERY_RESULT_BYTES:
            logger.warning("dropping query result: metadata budget exceeded (%s bytes)", size)
            continue
        seen.add(fingerprint)
        kept.append(result)
        total += size
        if len(kept) >= MAX_QUERY_RESULT_ENTRIES:
            break
    return kept


class AgentRuntimeLike(Protocol):
    """What the turn lifecycle needs from the Agent runtime, and nothing else."""

    async def admission(
        self, user_id: str, session_id: str, thread_id: str, client_context: Mapping | None
    ) -> Admission: ...

    async def recovery_hint(self, user_id: str, session_id: str, thread_id: str) -> Any: ...

    def conversation_lock(self, conversation_id: str) -> asyncio.Lock: ...

    def invoke(self, *args, **kwargs) -> AsyncIterator[AgentEvent]: ...

    @property
    def model(self) -> ModelClient: ...


@dataclass(frozen=True, slots=True)
class TurnPlan:
    """A first submission, already persisted, waiting for its graph call."""

    conversation_id: str
    thread_id: str
    receipt: Receipt
    message: str
    client_context: Mapping[str, Any] | None
    admission: Admission
    # A secret the user typed for this submission (a confirmation box's verification code).
    # It is carried on the plan rather than in `client_context` on purpose: the context is
    # hashed into the request fingerprint and echoed back in a replay, and a secret must be
    # in neither.
    verification_code: str | None = None
    # The server's own statement that the bank voided the operation this turn resumes. Like
    # `verification_code` it rides the turn rather than `client_context`, so it is in neither
    # the request fingerprint nor a replay -- but unlike it, no client may ever supply it: only
    # the decide route, after the bank itself said "cancelled", sets it.
    auto_cancel: bool = False


class HeldLock:
    """A conversation lock held by one request, released exactly once.

    Two paths can end a turn -- the request itself, and the response body finishing (or
    being abandoned) -- and both are allowed to release it. A leaked conversation lock
    would wedge that conversation for the life of the process, so the release is
    idempotent rather than trusted to happen on exactly one path.
    """

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lock.release()


class DeferredNaming:
    """The one naming attempt a turn is allowed, handed to the response to run after the stream.

    The turn decides *whether* to name -- only a turn that reached `final`; the HTTP layer decides
    *when*, as the response's background work, so the stream and the conversation lock are released
    the moment the answer is published (design spec S-2.1, m00787).
    """

    def __init__(self, turns: "ConversationTurns") -> None:
        self._turns = turns
        self._pending: tuple[AuditContext, str, TurnPlan] | None = None

    def defer(self, context: AuditContext, user_id: str, plan: TurnPlan) -> None:
        """Register this turn's naming attempt; it is not made here."""

        self._pending = (context, user_id, plan)

    async def run(self) -> None:
        """Run the naming attempt, if the turn asked for one. Failures are `_auto_title`'s to drop."""

        pending, self._pending = self._pending, None
        if pending is None:
            return
        context, user_id, plan = pending
        # The turn's audit context is a generator-local `with`, so it is gone by the time the
        # background work runs: re-entering it is what lets `apply_auto_title` write its business
        # row with this turn's user, conversation and run (audit/service.py:261-263).
        with audit_context(context):
            await self._turns._auto_title(user_id, plan)


class ConversationTurns:
    """Admission, idempotency, persistence and dispatch for one conversation at a time."""

    def __init__(self, service: ConversationService, runtime: AgentRuntimeLike) -> None:
        self.service = service
        # The runtime is read per call, never captured: a test (or the settings panel) may
        # replace the agent after this object exists, and a stale reference would dispatch
        # the wrong graph.
        self.runtime = runtime

    @property
    def audit(self):
        """The process's audit writer, or `None` in a runtime that has none.

        Read per call for the same reason the runtime is: a test may hand in a runtime whose
        audit service it replaced, and the turn must write to that one.
        """

        return getattr(self.runtime, "audit", None)

    def _record(self, event_type: RunEventType, context: AuditContext, **fields) -> None:
        """Append one run fact, if this runtime has somewhere to append it.

        The write is deliberately not wrapped in a `try`: the audit spec is explicit that a
        run must not quietly degrade into "only the log knows", so a store that cannot record
        a fact fails the turn instead of letting it proceed untraceable.
        """

        audit = self.audit
        if audit is None:
            return
        audit.record_agent_event(event_type=event_type, context=context, **fields)

    async def acquire(self, conversation_id: str) -> HeldLock:
        """Serialize this conversation's turns. Held across the whole turn, not one write."""

        lock = self.runtime.conversation_lock(conversation_id)
        await lock.acquire()
        return HeldLock(lock)

    async def prepare(
        self,
        user_id: str,
        session_id: str,
        conversation_id: str,
        *,
        idempotency_key: str,
        message: str,
        client_context: Mapping[str, Any] | None,
        verification_code: str | None = None,
        auto_cancel: bool = False,
    ) -> Receipt | TurnPlan:
        """Everything that must happen before a response type can be chosen.

        Returns the stored receipt when the key was already spent, and a plan when this is a
        first submission whose message is now persisted.

        `verification_code` is deliberately not part of the request fingerprint: the
        fingerprint decides whether a key replay is the *same* request, and a user who typed a
        wrong code and is now retyping it has made a new request with a new key. Mixing the
        secret into the hash would also spread it into a stored column.
        """

        conversation = await asyncio.to_thread(
            self.service.require_writable, user_id, conversation_id
        )
        if (client_context or {}).get("mode") == "new_goal":
            # Resetting a goal in place would mean resetting a LangGraph thread, and a thread
            # reset is not something this phase can do without a cross-store commit. The user
            # gets a new conversation instead, which is the isolation they were asking for.
            raise conversation_error(ConversationErrorCode.NEW_CONVERSATION_REQUIRED)

        expected_hash = request_hash(conversation_id, message, client_context)
        replay = await asyncio.to_thread(
            self.service.find_submission, user_id, idempotency_key, expected_hash
        )
        if replay is not None:
            return replay

        # Admission reads the checkpoint and writes nothing, so a refusal here leaves no
        # message, no submission and no consumed sequence behind.
        admission = await self.runtime.admission(
            user_id, session_id, conversation.thread_id, client_context
        )
        receipt = await asyncio.to_thread(
            self.service.append_user_message,
            user_id,
            conversation_id,
            idempotency_key=idempotency_key,
            expected_hash=expected_hash,
            content=message,
            message_kind="interrupt_answer" if admission.resuming else "message",
            question_id=admission.question_id if admission.resuming else None,
        )
        if receipt.duplicate:
            # A concurrent first submission won the unique key while this one was admitting.
            # The winner's receipt is the answer; this request's candidate row is gone.
            return receipt
        return TurnPlan(
            conversation_id=conversation_id,
            thread_id=conversation.thread_id,
            receipt=receipt,
            message=message,
            client_context=client_context,
            admission=admission,
            verification_code=verification_code,
            auto_cancel=auto_cancel,
        )

    async def stream(
        self,
        user_id: str,
        session_id: str,
        plan: TurnPlan,
        *,
        naming: DeferredNaming | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Attach the run, call the graph, and persist the reply before publishing it.

        Everything between the run id being allocated and the graph finishing runs inside one
        audit context. That is what makes a model call and the tool calls it caused share a
        conversation and a run without the graph having to carry either: the boundaries that
        write audit rows read the context, and the context is restored when the turn ends --
        normally, by an exception, or by the client walking away mid-stream.

        The one naming attempt a turn is allowed is not made here: when this generator sees an
        answer it registers the attempt on `naming`, and the HTTP layer runs it as the response's
        background work, after the stream -- and the conversation lock -- are done (S-2.1).
        """

        run_id = new_id("run")
        try:
            await asyncio.to_thread(self.service.attach_run, user_id, plan.receipt, run_id)
        except SQLAlchemyError:
            # The message is stored and the key is spent, but no call was made. Saying so is
            # the honest outcome: the client must not read this as "the turn is running".
            logger.error(
                "could not attach run %s to submission %s", run_id, plan.receipt.submission_id
            )
            raise conversation_error(ConversationErrorCode.PERSISTENCE_FAILED) from None

        turn = TurnContext(
            conversation_id=plan.conversation_id,
            thread_id=plan.thread_id,
            message_id=plan.receipt.message_id,
            content=plan.message,
            sequence=plan.receipt.sequence,
            run_id=run_id,
            resume=plan.admission.resume,
            question_id=plan.admission.question_id,
            verification_code=plan.verification_code,
            auto_cancel=plan.auto_cancel,
        )
        context = AuditContext(
            conversation_id=plan.conversation_id,
            run_id=run_id,
            user_id=user_id,
            source=SOURCE_CONVERSATION,
            agent_id=AGENT_ID_SUPERVISOR,
        )
        # Before the graph is called, not after it answers: a run that fails must still have
        # left the fact that it started, and the model calls it makes belong to it.
        self._record(
            RunEventType.RESUMED if plan.admission.resuming else RunEventType.RUN_STARTED,
            context,
            status="RUNNING",
        )
        # A turn is named only when it actually reached an answer, so the flag is bound before
        # the graph is called: a turn that raises on the way must not read an unbound local.
        final_seen = False
        # Reports shown during this turn, persisted with the assistant message so a different
        # device can render the same report behind a file placeholder. Collected before `final`
        # because the tool result arrives before the reply.
        analysis_reports: list[dict[str, Any]] = []
        query_results: list[dict[str, Any]] = []
        try:
            with audit_context(context):
                async for event in self.runtime.invoke(
                    session_id, user_id, plan.message, plan.client_context, turn=turn
                ):
                    if event.event == "tool_result":
                        report = _analysis_report_of(event)
                        if report is not None:
                            analysis_reports.append(report)
                        query = _query_result_of(event)
                        if query is not None:
                            query_results.append(query)
                    if event.event == "final":
                        # The ledger row and the `final` frame are ordered on purpose: a client
                        # that saw a reply must be able to read it back. Persisting after
                        # publishing would make a displayed answer disappear on refresh.
                        await self._record_reply(
                            user_id,
                            plan,
                            event,
                            run_id,
                            analysis_reports=analysis_reports,
                            query_results=query_results,
                        )
                        # Only a reply that reached the ledger is an answer. If persisting it
                        # fails, the turn failed and is recorded as one below.
                        final_seen = True
                    if event.event == "subtask_end" and event.data.get("grounding_blocked"):
                        # A domain replaced a model summary that cited IDs or amounts no tool
                        # returned. Record the refusal; the safe summary is what the user saw.
                        self._record(
                            RunEventType.GROUNDING_BLOCKED,
                            context,
                            status="BLOCKED",
                            agent_id=event.data.get("agent_id"),
                            payload={"reason": event.data.get("grounding_blocked")},
                        )
                    if event.event == "interrupt":
                        self._record(
                            RunEventType.INTERRUPTED,
                            context,
                            status="INTERRUPTED",
                            agent_id=event.data.get("agent_id"),
                            payload={"question_id": event.data.get("question_id")},
                        )
                    elif event.event == "agent_start":
                        # The graph publishes this when a domain agent begins, so it is
                        # recorded where it is observed rather than inferred from the
                        # checkpoint afterwards -- the checkpoint is not a run history.
                        self._record(
                            RunEventType.AGENT_STARTED,
                            context,
                            status="RUNNING",
                            agent_id=event.data.get("agent_id"),
                        )
                    yield event
                # After the answer, and only then: the `final` frame must not wait on a naming
                # call, and neither must the stream or the conversation lock -- the attempt is
                # registered and run as the response's background work (design spec S-2.1).
                if final_seen and naming is not None:
                    naming.defer(context, user_id, plan)
        except BaseException as error:
            # A failure is an outcome of the run, so it is recorded as one -- and only then
            # does the turn fail the way it always did. A client that disconnected lands
            # here too, and the records already written stay written: the stream is a display
            # channel and closing it cannot undo what happened.
            #
            # Unless the answer was already delivered. A client that leaves after the `final`
            # frame -- or a shutdown that cancels the generator before it returns -- is cut off
            # at the `yield` above, where `GeneratorExit` is raised: the reply is already stored
            # and published, but `naming.defer` below it has not run, so no naming attempt was
            # registered at all. (Nothing awaits between that line and the return, so the
            # "registered, then cancelled" path this comment used to describe is unreachable.)
            # That turn succeeded, and being cancelled or closed on the way out must not be
            # written over it as a failure (design spec S-2.6). The error still propagates:
            # `CancelledError` and `GeneratorExit` are the caller's business, not ours to swallow.
            if final_seen:
                self._record(RunEventType.RUN_SUCCEEDED, context, status="SUCCEEDED")
            elif isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                # The user stopped the task, or the tab went away. No answer was produced, but
                # this is not a system failure: keep it distinct in the trail so "stopped" can
                # never be read as "broke".
                self._record(
                    RunEventType.RUN_ABORTED,
                    context,
                    status="ABORTED",
                    error_code="ABORTED",
                )
            else:
                self._record(
                    RunEventType.RUN_FAILED,
                    context,
                    status="FAILED",
                    error_code=_failure_code(error),
                )
            raise
        self._record(RunEventType.RUN_SUCCEEDED, context, status="SUCCEEDED")

    async def _auto_title(self, user_id: str, plan: TurnPlan) -> None:
        """Name the conversation from the user's first message, once, after the answer.

        Called by `DeferredNaming.run` inside the turn's audit context: the stream is already over
        by the time this runs, so the context has to be re-entered for the business row
        `apply_auto_title` writes to carry this turn's user and conversation (S-2.1).

        The name comes from the text itself (`titling.derive_title`), not from a model: naming a
        label must not depend on a provider being reachable, and a turn that already answered must
        not wait on one. The two service calls run in a thread -- they are synchronous database
        work, and the write also takes a row lock -- and `to_thread` copies the context, so the
        business row written in that worker thread still sees the context re-entered above.

        Naming is an extra, never a dependency (design spec S-2.6): the reply is already stored
        and published by the time this runs, so any failure here -- a database error, a losing
        race -- is logged and dropped, never retried and never allowed to turn a successful turn
        into a failed one.
        """

        try:
            candidate = await asyncio.to_thread(
                self.service.auto_title_candidate, user_id, plan.conversation_id
            )
            if candidate is None:
                return
            title = derive_title(candidate)
            if not is_usable_title(title):
                return
            await asyncio.to_thread(
                self.service.apply_auto_title, user_id, plan.conversation_id, title
            )
        except Exception as error:
            logger.warning("could not name conversation %s: %s", plan.conversation_id, error)

    async def _record_reply(
        self,
        user_id: str,
        plan: TurnPlan,
        event: AgentEvent,
        run_id: str,
        *,
        analysis_reports: list[dict[str, Any]] | None = None,
        query_results: list[dict[str, Any]] | None = None,
    ) -> None:
        text = event.data.get("text")
        business_ids = event.data.get("business_ids")
        metadata: dict[str, Any] = (
            {"business_ids": list(business_ids)} if isinstance(business_ids, list) else {}
        )
        reports = _bounded_analysis_reports(analysis_reports or [])
        if reports:
            metadata["analysis_reports"] = reports
        queries = _bounded_query_results(query_results or [])
        if queries:
            metadata["query_results"] = queries
        try:
            await asyncio.to_thread(
                self.service.append_assistant_message,
                user_id,
                plan.conversation_id,
                run_id=run_id,
                content=text if isinstance(text, str) else "",
                metadata=metadata,
            )
        except SQLAlchemyError:
            logger.error("could not store the reply for run %s", run_id)
            raise conversation_error(ConversationErrorCode.PERSISTENCE_FAILED) from None

    async def recovery(self, user_id: str, session_id: str, conversation) -> dict[str, Any]:
        """What may be shown about a conversation's checkpoint, in the API's own shape."""

        hint = await self.runtime.recovery_hint(user_id, session_id, conversation.thread_id)
        return {"pending_interrupt": hint.pending, "recovery_required": hint.recovery_required}


def _failure_code(error: BaseException) -> str:
    """A stable reason for a failed run, never the provider's or the graph's own wording.

    The same rule the model boundary follows: a code callers branch on is used verbatim, and
    anything else is named by its type. A message can carry request contents, and this value
    is stored in a table operators read.
    """

    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else type(error).__name__
