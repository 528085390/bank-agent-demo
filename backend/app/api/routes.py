"""The HTTP surface.

Two chat paths, one turn lifecycle. `POST /api/conversations/{id}/messages` is the current
one; `POST /api/chat/messages` is an adapter that resolves the caller's session to a
conversation on the thread the old runtime derived, and then runs exactly the same pipeline.
There is no second implementation of admission, idempotency or persistence to keep honest.

Both POSTs require `Idempotency-Key`, and neither is necessarily a stream: a request whose
key was already spent is answered with a JSON receipt instead of SSE, so the client must
choose its parser from the response `Content-Type` rather than from the endpoint it called.
"""

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import text
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

# Re-exported on purpose: the decide state machine now lives in `app.api.pending_actions`, but
# callers and tests reach the interrupt/HTTP seam through this module, so the names stay
# importable from here. `X as X` is what marks them as re-exports rather than dead imports.
from app.agents.hitl import _authorize_card_action_if_needed as _authorize_card_action_if_needed
from app.agents.hitl import _click_matches as _click_matches
from app.agents.hitl import _requires_code as _requires_code
from app.api.pending_actions import (
    PendingActionPreparation,
    PendingActionResult,
    execute_pending_action_decision,
    submit_prepared_turn,
)
from app.api.schemas import (
    ChatMessage,
    ClientContext,
    ConversationCreate,
    ConversationMessageCreate,
    ConversationUpdate,
    ConversationBulkDelete,
    EmailSettingsRequest,
    LlmConfigRequest,
    PendingActionDecision,
)
from app.api.security import (
    SAFE_METHODS,
    presented_token,
    require_trusted_origin,
    set_session_cookie,
)
from app.api.sse import event_stream
from app.audit.context import AuditContext, audit_context
from app.conversation.turns import DeferredNaming, HeldLock, TurnPlan
from app.core.errors import AppError, ConversationErrorCode, conversation_error
from app.core.types import json_value
from app.identity.errors import AuthErrorCode, auth_error

router = APIRouter(prefix="/api")

# The streamed response must not be buffered anywhere between here and the browser, or the
# whole point of streaming a turn is lost.
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def runtime(request):
    return request.app.state.runtime


def envelope(request, data):
    return {"request_id": request.state.request_id, "data": json_value(data), "error": None}


def session_id(request):
    """The credential this request presents, and how it arrived.

    The name is kept because the caller below and the turn lifecycle use it, but what it
    returns is the *token*, not the internal session id: `session.session_id` is an identifier
    that shows up in foreign keys and logs and must never be usable as a credential.
    """

    return presented_token(request)


async def identity(request):
    """Resolve the caller, and enforce CSRF on anything that changes state.

    CSRF only applies to the cookie transport: a browser attaches the cookie on its own, so a
    third-party page can ride it. A bearer token has to be attached by code that already has
    it, which is not a cross-site request in the first place.

    可信来源校验放在这里，而不是只放在 `/api/auth/*` 上。P1 的规格写的是「所有修改类请求
    验证可信 Origin」，但落地时只在 auth 路由上调了一次 —— 于是登录被挡住，登录之后建会话、
    发消息、上传账单这些真正改变状态的动作反倒只靠 SameSite 和 CSRF token 两道。三条防线
    少一条不会立刻出事，但少的那条恰好是唯一能指出「是哪个代理改坏了 Host」的那条。

    放在这个函数里是因为它是所有带身份请求的唯一入口：新增路由自动获得保护，漏写一个
    依赖不会变成一个洞。空 Origin 仍然放行（curl、健康检查、CLI 工具都没有），见
    `app.api.security` 的说明。
    """

    require_trusted_origin(request)
    rt = runtime(request)
    token, source = session_id(request)
    who = await run_in_threadpool(rt.identity.authenticate, token)
    if source == "cookie" and request.method.upper() not in SAFE_METHODS:
        await run_in_threadpool(rt.identity.require_csrf, who, request.headers.get("x-csrf-token"))
    return who


async def admin(request):
    """The caller, if it is an administrator. Global switches live behind this."""

    who = await identity(request)
    await run_in_threadpool(runtime(request).identity.require_admin, who)
    return who


