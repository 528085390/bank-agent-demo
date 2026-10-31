import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  businessIdsOf, clearLastConversation, clearPending, conversationLabel, historyTurns, isWritable,
  listFilter, loadLastConversation, loadPending, listPendingConversationIds, mergeHistory,
  newIdempotencyKey, orderConversations, pendingBody, pendingInterruptOf, pendingMatches,
  runSubmission, saveLastConversation, savePending, unwritableHint, PENDING_SUBMISSION_KEY,
  PENDING_SUBMISSION_PREFIX, type StorageLike,
} from './conversations'
import { RequestError, type SubmitOutcome } from './api'
import { resetStorageScope, setStorageOwner } from './storageScope'
import type { AgentEventName, Conversation, ConversationDetail, ConversationMessage } from './types'

/**
 * The conversation view's pure rules: the record that makes a retry safe, and the readers that
 * turn the message ledger into what the page renders. Every rule here is one the page cannot
 * re-derive from a render, so nothing else would fail if it broke.
 */

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

/** The account these records belong to; every slot is scoped by it (`storageScope.ts`). */
const OWNER = 'u_a'

/** Where one conversation's record lands for `OWNER`. */
const slot = (conversationId: string) => `${PENDING_SUBMISSION_PREFIX}${OWNER}:${conversationId}`

beforeEach(() => setStorageOwner(OWNER))
afterEach(() => {
  resetStorageScope()
  vi.unstubAllGlobals()
})

/** A storage that only ever holds strings, so a read cannot accidentally share an object. */
function fakeStorage(): StorageLike & { dump: () => Record<string, string> } {
  const data = new Map<string, string>()
  return {
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => { data.set(key, value) },
    removeItem: key => { data.delete(key) },
    get length() { return data.size },
    key: index => [...data.keys()][index] ?? null,
    dump: () => Object.fromEntries(data),
  }
}

function message(
  sequence: number,
  role: 'user' | 'assistant',
  content: string,
  extra: Partial<ConversationMessage> = {},
): ConversationMessage {
  return {
    message_id: `m${sequence}`, sequence, role, content, message_kind: 'message',
    question_id: null, run_id: null, created_at: '2026-09-28T00:00:00Z', metadata: null, ...extra,
  }
}

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    conversation_id: 'c-1', title: '转账', pinned: false, archived: false, deleted: false,
    created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T00:00:00Z',
    last_message_at: null, message_count: 0, serial_no: 1, ...overrides,
  }
}

function detail(pending: Record<string, unknown> | null): ConversationDetail {
  return { conversation: conversation(), pending_interrupt: pending, recovery_required: false }
}

describe('newIdempotencyKey', () => {
  it('mints a v4 UUID, and a different one for every submission', () => {
    const first = newIdempotencyKey()
    expect(first).toMatch(UUID_V4)
    expect(newIdempotencyKey()).not.toBe(first)
  })

  it('still mints a key on an engine without randomUUID (jsdom, older browsers)', () => {
    vi.stubGlobal('crypto', {})
    expect(newIdempotencyKey()).toMatch(UUID_V4)
  })
})

