/**
 * 身份世代：让「上一个人的请求」写不进「这个人的界面」。
 *
 * 退出、掉线、换号之后，已经发出去的请求还在路上 —— SSE 还在流、账单还在轮询。它们回来时
 * 页面可能已经属于另一个人（设计 §7.3、验收 A11/A12）。这个模块是那条规则的实现：每个异步
 * 动作开始时取一个编号，回来先比一次；换人时也把在飞请求直接掐断。
 *
 * 这里只测模块自己的行为，页面上的表现由 `App.test.ts` 的「身份同步」一组覆盖。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  abortInflight,
  currentGeneration,
  identityChanged,
  isStale,
  listenIdentityChange,
  newIdentityScope,
  onIdentityChange,
  resetIdentity,
  trackRequest,
} from './identity'

class FakeBroadcastChannel {
  static all: FakeBroadcastChannel[] = []

  private listeners = new Set<(event: MessageEvent) => void>()
  readonly posted: unknown[] = []

  constructor(public readonly name: string) { FakeBroadcastChannel.all.push(this) }

  postMessage(data: unknown): void { this.posted.push(data) }

  addEventListener(_type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.add(listener)
  }

  removeEventListener(_type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.delete(listener)
  }

  close(): void { this.listeners.clear() }

  deliver(data: unknown): void {
    for (const listener of [...this.listeners]) listener({ data } as MessageEvent)
  }
}

beforeEach(() => {
  FakeBroadcastChannel.all = []
  vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
  resetIdentity()
})

afterEach(() => {
  resetIdentity()
  vi.unstubAllGlobals()
})

describe('世代编号', () => {
  it('换人之后，换人之前取到的编号就是过期的', () => {
    const before = newIdentityScope()
    expect(isStale(before)).toBe(false)

    identityChanged('logout')

    // 迟到的那次响应拿着 `before` 回来，必须整段作废。
    expect(isStale(before)).toBe(true)
    expect(currentGeneration()).toBe(before.generation + 1)
  })

  it('同一个世代里取的编号一直有效', () => {
    const first = newIdentityScope()
    const second = newIdentityScope()

    expect(isStale(first)).toBe(false)
    expect(isStale(second)).toBe(false)
    expect(second.generation).toBe(first.generation)
  })

  it('订阅者拿到原因和编号，取消订阅后不再收到', () => {
    const seen: Array<{ reason: string; generation: number }> = []
    const stop = onIdentityChange(change => seen.push({ ...change }))

    identityChanged('expired')
    stop()
    identityChanged('logout')

    expect(seen).toEqual([{ generation: 1, reason: 'expired' }])
  })
})

describe('在飞请求', () => {
  it('换人时被中止，而不是等它回来再丢掉', () => {
    const tracked = trackRequest()

    identityChanged('logout')

    expect(tracked.signal.aborted).toBe(true)
  })

  it('请求结束后释放的控制器不会再被中止', () => {
    const tracked = trackRequest()
    tracked.release()

    expect(() => abortInflight()).not.toThrow()
    expect(tracked.signal.aborted).toBe(false)
  })

  it('停止一条请求不会波及另一条', () => {
    const stopped = trackRequest()
    const untouched = trackRequest()

    stopped.abort()

    expect(stopped.signal.aborted).toBe(true)
    expect(untouched.signal.aborted).toBe(false)
    stopped.release()
    untouched.release()
  })
})

describe('跨标签页广播', () => {
  it('本页发起的变化会广播一次', () => {
    identityChanged('password')

    const channel = FakeBroadcastChannel.all[FakeBroadcastChannel.all.length - 1]
    expect(channel.posted).toEqual([{ reason: 'password' }])
  })

  it('收到广播后的自我清理不再广播回去', () => {
    // 频道先由本页的一次真实广播建起来，再看「不广播」这条路径会不会往里塞东西。
    identityChanged('logout')
    const channel = FakeBroadcastChannel.all[FakeBroadcastChannel.all.length - 1]
    channel.posted.length = 0

    identityChanged('logout', { broadcast: false })

    // 两个标签页互相回响，会把「清场」变成一条永不停止的消息。
    expect(channel.posted).toEqual([])
  })

  it('只处理带字符串原因的消息', () => {
    const reasons: string[] = []
    const stop = listenIdentityChange(reason => reasons.push(reason))

    const channel = FakeBroadcastChannel.all[FakeBroadcastChannel.all.length - 1]
    channel.deliver({ reason: 'logout' })
    channel.deliver({ reason: 42 })
    channel.deliver(null)
    stop()

    expect(reasons).toEqual(['logout'])
  })

  it('浏览器没有 BroadcastChannel 时静默降级，不影响清场', () => {
    vi.unstubAllGlobals()
    delete (globalThis as { BroadcastChannel?: unknown }).BroadcastChannel

    // 降级不是漏洞：本页照样清干净，别的标签页会在下一次 401 或聚焦复核时发现。
    expect(() => identityChanged('logout')).not.toThrow()
    expect(() => listenIdentityChange(() => {})()).not.toThrow()
  })
})
