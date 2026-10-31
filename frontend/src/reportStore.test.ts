import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { StorageLike } from './conversations'
import {
  loadSettledReports, reportFingerprint, saveSettledReports, settledReportsKey,
  type SettledReportEntry,
} from './reportStore'
import { resetStorageScope, setStorageOwner } from './storageScope'

const OWNER = 'u_a'

/** Where one conversation's reports live for `OWNER`. */
const key = (conversationId: string) => settledReportsKey(conversationId) ?? ''

beforeEach(() => setStorageOwner(OWNER))
afterEach(() => resetStorageScope())

class MemoryStorage implements StorageLike {
  private values = new Map<string, string>()

  get length(): number { return this.values.size }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null }
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
}

function display(generatedAt = '2026-09-01T10:00:00Z', text = '报告正文') {
  return {
    report_meta: {
      date_range: { start: '2026-08-01', end: '2026-08-31' },
      total_transactions: 128,
      accounts: [],
      generated_at: generatedAt,
    },
    modules: [],
    text_report: text,
  }
}

function entry(anchorMessageId: string | null, generatedAt: string): SettledReportEntry {
  return { anchorMessageId, display: display(generatedAt), savedAt: generatedAt }
}

describe('settled report storage', () => {
  it('round-trips the anchor and the report itself', () => {
    const storage = new MemoryStorage()
    const saved = entry('msg_3', '2026-09-01T10:00:00Z')
    saveSettledReports('conv_a', [saved], storage)

    const loaded = loadSettledReports('conv_a', storage)
    expect(loaded).toHaveLength(1)
    expect(loaded[0].anchorMessageId).toBe('msg_3')
    expect(loaded[0].display).toEqual(saved.display)
  })

  it('treats missing, corrupt and wrong-version payloads as empty', () => {
    const storage = new MemoryStorage()
    expect(loadSettledReports('conv_a', storage)).toEqual([])

    storage.setItem(key('conv_a'), 'not json')
    expect(loadSettledReports('conv_a', storage)).toEqual([])

    storage.setItem(key('conv_a'), JSON.stringify({
      version: 999, items: [entry('msg_3', '2026-09-01T10:00:00Z')],
    }))
    expect(loadSettledReports('conv_a', storage)).toEqual([])
  })

  it('drops entries that are not analysis reports', () => {
    const storage = new MemoryStorage()
    storage.setItem(key('conv_a'), JSON.stringify({
      version: 1,
      items: [
        { anchorMessageId: 'msg_3', display: { hello: 'world' }, savedAt: '' },
        entry('msg_4', '2026-09-02T10:00:00Z'),
      ],
    }))

    const loaded = loadSettledReports('conv_a', storage)
    expect(loaded).toHaveLength(1)
    expect(loaded[0].anchorMessageId).toBe('msg_4')
  })

  it('keeps one copy of a report that appears twice', () => {
    const storage = new MemoryStorage()
    storage.setItem(key('conv_a'), JSON.stringify({
      version: 1,
      items: [
        entry('msg_3', '2026-09-01T10:00:00Z'),
        entry('msg_3', '2026-09-01T10:00:00Z'),
      ],
    }))

    expect(loadSettledReports('conv_a', storage)).toHaveLength(1)
  })

  it('keeps only the newest reports for a conversation', () => {
    const storage = new MemoryStorage()
    const entries = Array.from({ length: 13 }, (_, index) =>
      entry(`msg_${index}`, `2026-09-${String(index + 1).padStart(2, '0')}T10:00:00Z`))

    saveSettledReports('conv_a', entries, storage)
    const loaded = loadSettledReports('conv_a', storage)
    expect(loaded).toHaveLength(12)
    // 最旧的那条被挤掉，最新的一定还在。
    expect(loaded.some(item => item.anchorMessageId === 'msg_0')).toBe(false)
    expect(loaded.some(item => item.anchorMessageId === 'msg_12')).toBe(true)
  })

  it('does not persist a single report larger than the per-conversation cap', () => {
    const storage = new MemoryStorage()
    saveSettledReports('conv_a', [entry('msg_3', '2026-09-01T10:00:00Z')], storage)
    const oversized = entry('msg_4', '2026-09-02T10:00:00Z')
    ;(oversized.display as { text_report: string }).text_report = 'x'.repeat(2_000_100)

    saveSettledReports('conv_a', [oversized], storage)

    // 旧值原样保留；过大的单份报告只留在内存里。
    const loaded = loadSettledReports('conv_a', storage)
    expect(loaded).toHaveLength(1)
    expect(loaded[0].anchorMessageId).toBe('msg_3')
  })

  it('keeps different reports apart even for the same period', () => {
    expect(reportFingerprint(display('2026-09-01T10:00:00Z')))
      .not.toBe(reportFingerprint(display('2026-09-02T10:00:00Z')))
  })
})

describe('who a stored report belongs to', () => {
  it('keeps one account’s report out of another account’s key', () => {
    const storage = new MemoryStorage()
    saveSettledReports('conv_a', [entry('msg_3', '2026-09-01T10:00:00Z')], storage)

    setStorageOwner('u_b')
    expect(loadSettledReports('conv_a', storage)).toEqual([])

    saveSettledReports('conv_a', [entry('msg_9', '2026-09-03T10:00:00Z')], storage)
    expect(loadSettledReports('conv_a', storage).map(item => item.anchorMessageId)).toEqual(['msg_9'])
    // A's copy is untouched, and B's write cannot have gone into it.
    setStorageOwner(OWNER)
    expect(loadSettledReports('conv_a', storage).map(item => item.anchorMessageId)).toEqual(['msg_3'])
  })

  it('writes nothing while the identity is unknown', () => {
    const storage = new MemoryStorage()
    setStorageOwner(null)
    saveSettledReports('conv_a', [entry('msg_3', '2026-09-01T10:00:00Z')], storage)
    expect(storage.length).toBe(0)
    expect(settledReportsKey('conv_a')).toBeNull()
    expect(loadSettledReports('conv_a', storage)).toEqual([])
  })
})