def llm_manager(request):
    manager = runtime(request).llm
    if manager is None:
        raise AppError("LLM_NOT_CONFIGURED", "模型配置服务不可用", 503)
    if not runtime(request).settings.demo_mode:
        raise AppError("DEMO_DISABLED", "模型配置仅在演示环境可用", 403)
    return manager


def idempotency_key(request: Request) -> str:
    """The client's key for this write, which every write must carry.

    There is no degraded mode: a server that accepts a keyless write cannot tell a retry
    from a new request, and silently appending a second message is worse than refusing the
    first one.
    """

    value = request.headers.get("Idempotency-Key")
    if value is None or not value.strip():
        raise conversation_error(ConversationErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    if len(value) > 255:
        raise AppError("INVALID_PARAMETER", "Idempotency-Key 过长", 422)
    return value


@router.get("/health")
def health(request: Request):
    with runtime(request).database.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    rt = runtime(request)
    return envelope(
        request,
        {
            "status": "ok",
            "model_configured": rt.agent.model_configured,
            "demo_mode": rt.settings.demo_mode,
        },
    )


@router.post("/session")
def create_session(request: Request, response: Response):
    """The credential-free demo entry.

    Kept as a route so an existing deployment can be switched over without a client release,
    but closed by default: `ALLOW_DEMO_LOGIN` has to be turned on deliberately. With it off,
    the only way in is `/api/auth/login`, which is the whole point of P1.
    """

    rt = runtime(request)
    if not rt.settings.allow_demo_login:
        raise auth_error(AuthErrorCode.DEMO_DISABLED)
    result = rt.identity.create_demo_session()
    set_session_cookie(response, result["token"], rt.settings, csrf_token=result["csrf_token"])
    return envelope(
        request,
        {
            "csrf_token": result["csrf_token"],
            "user_id": result["user_id"],
            "login_name": result["login_name"],
        },
    )


@router.get("/session")
async def get_session(request: Request):
    who = await identity(request)
    # `csrf_hash` is deliberately not echoed: the client already holds the token itself (in the
    # readable cookie), and returning the digest would put a value into the page that exists
    # only to be compared against.
    return envelope(
        request,
        {
            "session_id": who["session_id"],
            "user_id": who["user_id"],
            "login_name": who["login_name"],
            "role": who["role"],
        },
    )


@router.get("/settings/llm")
async def get_llm_settings(request: Request):
    # The model configuration is one global setting shared by every user, so reading it is
    # already an administrative act: it names the model and the endpoint the whole deployment
    # talks to.
    await admin(request)
    return envelope(request, llm_manager(request).public_snapshot())


@router.put("/settings/llm")
async def update_llm_settings(body: LlmConfigRequest, request: Request):
    who = await admin(request)
    # The probe this installs is a model call, and it belongs to the user who asked for it.
    # The context is established here because this is the layer that knows who that is; the
    # manager only adds which *kind* of probe it was. The request id travels with it so the
    # business row can be tied back to the request that caused it.
    with audit_context(AuditContext(user_id=who["user_id"], request_id=request.state.request_id)):
        result = await run_in_threadpool(
            llm_manager(request).configure,
            body.model_dump(),
            # The hook runs on the thread pool's worker thread, but `run_in_threadpool`
            # copies the caller's context into it, so `record_business_event` still reads
            # the `AuditContext` above and the row names the right user. The write commits
            # before `configure` changes memory, and a failure to write stops the change.
            on_change=lambda: runtime(request).audit.record_business_event(
                action="llm_config.update",
                resource_type="llm_config",
                resource_id="llm_config",
            ),
        )
    return envelope(request, result)


@router.post("/settings/llm/test")
async def test_llm_settings(body: LlmConfigRequest, request: Request):
    who = await admin(request)
    with audit_context(AuditContext(user_id=who["user_id"])):
        result = await run_in_threadpool(
            llm_manager(request).test,
            body.model_dump(),
        )
    return envelope(request, result)


@router.delete("/settings/llm")
async def clear_llm_settings(request: Request):
    who = await admin(request)
    # Same shape as the install above: the row commits before the model is removed, and the
    # context reaches the worker thread with the call, not by being re-established there.
    with audit_context(AuditContext(user_id=who["user_id"], request_id=request.state.request_id)):
        result = await run_in_threadpool(
            llm_manager(request).clear,
            on_change=lambda: runtime(request).audit.record_business_event(
                action="llm_config.clear",
                resource_type="llm_config",
                resource_id="llm_config",
            ),
        )
    return envelope(request, result)


@router.get("/settings/email")
async def get_email(request: Request):
    """当前绑定的收件邮箱，未绑定时为 `null`。

    设置面板靠它显示「当前绑定」。**不并进 `/session` 那份视图**：那个视图由身份服务拼装，
    改绑之后不会自动跟着变；这里直接读一次用户行，代价是一个查询，换来的是「打开面板看到的
    一定是当前值」。
    """

    who = await identity(request)
    email = await run_in_threadpool(runtime(request).notification.get_user_email, who["user_id"])
    return envelope(request, {"user_id": who["user_id"], "email": email})


@router.put("/settings/email")
async def set_email(body: EmailSettingsRequest, request: Request):
    who = await identity(request)
    email = await run_in_threadpool(
        runtime(request).notification.set_user_email, who["user_id"], body.email
    )
    return envelope(request, {"user_id": who["user_id"], "email": email})


@router.delete("/settings/email")
async def clear_email(request: Request):
    """解除邮箱绑定，与 `DELETE /settings/llm` 同一条路子。

    报告只发到绑定地址，所以必须能撤销 —— 否则地址填错之后只能改绑到另一个，回不到「未绑定」。
    """

    who = await identity(request)
    await run_in_threadpool(runtime(request).notification.clear_user_email, who["user_id"])
    return envelope(request, {"user_id": who["user_id"], "email": None})


@router.get("/queue/stats")
async def queue_stats(request: Request):
    await admin(request)
    return envelope(request, runtime(request).notification.queue_stats())


# --- conversations ---------------------------------------------------------------------


def service(request):
    return runtime(request).conversations


@router.post("/conversations")
async def create_conversation(body: ConversationCreate, request: Request):
    who = await identity(request)
    created = await run_in_threadpool(service(request).create, who["user_id"], body.title)
    return envelope(request, created.as_dict())


@router.get("/conversations")
async def list_conversations(
    request: Request,
    limit: int = 20,
    cursor: str | None = None,
    archived: bool = False,
    deleted: bool = False,
):
    who = await identity(request)
    page = await run_in_threadpool(
        service(request).list,
        who["user_id"],
        limit=limit,
        cursor=cursor,
        archived=archived,
        deleted=deleted,
    )
    return envelope(request, page)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    who = await identity(request)
    view = await run_in_threadpool(service(request).get, who["user_id"], conversation_id)
    hint = await runtime(request).turns.recovery(who["user_id"], who["session_id"], view)
    return envelope(
        request,
        {
            "conversation": view.as_dict(),
            "pending_interrupt": hint["pending_interrupt"],
            "recovery_required": hint["recovery_required"],
        },
    )


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationUpdate, request: Request):
    who = await identity(request)
    # Who made the change, to which conversation, in which request: the service records the
    # change it makes and needs all three. The context is a ContextVar and the threadpool
    # copies it, so the write in the worker thread still sees it.
    with audit_context(
        AuditContext(
            user_id=who["user_id"],
            conversation_id=conversation_id,
            request_id=request.state.request_id,
        )
    ):
        updated = await run_in_threadpool(
            service(request).update,
            who["user_id"],
            conversation_id,
            title=body.title,
            pinned=body.pinned,
            archived=body.archived,
        )
    return envelope(request, updated.as_dict())


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request):
    who = await identity(request)
    # The same context as the patch above: this change is recorded too.
    with audit_context(
        AuditContext(
            user_id=who["user_id"],
            conversation_id=conversation_id,
            request_id=request.state.request_id,
        )
    ):
        deleted = await run_in_threadpool(
            service(request).soft_delete, who["user_id"], conversation_id
        )
    return envelope(request, deleted.as_dict())


