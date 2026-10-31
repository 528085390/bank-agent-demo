import { describe, expect, it, beforeEach, vi } from 'vitest'

import { demoStore } from './demoStore'
import type { TaskNode } from './types'

/**
 * store 里任务图那一半：`plan`（`setPlan`）与 `task_update`（`updateTask`）。
 *
 * 最容易错的是合并：`plan` 在恢复执行时会重播一次，重播的那份 status 是旧的。
 * 「重播不把已经跑完的节点打回 pending」这条要一直钉在这里。
 */

/** 一张两个节点的小图：一个节点够测状态，两个节点才分得清「合并」和「整份替换」。 */
function twoNodes(): TaskNode[] {
  return [
    { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    { id: 'n2', label: '转账', owner: 'transfer', depends_on: ['n1'], status: 'pending' },
  ]
}

describe('demoStore task graph management', () => {
  beforeEach(() => {
    demoStore.resetTasks()
  })

  it('sets initial plan nodes', () => {
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    ])
    expect(demoStore.planId).toBe('plan_1')
    expect(demoStore.taskNodes).toHaveLength(1)
    expect(demoStore.taskNodes[0].status).toBe('pending')
  })

  it('updates task node status and artifact', () => {
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    ])
    demoStore.updateTask({
      node_id: 'n1',
      status: 'running',
    })
    expect(demoStore.taskNodes[0].status).toBe('running')

    demoStore.updateTask({
      node_id: 'n1',
      status: 'done',
      artifact: { summary: '本月结余 500 元' },
    })
    expect(demoStore.taskNodes[0].status).toBe('done')
    expect(demoStore.taskNodes[0].artifact).toEqual({ summary: '本月结余 500 元' })
  })

  it('preserves existing node status during setPlan incremental merge', () => {
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
      { id: 'n2', label: '转账', owner: 'transfer', depends_on: ['n1'], status: 'pending' },
    ])
    demoStore.updateTask({ node_id: 'n1', status: 'done' })

    // Simulate receiving plan re-broadcast
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
      { id: 'n2', label: '转账', owner: 'transfer', depends_on: ['n1'], status: 'pending' },
    ])
    expect(demoStore.taskNodes[0].status).toBe('done')
  })

  it('keeps a node status when a re-broadcast changes its label and owner', () => {
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    ])
    demoStore.updateTask({ node_id: 'n1', status: 'done', artifact: { summary: '结余 500' } })

    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账（改）', owner: 'bill_v2', depends_on: ['n0'], status: 'pending' },
    ])

    expect(demoStore.taskNodes).toEqual([
      {
        id: 'n1', label: '查账（改）', owner: 'bill_v2', depends_on: ['n0'],
        status: 'done', artifact: { summary: '结余 500' },
      },
    ])
  })

  it('takes a node it has never seen from the re-broadcast as given', () => {
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    ])
    demoStore.updateTask({ node_id: 'n1', status: 'done' })

    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
      { id: 'n2', label: '转账', owner: 'transfer', depends_on: ['n1'], status: 'running' },
    ])

    expect(demoStore.taskNodes).toEqual([
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'done' },
      { id: 'n2', label: '转账', owner: 'transfer', depends_on: ['n1'], status: 'running' },
    ])
  })

  it('drops a node that the re-broadcast omits, because the rebroadcast is the whole graph', () => {
    demoStore.setPlan('plan_1', twoNodes())

    demoStore.setPlan('plan_1', [
      { id: 'n2', label: '转账', owner: 'transfer', depends_on: [], status: 'pending' },
    ])

    expect(demoStore.taskNodes).toEqual([
      { id: 'n2', label: '转账', owner: 'transfer', depends_on: [], status: 'pending' },
    ])
  })

  it('replaces the graph when the plan id changes instead of merging', () => {
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    ])
    demoStore.updateTask({ node_id: 'n1', status: 'done' })

    demoStore.setPlan('plan_2', [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    ])

    expect(demoStore.planId).toBe('plan_2')
    expect(demoStore.taskNodes).toEqual([
      { id: 'n1', label: '查账', owner: 'bill', depends_on: [], status: 'pending' },
    ])
  })

  it('ignores an update for a node the plan never had, instead of inventing one', () => {
    demoStore.setPlan('plan_1', twoNodes())

    demoStore.updateTask({ node_id: 'n_missing', status: 'running' })

    expect(demoStore.taskNodes).toEqual(twoNodes())
    expect(demoStore.planId).toBe('plan_1')
  })

  it('does not throw on an update before any plan arrived', () => {
    expect(() => demoStore.updateTask({ node_id: 'n1', status: 'done' })).not.toThrow()
    expect(demoStore.taskNodes).toEqual([])
    expect(demoStore.planId).toBe('')
  })

  it('keeps a stored artifact when an update does not carry the key', () => {
    demoStore.setPlan('plan_1', twoNodes())
    demoStore.updateTask({ node_id: 'n1', status: 'done', artifact: { summary: '结余 500' } })

    demoStore.updateTask({ node_id: 'n1', status: 'done' })

    expect(demoStore.taskNodes[0].artifact).toEqual({ summary: '结余 500' })
  })

  it('clears the artifact when an update sets it to null explicitly', () => {
    demoStore.setPlan('plan_1', twoNodes())
    demoStore.updateTask({ node_id: 'n1', status: 'done', artifact: { summary: '结余 500' } })

    demoStore.updateTask({ node_id: 'n1', status: 'failed', artifact: null, error: '转账超时' })

    expect(demoStore.taskNodes[0].artifact).toBeNull()
    expect(demoStore.taskNodes[0].error).toBe('转账超时')
  })

  it('clears both the nodes and the plan id on reset', () => {
    demoStore.setPlan('plan_1', twoNodes())

    demoStore.resetTasks()

    expect(demoStore.taskNodes).toEqual([])
    expect(demoStore.planId).toBe('')
  })

  it('assigns a new node array on setPlan, so Vue sees one change', () => {
    demoStore.setPlan('plan_1', twoNodes())
    const before = demoStore.taskNodes

    demoStore.setPlan('plan_1', twoNodes())

    expect(demoStore.taskNodes).not.toBe(before)
    expect(demoStore.taskNodes).toEqual(twoNodes())
  })

  it('assigns a new node array on updateTask and leaves the other nodes alone', () => {
    demoStore.setPlan('plan_1', twoNodes())
    const before = demoStore.taskNodes
    const untouched = demoStore.taskNodes[1]

    demoStore.updateTask({ node_id: 'n1', status: 'running' })

    expect(demoStore.taskNodes).not.toBe(before)
    expect(demoStore.taskNodes[1]).toBe(untouched)
    expect(demoStore.taskNodes[0].status).toBe('running')
  })

  it('does not share its node objects with the caller', () => {
    const dependsOn = ['n0']
    const incoming: TaskNode[] = [
      { id: 'n1', label: '查账', owner: 'bill', depends_on: dependsOn, status: 'pending' },
    ]

    demoStore.setPlan('plan_1', incoming)
    dependsOn.push('n9')
    incoming[0].label = '调用方改的'

    expect(demoStore.taskNodes).toEqual([
      { id: 'n1', label: '查账', owner: 'bill', depends_on: ['n0'], status: 'pending' },
    ])
  })
})


