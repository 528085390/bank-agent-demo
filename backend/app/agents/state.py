"""The Supervisor graph's conversation state.

Only what orchestration needs is checkpointed: the running transcript, the instruction
the Supervisor handed to the current domain, the results this request has collected, and
how many routing turns the request has used. Orders, transfers and subscriptions are
business facts and live in a business service, never here.
"""

from typing import Annotated, Any, NotRequired, TypedDict, cast

from langchain_core.messages import AnyMessage, BaseMessage, convert_to_messages
from langgraph.graph.message import add_messages
from langgraph.types import Overwrite

# What a checkpointed message calls itself, in the spelling a prompt expects.
ROLE_NAMES = {"ai": "assistant", "human": "user"}


def append_results(
    current: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Append-only reducer for the structured results and routing records."""

    return [*(current or []), *(incoming or [])]


def append_messages(
    current: list[AnyMessage] | None, incoming: list[AnyMessage] | None
) -> list[AnyMessage]:
    """`add_messages` plus a tolerant receive end.

    A node writes whatever it produced; a plain `{"role", "content"}` mapping is a
    valid write here, so a graph or a test can stay free of the message classes.
    """

    return add_messages(current or [], convert_to_messages(incoming or []))


class PendingField(TypedDict):
    """One field a question asks for, as the client needs to render it.

    The model names a field; the module that owns the domain says what it looks like to a
    user. The split is deliberate: a model writing UI metadata would invent labels, and a
    module writing the question would have to know the model's reply shape.
    """

    name: str
    label: str
    type: str
    required: bool
    options: list[str]


class PendingQuestion(TypedDict):
    """The one question a domain is waiting on.

    Persisted before the pause so that the node calling `interrupt()` can read it back
    instead of asking the model again. That is what makes the answer match the question:
    LangGraph replays the interrupting node on resume, and a regenerated question is not
    guaranteed to be the one the user answered.

    `question_id` names this question. A resume has to echo it, so an answer meant for an
    earlier question of the same shape cannot be consumed by a later one. It is reused when
    the same question is asked again after an illegal answer -- the client is answering the
    question it was shown -- and replaced when a genuinely new question is created.

    `fields` are objects rather than bare names, because a name like `recipient_account`
    is an internal id and not something a user should be shown. The domain's declaration
    supplies the label and the input type; an undeclared field still renders.
    """

    kind: str
    question_id: str
    agent_id: str
    fields: list[PendingField]
    display: str
    attempt: int
    # 待确认操作（`kind="confirmation"`）的展示投影，来自模块的 `TurnResult.confirmation`。
    # 它随问题一起进检查点，所以刷新页面后确认框还在，而且显示的就是用户当时看到的那一笔。
    # 它不是授权凭据：真正的授权是带着 `pending_id` 的受控按钮事件，由模块自己校验归属、
    # 会话、意图版本与有效期。
    confirmation: NotRequired[dict[str, Any]]


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], append_messages]
    goal: str | None
    active_agent: str | None
    domain_results: Annotated[list[dict[str, Any]], append_results]
    turns: int
    # The waiting channels. `pending_question` is the only live pause point for a session;
    # only `mode="new_goal"` may abandon it without an answer.
    pending_question: PendingQuestion | None
    # The raw resume value, stored untouched by the node that interrupts and validated
    # only where it is consumed.
    pending_answer: Any | None
    # Answers accepted for the current domain task, so a second question can be answered
    # with the first one's answer still in hand.
    collected_inputs: dict[str, Any]
    # The domain that asked the user for more information on the previous turn. It is a
    # routing hint for the next turn's Supervisor, not a live pause -- `pending_question` is
    # the pause. `new_agent_state` deliberately does not write it, so an existing thread
    # keeps the value its checkpoint already holds.
    awaiting_agent: str | None
    # Bounded, non-authoritative memory a whole-turn module published about this conversation,
    # keyed by agent id: the current goal, what has been answered, what was shown, which records
    # were referenced. It is cross-turn memory exactly like `awaiting_agent`, so it is absent
    # from `new_agent_state` and survives between turns of one thread. It never carries a
    # confirmation, a verification code or an idempotency key -- `app.context.handoff` only
    # accepts a closed set of semantic fields, and the outer layer validates again before it
    # lands here.
    domain_context: dict[str, Any]
    turn_phase: str
    # Handoffs the model asked for that this round has not reached yet. It lives here, not in a
    # module global, because a queue is per conversation and has to survive the checkpoint the
    # same way the rest of the turn does.
    pending_handoffs: list[dict[str, str]]
    # The task graph this request is running, in the serialised form `TaskPlan.as_dict()`
    # produces, and the node it is on. Written by the dispatch that draws the plan and cleared
    # by the final that ends the turn: a finished turn must not leave a graph behind that reads
    # as work still to do (spec §4.3.3).
    task_plan: dict[str, Any] | None
    active_task_node_id: str | None
    # The activation's private transcript: what this domain said and what its tools answered.
    # It is deliberately not the conversation ledger: that one is the user's, and a domain
    # that wrote its tool chatter into it would leak the loop into every later prompt.
    domain_thread: list[dict[str, Any]]
    # How many tool rounds this activation has spent. A sibling of `domain_thread` on
    # purpose: both survive a parked question and both are cleared when the domain concludes.
    tool_rounds: int


def idle_work_state() -> dict[str, Any]:
    """The waiting channels in their idle form.

    Written by `new_agent_state` and by a domain that concludes, so a new request or a
    domain that finished can never leave a question behind that nobody is waiting on.
    `collected_inputs` is a last-value channel, so a plain `{}` really does replace it.
    The domain loop's working memory (`domain_thread`, `tool_rounds`) is cleared with them:
    a finished activation must not hand its tool chatter to the next one.
    """

    return {
        "pending_question": None,
        "pending_answer": None,
        "collected_inputs": {},
        "turn_phase": "decide",
        "domain_thread": [],
        "tool_rounds": 0,
    }


def goal_reset_state() -> dict[str, Any]:
    """What abandoning a goal writes: the parent graph's own fields, and only those.

    `messages` and `domain_results` are append channels, so a plain `[]` would append
    nothing and silently leave the abandoned goal's transcript and results in place --
    `Overwrite` is what makes the reset real.

    The waiting channels are deliberately absent. A parked question lives in the *domain
    subgraph's* checkpoint namespace, so the parent's `pending_question` is idle even while
    a real question is parked; writing it here would be a no-op that reads like proof the
    subgraph was cleared. What actually abandons the pause is replacing the pending task
    through `as_node="reset_goal"`.

    `awaiting_agent` is not a pause channel but cross-turn routing memory, so it *is* cleared
    here: abandoning the goal abandons the domain that was waiting on it, and the next goal
    must not be routed by a hint from the one that was dropped.

    The domain loop's working memory goes the same way: an abandoned goal's tool transcript
    and round counter belong to the dropped activation, not to whatever the next goal asks.

    The task graph goes with the goal too: its plan describes the work that was just dropped,
    and the next goal draws its own.
    """

    return {
        "messages": Overwrite([]),
        "goal": None,
        "active_agent": None,
        "domain_results": Overwrite([]),
        "turns": 0,
        "awaiting_agent": None,
        "pending_handoffs": [],
        "task_plan": None,
        "active_task_node_id": None,
        "domain_thread": [],
        "tool_rounds": 0,
    }


def new_agent_state(message: str = "", *, message_id: str | None = None) -> AgentState:
    """The state one request starts from, on an existing thread or a fresh one.

    The identity of the caller is never stored: a thread belongs to one session, and
    user_id and session_id stay in the trusted context of the tool handles.

    `goal`, `active_agent` and `turns` are last-value channels, so writing them here is
    itself the reset. `domain_results` is an append channel, where a plain `[]` would
    append nothing and silently leave the previous request's results in place --
    `Overwrite` is what makes that reset real. Results belong to the request that
    collected them: carrying them over would tell the next Supervisor it had already
    gathered things it never asked for, and its own instructions say not to re-ask for
    what it already has.

    `awaiting_agent` is deliberately absent. It is last-value memory of the previous
    request -- which domain asked the user for more information -- and cross-turn memory
    works precisely because a channel absent from the input keeps its checkpointed value.
    Writing it here would zero it on every turn, and the memory would never survive.

    `task_plan` and `active_task_node_id` are last-value channels, so writing None here is the
    reset: a new request starts with no graph, and the previous request's plan must not outlive
    it.

    `message_id` is the ledger's own id for this message. Carrying it into the checkpoint
    is what lets the transcript be reconciled with the ledger: LangGraph's message reducer
    merges by id, so writing the same message twice -- a replay, a resumed node, a retried
    request -- leaves one entry rather than two. A caller with no ledger (a direct runtime
    call) passes nothing, and the message is written the way it always was.
    """

    turn: dict[str, Any] = {"role": "user", "content": message}
    if message_id:
        turn["id"] = message_id
    return cast(
        AgentState,
        {
            "messages": [turn] if message else [],
            "goal": None,
            "active_agent": None,
            "domain_results": Overwrite([]),
            "turns": 0,
            "pending_handoffs": [],
            "task_plan": None,
            "active_task_node_id": None,
            **idle_work_state(),
        },
    )


def message_dicts(messages: list[AnyMessage] | None) -> list[dict[str, str]]:
    """The transcript as plain `{"role", "content"}` turns.

    Checkpointed messages are `BaseMessage` objects; prompts and node logic want the
    simple mapping form, so the conversion lives in exactly one place.
    """

    turns: list[dict[str, str]] = []
    for message in messages or []:
        role, text = role_and_text(message)
        if role is not None and text is not None:
            turns.append({"role": role, "content": text})
    return turns


def role_and_text(message: Any) -> tuple[str | None, str | None]:
    """Read one message as an object or as a plain mapping, whichever it is."""

    if isinstance(message, BaseMessage):
        content = message.content
        return ROLE_NAMES.get(message.type, message.type), content if isinstance(
            content, str
        ) else None
    if isinstance(message, dict):
        role = message.get("role")
        content = message.get("content")
        if isinstance(role, str) and isinstance(content, str):
            return ROLE_NAMES.get(role, role), content
    return None, None
