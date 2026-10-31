/**
 * 演示控制条，挂载后观察。
 *
 * 能在这里测的只有屏幕上的行为：打开抽屉要发且只发一次时钟读；拨钟只发一次 POST 且**不回读**；
 * 切到审计要发一次审计读；审计页开着的时候每 3 秒自己重读一次，「刷新」是立刻读的那一下；
 * 失败要变成一行文案，而不是一个未捕获的 rejection。
 *
 * 服务端是照 URL 分派的桩：它只认得本文件用到的四条路径，其余一律抛，多出来的请求会当场变成
 * 一次失败而不是悄悄通过。
 *
 * 第一条用例是硬约束：`App.test.ts` / `App.bill.test.ts` / `e2ePreview.test.ts` 的 health
 * 桩都返回 `demo_mode: true`，也就是这个条在那三套用例里都会被挂载，而它们的 fetch 桩是路径
 * 白名单 —— 组件只要在 `onMounted` 里发一个请求，17 个既有测试文件就全红。
 */

import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DemoControlBar from './DemoControlBar.vue'
import { demoStore } from './demoStore'

// --- 一个只会四条路径的假服务端 ------------------------------------------------------------

interface Recorded {
  method: string
  url: string
  body: unknown
}

/** 银行 `GET /clock` 的视图；`/api/demo/*` 三个端点原样透传的就是它。 */
interface ClockView {
  current_time: string
  run_id: string
  due_task_count: number
}

const START: ClockView = {
  current_time: '2026-10-22T09:00:00+08:00',
  run_id: '7',
  due_task_count: 2,
}

/** 审计的一页，oldest first —— 和后端一样；倒序是渲染的事，不是数据的事。 */
const EVENTS = [
  {
    event_id: 'evt_1',
    action: 'transfer_prepare',
    resource_type: 'policy_decision',
    resource_id: 'red',
    result: 'ok',
    reason: '大额转账需验证码',
    created_at: '2026-10-22T09:00:00+08:00',
  },
  {
    event_id: 'evt_2',
    action: 'transfer',
    resource_type: 'fund_write',
    resource_id: 'idem_2',
    result: 'failed',
    reason: '余额不足',
    created_at: '2026-10-22T10:15:00+08:00',
  },
]

let requests: Recorded[]
let clock: ClockView

function envelope<T>(data: T): string {
  return JSON.stringify({ request_id: 'req_test', data, error: null })
}

function errorEnvelope(code: string, message: string): string {
  return JSON.stringify({
    request_id: 'req_test',
    data: null,
    error: { code, message, retryable: false, details: {} },
  })
}

function json(body: string, status = 200): Response {
  return new Response(body, { status, headers: { 'Content-Type': 'application/json' } })
}

/**
 * 往前拨一步，仍按银行的 `+08:00` 写法返回。
 *
 * 断言里比的是人读得懂的那一行（`2026-10-23 09:00`），所以桩不能把时间漂成 UTC。
 */
function advanced(value: string, step: { days?: number; hours?: number }): string {
  const moment = new Date(value)
  moment.setTime(
    moment.getTime() + (step.days ?? 0) * 86_400_000 + (step.hours ?? 0) * 3_600_000,
  )
  const local = new Date(moment.getTime() + 8 * 3_600_000)
  return `${local.toISOString().slice(0, 19)}+08:00`
}

function installFetch(): void {
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    const method = (init.method ?? 'GET').toUpperCase()
    requests.push({ method, url, body: init.body ? JSON.parse(String(init.body)) : undefined })

    if (url === '/api/demo/clock') return json(envelope(clock))
    if (url === '/api/demo/clock/advance') {
      const step = JSON.parse(String(init.body)) as { days?: number; hours?: number }
      clock = { ...clock, current_time: advanced(clock.current_time, step), due_task_count: 3 }
      return json(envelope(clock))
    }
    if (url === '/api/demo/clock/run-tasks') {
      clock = { ...clock, due_task_count: 0 }
      return json(envelope({ processed_count: 2, completed_count: 1, failed_count: 1, items: [] }))
    }
    if (url === '/api/audit/business-events') {
      return json(envelope({ items: EVENTS, next_cursor: null }))
    }
    throw new Error(`unstubbed request: ${method} ${url}`)
  })
}

