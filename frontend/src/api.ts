import { csrfHeaders } from './csrf'
import type {
  AgentEventName, ClientContext, Conversation, ConversationDetail, ConversationMessageRequest,
  ConversationPage, ConversationPatch, InterruptEvent, MessagePage, SubmissionReceipt,
} from './types'

export interface ApiError { code: string; message: string; retryable?: boolean; details?: unknown }
export interface Envelope<T> { request_id: string; data: T; error: ApiError | null }

/**
 * A failed response, carrying the server's `details` alongside the message.
 *
 * `details` is not decoration: `ORIGIN_REJECTED` puts the origin it received and the hosts it
 * expected there, and that is the only way the page can tell a user *which* address disagreed.
 * Dropping it is what made every source-check failure read as the same sentence.
 */
export class RequestError extends Error {
  constructor(
    public code: string,
    message: string,
    public requestId: string,
    public details?: unknown,
  ) { super(message) }
}

/**
 * The codes that mean "this browser is no longer who the server thinks it is".
 *
 * `SESSION_REQUIRED` is the plain "no valid session"; `SESSION_MISMATCH` is a token issued for a
 * login that has since been revoked. Both are the same thing to the page: stop, and ask again.
 */
const AUTH_ERROR_CODES = new Set(['SESSION_REQUIRED', 'SESSION_MISMATCH'])

/**
 * The page's single reaction to a dead session.
 *
 * It lives here rather than in `auth.ts` because *every* transport goes through this module --
 * messages, streams, uploads, the bill card -- and a 401 noticed by only one of them is a page
 * left standing on another account's content. `auth.ts` imports this module, so the handler is
 * registered from there instead of imported back.
 */
let unauthorized: ((error: RequestError) => void) | null = null

export function onUnauthorized(handler: ((error: RequestError) => void) | null): void {
  unauthorized = handler
}

/** Every failure leaves through here, so no call site can forget to report a dead session. */
function raise(error: RequestError): RequestError {
  if (AUTH_ERROR_CODES.has(error.code)) unauthorized?.(error)
  return error
}

/** The only SSE event names the message endpoints are allowed to emit. */
const AGENT_EVENT_NAMES: readonly string[] = [
  'agent_start', 'tool_call', 'tool_result', 'interrupt', 'subtask_end',
  'reply_delta', 'narration', 'final',
  'plan', 'task_update', 'clock',
]

/**
 * The resume envelope for one answered question.
 *
 * The question is named, not just described by its fields: an answer to an earlier question
 * that happened to ask for the same fields must not be consumed by a later one.
 */
export function resumeFor(question: InterruptEvent, value: Record<string, unknown>): ClientContext {
  return { resume: { question_id: question.question_id, value } }
}

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

/** Query parameters the server does not know about are never invented here. */
function queryString(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [name, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(name, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

const conversationPath = (conversationId: string) => `/conversations/${encodeURIComponent(conversationId)}`

/**
 * A non-2xx answer is an envelope with a non-null error. A body that is not JSON at all still
 * has to surface as a `RequestError`, so the caller's error handling stays in one place.
 */
async function failure(response: Response): Promise<RequestError> {
  let payload: Envelope<unknown> | null = null
  try {
    payload = await response.json() as Envelope<unknown>
  } catch {
    payload = null
  }
  return raise(new RequestError(
    payload?.error?.code ?? 'HTTP_ERROR',
    payload?.error?.message ?? '请求失败',
    payload?.request_id ?? '',
    payload?.error?.details,
  ))
}

export async function request<T>(
  path: string,
  body?: unknown,
  key?: string,
  method?: Method,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: method ?? (body === undefined ? 'GET' : 'POST'), credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(),
      ...(key ? { 'Idempotency-Key': key } : {}),
    },
    ...(signal ? { signal } : {}),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const payload: Envelope<T> = await response.json()
  if (!response.ok || payload.error) {
    throw raise(new RequestError(payload.error?.code ?? 'HTTP_ERROR',
      payload.error?.message ?? '请求失败', payload.request_id, payload.error?.details))
  }
  return payload.data
}

/**
 * The conversation list.
 *
 * `archived` and `deleted` are two independent "include" flags, not one "show the inactive
 * ones" flag: `?archived=true` adds archived rows to the live ones, `?deleted=true` adds
 * soft-deleted rows, and neither implies the other. Both are always sent explicitly, so the
 * server's own default is never what decides what the list contains.
 */
export function listConversations(
  options: { limit?: number; cursor?: string | null; archived?: boolean; deleted?: boolean } = {},
): Promise<ConversationPage> {
  const search = queryString({
    limit: options.limit ?? 20,
    cursor: options.cursor ?? null,
    archived: options.archived ?? false,
    deleted: options.deleted ?? false,
  })
  return request<ConversationPage>(`/conversations${search}`)
}

export function createConversation(title?: string): Promise<Conversation> {
  return request<unknown>('/conversations', title ? { title } : {}).then(conversationOf)
}

export async function upload<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: 'POST', credentials: 'same-origin', headers: csrfHeaders(), body: form,
  })
  const payload: Envelope<T> = await response.json()
  if (!response.ok || payload.error) {
    throw raise(new RequestError(payload.error?.code ?? 'HTTP_ERROR', payload.error?.message ?? '上传失败', payload.request_id, payload.error?.details))
  }
  return payload.data
}