@router.post("/conversations/{conversation_id}/restore")
async def restore_conversation(conversation_id: str, request: Request):
    who = await identity(request)
    with audit_context(
        AuditContext(
            user_id=who["user_id"],
            conversation_id=conversation_id,
            request_id=request.state.request_id,
        )
    ):
        restored = await run_in_threadpool(
            service(request).restore, who["user_id"], conversation_id
        )
    return envelope(request, restored.as_dict())


@router.post("/conversations/bulk-delete")
async def bulk_delete_conversations(body: ConversationBulkDelete, request: Request):
    who = await identity(request)
    with audit_context(
        AuditContext(
            user_id=who["user_id"],
            conversation_id=None,
            request_id=request.state.request_id,
        )
    ):
        result = await run_in_threadpool(
            service(request).bulk_soft_delete,
            who["user_id"],
            conversation_ids=body.ids,
            delete_all=body.all,
        )
    return envelope(request, result)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    request: Request,
    limit: int = 50,
    after: int | None = None,
):
    who = await identity(request)
    page = await run_in_threadpool(
        service(request).messages, who["user_id"], conversation_id, limit=limit, after=after
    )
    return envelope(request, page)


def parse_client_context(raw):
    """Parse the client context, giving each structural failure its own code.

    Only the shape is judged here. Whether the answer names a question that is actually
    open is decided against the checkpoint, which is the only place that can know.
    """

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AppError("INVALID_PARAMETER", "client_context 必须是对象", 422)
    try:
        parsed = ClientContext.model_validate(raw)
    except ValidationError as exc:
        # A resume the graph could never interpret is a recovery failure, not a bad
        # parameter, and the client has to be able to tell those apart.
        code = "INVALID_INTERRUPT_ANSWER" if "resume" in raw else "INVALID_PARAMETER"
        raise AppError(
            code,
            "恢复请求的结构无法识别"
            if code == "INVALID_INTERRUPT_ANSWER"
            else "请求参数格式不正确",
            422,
            details={"fields": [".".join(str(x) for x in error["loc"]) for error in exc.errors()]},
        ) from exc
    if parsed.resume is not None and parsed.mode is not None:
        # Answering a question and abandoning the goal are different intentions; a request
        # that asks for both has not decided which it means.
        raise AppError("INVALID_PARAMETER", "resume 与 mode 不能同时使用", 422)
    return _context_payload(parsed)