describe('the pending submission record', () => {
  it('survives a reload as exactly the request that has to be repeated', () => {
    const storage = fakeStorage()
    const record = {
      key: 'k-1', conversation_id: 'c-1', message: '补充信息：金额：100',
      client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
    }

    savePending(record, storage)

    // Read back through the serialized form: a reload sees storage, never module state.
    expect(JSON.parse(storage.dump()[slot('c-1')])).toEqual(record)
    expect(loadPending('c-1', storage)).toEqual(record)
  })

  it('keeps a plain message free of a client_context key it never had', () => {
    const storage = fakeStorage()
    savePending({ key: 'k-1', conversation_id: 'c-1', message: '查询余额' }, storage)

    expect(JSON.parse(storage.dump()[slot('c-1')]))
      .toEqual({ key: 'k-1', conversation_id: 'c-1', message: '查询余额' })
    expect(loadPending('c-1', storage)).toEqual({ key: 'k-1', conversation_id: 'c-1', message: '查询余额' })
  })

  it('is absent before anything is stored and after it is cleared', () => {
    const storage = fakeStorage()
    expect(loadPending('c-1', storage)).toBeNull()

    savePending({ key: 'k-1', conversation_id: 'c-1', message: '查询余额' }, storage)
    clearPending('c-1', storage)

    expect(loadPending('c-1', storage)).toBeNull()
    expect(storage.dump()).toEqual({})
  })

  it('treats a damaged record as absent rather than repairing it', () => {
    const storage = fakeStorage()
    for (const raw of [
      '{"key":"k-1","conversation_id":"c-1"}', // no message
      '{"key":"","conversation_id":"c-1","message":"x"}', // no key
      '{"key":"k-1","message":"x"}', // no conversation
      'not json',
    ]) {
      storage.setItem(slot('c-1'), raw)
      expect(loadPending('c-1', storage)).toBeNull()
    }
  })

  it('keeps one conversation’s record out of another’s slot', () => {
    const storage = fakeStorage()
    savePending({ key: 'key-a', conversation_id: 'conv_a', message: '甲' }, storage)
    savePending({ key: 'key-b', conversation_id: 'conv_b', message: '乙' }, storage)

    expect(loadPending('conv_a', storage)).toMatchObject({ key: 'key-a', message: '甲' })
    expect(loadPending('conv_b', storage)).toMatchObject({ key: 'key-b', message: '乙' })
    expect(loadPending('conv_c', storage)).toBeNull()
  })

  it('cannot see another account’s record for the same conversation', () => {
    const storage = fakeStorage()
    savePending({ key: 'key-a', conversation_id: 'conv_a', message: '甲的秘密' }, storage)

    // Same browser, later login: the key is built from the account, so this slot is a different
    // one. Nothing to read, and nothing of the previous user's to leak (design §7.3).
    setStorageOwner('u_b')
    expect(loadPending('conv_a', storage)).toBeNull()
    expect(listPendingConversationIds(storage)).toEqual([])

    // Writing now lands in B's own slot and leaves A's untouched.
    savePending({ key: 'key-b', conversation_id: 'conv_a', message: '乙的' }, storage)
    expect(Object.keys(storage.dump()).sort()).toEqual([slot('conv_a'), `${PENDING_SUBMISSION_PREFIX}u_b:conv_a`].sort())
  })

  it('does not read the legacy single slot left by an earlier version', () => {
    const storage = fakeStorage()
    // What an upgrade leaves behind: one slot, from before the records were split and before
    // they knew whose they were. It cannot be proven to belong to whoever logs in next, so it
    // is not adopted -- design §7.3 requires it to be discarded, not migrated.
    storage.setItem(PENDING_SUBMISSION_KEY, JSON.stringify({
      key: 'key-old', conversation_id: 'conv_a', message: '旧的',
    }))

    expect(loadPending('conv_a', storage)).toBeNull()
    expect(listPendingConversationIds(storage)).toEqual([])
  })

  it('clears only its own slot', () => {
    const storage = fakeStorage()
    savePending({ key: 'key-a', conversation_id: 'conv_a', message: '甲' }, storage)
    savePending({ key: 'key-b', conversation_id: 'conv_b', message: '乙' }, storage)

    clearPending('conv_a', storage)

    expect(loadPending('conv_a', storage)).toBeNull()
    expect(loadPending('conv_b', storage)).toMatchObject({ key: 'key-b' })
  })

  it('lists every conversation with a record, in the order they were written', () => {
    const storage = fakeStorage()
    savePending({ key: 'key-b', conversation_id: 'conv_b', message: '乙' }, storage)
    savePending({ key: 'key-a', conversation_id: 'conv_a', message: '甲' }, storage)

    expect(listPendingConversationIds(storage)).toEqual(['conv_b', 'conv_a'])
  })
})

describe('pendingMatches', () => {
  const pending = {
    key: 'k-1', conversation_id: 'c-1', message: '转账 100',
    client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
  }

  it('recognizes the identical request, so only a repeat may reuse the key', () => {
    expect(pendingMatches(pending, 'c-1', {
      message: '转账 100', client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
    })).toBe(true)
  })

  it('refuses a different conversation, message, question or answer', () => {
    const body = {
      message: '转账 100', client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
    }
    expect(pendingMatches(pending, 'c-2', body)).toBe(false)
    expect(pendingMatches(pending, 'c-1', { ...body, message: '转账 200' })).toBe(false)
    expect(pendingMatches(pending, 'c-1', {
      message: '转账 100', client_context: { resume: { question_id: 'q_2', value: { amount: '100' } } },
    })).toBe(false)
    expect(pendingMatches(pending, 'c-1', {
      message: '转账 100', client_context: { resume: { question_id: 'q_1', value: { amount: '999' } } },
    })).toBe(false)
    expect(pendingMatches(pending, 'c-1', { message: '转账 100' })).toBe(false)
    expect(pendingMatches(null, 'c-1', body)).toBe(false)
  })
})