/** store 是模块级单例，每个用例都得从同一块空地开始。 */
function resetStore(): void {
  demoStore.open = false
  demoStore.tab = 'clock'
  demoStore.clock = null
  demoStore.audit = []
  demoStore.lastRun = null
  demoStore.busy = false
  demoStore.error = ''
  // 任务图也在这份 store 里：前一个用例留下的图不能被后一个读到。
  demoStore.resetTasks()
}

function hits(url: string): Recorded[] {
  return requests.filter(request => request.url === url)
}

async function open(wrapper: ReturnType<typeof mount>): Promise<void> {
  await wrapper.find('[data-test="toggle-panel"]').trigger('click')
  await flushPromises()
}

/** 最近一次拨钟请求的 body —— 断言只看它，不看第几次。 */
function lastAdvance(): Recorded | undefined {
  const advances = hits('/api/demo/clock/advance')
  return advances[advances.length - 1]
}

/**
 * 扣住审计那一次读，让用例看见「读取中」的界面。只扣这一条路径（精确匹配 URL），
 * 时钟和其它请求照常放行。
 */
function holdAudit() {
  const waiting: Array<() => void> = []
  let released = false
  const inner = globalThis.fetch
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
    if (!released && String(input) === '/api/audit/business-events') {
      await new Promise<void>(resolve => waiting.push(resolve))
    }
    return inner(input, init)
  })
  return {
    release: () => {
      released = true
      while (waiting.length) waiting.shift()!()
    },
  }
}

// 审计页的轮询计时器挂在组件上：用例结束必须卸载，否则它会跨用例继续发请求。
enableAutoUnmount(afterEach)

beforeEach(() => {
  requests = []
  clock = { ...START }
  resetStore()
  installFetch()
})

