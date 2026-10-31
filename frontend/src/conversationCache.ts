/**
 * What the view already read, kept in memory for the way back.
 *
 * Switching conversations used to mean a blank pane and a round trip to a database on the
 * far side of the country. The ledger and the detail of a conversation that was just open are
 * still what the server would send back, so they are kept here and painted at once; the view
 * then revalidates behind the pane and only what the server disagrees with changes.
 *
 * Nothing is written to localStorage. A ledger carries facts about money, and those should not
 * outlive the tab. Either half is handed back as soon as it has been read: the ledger fills the
 * pane without the title, and the detail sets the confirmed question without the ledger, so a
 * click that arrives while the other half is still on its way is not left waiting for it.
 */

import type { Conversation, ConversationDetail, ConversationMessage } from './types'

/** How many conversations are kept. The sidebar pages 30 rows at a time; 20 covers a working set. */
export const MAX_ENTRIES = 20

/** An entry older than this is not worth showing: the turn behind it may have moved on. */
export const CACHE_TTL_MS = 5 * 60_000

/** Either half may be missing: the two reads of a switch race, and the pane shows what came back. */
export interface CachedView {
  messages?: ConversationMessage[]
  historyCursor?: string | null
  detail?: ConversationDetail
}

/** A reader hands over one half at a time. The two reads of a switch race, so either may arrive first. */
export type CachedHalf =
  | { messages: ConversationMessage[]; historyCursor: string | null }
  | { detail: ConversationDetail }

interface Entry {
  messages?: ConversationMessage[]
  historyCursor?: string | null
  detail?: ConversationDetail
  readAt: number
}

/** Insertion order is the eviction order, so re-inserting is what marks an entry as recently used. */
const entries = new Map<string, Entry>()

function touch(id: string, entry: Entry): void {
  entries.delete(id)
  entries.set(id, entry)
}

export function recall(id: string): CachedView | undefined {
  const entry = entries.get(id)
  if (!entry) return undefined
  if (Date.now() - entry.readAt > CACHE_TTL_MS) {
    entries.delete(id)
    return undefined
  }
  touch(id, entry)
  return {
    // A copy, so a caller that goes on editing its ledger cannot edit what is stored here.
    messages: entry.messages && [...entry.messages],
    historyCursor: entry.historyCursor,
    detail: entry.detail,
  }
}

export function remember(id: string, half: CachedHalf): void {
  const entry = entries.get(id) ?? { readAt: Date.now() }
  if ('messages' in half) {
    entry.messages = [...half.messages]
    entry.historyCursor = half.historyCursor
  } else {
    entry.detail = half.detail
  }
  entry.readAt = Date.now()
  touch(id, entry)
  while (entries.size > MAX_ENTRIES) {
    const oldest = entries.keys().next()
    if (oldest.done) return
    entries.delete(oldest.value)
  }
}

/** A rename or an archive shows in the pane title and the sidebar at the same time, so they share a source. */
export function patch(id: string, conversation: Conversation): void {
  const entry = entries.get(id)
  if (!entry?.detail) return
  entry.detail = { ...entry.detail, conversation }
  entry.readAt = Date.now()
  touch(id, entry)
}

export function forget(id: string): void {
  entries.delete(id)
}

export function resetCache(): void {
  entries.clear()
}