describe('pendingBody', () => {
  it('rebuilds the request the record describes, field for field', () => {
    expect(pendingBody({ key: 'k-1', conversation_id: 'c-1', message: '查询余额' }))
      .toEqual({ message: '查询余额' })
    expect(pendingBody({
      key: 'k-1', conversation_id: 'c-1', message: '补充信息：金额：100',
      client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
    })).toEqual({
      message: '补充信息：金额：100',
      client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
    })
  })
})

describe('the last used conversation', () => {
  it('is remembered until it is cleared', () => {
    const storage = fakeStorage()
    expect(loadLastConversation(storage)).toBeNull()

    saveLastConversation('c-9', storage)
    expect(loadLastConversation(storage)).toBe('c-9')

    clearLastConversation(storage)
    expect(loadLastConversation(storage)).toBeNull()
  })
})

describe('runSubmission', () => {
  /** A stored request, as `savePending` leaves it before the submission starts. */
  const record = {
    key: 'k-1', conversation_id: 'c-1', message: '转账 100',
    client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
  }
  const receipt = { submission_id: 's-1', message_id: 'm-1', run_id: null, duplicate: true as const }

  /** Runs one submission whose frames are `names`, and whose outcome is `outcome`. */
  async function run(
    names: AgentEventName[],
    outcome: SubmitOutcome | Error,
    storage: StorageLike,
  ) {
    const seen: string[] = []
    const result = await runSubmission({
      conversationId: record.conversation_id,
      storage,
      onEvent: name => { seen.push(name) },
      submit: async onEvent => {
        for (const name of names) onEvent(name, {})
        if (outcome instanceof Error) throw outcome
        return outcome
      },
    })
    return { result, seen }
  }

  it('clears the record when the turn reaches final', async () => {
    const storage = fakeStorage()
    savePending(record, storage)

    const { result, seen } = await run(['agent_start', 'final'], { receipt: null }, storage)

    expect(seen).toEqual(['agent_start', 'final'])
    expect(result).toEqual({ kind: 'settled', receipt: null })
    expect(loadPending(record.conversation_id, storage)).toBeNull()
  })

  it('clears the record when the turn pauses on an interrupt', async () => {
    const storage = fakeStorage()
    savePending(record, storage)

    const { result } = await run(['final', 'interrupt'], { receipt: null }, storage)

    expect(result).toEqual({ kind: 'settled', receipt: null })
    expect(loadPending(record.conversation_id, storage)).toBeNull()
  })

  it('clears the record on a JSON receipt, which ends the request without a stream', async () => {
    const storage = fakeStorage()
    savePending(record, storage)

    const { result, seen } = await run([], { receipt }, storage)

    expect(seen).toEqual([])
    expect(result).toEqual({ kind: 'settled', receipt })
    expect(loadPending(record.conversation_id, storage)).toBeNull()
  })

  it('keeps the record when the stream ended without final, interrupt or receipt', async () => {
    const storage = fakeStorage()
    savePending(record, storage)

    const { result } = await run(['agent_start', 'tool_call'], { receipt: null }, storage)

    expect(result).toEqual({ kind: 'unfinished' })
    // Keeping it is what lets a reload repeat the identical request under the identical key.
    expect(loadPending(record.conversation_id, storage)).toEqual(record)
  })

  it('keeps the record when the transport failed, and reports the failure', async () => {
    const storage = fakeStorage()
    savePending(record, storage)
    const failure = new TypeError('网络中断')

    const { result } = await run([], failure, storage)

    expect(result).toEqual({ kind: 'failed', error: failure })
    expect(loadPending(record.conversation_id, storage)).toEqual(record)
  })

  it('clears the record when the server refuses the request, and reports its code', async () => {
    const storage = fakeStorage()
    savePending(record, storage)
    const refusal = new RequestError('IDEMPOTENCY_CONFLICT', '同一个幂等键被用于了不同的请求', 'req_1')

    const { result } = await run([], refusal, storage)

    expect(result).toEqual({ kind: 'refused', error: refusal })
    // A refusal is a verdict: repeating it would be refused the same way, so the key is released.
    expect(loadPending(record.conversation_id, storage)).toBeNull()
  })
})

