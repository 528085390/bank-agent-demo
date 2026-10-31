import { describe, expect, it, vi } from 'vitest'

import { share } from './inflight'

/**
 * The registry that keeps a read-ahead and the click that follows it from asking the server
 * the same question twice. Everything here is about promises, not about the view.
 */
describe('the in-flight reads', () => {
  it('runs one read for callers that ask for the same key at the same time', async () => {
    const run = vi.fn(async () => 'ledger')
    const [first, second] = await Promise.all([
      share('messages:conv_a:first', run),
      share('messages:conv_a:first', run),
    ])

    expect(run).toHaveBeenCalledTimes(1)
    expect(first).toBe('ledger')
    expect(second).toBe('ledger')
  })

  it('keeps different keys apart', async () => {
    const run = vi.fn(async (value: string) => value)

    const [first, second] = await Promise.all([
      share('messages:conv_a:first', () => run('a')),
      share('messages:conv_b:first', () => run('b')),
    ])

    expect(run).toHaveBeenCalledTimes(2)
    expect(first).toBe('a')
    expect(second).toBe('b')
  })

  it('lets a later caller start a new read once the first has settled', async () => {
    const run = vi.fn(async () => 'ledger')

    await share('messages:conv_a:first', run)
    await share('messages:conv_a:first', run)

    // Nothing is remembered here: the answer may have moved on since it was read.
    expect(run).toHaveBeenCalledTimes(2)
  })

  it('forgets a read that failed, so the next caller asks again', async () => {
    const run = vi.fn(async () => {
      throw new Error('the connection is closed')
    })

    await expect(share('detail:conv_a', run)).rejects.toThrow('the connection is closed')
    await expect(share('detail:conv_a', async () => 'ok')).resolves.toBe('ok')
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('tells every caller waiting on a read that it failed', async () => {
    let fail!: (reason: Error) => void
    const run = () => new Promise<string>((_resolve, reject) => { fail = reject })

    const first = share('detail:conv_a', run)
    const second = share('detail:conv_a', run)
    // The read itself starts on the next microtask, so the registry is filled first.
    await Promise.resolve()
    fail(new Error('服务暂时异常'))

    await expect(first).rejects.toThrow('服务暂时异常')
    await expect(second).rejects.toThrow('服务暂时异常')
  })
})
