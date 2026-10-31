export interface Session { session_id: string; user_id: string; login_name: string | null }
export interface LlmSettings {
  configured: boolean; mode: 'unconfigured' | 'model'; base_url: string; model: string;
  response_format: 'json_schema' | 'json_object'; timeout_seconds: number; api_key_masked: string;
}

/**
 * Optional per-request context for POST /api/conversations/{id}/messages. Unknown keys are
 * rejected with HTTP 422.
 *
 * `mode` is deliberately absent: a new goal needs a new conversation, and the server refuses
 * `mode: "new_goal"` with `NEW_CONVERSATION_REQUIRED`, so this client has no way to send it.
 */
export interface ClientContext {
  /**
   * The answer to the question that is currently waiting. `question_id` names that question,
   * so an answer meant for an earlier question of the same shape cannot be consumed by a
   * later one. It is optional only for the transition: the server names the single open
   * question when it is omitted.
   */
  resume?: { question_id?: string; value: unknown } | null
}

/** The body of POST /api/conversations/{id}/messages. The conversation is in the path. */
export interface ConversationMessageRequest {
  message: string
  client_context?: ClientContext
}

/** `agent_start` — an agent began working on the current turn. */
export interface AgentStartEvent { agent_id: string }
/** `tool_call` — an agent invoked a tool. */
export interface ToolCallEvent {
  agent_id: string; tool_name: string; call_id: string; arguments: Record<string, unknown>;
}
/** `tool_result` — the outcome of a previously announced tool call. */
export interface ToolResultEvent {
  agent_id: string; tool_name: string; call_id: string; status: string;
  /**
   * The tool's projected envelope -- only what the page may render. `facts` is module-private
   * and is not part of this shape, so the feed cannot print an account number it was not
   * meant to show.
   */
  data: { summary: string | null; display: unknown; business_ids: Record<string, string> | null } | null;
  error: string | null;
  /**
   * The machine-readable failure code, so the UI can branch without matching Chinese:
   * `NO_CUSTOMER_BINDING` offers a binding step, `INSUFFICIENT_BALANCE` does not.
   * `null` on success, and on a failure the server did not classify.
   */
  error_code: string | null;
}
/** One field a paused question asks for. The label is what the user reads; the name is the key. */
export interface InterruptField {
  name: string
  label: string
  type: 'text' | 'number' | 'date' | 'select'
  required: boolean
  options: string[]
}
/**
 * One pending operation a confirmation box shows, as the module that froze it described it.
 *
 * It is a display projection and nothing more: no idempotency key, no verification code, no
 * snapshot. The `pending_id` is the server-issued name of the pending operation, and it is
 * what a click submits -- it does not carry any authority by itself, because the server
 * re-checks ownership, session, intent and expiry before anything is submitted.
 *
 * `requires_code` is what makes the box take the form the operation needs: true means the
 * bank will demand a code alongside the click, so the box renders a code input and the
 * confirm button stays disabled until one is typed. False means a plain confirm box -- asking
 * for a code there would make the user type a secret into a door that does not exist.
 */
export interface ConfirmationCard {
  pending_id: string
  operation: string
  title: string
  /**
   * 卡面金额（分）。`null` = 这次操作没有金额（账单发邮件、冻结银行卡），不显示金额；
   * 数字即使是 0 也照常显示，因为「0 元」是一个金额，不是「没有」。
   */
  amount_minor: number | null
  product_name: string
  account_display: string
  confirm_label: string
  cancel_label: string
  expires_at: string | null
  requires_code: boolean
  code_label: string
}

/** `interrupt` — the turn paused on one question and waits for the fields it names. */
export interface InterruptEvent {
  kind: string
  question_id: string
  agent_id: string
  fields: InterruptField[]
  display: string
  attempt: number
  /** Present exactly on a confirmation box (`kind: "confirmation"`). */
  confirmation?: ConfirmationCard
}
/** `subtask_end` — a delegated sub-agent finished. */
export interface SubtaskEndEvent {
  agent_id: string
  status: string
  summary: string
  /**
   * The module's own status, when it answered the whole turn. `status` speaks the frozen
   * event vocabulary, which has no word for "neither succeeded nor failed"; `unknown` here is
   * how the page can say 待核实 instead of showing that fund write as a failure.
   */
  turn_status: string | null
}
/** `reply_delta` — a piece of the assistant's text, sent while the turn is still running. */
export interface ReplyDeltaEvent { text: string }
/**
 * `narration` — text the assistant streamed before delegating. It is what the model is about
 * to do, not the answer, so the page shows it as activity rather than as the reply.
 */
export interface NarrationEvent { text: string }
/** `final` — the complete assistant reply for the turn, and why the turn ended. */
export interface FinalEvent {
  text: string
  business_ids: string[]
  /**
   * Why the turn ended: `api_error` (the stream broke after text was already sent),
   * `max_turns` (the round hit its ceiling) or `aborted_streaming`. `null` means it
   * completed -- and an older server that never sends the field reads as `null` too.
   */
  terminal_reason: string | null
  /** The upstream HTTP status behind `api_error`, when the server knew it. */
  error_status: number | null
}