export function getConversation(conversationId: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(conversationPath(conversationId))
}

export function updateConversation(conversationId: string, patch: ConversationPatch): Promise<Conversation> {
  return request<unknown>(conversationPath(conversationId), patch, undefined, 'PATCH').then(conversationOf)
}

export function deleteConversation(conversationId: string): Promise<Conversation> {
  return request<unknown>(conversationPath(conversationId), undefined, undefined, 'DELETE').then(conversationOf)
}

export function restoreConversation(conversationId: string): Promise<Conversation> {
  return request<unknown>(`${conversationPath(conversationId)}/restore`, {}).then(conversationOf)
}

/**
 * Batch soft-delete. `all` wipes every live conversation the caller owns; otherwise only the
 * named ids are touched. The server skips ids it does not own or that are already gone, so the
 * caller never has to pre-filter.
 */
export interface ConversationBulkDeleteBody {
  ids?: string[]
  all?: boolean
}

export interface ConversationBulkDeleteResult {
  deleted_count: number
  conversation_ids: string[]
}

export function bulkDeleteConversations(
  body: ConversationBulkDeleteBody,
): Promise<ConversationBulkDeleteResult> {
  return request<ConversationBulkDeleteResult>('/conversations/bulk-delete', body)
}

export function listMessages(
  conversationId: string,
  options: { limit?: number; after?: string | null } = {},
): Promise<MessagePage> {
  const search = queryString({ limit: options.limit ?? 50, after: options.after ?? null })
  return request<MessagePage>(`${conversationPath(conversationId)}/messages${search}`)
}

function isConversation(value: unknown): value is Conversation {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  // The serial number is part of what the sidebar renders, so a payload without one is not a
  // conversation this client can show: it would render "#undefined".
  return typeof record.conversation_id === 'string' && record.conversation_id !== ''
    && typeof record.serial_no === 'number'
}

/**
 * Every single-conversation mutation answers with the conversation itself. The frozen ledger
 * writes the POST row as `{conversation}` while the brief writes `data = conversation`; both
 * readings are accepted here, so the list works whichever one the server implemented.
 */
function conversationOf(data: unknown): Conversation {
  if (isConversation(data)) return data
  if (typeof data === 'object' && data !== null) {
    const wrapped = (data as Record<string, unknown>).conversation
    if (isConversation(wrapped)) return wrapped
  }
  throw new RequestError('UNEXPECTED_RESPONSE', '服务端返回了无法识别的会话数据', '')
}

/** What a POST to the messages endpoint actually produced. */
export interface SubmitOutcome {
  /** Non-null when the server answered with a JSON receipt instead of a stream. */
  receipt: SubmissionReceipt | null
}

/**
 * Submits one message and streams its turn.
 *
 * The response type varies by request: a first submission streams the six agent events, while
 * a repeat of the same key answers with a JSON receipt and runs no graph. The `Content-Type`
 * is therefore read before a parser is chosen -- a POST is never assumed to be SSE.
 */
