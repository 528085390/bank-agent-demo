import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SubscriptionReminders from './SubscriptionReminders.vue'

function stub(items: unknown[]): void {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    request_id: 'req_1',
    data: { items, count: items.length, total_minor: 19900, horizon_days: 14 },
    error: null,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } })))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SubscriptionReminders.vue', () => {
  it('shows the count, the due date and hands a cancellation sentence to the composer', async () => {
    stub([{
      subscription_id: 'sub_1', name: '视频会员', merchant_name: '示例视频',
      amount_minor: 19900, next_due_date: '2026-11-02', days_until_due: 3,
    }])
    const wrapper = mount(SubscriptionReminders)
    await flushPromises()

    expect(wrapper.find('[data-test="reminder-count"]').text()).toBe('1')
    await wrapper.find('[data-test="reminder-bell"]').trigger('click')
    expect(wrapper.text()).toContain('3 天后自动续费')
    expect(wrapper.text()).toContain('199.00 元')

    await wrapper.find('[data-test="reminder-cancel"]').trigger('click')
    expect(wrapper.emitted('ask')?.[0]).toEqual(['帮我取消订阅「视频会员」'])
    expect(wrapper.find('[data-test="reminder-bell"]').exists()).toBe(true)
  })

  it('stays out of the header when nothing is due', async () => {
    stub([])
    const wrapper = mount(SubscriptionReminders)
    await flushPromises()

    expect(wrapper.find('[data-test="reminder-bell"]').exists()).toBe(false)
  })

  it('hides itself when the reminder read fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    const wrapper = mount(SubscriptionReminders)
    await flushPromises()

    expect(wrapper.find('[data-test="reminder-bell"]').exists()).toBe(false)
  })
})
