/**
 * 「IM 接入」面板。
 *
 * 能在这里测的只有屏幕上的行为：打开面板要读且只读一次状态；点接入要发一次出码请求并把二维码
 * 摆出来；往后每秒醒一次、但只有到了服务端说的时刻才真的去问授权进展；授权到手就停轮询并刷新
 * 状态；两条退路（取消扫码、取消接入）要真的发对应的请求。
 *
 * 服务端是照 URL 分派的桩：它只认得本文件用到的四条路径，其余一律抛，多出来的请求会当场变成
 * 一次失败而不是悄悄通过。
 */

import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ImConnect from './ImConnect.vue'
import type { FeishuStatus } from './api'

enableAutoUnmount(afterEach)

interface Recorded {
  method: string
  url: string
}

const QR_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21"><rect width="21" height="21" /></svg>'

const NOT_CONFIGURED: FeishuStatus = {
  enabled: true, configured: false, app_id: '', running: false, bound_users: 0,
}

const CONNECTED: FeishuStatus = {
  enabled: true, configured: true, app_id: 'cli_a1****f9', running: true, bound_users: 2,
}

let requests: Recorded[]
let status: FeishuStatus
/** 第一次问是 pending，第二次就 authorized —— 模拟「人在飞书里点了确认」。 */
let polls: number

function envelope(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ request_id: 'req_test', data, error: null }),
  } as unknown as Response
}

/** 假服务端：只认这四条路径。 */
function serve(url: string, method: string): Response {
  requests.push({ method, url })
  if (url === '/api/channels/feishu/status' && method === 'GET') return envelope(status)
  if (url === '/api/channels/feishu/setup' && method === 'POST') {
    return envelope({
      setup_id: 'setup_abc', user_code: 'QUX3-SXDK', link: 'https://open.feishu.cn/page/launcher?user_code=QUX3-SXDK',
      qr_svg: QR_SVG, expires_in: 600, interval: 2,
    })
  }
  if (url === '/api/channels/feishu/disconnect' && method === 'POST') {
    status = NOT_CONFIGURED
    return envelope({ disconnected: true, bound_users: 0 })
  }
  if (url.indexOf('/api/channels/feishu/setup/') === 0) {
    if (method === 'DELETE') {
      return envelope({ setup_id: 'setup_abc', state: 'cancelled', message: '已取消扫码', retry_after: 0 })
    }
    polls += 1
    if (polls < 2) return envelope({ setup_id: 'setup_abc', state: 'pending', message: '', retry_after: 2 })
    // 授权之后 backend 已经写盘并热启动，所以状态也一起变了。
    status = CONNECTED
    return envelope({ setup_id: 'setup_abc', state: 'authorized', message: '接入成功', retry_after: 2 })
  }
  throw new Error('unexpected request: ' + method + ' ' + url)
}

/** 一次 UI 动作之后：让请求、微任务和随之而来的重渲染都跑完。 */
async function settle() {
  await flushPromises()
  await flushPromises()
}

async function advance(ms: number) {
  vi.advanceTimersByTime(ms)
  await settle()
}

function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const found = wrapper.findAll('button').find((item) => item.text().indexOf(text) >= 0)
  if (!found) throw new Error('button not found: ' + text)
  return found
}

beforeEach(() => {
  requests = []
  status = NOT_CONFIGURED
  polls = 0
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    return serve(String(url), String(init?.method ?? 'GET'))
  }))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('IM 接入面板', () => {
  it('打开面板只读一次状态', async () => {
    const wrapper = mount(ImConnect)
    await settle()

    expect(requests.filter((item) => item.url === '/api/channels/feishu/status')).toHaveLength(1)
    expect(wrapper.text()).toContain('接入飞书，用手机和助手对话')
    expect(wrapper.text()).toContain('扫码接入')
    // 面板一打开就自己轮询是最容易忘的退路：没点接入之前不该有任何定时请求。
    await advance(5000)
    expect(requests).toHaveLength(1)
  })

  it('点接入出码，轮询到授权后停手并刷新状态', async () => {
    const wrapper = mount(ImConnect)
    await settle()

    await buttonByText(wrapper, '扫码接入').trigger('click')
    await settle()

    expect(requests.some((item) => item.method === 'POST' && item.url === '/api/channels/feishu/setup')).toBe(true)
    expect(wrapper.html()).toContain('<svg xmlns="http://www.w3.org/2000/svg"')
    expect(wrapper.text()).toContain('QUX3-SXDK')

    // 第一秒就问一次，服务端说还要等：二维码留着。
    await advance(1000)
    expect(polls).toBe(1)
    expect(wrapper.text()).toContain('QUX3-SXDK')

    // retry_after=2 秒之内不再问。
    await advance(1000)
    expect(polls).toBe(1)

    // 到点再问，这次授权到手：轮到「已接入」那一支，并且状态被重新读了一次。
    await advance(1000)
    expect(polls).toBe(2)
    await settle()
    expect(wrapper.text()).toContain('已接入飞书')
    expect(wrapper.text()).toContain('cli_a1****f9')
    expect(requests.filter((item) => item.url === '/api/channels/feishu/status')).toHaveLength(2)

    // 已经接好了就别再打扰服务端。
    const before = requests.length
    await advance(10000)
    expect(requests).toHaveLength(before)
  })

  it('取消扫码会真的发一次 DELETE 并留在面板上', async () => {
    const wrapper = mount(ImConnect)
    await settle()
    await buttonByText(wrapper, '扫码接入').trigger('click')
    await settle()

    await buttonByText(wrapper, '取消扫码').trigger('click')
    await settle()

    const deletes = requests.filter((item) => item.method === 'DELETE')
    expect(deletes).toHaveLength(1)
    expect(deletes[0].url).toBe('/api/channels/feishu/setup/setup_abc')
    expect(wrapper.text()).toContain('本次扫码没有完成')
    expect(wrapper.text()).toContain('已取消扫码')

    // 取消之后不许再有轮询：定时器要跟着停。
    const before = requests.length
    await advance(10000)
    expect(requests).toHaveLength(before)
  })

  it('取消接入断开长连接并把面板退回未接入', async () => {
    status = CONNECTED
    const wrapper = mount(ImConnect)
    await settle()
    expect(wrapper.text()).toContain('已接入飞书')
    expect(wrapper.text()).toContain('2 人')

    await buttonByText(wrapper, '取消接入').trigger('click')
    await settle()

    expect(requests.some((item) => item.method === 'POST' && item.url === '/api/channels/feishu/disconnect')).toBe(true)
    expect(wrapper.text()).toContain('已取消接入：长连接已断开')
    expect(wrapper.text()).toContain('扫码接入')
  })

  it('已配置但长连接没连上时要说明白', async () => {
    status = { enabled: true, configured: true, app_id: 'cli_a1****f9', running: false, bound_users: 0 }
    const wrapper = mount(ImConnect)
    await settle()

    expect(wrapper.text()).toContain('已配置，长连接未连上')
    expect(wrapper.text()).toContain('未连接')
  })
})
