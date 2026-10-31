import { describe, expect, it } from 'vitest'

import { formatAbsolute, formatDay, formatMessageTime, formatReportTime, stampOf } from './times'
import type { Conversation } from './types'

/** A moment on the reader's own wall clock, so every expectation below holds in any zone. */
function at(year: number, month: number, day: number, hour = 0, minute = 0): string {
  return new Date(year, month - 1, day, hour, minute).toISOString()
}

/** 2026-10-13 15:30: the clock the expectations below are written against. */
const NOW = new Date(2026, 9, 13, 15, 30)

function conversation(overrides: Partial<Conversation>): Conversation {
  return {
    conversation_id: 'conv',
    serial_no: 1,
    title: '会话',
    pinned: false,
    archived: false,
    deleted: false,
    created_at: at(2026, 10, 1, 9, 0),
    updated_at: at(2026, 10, 1, 9, 0),
    last_message_at: null,
    message_count: 0,
    ...overrides,
  }
}

describe('formatDay', () => {
  it('names the day it is now', () => {
    expect(formatDay(at(2026, 10, 13, 9, 5), NOW)).toBe('今天')
  })

  it('names the day before it', () => {
    expect(formatDay(at(2026, 10, 12, 23, 59), NOW)).toBe('昨天')
  })

  it('rolls back over a month boundary', () => {
    // The reader's clock is just past midnight on the first of the next month.
    const now = new Date(2026, 10, 1, 0, 30)
    expect(formatDay(at(2026, 10, 31, 23, 0), now)).toBe('昨天')
  })

  it('shows the month and the day for an earlier day of the same year', () => {
    expect(formatDay(at(2026, 9, 28, 8, 0), NOW)).toBe('09-28')
  })

  it('adds the year when the day belongs to another one', () => {
    expect(formatDay(at(2025, 12, 31, 8, 0), NOW)).toBe('2025-12-31')
  })

  it('renders nothing for a value it cannot read', () => {
    expect(formatDay('', NOW)).toBe('')
    expect(formatDay(null, NOW)).toBe('')
    expect(formatDay(undefined, NOW)).toBe('')
    expect(formatDay('昨天下午', NOW)).toBe('')
  })
})

describe('formatMessageTime', () => {
  it('shows the clock alone for a message written today', () => {
    expect(formatMessageTime(at(2026, 10, 13, 9, 5), NOW)).toBe('09:05')
  })

  it('prefixes a message written yesterday with the word', () => {
    expect(formatMessageTime(at(2026, 10, 12, 23, 59), NOW)).toBe('昨天 23:59')
  })

  it('prefixes an older message with its day', () => {
    expect(formatMessageTime(at(2026, 9, 28, 8, 0), NOW)).toBe('09-28 08:00')
  })

  it('spells out the whole day for another year', () => {
    expect(formatMessageTime(at(2025, 12, 31, 8, 0), NOW)).toBe('2025-12-31 08:00')
  })

  it('renders nothing for a value it cannot read', () => {
    expect(formatMessageTime(null, NOW)).toBe('')
    expect(formatMessageTime('nonsense', NOW)).toBe('')
  })
})

describe('formatAbsolute', () => {
  it('spells out the whole local timestamp, for a tooltip', () => {
    expect(formatAbsolute(at(2026, 9, 28, 8, 0))).toBe('2026-09-28 08:00')
  })

  it('renders nothing for a value it cannot read', () => {
    expect(formatAbsolute(null)).toBe('')
    expect(formatAbsolute('')).toBe('')
  })
})

describe('formatReportTime', () => {
  it("shows the moment on the reader's clock, to the second", () => {
    // Constructed as local time, so the expectation holds in any zone: the report's
    // ISO string carries UTC, and the header must show the same wall clock, not UTC.
    const iso = new Date(2026, 9, 13, 15, 30, 42).toISOString()
    expect(formatReportTime(iso)).toBe('2026-10-13 15:30:42')
  })

  it('renders nothing for a value it cannot read', () => {
    expect(formatReportTime(null)).toBe('')
    expect(formatReportTime('nonsense')).toBe('')
  })
})

describe('stampOf', () => {
  it('prefers the last message over the day the conversation was created', () => {
    expect(stampOf(conversation({ last_message_at: at(2026, 10, 12, 20, 0) }))).toBe(
      at(2026, 10, 12, 20, 0),
    )
  })

  it('falls back to the creation for a conversation nobody has written in', () => {
    expect(stampOf(conversation({ created_at: at(2026, 10, 5, 9, 0) }))).toBe(at(2026, 10, 5, 9, 0))
  })
})
