import { beforeEach, describe, expect, it } from 'vitest'

import {
  EMPTY_SESSION, MAX_SESSIONS, focusSession, forgetSession, peekSession, resetSessions, sessionOf,
} from './sessions'
import type { LiveTurn } from './sessions'
import type { InterruptEvent } from './types'

/**
 * The view state of one conversation. It is a Map and not a page-level singleton, which is the
 * whole point: a turn running in one conversation must not be able to freeze another.
 */
describe('the view state of one conversation', () => {
  beforeEach(() => resetSessions())

  it('hands back the same object for the same conversation and a separate one for another', () => {
    const a = sessionOf('conv_a')
    a.running = true

    expect(sessionOf('conv_a')).toBe(a)
    expect(sessionOf('conv_a').running).toBe(true)
    expect(sessionOf('conv_b').running).toBe(false)
  })

  it('starts a conversation idle, with nothing loaded', () => {
    const session = sessionOf('conv_a')

    expect(session).toMatchObject({
      messages: [], historyCursor: null, historyBusy: false, historyLoaded: false, live: null, pending: null,
      recoveryRequired: false, replyNotice: null, error: '', trace: '', canRetry: false,
      confirmationCode: '', cardVerificationCode: '', cardAuthorizationError: '',
      running: false, unseen: false,
    })
    expect(session.answers).toEqual({})
    expect(EMPTY_SESSION.messages).toEqual([])
    expect(EMPTY_SESSION.live).toBeNull()
    expect(EMPTY_SESSION.running).toBe(false)
  })

  it('never hands the empty default out as a conversation of its own', () => {
    expect(peekSession('conv_a')).toBeUndefined()
    expect(sessionOf('conv_a')).not.toBe(EMPTY_SESSION)
    expect(peekSession('conv_a')).toBe(sessionOf('conv_a'))
  })

  it('forgets one conversation without touching the others', () => {
    sessionOf('conv_a').running = true
    sessionOf('conv_b')

    forgetSession('conv_a')

    expect(peekSession('conv_a')).toBeUndefined()
    expect(peekSession('conv_b')).toBeDefined()
  })

  it('drops the least recently used conversation once the cap is passed', () => {
    const ids = Array.from({ length: MAX_SESSIONS }, (_, index) => `conv_` + index)
    for (const id of ids) sessionOf(id)
    // Using one makes it the most recently used, so conv_1 is the oldest idle conversation.
    sessionOf('conv_0')

    sessionOf('conv_new')

    expect(peekSession('conv_0')).toBeDefined()
    expect(peekSession('conv_1')).toBeUndefined()
    expect(peekSession('conv_new')).toBeDefined()
  })

  it('never drops a conversation that is running or holds something unseen', () => {
    // The two holders are made first, so they sit ahead of the idle conversations in the
    // least-recently-used order. Eviction walks that order and stops as soon as the cap is met
    // again, so only holders the walk reaches first can show the protection: with either check
    // gone it would take conv_0 or conv_1 instead of the idle conv_2 and conv_3.
    sessionOf('conv_0').running = true
    sessionOf('conv_1').unseen = true
    for (let index = 2; index < MAX_SESSIONS; index += 1) sessionOf(`conv_` + index)

    sessionOf('conv_new_a')
    sessionOf('conv_new_b')

    // A running turn and a result nobody has looked at are both the user's to come back to: both
    // holders survive, and the two oldest idle conversations pay for the two new ones.
    expect(peekSession('conv_0')).toBeDefined()
    expect(peekSession('conv_1')).toBeDefined()
    expect(peekSession('conv_2')).toBeUndefined()
    expect(peekSession('conv_3')).toBeUndefined()
  })

  it('never drops a conversation that holds a live turn', () => {
    const liveTurn: LiveTurn = {
      user: 'a message',
      at: '2026-10-13T20:47:29+08:00',
      activity: [],
      reply: null,
      replyDraft: '',
      unconfirmed: false,
      watermark: null,
    }
    // The two holders are made first, so they sit ahead of the idle conversations in the
    // least-recently-used order. Eviction walks that order and stops as soon as the cap is met
    // again, so only holders the walk reaches first can show the protection: with the live check
    // gone it would take conv_0 and conv_1 instead of the idle conv_2.
    sessionOf('conv_0').live = liveTurn
    sessionOf('conv_1').live = liveTurn
    for (let index = 2; index < MAX_SESSIONS; index += 1) sessionOf(`conv_` + index)

    sessionOf('conv_new_a')
    sessionOf('conv_new_b')

    // A streaming turn has nowhere else to live: both holders survive, and the two oldest idle
    // conversations pay for the two new ones.
    expect(peekSession('conv_0')).toBeDefined()
    expect(peekSession('conv_1')).toBeDefined()
    expect(peekSession('conv_2')).toBeUndefined()
    expect(peekSession('conv_3')).toBeUndefined()
  })

  it('never drops a conversation that is waiting on a pending question', () => {
    const pendingQuestion: InterruptEvent = {
      kind: 'question',
      question_id: 'q_1',
      agent_id: 'agent_1',
      fields: [],
      display: 'Which account?',
      attempt: 1,
    }
    // Same shape as the live case: the holders come first in the eviction walk, so the pending
    // check is what makes it pass over them and reach the idle conversations.
    sessionOf('conv_0').pending = pendingQuestion
    sessionOf('conv_1').pending = pendingQuestion
    for (let index = 2; index < MAX_SESSIONS; index += 1) sessionOf(`conv_` + index)

    sessionOf('conv_new_a')
    sessionOf('conv_new_b')

    // An unanswered question is the user's to come back to: both holders survive, and the two
    // oldest idle conversations go.
    expect(peekSession('conv_0')).toBeDefined()
    expect(peekSession('conv_1')).toBeDefined()
    expect(peekSession('conv_2')).toBeUndefined()
    expect(peekSession('conv_3')).toBeUndefined()
  })

  it('never drops a conversation that holds a result nobody has looked at yet', () => {
    // Same shape as the live and pending cases: the two holders are made first, so they sit ahead
    // of the idle conversations in the least-recently-used order. Eviction walks that order and
    // stops as soon as the cap is met again, so only holders the walk reaches first can show the
    // protection: with the unseen check gone it would take conv_0 and conv_1 instead of the idle
    // conv_2 and conv_3.
    sessionOf('conv_0').unseen = true
    sessionOf('conv_1').unseen = true
    for (let index = 2; index < MAX_SESSIONS; index += 1) sessionOf(`conv_` + index)

    sessionOf('conv_new_a')
    sessionOf('conv_new_b')

    // A finished turn the user has not looked at is still theirs to come back to: both holders
    // survive, and the two oldest idle conversations pay for the two new ones.
    expect(peekSession('conv_0')).toBeDefined()
    expect(peekSession('conv_1')).toBeDefined()
    expect(peekSession('conv_2')).toBeUndefined()
    expect(peekSession('conv_3')).toBeUndefined()
  })

  it('does not create a conversation when the sidebar only looks at one', () => {
    for (let index = 0; index < MAX_SESSIONS; index += 1) sessionOf(`conv_` + index)
    // A render reads every row: if peeking created state, these eight reads would be eight sessions
    // and the ghosts would be in the map.
    for (let index = 0; index < MAX_SESSIONS; index += 1) peekSession(`ghost_` + index)

    expect(peekSession('ghost_0')).toBeUndefined()

    sessionOf('conv_new')

    // The loop above created nothing, so exactly one idle conversation goes: the oldest one.
    expect(peekSession('conv_0')).toBeUndefined()
    expect(peekSession('conv_1')).toBeDefined()
  })

  it('leaves the conversation the pane is showing out of the eviction', () => {
    for (let index = 0; index < MAX_SESSIONS; index += 1) sessionOf(`conv_` + index)
    focusSession('conv_0')

    sessionOf('conv_new')

    expect(peekSession('conv_0')).toBeDefined()
    expect(peekSession('conv_1')).toBeUndefined()
  })

  it('keeps every conversation when they are all in a state the user can come back to', () => {
    for (let index = 0; index < MAX_SESSIONS; index += 1) sessionOf(`conv_` + index).running = true

    // Nothing may be dropped, so the soft cap is passed rather than losing eight running turns.
    sessionOf('conv_new')

    expect(peekSession('conv_0')).toBeDefined()
    expect(peekSession('conv_new')).toBeDefined()
  })

  it('drops every conversation when the page resets', () => {
    const before = sessionOf('conv_a')

    resetSessions()

    expect(peekSession('conv_a')).toBeUndefined()
    expect(sessionOf('conv_a')).not.toBe(before)
  })
})
