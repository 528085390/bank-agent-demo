/**
 * 已结算深度分析报告的本地存储。
 *
 * 报告只存在于 SSE 的 `tool_result.display` 里，消息账本不落工具结果（后端只存文本和
 * `business_ids`）。页面刷新后后端给不回来，所以这里按会话把报告留在浏览器里：
 *
 * - 键是「账号 + 会话」，各会话互不干扰，换一个人登录也不会读到上一个人的键；
 * - 每条记录带 `anchorMessageId`，回到账本里它所属的那一问下面，而不是永远沉在页面底部；
 * - 写入失败（隐私模式、配额满）静默降级为「只在内存里」，不影响页面其余部分。
 *
 * 这里只认已经通过 `isAnalysisReport` 的报告，损坏或旧版本的数据直接当作没有。
 */

import { reactive } from 'vue'

import { isAnalysisReport } from './analysisResult'
import type { StorageLike } from './conversations'
import { CONTENT_STORAGE, scopedStorageKey } from './storageScope'

export interface SettledReportEntry {
  /** 账本里这一轮用户消息的 id；`null` 表示暂时找不到归属（极少数结算失败的路径）。 */
  anchorMessageId: string | null
  /** 工具结果里的报告 display，原样保存。 */
  display: unknown
  /** ISO 时间，用来恢复时稳定排序。 */
  savedAt: string
}

const STORAGE_PREFIX = CONTENT_STORAGE.reports
const STORAGE_VERSION = 1
/** 每个会话最多留这么多份报告，避免一个很长的会话无限占空间。 */
const MAX_ENTRIES = 12
/**
 * 每个会话的序列化上限。localStorage 通常只有约 5MB，而这是「按会话」的键，留出余量。
 * 单位是 UTF-16 字符数；正常报告远小于这个值。
 */
const MAX_CHARS = 2_000_000

function browserStorage(): StorageLike | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

/**
 * Where one conversation's reports live, or `null` when this browser does not know who is
 * logged in -- in which case nothing is read and nothing is written (see `storageScope`).
 */
export function settledReportsKey(conversationId: string): string | null {
  return scopedStorageKey(STORAGE_PREFIX, conversationId)
}

/**
 * 同一份报告的身份。`generated_at` 是分析时生成的时间，再带上期间与笔数，
 * 足以区分不同报告，也让「结算时保存一次、刷新后恢复一次」不会变成两份。
 */
export function reportFingerprint(display: unknown): string {
  const meta = (display as {
    report_meta?: {
      generated_at?: unknown
      total_transactions?: unknown
      date_range?: { start?: unknown; end?: unknown }
    }
  } | null)?.report_meta
  if (!meta) return ''
  const range = meta.date_range ?? {}
  const parts = [
    meta.generated_at,
    range.start,
    range.end,
    meta.total_transactions,
  ].map(value => String(value ?? ''))
  return parts.some(Boolean) ? parts.join('|') : JSON.stringify(display)
}

function entryOf(value: unknown): SettledReportEntry | null {
  if (typeof value !== 'object' || value === null) return null
  const raw = value as Record<string, unknown>
  if (!isAnalysisReport(raw.display)) return null
  if (raw.anchorMessageId !== null && typeof raw.anchorMessageId !== 'string') return null
  return {
    anchorMessageId: (raw.anchorMessageId as string | null) ?? null,
    display: raw.display,
    savedAt: typeof raw.savedAt === 'string' ? raw.savedAt : '',
  }
}

/** Reads what a previous page load left for this conversation, newest data wins on duplicates. */
export function loadSettledReports(
  conversationId: string,
  storage: StorageLike | null = browserStorage(),
): SettledReportEntry[] {
  const store = storage ?? browserStorage()
  if (!store) return []
  const key = settledReportsKey(conversationId)
  if (!key) return []
  let payload: unknown
  try {
    const raw = store.getItem(key)
    if (!raw) return []
    payload = JSON.parse(raw)
  } catch {
    return []
  }
  if (typeof payload !== 'object' || payload === null) return []
  const candidate = payload as { version?: unknown; items?: unknown }
  if (candidate.version !== STORAGE_VERSION || !Array.isArray(candidate.items)) return []

  const seen = new Set<string>()
  const entries: SettledReportEntry[] = []
  for (const value of candidate.items) {
    const entry = entryOf(value)
    if (!entry) continue
    const fingerprint = reportFingerprint(entry.display)
    if (seen.has(fingerprint)) continue
    seen.add(fingerprint)
    entries.push(entry)
  }
  return entries
}

