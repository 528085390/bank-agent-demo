/**
 * 本地存档属于谁。
 *
 * `localStorage` 是整个源的，不是某个账号的：退出登录不会清空它，换一个人登录也不会。P3 的
 * 取舍是「留着这份加速副本，但按账号分键、换人时清干净」（设计 §7.3），而这份取舍能不能成立
 * 全看这个模块的两条规则：键里有账号段，身份一变就清。
 *
 * 这里逐条钉住它们，因为漏一条不会报错 —— 只会在共用电脑上，把上一个人的报告留给下一个人。
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  clearAppStorage,
  CONTENT_STORAGE,
  currentStorageOwner,
  isPreferenceKey,
  purgeContentStorage,
  resetStorageScope,
  scopedStorageKey,
  scopedStoragePrefix,
  setStorageOwner,
} from './storageScope'

const ALICE = 'user_0001'
const BOB = 'user_0002'

beforeEach(() => {
  localStorage.clear()
  resetStorageScope()
})

afterEach(() => {
  resetStorageScope()
})

function keys(): string[] {
  return Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '')
    .sort()
}

describe('按账号分键', () => {
  it('身份未知时不给键：宁可少一份缓存，也不写到下一个账号能读的位置', () => {
    expect(currentStorageOwner()).toBeNull()
    expect(scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')).toBeNull()
    expect(scopedStoragePrefix(CONTENT_STORAGE.reports)).toBeNull()
  })

  it('键里带账号段，两个账号的同名会话不是同一个键', () => {
    setStorageOwner(ALICE)
    const aliceKey = scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')
    setStorageOwner(BOB)
    const bobKey = scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')

    expect(aliceKey).toBe(`${CONTENT_STORAGE.reports}:user_0001:conv_a`)
    expect(bobKey).toBe(`${CONTENT_STORAGE.reports}:user_0002:conv_a`)
    expect(aliceKey).not.toBe(bobKey)
  })

  it('账号段做 URL 编码，用户号里带冒号也不会串到别人的键上', () => {
    setStorageOwner('user:with:colons')

    const key = scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')

    expect(key).toBe(`${CONTENT_STORAGE.reports}:user%3Awith%3Acolons:conv_a`)
    // 按 `:` 分一次就能切出账号段，不会被用户号里的冒号骗到。
    expect(key?.split(':')[1]).toBe('user%3Awith%3Acolons')
  })

  it('空用户号当作身份未知', () => {
    setStorageOwner('')

    expect(currentStorageOwner()).toBeNull()
    expect(scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')).toBeNull()
  })
})

describe('换人时的清理', () => {
  it('只留下当前账号的内容键，别人的与旧版没带账号段的都清掉', () => {
    setStorageOwner(ALICE)
    const aliceReport = scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')!
    const aliceDraft = scopedStorageKey(CONTENT_STORAGE.pending, 'conv_a')!
    localStorage.setItem(aliceReport, '甲的旧报告')
    localStorage.setItem(aliceDraft, '甲的旧草稿')

    setStorageOwner(BOB)
    const bobReport = scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')!
    localStorage.setItem(bobReport, '乙的报告')
    // 升级前的键：只有基名，没有账号段，也没有会话段。
    localStorage.setItem(CONTENT_STORAGE.pending, '升级前留下的提交')
    // 上一版带会话段但没有账号段。
    localStorage.setItem(`${CONTENT_STORAGE.reports}:conv_a`, '升级前留下的报告')

    purgeContentStorage()

    expect(keys()).toEqual([bobReport])
    expect(localStorage.getItem(bobReport)).toBe('乙的报告')
  })

  it('身份未知时一个内容键都不留', () => {
    localStorage.setItem(`${CONTENT_STORAGE.reports}:conv_a`, '无主的报告')
    localStorage.setItem(`${CONTENT_STORAGE.reports}:user_0001:conv_a`, '有主的报告')

    resetStorageScope()
    purgeContentStorage()

    expect(keys()).toEqual([])
  })

  it('偏好键不是正文，换人不该被内容清理顺手删掉', () => {
    setStorageOwner(BOB)
    localStorage.setItem('bank-agent.last-conversation', 'conv_a')

    purgeContentStorage()

    expect(localStorage.getItem('bank-agent.last-conversation')).toBe('conv_a')
    expect(isPreferenceKey('bank-agent.last-conversation')).toBe(true)
  })

  it('退出登录清掉本应用的一切，包括偏好与旧版残留', () => {
    setStorageOwner(ALICE)
    localStorage.setItem(scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')!, '报告')
    localStorage.setItem(scopedStorageKey(CONTENT_STORAGE.pending, 'conv_a')!, '草稿')
    localStorage.setItem('bank-agent.last-conversation', 'conv_a')
    localStorage.setItem(`${CONTENT_STORAGE.reports}:conv_a`, '旧版报告')
    // 别的应用写在这个源上的键不归这里管。
    localStorage.setItem('someone-else.key', '不是我们的')

    clearAppStorage()

    expect(keys()).toEqual(['someone-else.key'])
  })

  it('清空之后读不到上一个人的报告', () => {
    setStorageOwner(ALICE)
    const key = scopedStorageKey(CONTENT_STORAGE.reports, 'conv_a')!
    localStorage.setItem(key, '甲的深度分析')

    clearAppStorage()
    setStorageOwner(ALICE)

    // 同一个账号再进来也读不到了 —— 这是一次明确的丢弃，不是「暂时藏起来」。
    expect(localStorage.getItem(key)).toBeNull()
  })
})