describe('historyTurns', () => {
  it('pairs each user message with the replies that followed it', () => {
    const turns = historyTurns([
      message(1, 'user', '转账 100'),
      message(2, 'assistant', '已为你完成转账。', { metadata: { business_ids: ['BIZ-1'] } }),
    ])

    expect(turns).toHaveLength(1)
    expect(turns[0].key).toBe('m1')
    expect(turns[0].user?.content).toBe('转账 100')
    expect(turns[0].replies.map(reply => reply.content)).toEqual(['已为你完成转账。'])
    expect(turns[0].unconfirmed).toBe(false)
  })

  it('marks a reply-less tail as unconfirmed, because its outcome is unknown', () => {
    const turns = historyTurns([
      message(1, 'user', '转账 100'),
      message(2, 'assistant', '已完成'),
      message(3, 'user', '再查一下余额'),
    ])

    expect(turns.map(turn => turn.key)).toEqual(['m1', 'm3'])
    expect(turns[0].unconfirmed).toBe(false)
    expect(turns[1].unconfirmed).toBe(true)
  })

  it('does not call an answered question unconfirmed: the flow continued past it', () => {
    const turns = historyTurns([
      message(1, 'user', '转账'),
      message(2, 'user', '补充信息：金额：100', { message_kind: 'interrupt_answer', question_id: 'q_1' }),
      message(3, 'assistant', '已为你完成转账。'),
    ])

    expect(turns).toHaveLength(2)
    expect(turns[0].unconfirmed).toBe(false)
    expect(turns[1].unconfirmed).toBe(false)
  })

  it('keeps an assistant reply a page begins with instead of dropping it', () => {
    const turns = historyTurns([message(5, 'assistant', '上一轮的回复')])

    expect(turns).toHaveLength(1)
    expect(turns[0].user).toBeNull()
    expect(turns[0].replies.map(reply => reply.content)).toEqual(['上一轮的回复'])
  })

  it('renders the ledger in sequence order, whatever order it arrived in', () => {
    const turns = historyTurns([message(3, 'assistant', '回复'), message(1, 'user', '提问')])

    expect(turns).toHaveLength(1)
    expect(turns[0].key).toBe('m1')
    expect(turns[0].user?.content).toBe('提问')
    expect(turns[0].replies.map(reply => reply.content)).toEqual(['回复'])
  })
})

describe('businessIdsOf', () => {
  it('reads the business ids an assistant message carries', () => {
    expect(businessIdsOf(message(1, 'assistant', 'x', { metadata: { business_ids: ['BIZ-1', 'BIZ-2'] } })))
      .toEqual(['BIZ-1', 'BIZ-2'])
  })

  it('renders nothing for absent, empty or unreadable metadata', () => {
    expect(businessIdsOf(message(1, 'assistant', 'x'))).toEqual([])
    expect(businessIdsOf(message(2, 'assistant', 'x', { metadata: {} }))).toEqual([])
    expect(businessIdsOf(message(3, 'assistant', 'x', { metadata: { business_ids: null } }))).toEqual([])
    expect(businessIdsOf(message(4, 'assistant', 'x', {
      metadata: { business_ids: ['BIZ-1', 7, null] } as ConversationMessage['metadata'],
    }))).toEqual(['BIZ-1'])
    expect(businessIdsOf(message(5, 'assistant', 'x', {
      metadata: { business_ids: 'BIZ-1' } as unknown as ConversationMessage['metadata'],
    }))).toEqual([])
  })
})

describe('mergeHistory', () => {
  it('unions two reads by message id, lets the newer read win, and orders by sequence', () => {
    const loaded = [message(2, 'assistant', '旧内容'), message(3, 'user', '再查一下余额')]
    const refreshed = [message(1, 'user', '转账 100'), message(2, 'assistant', '新内容')]

    expect(mergeHistory(loaded, refreshed).map(item => [item.sequence, item.content]))
      .toEqual([[1, '转账 100'], [2, '新内容'], [3, '再查一下余额']])
  })

  it('does not duplicate a message both reads carry', () => {
    const read = [message(1, 'user', '转账 100'), message(2, 'assistant', '已完成')]

    expect(mergeHistory(read, read)).toHaveLength(2)
  })
})

describe('pendingInterruptOf', () => {
  const card = {
    kind: 'input', question_id: 'q_1', agent_id: 'transfer', display: '请补充金额',
    attempt: 1, fields: [{ name: 'amount', label: '金额', type: 'number', required: true, options: [] }],
  }

  it('shows the interrupt the server confirms', () => {
    expect(pendingInterruptOf(detail(card))).toEqual(card)
  })

  it('shows nothing when the server confirms no interrupt', () => {
    expect(pendingInterruptOf(detail(null))).toBeNull()
  })

  it('drops a payload nobody could answer', () => {
    expect(pendingInterruptOf(detail({ ...card, question_id: '' }))).toBeNull()
    expect(pendingInterruptOf(detail({ ...card, fields: [] }))).toBeNull()
  })
})