describe('demoStore security & lock management (FE-SEC-08)', () => {
  beforeEach(() => {
    demoStore.security = null
    demoStore.dismissed = false
    demoStore.securityBusy = false
  })

  it('computes isLocked and isBannerVisible correctly', () => {
    expect(demoStore.isLocked).toBe(false)
    expect(demoStore.isBannerVisible).toBe(false)

    demoStore.security = {
      user_id: 'u_wealth',
      tool_name: 'fund_transfer',
      failure_count: 5,
      locked_until: '2026-10-26T17:15:00+08:00',
      last_reason: 'invalid_code',
      tools: [],
    }

    expect(demoStore.isLocked).toBe(true)
    expect(demoStore.isBannerVisible).toBe(true)

    demoStore.dismissBanner()
    expect(demoStore.dismissed).toBe(true)
    expect(demoStore.isBannerVisible).toBe(false)
  })

  it('markLockedFromEvent reactivates dismissed banner immediately', () => {
    demoStore.dismissed = true
    demoStore.markLockedFromEvent('2026-10-26T17:15:00+08:00')

    expect(demoStore.dismissed).toBe(false)
    expect(demoStore.isLocked).toBe(true)
    expect(demoStore.isBannerVisible).toBe(true)
  })

  it('loads security status via loadSecurity and manages busy state', async () => {
    const payload = {
      user_id: 'u_wealth',
      tool_name: 'fund_transfer',
      failure_count: 5,
      locked_until: '2026-10-26T17:15:00+08:00',
      last_reason: 'invalid_code',
      tools: [],
    }
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ request_id: 'req_1', data: payload, error: null }),
    })
    vi.stubGlobal('fetch', fetchSpy)

    await demoStore.loadSecurity()

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/demo/security',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(demoStore.security).toEqual(payload)
    expect(demoStore.securityBusy).toBe(false)
  })

  it('resets lock via resetLock and clears error/busy flags', async () => {
    const resetPayload = {
      user_id: 'u_wealth',
      tool_name: 'fund_transfer',
      failure_count: 0,
      locked_until: null,
      last_reason: 'invalid_code',
      tools: [],
    }
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ request_id: 'req_2', data: resetPayload, error: null }),
    })
    vi.stubGlobal('fetch', fetchSpy)

    demoStore.dismissed = true
    await demoStore.resetLock()

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/demo/security/reset-lock',
      expect.objectContaining({ method: 'POST', body: '{}' }),
    )
    expect(demoStore.security).toEqual(resetPayload)
    expect(demoStore.dismissed).toBe(false)
    expect(demoStore.securityBusy).toBe(false)
  })
})
