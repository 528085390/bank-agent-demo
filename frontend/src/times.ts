/**
 * The page's timestamps: the day on a conversation row, the clock under a message.
 *
 * The server sends two kinds of stamp -- \`created_at\` in UTC and \`last_message_at\` carrying the
 * writer's own offset -- so nothing here reads a string apart: every value goes through \`Date\`
 * and comes back on the reader's own clock. Both formatters take \`now\` as an argument, so which
 * day counts as "today" is the caller's decision and the tests can pin it down.
 */
import type { Conversation } from './types'

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? null : at
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** A calendar day as one number, so two moments compare without a date library. */
function dayOf(at: Date): number {
  return at.getFullYear() * 10000 + (at.getMonth() + 1) * 100 + at.getDate()
}

function yesterdayOf(now: Date): Date {
  // Through the constructor, not \`now - 86400000\`: a day is not always 24 hours long.
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
}

/** The moment a conversation row shows: its latest message, or when it was created. */
export function stampOf(conversation: Conversation): string {
  return conversation.last_message_at ?? conversation.created_at
}

/** The day a moment belongs to, in the reader's own time zone. */
export function formatDay(iso: string | null | undefined, now: Date): string {
  const at = parse(iso)
  if (!at) return ''
  if (dayOf(at) === dayOf(now)) return '今天'
  if (dayOf(at) === dayOf(yesterdayOf(now))) return '昨天'
  const day = pad(at.getMonth() + 1) + '-' + pad(at.getDate())
  return at.getFullYear() === now.getFullYear() ? day : at.getFullYear() + '-' + day
}

/** The clock under one message: alone for today, behind its day for anything older. */
export function formatMessageTime(iso: string | null | undefined, now: Date): string {
  const at = parse(iso)
  if (!at) return ''
  const clock = pad(at.getHours()) + ':' + pad(at.getMinutes())
  return dayOf(at) === dayOf(now) ? clock : formatDay(iso, now) + ' ' + clock
}

/** A report's generation moment, to the second, on the reader's own clock. */
export function formatReportTime(iso: string | null | undefined): string {
  const at = parse(iso)
  if (!at) return ''
  return (
    at.getFullYear() +
    '-' +
    pad(at.getMonth() + 1) +
    '-' +
    pad(at.getDate()) +
    ' ' +
    pad(at.getHours()) +
    ':' +
    pad(at.getMinutes()) +
    ':' +
    pad(at.getSeconds())
  )
}

/** The whole local timestamp, for a tooltip with room for it. */
export function formatAbsolute(iso: string | null | undefined): string {
  const at = parse(iso)
  if (!at) return ''
  return (
    at.getFullYear() +
    '-' +
    pad(at.getMonth() + 1) +
    '-' +
    pad(at.getDate()) +
    ' ' +
    pad(at.getHours()) +
    ':' +
    pad(at.getMinutes())
  )
}
