/**
 * 登录 / 注册页，挂载后观察。
 *
 * 这里钉两件被用户直接指出来过的事：
 *
 * 1. 注册表单**只有一个名字**——登录名。曾经还有「显示名」，没人能改、没有校验，注册时留空
 *    就默认填成登录名，于是每个屏幕都得决定显示哪一个。现在它连同数据库列一起删掉了，这条
 *    用例守着别再长回来。
 * 2. 来源校验失败必须说清**哪个地址对不上**。以前三种失败共用一句「请求来源校验失败，请刷新
 *    页面后重试」，而刷新治不了代理改写 Host——那正是它真实的成因。
 */

import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AuthView from './AuthView.vue'
import { RequestError } from './api'

// `vi.mock` 会被提升到文件顶部，所以桩必须用 `vi.hoisted` 建，否则工厂执行时变量还没初始化。
const { loginMock, registerMock } = vi.hoisted(() => ({
  loginMock: vi.fn(),
  registerMock: vi.fn(),
}))

vi.mock('./auth', () => ({
  login: (...args: unknown[]) => loginMock(...args),
  register: (...args: unknown[]) => registerMock(...args),
}))

enableAutoUnmount(afterEach)

beforeEach(() => {
  loginMock.mockReset()
  registerMock.mockReset()
})

/** 切到注册页并填好一份能提交的表单。 */
async function mountRegister() {
  const wrapper = mount(AuthView)
  await wrapper.findAll('.auth-tabs button')[1].trigger('click')
  await wrapper.find('input[name="login_name"]').setValue('cghry')
  await wrapper.find('input[name="password"]').setValue('a-long-enough-password')
  await wrapper.find('input[name="confirm_password"]').setValue('a-long-enough-password')
  return wrapper
}

describe('注册表单', () => {
  it('只有一个名字：登录名，没有第二个「显示名」', async () => {
    const wrapper = await mountRegister()

    expect(wrapper.findAll('input').map(field => field.attributes('name')))
      .toEqual(['login_name', 'password', 'confirm_password'])
    expect(wrapper.text()).not.toContain('显示名')
  })

  it('两次密码不一致时本地就拦住，不发注册请求', async () => {
    const wrapper = await mountRegister()
    await wrapper.find('input[name="confirm_password"]').setValue('another-password')

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(registerMock).not.toHaveBeenCalled()
    expect(wrapper.find('.auth-message.error').text()).toContain('两次输入的密码不一致')
  })

  it('注册成功后回到登录页，并留下刚才的登录名', async () => {
    registerMock.mockResolvedValueOnce(undefined)
    const wrapper = await mountRegister()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(registerMock).toHaveBeenCalledWith('cghry', 'a-long-enough-password')
    // 让用户重新输一遍密码是对的，让他重新输一遍账号不是。
    expect((wrapper.find('input[name="login_name"]').element as HTMLInputElement).value).toBe('cghry')
    expect(wrapper.find('input[name="confirm_password"]').exists()).toBe(false)
    expect(wrapper.find('.auth-message.success').text()).toContain('账号已创建')
  })
})

describe('失败文案', () => {
  it('来源不一致时念出收到的来源和本站期望的主机', async () => {
    registerMock.mockRejectedValueOnce(new RequestError(
      'ORIGIN_REJECTED',
      '请求来源与本站不一致，已被拒绝（跨站请求会被拦截）',
      'req_origin',
      { origin: 'evil.example', expected: ['127.0.0.1:5173'] },
    ))
    const wrapper = await mountRegister()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const text = wrapper.find('.auth-message.error').text()
    expect(text).toContain('evil.example')
    expect(text).toContain('127.0.0.1:5173')
  })

  it('details 缺失时退回服务端那句话，不显示半句残句', async () => {
    registerMock.mockRejectedValueOnce(new RequestError(
      'ORIGIN_REJECTED', '请求来源与本站不一致，已被拒绝（跨站请求会被拦截）', 'req_origin',
    ))
    const wrapper = await mountRegister()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.auth-message.error').text())
      .toBe('请求来源与本站不一致，已被拒绝（跨站请求会被拦截）')
  })

  it('CSRF 缺失与令牌不符各有各的话，不再和来源问题混成一句', async () => {
    registerMock.mockRejectedValueOnce(new RequestError(
      'CSRF_TOKEN_MISSING', '请求缺少 CSRF 令牌，请刷新页面后重试', 'req_2',
    ))
    const wrapper = await mountRegister()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.auth-message.error').text()).toContain('缺少 CSRF 令牌')
  })
})

describe('密码框可以看一眼', () => {
  it('登录页的密码框点一下就显示明文，且不影响提交', async () => {
    const wrapper = mount(AuthView)
    const toggle = wrapper.find('button[aria-label="显示密码"]')

    expect(toggle.exists()).toBe(true)
    expect(wrapper.find('input[name="password"]').attributes('type')).toBe('password')

    await toggle.trigger('click')

    expect(wrapper.find('input[name="password"]').attributes('type')).toBe('text')
    // 眼睛是 `type="button"`：点它不该发出登录请求。
    expect(loginMock).not.toHaveBeenCalled()
  })

  it('确认密码那一格也能看，而且和上面那格各管各的', async () => {
    const wrapper = await mountRegister()

    await wrapper.find('button[aria-label="显示确认密码"]').trigger('click')

    expect(wrapper.find('input[name="confirm_password"]').attributes('type')).toBe('text')
    expect(wrapper.find('input[name="password"]').attributes('type')).toBe('password')
  })
})

describe('离场提示是一次性的', () => {
  it('退出登录后提示浮出来，几秒后自己消失，表单上不留痕迹', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(AuthView, { props: { reason: '已退出登录' } })
      await wrapper.vm.$nextTick()

      expect(wrapper.find('.auth-toast').text()).toBe('已退出登录')
      // 它是浮层，不在表单里：出现和消失都不该把按钮顶来顶去。
      expect(wrapper.find('form .auth-toast').exists()).toBe(false)

      vi.advanceTimersByTime(4000)
      await wrapper.vm.$nextTick()
      await wrapper.vm.$nextTick()

      // 提示消失之后，页面上再也找不到这句话 —— 不是被挪到别处继续挂着。
      expect(wrapper.text()).not.toContain('已退出登录')
    } finally {
      vi.useRealTimers()
    }
  })

  it('没有原因时什么都不显示 —— 刚打开页面不该有人解释上一段会话', () => {
    const wrapper = mount(AuthView)

    expect(wrapper.find('.auth-toast').exists()).toBe(false)
  })
})