afterEach(() => {
  // 轮询用例中途切过假定时器；漏了这一步，后面的用例会拿到一个停住的钟。
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('演示控制条', () => {
  it('挂载时不发任何请求', async () => {
    // 一被调用就炸：`App.test.ts` 那三套用例的桩是路径白名单，未知路径正是这个反应。
    const fetchSpy = vi.fn(() => { throw new Error('unexpected fetch') })
    vi.stubGlobal('fetch', fetchSpy)

    const wrapper = mount(DemoControlBar)
    await flushPromises()

    expect(fetchSpy).not.toHaveBeenCalled()
    expect(requests).toHaveLength(0)
    // 没数据时那一行是一句可点的话，抽屉里的面板体还没有渲染。
    expect(wrapper.find('[data-test="clock-label"]').text()).toBe('模拟时钟 · 点击查看')
    expect(wrapper.find('[data-test="panel-clock"]').exists()).toBe(false)
  })

  it('点开面板时只读一次时钟', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()
    expect(requests).toHaveLength(0)

    await open(wrapper)

    expect(demoStore.open).toBe(true)
    const reads = hits('/api/demo/clock')
    expect(reads).toHaveLength(1)
    expect(reads[0].method).toBe('GET')
    // 抽屉真的开了：面板体渲染出来，时钟条换成银行报的时间。
    expect(wrapper.find('[data-test="panel-clock"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="clock-label"]').text()).toBe('2026-10-22 09:00')
    expect(demoStore.error).toBe('')
  })

  it('前进一天只发一次 POST，且不回读时钟', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()

    await wrapper.find('[data-test="advance-1d"]').trigger('click')
    await flushPromises()

    // 恰好一次请求，且就是拨钟本身：body 只带 days，多的字段银行会当成另一次拨动。
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('POST')
    expect(requests[0].url).toBe('/api/demo/clock/advance')
    expect(requests[0].body).toEqual({ days: 1 })
    // 响应体就是新的时钟，所以没有第二次 GET。
    expect(hits('/api/demo/clock')).toHaveLength(0)
    expect(wrapper.find('[data-test="clock-label"]').text()).toBe('2026-10-23 09:00')
    expect(demoStore.clock?.due_task_count).toBe(3)
    expect(demoStore.error).toBe('')
  })

  it('三个页签各切到自己的面板，切到审计时读一次审计', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)

    const tabs = wrapper.findAll('.demo-tab-btn')
    expect(tabs).toHaveLength(3)
    expect(wrapper.find('[data-test="panel-clock"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="panel-audit"]').exists()).toBe(false)

    await wrapper.find('[data-test="tab-audit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="panel-clock"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="panel-audit"]').exists()).toBe(true)
    const reads = hits('/api/audit/business-events')
    expect(reads).toHaveLength(1)
    expect(reads[0].method).toBe('GET')

    await wrapper.find('[data-test="tab-tasks"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="panel-audit"]').exists()).toBe(false)
    // 任务图页是真组件，不是占位：面板挂着，空态由 TaskGraphTab 自己给。
    expect(wrapper.find('[data-test="panel-tasks"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="tasks-empty"]').text()).toBe('当前无后台任务（对话为直接答复或尚未开始）')
    expect(wrapper.find('.demo-drawer-body').text()).not.toContain('任务编排视图将在 L2 接入')

    await wrapper.find('[data-test="tab-clock"]').trigger('click')
    await flushPromises()

    // 回到时钟页不再取数：时钟在打开抽屉时已经读过。
    expect(wrapper.find('[data-test="panel-clock"]').exists()).toBe(true)
    expect(hits('/api/demo/clock')).toHaveLength(1)
    expect(demoStore.error).toBe('')
  })

  it('请求失败显示成一行文案，不抛未捕获异常', async () => {
    // 非 2xx + 错误信封：`request()` 会 throw RequestError，store 必须把它接住。
    vi.stubGlobal('fetch', vi.fn(async () =>
      json(errorEnvelope('BANK_UNAVAILABLE', '银行模拟时钟暂不可用'), 502)))

    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)

    // 抽屉照开，失败是里面那一行字 —— 不是一次静默的空白。
    expect(demoStore.open).toBe(true)
    expect(wrapper.find('.demo-error').text()).toContain('银行模拟时钟暂不可用')
    expect(demoStore.error).toBe('银行模拟时钟暂不可用')
    expect(demoStore.busy).toBe(false)

    // 500 也走同一条通道；错误文案跟着换，busy 也回到 false。
    vi.stubGlobal('fetch', vi.fn(async () =>
      json(errorEnvelope('INTERNAL_ERROR', '服务端开小差了'), 500)))
    await wrapper.find('[data-test="advance-1d"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('.demo-error').text()).toContain('服务端开小差了')
    expect(demoStore.busy).toBe(false)
    expect(demoStore.clock).toBeNull()
  })

  it('审计按时间倒序渲染，store 里的顺序不动', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)
    await wrapper.find('[data-test="tab-audit"]').trigger('click')
    await flushPromises()

    const rows = wrapper.findAll('.demo-audit-row')
    expect(rows).toHaveLength(2)
    // 新的在上：evt_2 是 10:15，evt_1 是 09:00。
    expect(rows[0].find('.demo-audit-time').text()).toBe('10:15')
    expect(rows[0].find('.demo-audit-action').text()).toBe('transfer')
    expect(rows[0].find('.demo-audit-result').text()).toBe('failed')
    expect(rows[0].find('.demo-audit-result').classes()).toContain('failed')
    expect(rows[0].text()).toContain('余额不足')
    expect(rows[1].find('.demo-audit-time').text()).toBe('09:00')
    expect(rows[1].find('.demo-audit-action').text()).toBe('transfer_prepare')
    expect(rows[1].find('.demo-audit-result').classes()).toContain('ok')
    // 后端顺序（oldest first）没有被本地重排改写：翻页游标是跟着它走的。
    expect(demoStore.audit.map(event => event.event_id)).toEqual(['evt_1', 'evt_2'])
  })

  it('跑到期任务：先 POST 再回读时钟，并显示计数', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)

    await wrapper.find('[data-test="run-tasks"]').trigger('click')
    await flushPromises()

    const runs = hits('/api/demo/clock/run-tasks')
    expect(runs).toHaveLength(1)
    expect(runs[0].method).toBe('POST')
    // 没点名结局就不发字段：空 body 与「success」在银行那边是同一个调用。
    expect(runs[0].body).toEqual({})

    // 结果里没有到期数，所以跑完要回读一次时钟：2 变 0。
    expect(hits('/api/demo/clock')).toHaveLength(2)
    expect(wrapper.find('[data-test="due-count"]').text()).toBe('0')
    const result = wrapper.find('[data-test="run-result"]').text()
    expect(result).toContain('已处理 2')
    expect(result).toContain('完成 1')
    expect(result).toContain('失败 1')
    expect(demoStore.error).toBe('')
  })

  it('审计页开着时每 3 秒重读一次，切页签或收起抽屉就停', async () => {
    // 只假 setInterval / clearInterval：flushPromises 自己用的是 setTimeout，一起假掉会卡死。
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })

    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)
    await wrapper.find('[data-test="tab-audit"]').trigger('click')
    await flushPromises()

    // 切过来的这一下由 setTab 立刻读一次。
    expect(hits('/api/audit/business-events')).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(2)

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(3)

    // 切到任务图：再等三个间隔也不该多读一次。
    await wrapper.find('[data-test="tab-tasks"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(9000)
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(3)

    // 回到审计：立刻读一次，然后重新开始轮询。
    await wrapper.find('[data-test="tab-audit"]').trigger('click')
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(4)
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(5)

    // 收起抽屉也停：面板都收了，没有理由还在读。
    await wrapper.find('[data-test="toggle-panel"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(9000)
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(5)

    vi.useRealTimers()
  })

  it('「刷新」立刻重读一次，读取期间按钮禁用', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)
    await wrapper.find('[data-test="tab-audit"]').trigger('click')
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(1)

    const gate = holdAudit()
    await wrapper.find('[data-test="refresh-audit"]').trigger('click')
    await flushPromises()

    // 请求已经发出、还没回来（桩扣在记录之前，所以这里看 store 的飞行状态）：
    // 按钮禁用，提示说在更新。
    expect(demoStore.busy).toBe(true)
    expect(wrapper.find('[data-test="refresh-audit"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.demo-audit-hint').text()).toBe('更新中…')

    gate.release()
    await flushPromises()
    expect(hits('/api/audit/business-events')).toHaveLength(2)
    expect(demoStore.busy).toBe(false)
    expect(wrapper.find('[data-test="refresh-audit"]').attributes('disabled')).toBeUndefined()
    // 刷新不是「重置」：倒序渲染照旧，store 里仍是后端顺序。
    expect(demoStore.audit.map(event => event.event_id)).toEqual(['evt_1', 'evt_2'])
  })

  it('+2 / +7 天与自定义天数都走同一个拨钟动作', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)

    await wrapper.find('[data-test="step-2d"]').trigger('click')
    await flushPromises()
    expect(lastAdvance()?.body).toEqual({ days: 2 })

    await wrapper.find('[data-test="step-7d"]').trigger('click')
    await flushPromises()
    expect(lastAdvance()?.body).toEqual({ days: 7 })

    await wrapper.find('[data-test="step-days"]').setValue('30')
    await wrapper.find('form.demo-custom').trigger('submit')
    await flushPromises()
    expect(lastAdvance()?.body).toEqual({ days: 30 })

    // 三次拨动都只有一次 POST，没有一次额外的时钟读。
    expect(hits('/api/demo/clock/advance')).toHaveLength(3)
    expect(hits('/api/demo/clock')).toHaveLength(1)
    // 条上的时间跟着走：2026-10-22 + 39 天。
    expect(wrapper.find('[data-test="clock-label"]').text()).toBe('2026-11-30 09:00')
    expect(demoStore.error).toBe('')
  })

  it('自定义天数只收 1-365 的整数，非法值不发请求', async () => {
    const wrapper = mount(DemoControlBar)
    await flushPromises()
    await open(wrapper)
    const before = requests.length

    for (const bad of ['0', '366', '', '2.5', '-1']) {
      await wrapper.find('[data-test="step-days"]').setValue(bad)
      await wrapper.find('form.demo-custom').trigger('submit')
      await flushPromises()

      expect(requests).toHaveLength(before)
      expect(wrapper.find('[data-test="step-error"]').text()).toContain('1 到 365')
    }

    // 合法值：提示消失，请求发出，天数原样带上。
    await wrapper.find('[data-test="step-days"]').setValue('365')
    await wrapper.find('form.demo-custom').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[data-test="step-error"]').exists()).toBe(false)
    expect(lastAdvance()?.body).toEqual({ days: 365 })
  })
})
