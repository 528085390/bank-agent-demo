/**
 * The conversation view's pure half: the pending-submission record that makes a retry safe,
 * and the readers that turn the message ledger into what the page renders.
 *
 * None of this touches `fetch` or a component, so every rule below is unit-testable: the
 * storage is passed in, and the ledger readers take plain arrays.
 */

import { RequestError, type SubmitOutcome } from './api'
import { toEvent } from './events'
import { CONTENT_STORAGE, scopedStorageKey, scopedStoragePrefix } from './storageScope'
import type {
  AgentEventName, ClientContext, Conversation, ConversationDetail, ConversationMessage,
  ConversationMessageRequest, InterruptEvent, SubmissionReceipt,
} from './types'

/** The slot the pending record of one conversation lives in: `<base>:<account>:<conversation>`. */
export const PENDING_SUBMISSION_PREFIX = `${CONTENT_STORAGE.pending}:`

/**
 * The single slot every conversation shared before the records were split.
 *
 * Read by nothing and written by nothing: the account-scoped slots replaced it, and a record
 * written by an earlier version cannot be proven to belong to whoever is logging in now. It is
 * kept as a name so the cleanup at login (`storageScope.purgeContentStorage`) can be spelled
 * out in the tests it protects.
 */
export const PENDING_SUBMISSION_KEY = CONTENT_STORAGE.pending
/** The conversation the page reopens on the next visit. */
export const LAST_CONVERSATION_KEY = 'bank-agent.last-conversation'

/**
 * The slice of `localStorage` this module uses, so a test can supply a fake one. `length` and
 * `key` are what `listPendingConversationIds` needs to enumerate the stored slots.
 */
export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
  length: number
  key(index: number): string | null
}

/**
 * A submission that has not reached `final`, `interrupt` or a JSON receipt yet. The four
 * fields are exactly what a repeat needs; nothing about the view is stored.
 */
export interface PendingSubmission {
  key: string
  conversation_id: string
  message: string
  client_context?: ClientContext
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * `localStorage` is absent in the test runner and can throw when storage is disabled by
 * policy; both degrade to "no record" rather than taking the page down.
 */
function browserStorage(): StorageLike | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

function readJson(key: string, storage?: StorageLike): unknown {
  const store = storage ?? browserStorage()
  if (!store) return null
  try {
    const raw = store.getItem(key)
    if (!raw) return null
    return JSON.parse(raw) as unknown
  } catch {
    // Unreadable storage and unparsable JSON both mean the same thing here: no record.
    return null
  }
}

function writeRaw(key: string, value: string, storage?: StorageLike): void {
  const store = storage ?? browserStorage()
  if (!store) return
  try {
    store.setItem(key, value)
  } catch {
    // A full or read-only storage must not break the turn; the record is simply not kept.
  }
}

/**
 * A fresh idempotency key. `crypto.randomUUID` needs a secure context, so a jsdom or older
 * engine falls back to the same v4 construction from `getRandomValues`, and only then to
 * `Math.random` -- a key has to be unique per submission, not unpredictable.
 */
export function newIdempotencyKey(): string {
  const engine = globalThis.crypto
  if (engine && typeof engine.randomUUID === 'function') return engine.randomUUID()
  const bytes = new Uint8Array(16)
  if (engine && typeof engine.getRandomValues === 'function') engine.getRandomValues(bytes)
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function pendingKey(conversationId: string): string | null {
  return scopedStorageKey(CONTENT_STORAGE.pending, conversationId)
}

/** Stores the one request that has to be repeated for this conversation. */
export function savePending(record: PendingSubmission, storage: StorageLike | null = browserStorage()): void {
  if (!storage) return
  const slot = pendingKey(record.conversation_id)
  // No identity, no slot: a draft holds the user's own words, so it must not be written
  // somewhere a later account could read it (storageScope).
  if (!slot) return
  const value: Record<string, unknown> = {
    key: record.key,
    conversation_id: record.conversation_id,
    message: record.message,
  }
  if (record.client_context) value.client_context = record.client_context
  writeRaw(slot, JSON.stringify(value), storage)
}

/**
 * The request this conversation still owes the server, or null.
 *
 * Only this account's slot is read. A record from the previous version, or one left by another
 * account on this machine, is invisible on purpose -- it is someone else's words.
 */
export function loadPending(conversationId: string, storage: StorageLike | null = browserStorage()): PendingSubmission | null {
  const slot = pendingKey(conversationId)
  if (!storage || !slot) return null
  return readPending(slot, storage)
}

export function clearPending(conversationId: string, storage: StorageLike | null = browserStorage()): void {
  const slot = pendingKey(conversationId)
  if (!storage || !slot) return
  try {
    storage.removeItem(slot)
  } catch {
    // A record that cannot be cleared is still safe to repeat: the server dedupes the key.
  }
}

/** Every conversation that still owes the server a request, in the order the records were written. */
export function listPendingConversationIds(storage: StorageLike | null = browserStorage()): string[] {
  if (!storage) return []
  const prefix = scopedStoragePrefix(CONTENT_STORAGE.pending)
  if (!prefix) return []
  const ids: string[] = []
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index)
    if (key?.startsWith(prefix)) ids.push(key.slice(prefix.length))
  }
  return ids
}