/**
 * The six states a task graph node can be in, exactly as the backend's `TaskNodeStatus` names
 * them. Nothing else is a status: a value outside this list is not a state the graph can draw.
 */
export type TaskNodeStatus =
  | 'pending'
  | 'running'
  | 'waiting_confirm'
  | 'done'
  | 'failed'
  | 'skipped'

/** One node of the task graph the supervisor drew for this turn. */
export interface TaskNode {
  /** The node's stable id, and the key a `task_update` matches it by. */
  id: string
  label: string
  /** The `agent_id` that owns the node. */
  owner: string
  /** Ids of the nodes that must finish first. */
  depends_on: string[]
  status: TaskNodeStatus
  /** The node's structured result summary, when it finished and produced one. */
  artifact?: Record<string, unknown> | null
  /** Why the node failed, when it did. */
  error?: string | null
}

/**
 * `plan` — the whole task graph. It is broadcast once when the supervisor draws it, and again
 * on a resume, so a page that was refreshed can redraw the graph it missed.
 */
export interface PlanEvent {
  plan_id?: string
  nodes: TaskNode[]
}
/**
 * `task_update` — one node's state transition. Most frames carry neither `artifact` nor
 * `error`; the parser always fills both, so the page reads `null` rather than `undefined`
 * and cannot mistake an absent field for an artifact it forgot to render.
 */
export interface TaskUpdateEvent {
  plan_id?: string
  node_id: string
  status: TaskNodeStatus
  artifact?: Record<string, unknown> | null
  error?: string | null
}
export interface ClockEvent {
  current_time: string
  [key: string]: unknown
}

export type AgentEvent =
  | { name: 'agent_start'; data: AgentStartEvent }
  | { name: 'tool_call'; data: ToolCallEvent }
  | { name: 'tool_result'; data: ToolResultEvent }
  | { name: 'interrupt'; data: InterruptEvent }
  | { name: 'subtask_end'; data: SubtaskEndEvent }
  | { name: 'reply_delta'; data: ReplyDeltaEvent }
  | { name: 'narration'; data: NarrationEvent }
  | { name: 'final'; data: FinalEvent }
  | { name: 'plan'; data: PlanEvent }
  | { name: 'task_update'; data: TaskUpdateEvent }
  | { name: 'clock'; data: ClockEvent }

export type AgentEventName = AgentEvent['name']

/** One conversation, as `GET /api/conversations` and every mutation return it. */
export interface Conversation {
  conversation_id: string
  /** The per-user stable number the sidebar shows as its `#N` badge. */
  serial_no: number
  title: string
  pinned: boolean
  archived: boolean
  deleted: boolean
  created_at: string
  updated_at: string
  /** Null until the first visible message is appended. */
  last_message_at: string | null
  message_count: number
}

/** One row of the append-only visible-message ledger. */
export interface ConversationMessage {
  message_id: string
  /** Monotonic within the conversation; the ledger's order and its paging cursor. */
  sequence: number
  role: 'user' | 'assistant'
  content: string
  message_kind: 'message' | 'interrupt_answer'
  /** Non-null exactly on an `interrupt_answer`. */
  question_id: string | null
  run_id: string | null
  created_at: string
  metadata: MessageMetadata | null
}

/**
 * The server's per-message metadata. Only `business_ids` is rendered today; anything else the
 * payload carries stays out of the template rather than being spread into the DOM.
 */
export interface MessageMetadata {
  business_ids?: string[] | null
  /**
   * Deep-analysis reports the turn produced, persisted with the assistant message. A device
   * that does not have the report in its local cache shows these as a file placeholder and
   * loads the full report only when the user asks for it.
   */
  analysis_reports?: unknown[] | null
  /**
   * Current pages of `bill_query_transactions` results kept with the assistant message.
   * Only the page that was on screen is stored; the full detail is fetched on demand.
   */
  query_results?: unknown[] | null
  [key: string]: unknown
}

export interface ConversationPage { items: Conversation[]; next_cursor: string | null }
export interface MessagePage { items: ConversationMessage[]; next_cursor: string | null }

/** `GET /api/conversations/{id}`: metadata plus what the checkpoint can safely confirm. */
export interface ConversationDetail {
  conversation: Conversation
  /**
   * Raw wire payload: the same shape an SSE `interrupt` event carries, but still unvalidated
   * data. Narrow it with `pendingInterruptOf` before it reaches a card.
   */
  pending_interrupt: Record<string, unknown> | null
  /** An unfinished execution the server will not resume; the user can only start a new conversation. */
  recovery_required: boolean
}

/** `PATCH /api/conversations/{id}`. Absent fields are left alone. */
export interface ConversationPatch { title?: string; pinned?: boolean; archived?: boolean }

/**
 * The JSON answer to a repeated POST: the user message is already persisted and the graph is
 * not running for this request. It is not an execution result.
 */
export interface SubmissionReceipt {
  submission_id: string
  message_id: string
  run_id: string | null
  duplicate: true
}