describe('writability', () => {
  it('accepts only a live conversation', () => {
    expect(isWritable(conversation())).toBe(true)
    expect(isWritable(conversation({ archived: true }))).toBe(false)
    expect(isWritable(conversation({ deleted: true }))).toBe(false)
    expect(isWritable(null)).toBe(false)
  })

  it('explains why the composer is closed', () => {
    expect(unwritableHint(conversation({ archived: true }))).toContain('归档')
    expect(unwritableHint(conversation({ deleted: true }))).toContain('删除')
    expect(unwritableHint(conversation())).toBe('')
    expect(unwritableHint(null)).toBe('')
  })

  it('labels a conversation the user never named', () => {
    expect(conversationLabel(conversation({ title: '  ' }))).toBe('未命名会话')
    expect(conversationLabel(conversation({ title: '转账' }))).toBe('转账')
  })

  it('never folds the serial number into the label', () => {
    // A-15: the number is its own badge; it must not leak into the title text.
    expect(conversationLabel(conversation({ serial_no: 3, title: '转账' }))).toBe('转账')
  })
})

describe('listFilter', () => {
  it('leaves each flag exactly as its own toggle has it, so all four states are reachable', () => {
    // The server's flags are independent: ?archived=true never brings back a soft-deleted
    // conversation, and ?deleted=true never brings back an archived one. A filter that coupled
    // them would make one of the two kinds unreachable.
    expect(listFilter({ archived: false, deleted: false })).toEqual({ archived: false, deleted: false })
    expect(listFilter({ archived: true, deleted: false })).toEqual({ archived: true, deleted: false })
    expect(listFilter({ archived: false, deleted: true })).toEqual({ archived: false, deleted: true })
    expect(listFilter({ archived: true, deleted: true })).toEqual({ archived: true, deleted: true })
  })

  it('does not let one toggle switch the other on', () => {
    expect(listFilter({ archived: true, deleted: false }).deleted).toBe(false)
    expect(listFilter({ archived: false, deleted: true }).archived).toBe(false)
  })
})

describe('orderConversations', () => {
  /** A conversation whose activity is known: last_message_at set, or null for "never written in". */
  function at(iso: string, overrides: Partial<Conversation> = {}): Conversation {
    return conversation({ created_at: iso, last_message_at: iso, ...overrides })
  }

  it('puts a pinned conversation above every unpinned one, however old it is', () => {
    const old = at('2026-09-01T00:00:00Z', { conversation_id: 'old', pinned: true })
    const fresh = at('2026-09-30T00:00:00Z', { conversation_id: 'fresh' })

    expect(orderConversations([fresh, old]).map(item => item.conversation_id)).toEqual(['old', 'fresh'])
  })

  it('counts a conversation nobody has written in from when it was created', () => {
    // The server orders by COALESCE(last_message_at, created_at) (backend/app/conversation/
    // service.py): a brand new conversation has no message yet, and it is still the newest thing
    // on the page. Ordering by last_message_at alone buried it under every older conversation.
    const quiet = conversation({
      conversation_id: 'quiet', created_at: '2026-09-30T00:00:00Z', last_message_at: null,
    })
    const loud = conversation({
      conversation_id: 'loud', created_at: '2026-09-01T00:00:00Z',
      last_message_at: '2026-09-02T00:00:00Z',
    })

    expect(orderConversations([loud, quiet]).map(item => item.conversation_id)).toEqual(['quiet', 'loud'])
  })

  it('breaks a tie by the conversation id, newest first, the way the cursor pages', () => {
    const a = at('2026-09-30T00:00:00Z', { conversation_id: 'conv_a' })
    const b = at('2026-09-30T00:00:00Z', { conversation_id: 'conv_b' })

    expect(orderConversations([a, b]).map(item => item.conversation_id)).toEqual(['conv_b', 'conv_a'])
  })

  it('leaves the array it was given alone', () => {
    const older = at('2026-09-01T00:00:00Z', { conversation_id: 'older' })
    const newer = at('2026-09-02T00:00:00Z', { conversation_id: 'newer' })
    const items = [older, newer]

    orderConversations(items)

    expect(items.map(item => item.conversation_id)).toEqual(['older', 'newer'])
  })
})