/** One stored record, or null when it is absent or damaged. `readJson` reads one slot by its key. */
function readPending(slot: string, storage: StorageLike): PendingSubmission | null {
  const parsed = readJson(slot, storage)
  if (!isRecord(parsed)) return null
  const { key, conversation_id, message } = parsed
  if (typeof key !== 'string' || !key) return null
  if (typeof conversation_id !== 'string' || !conversation_id) return null
  if (typeof message !== 'string') return null
  const record: PendingSubmission = { key, conversation_id, message }
  const context = asClientContext(parsed.client_context)
  if (context) record.client_context = context
  return record
}

function asClientContext(value: unknown): ClientContext | undefined {
  if (!isRecord(value)) return undefined
  const resume = isRecord(value.resume) ? value.resume : null
  if (!resume) return {}
  const questionId = typeof resume.question_id === 'string' && resume.question_id ? resume.question_id : undefined
  return { resume: questionId ? { question_id: questionId, value: resume.value } : { value: resume.value } }
}

/**
 * The exact body a stored record has to be repeated with. One function, so a retry cannot
 * drift from the attempt it is repeating.
 */
export function pendingBody(record: PendingSubmission): ConversationMessageRequest {
  return record.client_context
    ? { message: record.message, client_context: record.client_context }
    : { message: record.message }
}

/**
 * Whether a stored record is this exact request, and may therefore lend it its key. A new
 * message that merely looks similar is not a repeat: the caller mints a fresh key instead.
 */
export function pendingMatches(
  pending: PendingSubmission | null,
  conversationId: string,
  body: ConversationMessageRequest,
): boolean {
  if (!pending) return false
  return pending.conversation_id === conversationId
    && pending.message === body.message
    && contextFingerprint(pending.client_context) === contextFingerprint(body.client_context)
}

/** `undefined` and `{}` are the same request; the answer's value is compared, not its key order. */
function contextFingerprint(context?: ClientContext): string {
  const resume = context?.resume
  if (!resume) return ''
  return JSON.stringify([resume.question_id ?? null, resume.value ?? null])
}

/** What one submission turned out to be, and what that means for the stored record. */
export type TurnResult =
  /** The server confirmed an end to the request: `final`, `interrupt` or a JSON receipt. */
  | { kind: 'settled'; receipt: SubmissionReceipt | null }
  /** The request never reached an end -- the stream stopped early, or the transport failed. */
  | { kind: 'unfinished' }
  /** The server refused the request, so an identical repeat would be refused the same way. */
  | { kind: 'refused'; error: RequestError }
  /** No verdict: the record is kept, and an identical retry is the only meaningful next step. */
  | { kind: 'failed'; error: unknown }

/**
 * Runs one submission and keeps the pending record exactly as long as the request is
 * unfinished.
 *
 * The key and the body are never minted here: a repeat has to be the identical request, so
 * only the caller that stored it decides what is sent. The record is cleared when the server
 * confirms an end (`final`, `interrupt`, or a receipt -- a receipt means the user message is
 * persisted and no graph is running) and when the server refuses the request, because a
 * refusal is a verdict rather than a transport failure. It survives everything else, so a
 * reload or a network error repeats the same request under the same key.
 */
export async function runSubmission(options: {
  submit: (onEvent: (name: AgentEventName, data: Record<string, unknown>) => void) => Promise<SubmitOutcome>
  onEvent?: (name: AgentEventName, data: Record<string, unknown>) => void
  conversationId: string
  storage?: StorageLike | null
}): Promise<TurnResult> {
  const storage = options.storage === undefined ? browserStorage() : options.storage
  let settled = false
  try {
    const outcome = await options.submit((name, data) => {
      if (name === 'final' || name === 'interrupt') settled = true
      options.onEvent?.(name, data)
    })
    if (outcome.receipt) settled = true
    if (!settled) return { kind: 'unfinished' }
    clearPending(options.conversationId, storage)
    return { kind: 'settled', receipt: outcome.receipt }
  } catch (exc) {
    if (exc instanceof RequestError) {
      clearPending(options.conversationId, storage)
      return { kind: 'refused', error: exc }
    }
    return { kind: 'failed', error: exc }
  }
}

export function saveLastConversation(conversationId: string, storage?: StorageLike): void {
  writeRaw(LAST_CONVERSATION_KEY, conversationId, storage)
}

export function loadLastConversation(storage?: StorageLike): string | null {
  const store = storage ?? browserStorage()
  if (!store) return null
  try {
    const value = store.getItem(LAST_CONVERSATION_KEY)
    return value ? value : null
  } catch {
    return null
  }
}

export function clearLastConversation(storage?: StorageLike): void {
  const store = storage ?? browserStorage()
  if (!store) return
  try {
    store.removeItem(LAST_CONVERSATION_KEY)
  } catch {
    // See clearPending.
  }
}

/** The ledger's order is its own `sequence`; the server already sends it that way. */
function ordered(messages: ConversationMessage[]): ConversationMessage[] {
  return [...messages].sort((left, right) => left.sequence - right.sequence)
}