export async function submitMessage(
  conversationId: string,
  body: ConversationMessageRequest,
  key: string,
  onEvent: (name: AgentEventName, data: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<SubmitOutcome> {
  const response = await fetch(`/api${conversationPath(conversationId)}/messages`, {
    method: 'POST', credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json', 'Idempotency-Key': key, ...csrfHeaders(),
    },
    ...(signal ? { signal } : {}),
    body: JSON.stringify(body),
  })
  return readSubmission(response, onEvent)
}

/** One click on a confirmation box, as the button route takes it. */
export interface PendingActionRequest {
  question_id: string
  decision: 'confirm' | 'cancel'
  /**
   * What the user typed into the code input, present exactly when the card asked for one.
   * It is a secret, and it lives only in this request: it is never stored, and the page keeps
   * it in memory only until the click is answered.
   */
  verification_code?: string
  transaction_password?: { old_password?: string; new_password: string; confirm_password: string }
}

/**
 * Submits one click on a confirmation box and streams the turn it resumes.
 *
 * This is a route of its own rather than a message: the click is a structured, authenticated
 * event that names the pause it answers and the pending operation that pause was built from,
 * so it cannot be forged from chat text.
 *
 * The idempotency key is the caller's, as on the message route. A double click repeats one
 * request and is answered with the first one's receipt; a *retry* after a rejected code is a
 * different request and must not be deduplicated against it -- which is exactly what a
 * server-derived key would have done, since the click looks identical from the outside.
 */
export async function decidePendingAction(
  conversationId: string,
  pendingId: string,
  body: PendingActionRequest,
  key: string,
  onEvent: (name: AgentEventName, data: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<SubmitOutcome> {
  const response = await fetch(
    `/api${conversationPath(conversationId)}/pending-actions/${encodeURIComponent(pendingId)}/decide`,
    {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key, ...csrfHeaders() },
      ...(signal ? { signal } : {}),
      body: JSON.stringify(body),
    },
  )
  return readSubmission(response, onEvent)
}

/**
 * One submission's response, whichever shape it came back in.
 *
 * Shared by the message route and the button route so the two cannot drift: both answer with
 * either a stream of the six agent events or a JSON receipt, and the `Content-Type` -- not the
 * endpoint -- is what decides which.
 */
async function readSubmission(
  response: Response,
  onEvent: (name: AgentEventName, data: Record<string, unknown>) => void,
): Promise<SubmitOutcome> {
  const contentType = (response.headers.get('Content-Type') ?? '').toLowerCase()
  if (!response.ok) throw await failure(response)
  if (contentType.includes('text/event-stream')) {
    await readEventStream(response, onEvent)
    return { receipt: null }
  }
  if (contentType.includes('application/json')) {
    const payload: Envelope<SubmissionReceipt | null> = await response.json()
    if (payload.error) throw raise(new RequestError(payload.error.code, payload.error.message, payload.request_id, payload.error.details))
    return { receipt: payload.data }
  }
  throw new RequestError('UNEXPECTED_CONTENT_TYPE', '服务端返回了无法识别的响应类型', '')
}

/**
 * Reads one SSE response. Every frame is `event: <name>\ndata: <envelope>\n\n`; the name is
 * always one of the six known agent events, so unknown names and data-only frames are dropped
 * instead of being surfaced as messages. A non-null `error` in any frame rejects the promise.
 */
export async function readEventStream(
  response: Response,
  onEvent: (name: AgentEventName, data: Record<string, unknown>) => void,
): Promise<void> {
  if (!response.body) throw new Error('响应流不可用')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  function consume(frame: string) {
    const lines = frame.split('\n')
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
    if (!data) return
    const payload: Envelope<Record<string, unknown>> = JSON.parse(data)
    if (payload.error) throw raise(new RequestError(payload.error.code, payload.error.message, payload.request_id, payload.error.details))
    const eventLine = lines.find(line => line.startsWith('event:'))
    if (!eventLine) return
    const name = eventLine.slice(6).trim()
    if (!AGENT_EVENT_NAMES.includes(name)) return
    if (payload.data === null || typeof payload.data !== 'object') return
    onEvent(name as AgentEventName, payload.data)
  }
  try {
    for (;;) {
      const { value, done } = await reader.read()
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, '\n')
      let end: number
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        consume(buffer.slice(0, end)); buffer = buffer.slice(end + 2)
      }
      if (done) { if (buffer.trim()) consume(buffer); break }
    }
  } finally { await reader.cancel(); reader.releaseLock() }
}

// --- 飞书 IM 接入 -------------------------------------------------------------------------

/** 面板打开时后端告诉我们的三件事：配没配、连没连、几个人绑着。 */
export interface FeishuStatus {
  enabled: boolean
  configured: boolean
  app_id: string
  running: boolean
  bound_users: number
}

/** 一次扫码：二维码由服务端渲染成 SVG（前端没有 QR 依赖），device_code 留在服务端。 */
export interface FeishuSetup {
  setup_id: string
  user_code: string
  link: string
  qr_svg: string
  expires_in: number
  interval: number
}

export type FeishuSetupState = 'pending' | 'authorized' | 'denied' | 'expired' | 'cancelled'

export interface FeishuSetupProgress {
  setup_id: string
  state: FeishuSetupState
  message: string
  retry_after: number
}

/** 终态：之后再问也不会变了，轮询该停。 */
export function isFeishuSetupSettled(state: FeishuSetupState): boolean {
  return state !== 'pending'
}

export const feishuStatus = () => request<FeishuStatus>('/channels/feishu/status')

export const startFeishuSetup = () => request<FeishuSetup>('/channels/feishu/setup', {})

export const pollFeishuSetup = (setupId: string) =>
  request<FeishuSetupProgress>('/channels/feishu/setup/' + encodeURIComponent(setupId))

/** 取消扫码：幂等，扫到一半改主意时用。 */
export const cancelFeishuSetup = (setupId: string) =>
  request<FeishuSetupProgress>('/channels/feishu/setup/' + encodeURIComponent(setupId), undefined, undefined, 'DELETE')

/** 取消接入：停长连接、抹掉凭据、清掉绑定。 */
export const disconnectFeishu = () =>
  request<{ disconnected: boolean; bound_users: number }>('/channels/feishu/disconnect', {})


// --- 续费提醒 -----------------------------------------------------------------------------

/** 一条即将自动扣款的订阅；金额是最小单位分，展示前交给 money.ts。 */
export interface SubscriptionReminder {
  subscription_id: string
  name: string
  merchant_name: string
  amount_minor: number
  next_due_date: string
  days_until_due: number
}

export interface SubscriptionReminderPage {
  items: SubscriptionReminder[]
  count: number
  total_minor: number
  horizon_days: number
}

/** 工作台启动时自己调用；默认窗口 14 天，后台按银行模拟日期计算。 */
export const subscriptionReminders = () =>
  request<SubscriptionReminderPage>('/reminders/subscriptions')