def _context_payload(parsed) -> dict:
    """The parsed context as a plain dict, keeping whether `question_id` was sent at all.

    `exclude_none=True` would erase an explicit `question_id: null`, and the runtime cannot
    then tell a client that sent a broken id from one that sent none: the first is a
    protocol error, the second is the legacy shape it still has to support. Presence is
    carried by `model_fields_set`, which is the only place that distinction survives.
    """

    payload = parsed.model_dump(exclude_none=True)
    if parsed.resume is not None:
        resume = parsed.resume.model_dump(exclude_none=False)
        if "question_id" not in parsed.resume.model_fields_set:
            resume.pop("question_id", None)
        payload["resume"] = resume
    return payload


async def _turn_response(
    request: Request,
    who: dict,
    conversation_id: str,
    message: str,
    context: Any,
    key: str | None = None,
    verification_code: str | None = None,
    auto_cancel: bool = False,
) -> Response:
    """Run one submission, choosing SSE or a JSON receipt by what the key has already bought.

    The submission itself -- the conversation lock, the admission, the ledger write and the
    decision between a stream and a receipt -- lives in `app.api.pending_actions`, because the
    decide route needs exactly the same lifecycle and a second copy of it would be a second
    lock protocol. What stays here is the transport: which key to use, and which response shape
    the result calls for.

    `key` is the caller's own idempotency key when it has one to mint; otherwise it is the
    client's header, which every client-supplied write must carry. `verification_code` is a
    secret the user typed for this submission: it rides the turn, not the graph state, so it
    is never checkpointed.
    """

    result = await submit_prepared_turn(
        runtime(request),
        who,
        PendingActionPreparation(
            conversation_id=conversation_id,
            message=message,
            context=context,
            key=key or idempotency_key(request),
            verification_code=verification_code,
            auto_cancel=auto_cancel,
        ),
    )
    return _submission_response(request, who, result)


def _submission_response(request: Request, who: dict, result: PendingActionResult) -> Response:
    """The one place a prepared submission becomes an HTTP answer.

    Held here rather than in the state machine because only this layer knows about SSE, JSON
    envelopes and header values -- and only this layer can hand the lock to the response body,
    which is what releases it when the stream ends. Every path out releases it: the body's
    `finally` for a stream, `submit_prepared_turn` for a receipt, and this function for the
    one case where a response is never produced.
    """

    if result.status == "receipt":
        # The key is spent, so this is a replay: the user message is stored and no graph call
        # is made. Answering with JSON rather than an empty stream is what lets the client
        # tell "nothing new happened" from "the assistant said nothing".
        return envelope_json(request, result.outcome.as_dict())
    try:
        return StreamingResponse(
            _stream_turn(request, who, result.held_lock, result.outcome, result.naming),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
            background=BackgroundTask(result.naming.run),
        )
    except BaseException:
        # No body was ever produced, so nothing else can end this turn: release the lock here
        # rather than leave the conversation wedged for the life of the process.
        result.held_lock.release()
        raise


