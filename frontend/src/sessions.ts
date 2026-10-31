import { reactive } from 'vue'

import type { AgentEvent, ConversationMessage, FinalEvent, InterruptEvent } from './types'

/**
 * One conversation's own view state. Everything in here used to be a page-level singleton, which
 * is exactly why a turn in flight made the whole page -- every other conversation included --
 * untouchable.
 */

/** A turn as it is being streamed, before the ledger can know anything about it. */
export interface LiveTurn {
  user: string
  at: string
  activity: AgentEvent[]
  reply: FinalEvent | null
  replyDraft: string
  unconfirmed: boolean
  /**
   * The id of the last message the ledger held when the turn started, or null for an empty
   * ledger. The pane renders the ledger only up to this message, so once the ledger catches up
   * the user message and the reply are not drawn a second time.
   */
  watermark: string | null
}

export interface ConversationSession {
  messages: ConversationMessage[]
  historyCursor: string | null
  historyBusy: boolean
  /**
   * The ledger in `messages` has been read from the server at least once. A turn needs this before
   * it can record its cut: an empty `messages` means "the ledger is empty" only when the ledger has
   * actually been read -- otherwise a turn started on a conversation nobody has opened would record
   * a null cut and hide the whole ledger once it arrives.
   */
  historyLoaded: boolean
  live: LiveTurn | null
  pending: InterruptEvent | null
  recoveryRequired: boolean
  replyNotice: string | null
  error: string
  trace: string
  canRetry: boolean
  answers: Record<string, string | number>
  confirmationCode: string
  cardVerificationCode: string
  cardAuthorizationError: string
  /** A turn is streaming in this conversation; nothing else may be sent into it until it ends. */
  running: boolean
  /** The running turn is ordinary reasoning and can be stopped; a confirmed write cannot. */
  stoppable: boolean
  /** It finished something while it was not on screen and nobody has looked at it yet. */
  unseen: boolean
}

/** Beyond this many conversations the least recently used idle one is dropped. */
export const MAX_SESSIONS = 8

/**
 * The one definition of an untouched conversation, so a new session and the no-conversation
 * fallback can never drift apart. It hands back a fresh object -- with its own messages array and
 * its own answers object -- every call, because two sessions that shared either would write into
 * each other.
 */
function emptySession(): ConversationSession {
  return {
    messages: [],
    historyCursor: null,
    historyBusy: false,
    historyLoaded: false,
    live: null,
    pending: null,
    recoveryRequired: false,
    replyNotice: null,
    error: '',
    trace: '',
    canRetry: false,
    answers: {},
    confirmationCode: '',
    cardVerificationCode: '',
    cardAuthorizationError: '',
    running: false,
    stoppable: false,
    unseen: false,
  }
}

/**
 * What the pane shows when no conversation is open, or one has not been touched yet. Every such
 * pane shares this one object, so nothing may write into it: the only v-models that could are the
 * card fields, and those sit behind `v-if="pane.pending"`, which this object never satisfies.
 */
export const EMPTY_SESSION: ConversationSession = emptySession()

const sessions = reactive(new Map<string, ConversationSession>())

/**
 * The conversation the pane is showing. It is never evicted: dropping it would blank the open
 * conversation out from under the user, and take its answers, its live turn and its retry banner
 * with it.
 */
let focusedId: string | null = null

/** Marks which conversation the pane is showing, so eviction can leave it alone. */
export function focusSession(conversationId: string | null): void {
  focusedId = conversationId
}

/**
 * The view state of one conversation, created on demand and moved to the most recently used end.
 * Only code that is about to write into a conversation calls this: a render must never create
 * state, or merely drawing the sidebar would evict something.
 */
export function sessionOf(conversationId: string): ConversationSession {
  const existing = sessions.get(conversationId)
  if (existing) {
    sessions.delete(conversationId)
    sessions.set(conversationId, existing)
    return existing
  }
  const created = reactive(emptySession())
  sessions.set(conversationId, created)
  evict(conversationId)
  return created
}

/** The view state of a conversation if it has one. Reading never creates and never reorders. */
export function peekSession(conversationId: string): ConversationSession | undefined {
  return sessions.get(conversationId)
}

/**
 * Drops a conversation's view state outright. Nothing in the app calls this yet -- a session is
 * kept until the cap evicts it -- but it is the hook a shrinking conversation list would use, and
 * the eviction tests drive it directly.
 */
export function forgetSession(conversationId: string): void {
  sessions.delete(conversationId)
}

export function resetSessions(): void {
  sessions.clear()
  focusedId = null
}

/**
 * Drops the least recently used conversation that is not in a state the user can still come back
 * to: the pane's own, one that is running, one holding a result nobody has looked at yet, and one
 * holding a card the user is part-way through. The one just touched is never dropped either: it
 * is the most recently used, and evicting it would make the conversation the user just acted on
 * vanish while older idle ones stay. When every session is in such a state the cap is passed
 * rather than met -- it is a soft cap, because dropping any of them would lose work.
 */
function evict(keepId: string): void {
  if (sessions.size <= MAX_SESSIONS) return
  for (const [id, session] of sessions) {
    if (id === keepId || id === focusedId || session.running || session.unseen || session.live || session.pending) continue
    sessions.delete(id)
    if (sessions.size <= MAX_SESSIONS) return
  }
}
