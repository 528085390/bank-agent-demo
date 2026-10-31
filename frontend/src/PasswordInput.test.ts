/**
 * 密码框的「显示 / 隐藏」。
 *
 * 这几条钉的是组件自己的契约，而不是某处的用法：属性要透传（不然 `name`、`autocomplete`、
 * `@keydown.enter` 会被包一层吞掉），按钮不能提交表单（不然在登录页点一下眼睛就等于拿半截
 * 密码去换 401），`type` 由组件说了算（不然调用方传个 `type` 就把功能架空了）。
 */

import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import PasswordInput from './PasswordInput.vue'

enableAutoUnmount(afterEach)

describe('PasswordInput', () => {
  it('默认是密码框，点一下变成明文，再点一下变回去', async () => {
    const wrapper = mount(PasswordInput)
    const input = wrapper.find('input')
    const toggle = wrapper.find('button')

    expect(input.attributes('type')).toBe('password')
    expect(toggle.attributes('aria-pressed')).toBe('false')

    await toggle.trigger('click')
    expect(wrapper.find('input').attributes('type')).toBe('text')
    expect(wrapper.find('button').attributes('aria-pressed')).toBe('true')

    await wrapper.find('button').trigger('click')
    expect(wrapper.find('input').attributes('type')).toBe('password')
  })

  it('按钮的读屏名字跟着状态走，并用调用方给的名字', async () => {
    const wrapper = mount(PasswordInput, { props: { label: '支付密码' } })
    const toggle = wrapper.find('button')

    expect(toggle.attributes('aria-label')).toBe('显示支付密码')
    expect(toggle.attributes('title')).toBe('显示支付密码')

    await toggle.trigger('click')
    expect(wrapper.find('button').attributes('aria-label')).toBe('隐藏支付密码')
  })

  it('切换不清空已经输入的内容', async () => {
    const wrapper = mount(PasswordInput)
    await wrapper.find('input').setValue('a-long-enough-password')

    await wrapper.find('button').trigger('click')

    expect((wrapper.find('input').element as HTMLInputElement).value).toBe(
      'a-long-enough-password',
    )
  })

  it('输入的仍然是原值，`v-model` 拿到的是明文', async () => {
    const wrapper = mount(PasswordInput, { props: { modelValue: '' } })
    await wrapper.find('input').setValue('secret-1')

    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['secret-1'])
  })

  it('按钮不会提交表单', async () => {
    const wrapper = mount(PasswordInput)

    expect(wrapper.find('button').attributes('type')).toBe('button')
  })

  it('属性透传：placeholder、name、autocomplete、inputmode 和键盘监听都落到了 input 上', async () => {
    const wrapper = mount(PasswordInput, {
      attrs: {
        name: 'password',
        placeholder: '请输入密码',
        autocomplete: 'current-password',
        inputmode: 'numeric',
        maxlength: '32',
      },
    })
    const input = wrapper.find('input')

    expect(input.attributes('name')).toBe('password')
    expect(input.attributes('placeholder')).toBe('请输入密码')
    expect(input.attributes('autocomplete')).toBe('current-password')
    expect(input.attributes('inputmode')).toBe('numeric')
    expect(input.attributes('maxlength')).toBe('32')
  })

  it('调用方传 type 也改不动它', async () => {
    const wrapper = mount(PasswordInput, { attrs: { type: 'password' } })

    await wrapper.find('button').trigger('click')

    expect(wrapper.find('input').attributes('type')).toBe('text')
  })
})