async def _stream_turn(
    request: Request, who: dict, held: HeldLock, plan: TurnPlan, naming: DeferredNaming
) -> AsyncIterator[str]:
    turns = runtime(request).turns
    try:
        source = turns.stream(who["user_id"], who["session_id"], plan, naming=naming)
        async for frame in event_stream(source, request.state.request_id):
            yield frame
    finally:
        # The request, the body generator and a client disconnect can all end a turn, so the
        # release is idempotent rather than trusted to happen on exactly one path.
        held.release()


def envelope_json(request: Request, data) -> Response:
    """The same envelope as every other JSON response, for the receipt that is not a stream."""

    return JSONResponse(content=envelope(request, data))


@router.post("/conversations/{conversation_id}/messages")
async def post_conversation_message(
    conversation_id: str, body: ConversationMessageCreate, request: Request
):
    who = await identity(request)
    context = parse_client_context(body.client_context)
    return await _turn_response(request, who, conversation_id, body.message, context)


@router.post("/conversations/{conversation_id}/pending-actions/{pending_id}/decide")
async def decide_pending_action(
    conversation_id: str,
    pending_id: str,
    body: PendingActionDecision,
    request: Request,
):
    """One click on a confirmation box: the authenticated, structured button event.

    It is the only way a confirmation reaches a module from the assistant. A chat message
    that says 确认 is an ordinary message and travels the ordinary route, where no module has
    an action that could mint an authorization -- so the two cannot be confused.

    What makes a click an authorization is the binding, and the binding -- with the rest of the
    state machine -- lives in `app.api.pending_actions`, because an IM channel will answer the
    same cards and must not restate those rules. This route is the HTTP adaptation: it
    authenticates the caller, hands the click over, and returns whichever shape came back.

    `verification_code` is required exactly when the card asked for one. It is handed to the
    turn lifecycle as a secret -- not folded into the request body that gets persisted, not
    into the fingerprint, not into the ledger -- so it reaches the module without ever being
    written down.

    The idempotency key comes from the client, as it does on the message route: a double click
    repeats one request and is answered with the first one's receipt, while a *retry* after a
    rejected code is a genuinely different request and must not be deduplicated against it.
    The header is read through a reader, not before the call, so that it is still read *after*
    the writability check -- otherwise a conversation that is not the caller's would answer
    "missing Idempotency-Key" instead of 404.
    """

    who = await identity(request)

    result = await execute_pending_action_decision(
        rt=runtime(request),
        who=who,
        conversation_id=conversation_id,
        pending_id=pending_id,
        question_id=body.question_id,
        decision=body.decision,
        verification_code=body.verification_code,
        transaction_password=body.transaction_password,
        idempotency_key=lambda: idempotency_key(request),
    )
    return _submission_response(request, who, result)


@router.post("/chat/messages")
async def chat(body: ChatMessage, request: Request):
    """The legacy path: a session, resolved to the conversation on that session's thread.

    The body shape is still accepted so an already-deployed frontend keeps working once it
    carries a key. What is *not* accepted is a body without `Idempotency-Key`, and the
    response is no longer always a stream -- a replay answers with a JSON receipt here too,
    because both routes run the same lifecycle.

    A session resolves to a real conversation, so this path audits as `conversation` like any
    other turn: the conversation it runs in is a fact, and naming it `session_chat` would
    make the same interaction look like two different things depending on the URL.
    """

    who = await identity(request)
    if who["session_id"] != body.session_id:
        raise AppError("SESSION_MISMATCH", "聊天会话与登录会话不一致", 401)
    context = parse_client_context(body.client_context)
    conversation = await run_in_threadpool(
        service(request).conversation_for_session, who["user_id"], who["session_id"]
    )
    return await _turn_response(request, who, conversation.conversation_id, body.message, context)
