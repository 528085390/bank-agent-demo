/**
 * The conversation cache on its own.
 *
 * `App.test.ts` checks what the mounted view does with it. The rules that decide whether a
 * stale ledger can be handed back live here: which half of an entry is worth handing back, how
 * long one lives, and which one an eviction takes. They are pure, so they are checked directly.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  CACHE_TTL_MS,
  MAX_ENTRIES,
  forget,
  patch,
  recall,
  remember,
  resetCache,
} from './conversationCache'
import type { Conversation, ConversationDetail, ConversationMessage } from './types'

function message(messageId: string, sequence: number, content: string): ConversationMessage {
  return {
    message_id: messageId,
    sequence,
    role: 'user',
    content,
    message_kind: 'message',
    question_id: null,
    run_id: null,
    created_at: '2026-10-13T00:00:00Z',
    metadata: {},
  }
}

function conversation(conversationId: string, title = conversationId): Conversation {
  return {
    conversation_id: conversationId,
    serial_no: 1,
    title,
    pinned: false,
    archived: false,
    deleted: false,
    created_at: '2026-10-13T00:00:00Z',
    updated_at: '2026-10-13T00:00:00Z',
    last_message_at: '2026-10-13T00:00:00Z',
    message_count: 1,
  }
}

function detail(conversationId: string, title = conversationId): ConversationDetail {
  return {
    conversation: conversation(conversationId, title),
    pending_interrupt: null,
    recovery_required: false,
  }
}

/** The ledger half. The cursor always travels with the page it belongs to. */
function readLedger(conversationId: string, content = `${conversationId} 的消息`) {
  remember(conversationId, { messages: [message(`${conversationId}-1`, 1, content)], historyCursor: null })
}

function readBoth(conversationId: string, content?: string) {
  readLedger(conversationId, content)
  remember(conversationId, { detail: detail(conversationId) })
}

beforeEach(() => {
  resetCache()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  resetCache()
})

describe('the conversation cache', () => {
  it('has nothing for a conversation it never read', () => {
    expect(recall('conv_a')).toBeUndefined()
  })

  it('hands back the ledger half on its own, so a click can show it at once', () => {
    readLedger('conv_a')
    const ledger = recall('conv_a')
    expect(ledger?.messages?.[0].content).toBe('conv_a 的消息')
    expect(ledger?.detail).toBeUndefined()

    remember('conv_a', { detail: detail('conv_a') })
    const both = recall('conv_a')
    expect(both?.messages?.[0].content).toBe('conv_a 的消息')
    expect(both?.detail?.conversation.conversation_id).toBe('conv_a')
  })

  it('hands back the detail half first the same way', () => {
    remember('conv_a', { detail: detail('conv_a') })
    const cached = recall('conv_a')
    expect(cached?.messages).toBeUndefined()
    expect(cached?.detail?.conversation.conversation_id).toBe('conv_a')
  })

  it('keeps the ledger it was handed, not the array the caller still holds', () => {
    const messages = [message('a1', 1, '甲的问题')]
    remember('conv_a', { messages, historyCursor: null })
    remember('conv_a', { detail: detail('conv_a') })

    messages.push(message('a2', 2, '甲的回答'))
    expect(recall('conv_a')?.messages).toHaveLength(1)
  })

  it('does not invent the other half from the one it has', () => {
    readLedger('conv_a')
    expect(recall('conv_a')?.detail).toBeUndefined()
    expect(recall('conv_a')?.historyCursor).toBeNull()
  })

  it('replaces only the conversation when its metadata changes', () => {
    readBoth('conv_a')
    patch('conv_a', conversation('conv_a', '改过名的会话'))

    const cached = recall('conv_a')
    expect(cached?.detail?.conversation.title).toBe('改过名的会话')
    expect(cached?.messages?.[0].content).toBe('conv_a 的消息')
  })

  it('does not invent an entry for metadata of a conversation it never read', () => {
    patch('conv_a', conversation('conv_a', '改过名的会话'))
    expect(recall('conv_a')).toBeUndefined()
  })

  it('drops the entry that was read longest ago once it is full', () => {
    for (let index = 0; index <= MAX_ENTRIES; index += 1) readBoth(`conv_${index}`)

    expect(recall('conv_0')).toBeUndefined()
    expect(recall(`conv_${MAX_ENTRIES}`)?.detail?.conversation.conversation_id).toBe(`conv_${MAX_ENTRIES}`)
  })

  it('counts a read as a use, so the entry just read survives the next eviction', () => {
    for (let index = 0; index < MAX_ENTRIES; index += 1) readBoth(`conv_${index}`)

    expect(recall('conv_0')).toBeDefined()
    readBoth('conv_extra')

    expect(recall('conv_0')).toBeDefined()
    expect(recall('conv_1')).toBeUndefined()
  })

  it('drops an entry older than its time to live', () => {
    readBoth('conv_a')
    vi.advanceTimersByTime(CACHE_TTL_MS + 1)
    expect(recall('conv_a')).toBeUndefined()
  })

  it('forgets one conversation, and clears all of them on request', () => {
    readBoth('conv_a')
    readBoth('conv_b')

    forget('conv_a')
    expect(recall('conv_a')).toBeUndefined()
    expect(recall('conv_b')).toBeDefined()

    resetCache()
    expect(recall('conv_b')).toBeUndefined()
  })
})