/**
 * Stores this conversation's reports. Old entries are dropped first when the payload is too
 * large or there are too many of them; a single oversized report is simply not persisted.
 */
export function saveSettledReports(
  conversationId: string,
  entries: SettledReportEntry[],
  storage: StorageLike | null = browserStorage(),
): void {
  const store = storage ?? browserStorage()
  if (!store) return
  const key = settledReportsKey(conversationId)
  // No identity, no key: a report must not land anywhere a later account could read it.
  if (!key) return
  const items = entries.slice(-MAX_ENTRIES)
  let encoded = encode(items)
  if (!encoded) return
  while (items.length > 1 && encoded.length > MAX_CHARS) {
    items.shift()
    encoded = encode(items)
    if (!encoded) return
  }
  // A report that cannot fit on its own stays in memory for this page session only.
  if (encoded.length > MAX_CHARS) return
  try {
    store.setItem(key, encoded)
  } catch {
    // Quota exceeded or storage disabled: the in-memory copy still renders this session.
  }
}

function encode(items: SettledReportEntry[]): string {
  try {
    return JSON.stringify({ version: STORAGE_VERSION, items })
  } catch {
    return ''
  }
}

// --- the in-memory view ----------------------------------------------------------------
//
// Kept here rather than in `App.vue` for one concrete reason: these reports must survive the
// component being torn down and rebuilt. A page reload rebuilds both; a test that remounts the
// component without clearing this state would not be simulating one.

const reports = reactive<Record<string, SettledReportEntry[]>>({})
const hydrated = new Set<string>()
/**
 * Reports whose "send to email" prompt has already been shown (or restored from a reload).
 *
 * Keyed by report fingerprint rather than object identity: reports restored from JSON are plain
 * objects, while the same report read through Vue's reactive array is a proxy -- the two are not
 * `===`. The fingerprint (generated_at + period + count) is stable across both.
 */
let prompted = new Set<string>()

/** The reports currently held for one conversation. */
export function settledReportsOf(conversationId: string): SettledReportEntry[] {
  return reports[conversationId] ?? []
}

export function hasPromptedSend(display: unknown): boolean {
  if (typeof display !== 'object' || display === null) return false
  return prompted.has(reportFingerprint(display))
}

export function markSendPrompted(display: unknown): void {
  if (typeof display !== 'object' || display === null) return
  prompted.add(reportFingerprint(display))
}

/**
 * Loads what a previous page load persisted. Restored copies count as already prompted: they
 * were displayed (and normally asked about) before the reload, so asking again would be noise.
 */
export function hydrateSettledReports(conversationId: string): void {
  if (hydrated.has(conversationId)) return
  hydrated.add(conversationId)
  const stored = loadSettledReports(conversationId)
  if (!stored.length) return
  const existing = reports[conversationId] ?? []
  const seen = new Set(existing.map(entry => reportFingerprint(entry.display)))
  const merged = [...existing]
  for (const entry of stored) {
    const fingerprint = reportFingerprint(entry.display)
    if (seen.has(fingerprint)) continue
    seen.add(fingerprint)
    merged.push(entry)
    markSendPrompted(entry.display)
  }
  merged.sort((left, right) => left.savedAt.localeCompare(right.savedAt))
  reports[conversationId] = merged
}

/**
 * Adds the reports of one finished turn, anchored to its user message, and persists them.
 * Returns how many were genuinely new (duplicates from a double settle are ignored).
 */
export function rememberSettledReports(
  conversationId: string,
  displays: unknown[],
  anchorMessageId: string | null,
): number {
  if (!displays.length) return 0
  const existing = reports[conversationId] ?? []
  const seen = new Set(existing.map(entry => reportFingerprint(entry.display)))
  const savedAt = new Date().toISOString()
  const added: SettledReportEntry[] = []
  for (const display of displays) {
    const fingerprint = reportFingerprint(display)
    if (seen.has(fingerprint)) continue
    seen.add(fingerprint)
    added.push({ anchorMessageId, display, savedAt })
  }
  if (!added.length) return 0
  reports[conversationId] = [...existing, ...added]
  saveSettledReports(conversationId, reports[conversationId])
  return added.length
}

/** Test seam for simulating a page reload: the persisted entries stay, the view state does not. */
export function resetSettledReports(): void {
  for (const key of Object.keys(reports)) delete reports[key]
  hydrated.clear()
  prompted = new Set<string>()
}