/** `metadata.business_ids`, with everything that cannot be rendered dropped. */
export function businessIdsOf(message: ConversationMessage): string[] {
  const ids = message.metadata?.business_ids
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : []
}

export interface HistoryTurn {
  /** Stable render key: the user message's id, or the first reply's for a reply-only turn. */
  key: string
  /** Null when a page begins with an assistant reply -- that message is shown, not dropped. */
  user: ConversationMessage | null
  replies: ConversationMessage[]
  /**
   * The last turn produced no assistant reply, so its outcome is unknown -- never a failure
   * and never a success. An earlier reply-less turn is not unconfirmed: the only way the flow
   * continued past it was an answer to the question it asked.
   */
  unconfirmed: boolean
}

/** The ledger as turns: each user message, then the assistant messages that followed it. */
export function historyTurns(messages: ConversationMessage[]): HistoryTurn[] {
  const turns: HistoryTurn[] = []
  for (const message of ordered(messages)) {
    const last = turns[turns.length - 1]
    if (message.role === 'user') {
      turns.push({ key: message.message_id, user: message, replies: [], unconfirmed: false })
    } else if (last) {
      last.replies.push(message)
    } else {
      turns.push({ key: message.message_id, user: null, replies: [message], unconfirmed: false })
    }
  }
  const tail = turns[turns.length - 1]
  if (tail && tail.replies.length === 0) tail.unconfirmed = true
  return turns
}

/**
 * Union of two ledger reads, by `message_id`. The newer read wins a conflict, and the result
 * is ordered by the server's `sequence` -- so refreshing after a receipt or a `final` cannot
 * render the same message twice.
 */
export function mergeHistory(
  current: ConversationMessage[],
  incoming: ConversationMessage[],
): ConversationMessage[] {
  const byId = new Map(current.map(message => [message.message_id, message]))
  for (const message of incoming) byId.set(message.message_id, message)
  return ordered([...byId.values()])
}

/**
 * The interrupt the server confirms, narrowed through the same reader the SSE frames use: a
 * payload without a question id or without fields is not a question anyone can answer.
 */
export function pendingInterruptOf(detail: ConversationDetail): InterruptEvent | null {
  const raw = detail.pending_interrupt
  if (!raw) return null
  const event = toEvent('interrupt', raw)
  return event && event.name === 'interrupt' ? event.data : null
}

/**
 * The title the server gives a conversation it has not named yet (`DEFAULT_TITLE` in
 * backend/app/conversation/service.py:42). Naming runs after the answer, so this exact value is
 * what "the name is still in flight" looks like in the list.
 */
export const DEFAULT_CONVERSATION_TITLE = '新会话'

/** A conversation with no title still needs a label in the list. */
export function conversationLabel(conversation: Conversation): string {
  return (conversation.title ?? '').trim() || '未命名会话'
}

/** Archived and soft-deleted conversations accept no new message; running work is not cancelled. */
export function isWritable(conversation: Conversation | null): boolean {
  return !!conversation && !conversation.archived && !conversation.deleted
}

/**
 * The list filter the two sidebar toggles ask for.
 *
 * The server's `archived` and `deleted` are independent include flags -- `?archived=true` never
 * brings back a soft-deleted conversation, and `?deleted=true` never brings back an archived one
 * -- so each toggle drives exactly its own flag and the other is left as its own toggle says.
 * All four combinations are therefore reachable, and a row that is listed always carries the
 * marker for why it is not live.
 */
export function listFilter(
  toggles: { archived: boolean; deleted: boolean },
): { archived: boolean; deleted: boolean } {
  return { archived: toggles.archived, deleted: toggles.deleted }
}

/** When a conversation was last worth looking at, in milliseconds: its last message, or its birth. */
function activityOf(conversation: Conversation): number {
  const stamp = Date.parse(conversation.last_message_at ?? conversation.created_at)
  return Number.isNaN(stamp) ? 0 : stamp
}

/**
 * The list's order, as the server states it: pinned first, then the newest activity, then the id.
 *
 * The server orders by `COALESCE(last_message_at, created_at)` (backend/app/conversation/
 * service.py:432-436): a conversation nobody has written in yet is ordered from its creation, so
 * a brand new one is the newest row rather than the last. The id breaks a tie the same way the
 * cursor pages, so a locally reordered list agrees with the pages the server hands back.
 */
export function orderConversations(items: Conversation[]): Conversation[] {
  return [...items].sort((left, right) => {
    if (left.pinned !== right.pinned) return left.pinned ? -1 : 1
    const at = activityOf(left)
    const bt = activityOf(right)
    if (at !== bt) return bt - at
    if (left.conversation_id === right.conversation_id) return 0
    return left.conversation_id < right.conversation_id ? 1 : -1
  })
}

export function unwritableHint(conversation: Conversation | null): string {
  if (!conversation) return ''
  if (conversation.deleted) return '该会话已删除，恢复后才能继续发送'
  if (conversation.archived) return '该会话已归档，取消归档后才能继续发送'
  return ''
}
