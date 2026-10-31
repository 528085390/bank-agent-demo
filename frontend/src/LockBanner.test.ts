import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LockBanner from './LockBanner.vue'

describe('LockBanner.vue (Presentational Component)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('FE-SEC-01: zero network requests on mount (preserves fetch whitelist)', () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)

    mount(LockBanner, {
      props: {
        lockedUntil: '2026-10-26T17:15:00+08:00',
      },
    })

    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('FE-SEC-02: renders warning banner text, countdown, and action buttons', () => {
    const wrapper = mount(LockBanner, {
      props: {
        lockedUntil: '2026-10-26T17:15:00+08:00',
        bankCurrentTime: '2026-10-26T17:00:00+08:00',
      },
    })

    expect(wrapper.text()).toContain('资金工具已锁定，查询仍可用')
    expect(wrapper.text()).toContain('15:00')
    expect(wrapper.find('.lock-unlock-btn').text()).toBe('解除锁定')
    expect(wrapper.find('.lock-close-btn').exists()).toBe(true)
  })

  it('FE-SEC-03: unlock click emits unlock and displays busy state', async () => {
    const wrapper = mount(LockBanner, {
      props: {
        lockedUntil: '2026-10-26T17:15:00+08:00',
        busy: false,
      },
    })

    await wrapper.find('.lock-unlock-btn').trigger('click')
    expect(wrapper.emitted('unlock')).toHaveLength(1)

    await wrapper.setProps({ busy: true })
    const btn = wrapper.find('.lock-unlock-btn')
    expect(btn.attributes('disabled')).toBeDefined()
    expect(btn.text()).toBe('正在解除...')
  })

  it('FE-SEC-04: countdown ticks down and emits expired at 0', async () => {
    const wrapper = mount(LockBanner, {
      props: {
        lockedUntil: '2026-10-26T17:00:10+08:00',
        bankCurrentTime: '2026-10-26T17:00:00+08:00',
      },
    })

    expect(wrapper.text()).toContain('00:10')

    vi.advanceTimersByTime(5000)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('00:05')

    vi.advanceTimersByTime(5000)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('00:00')
    expect(wrapper.emitted('expired')).toHaveLength(1)
  })

  it('FE-SEC-05: clicking close button emits close', async () => {
    const wrapper = mount(LockBanner, {
      props: {
        lockedUntil: '2026-10-26T17:15:00+08:00',
      },
    })

    await wrapper.find('.lock-close-btn').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('FE-SEC-06: robust simulated clock drift defense against past dates', () => {
    // Simulated bank clock is in past (e.g. 2026-03-01) while system time is current
    const wrapper = mount(LockBanner, {
      props: {
        lockedUntil: '2026-03-01T09:15:00+08:00',
        bankCurrentTime: '2026-03-01T09:00:00+08:00',
      },
    })

    expect(wrapper.text()).toContain('15:00')
  })

  it('FE-SEC-07: cleans interval on unmount to prevent timer leak', () => {
    const clearIntervalSpy = vi.spyOn(global, 'clearInterval')
    const wrapper = mount(LockBanner, {
      props: {
        lockedUntil: '2026-10-26T17:15:00+08:00',
      },
    })

    wrapper.unmount()
    expect(clearIntervalSpy).toHaveBeenCalled()
  })
})
