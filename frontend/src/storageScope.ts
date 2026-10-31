/**
 * 本地存档属于谁。
 *
 * 浏览器里的 `localStorage` 是整个源的，不是某个账号的：退出登录不会清空它，换一个人登录也
 * 不会。P1/P2 把服务端的隔离做完了，剩下的这条缝在这里 —— 同一台电脑上，A 的报告和未发完的
 * 草稿留在浏览器里，B 登录后仍能读到（设计 §7.3）。
 *
 * 规则只有两条：
 *
 * 1. **内容键带账号段**：`<base>:<账号>:<会话>`。读的时候按当前账号拼键，所以 B 的键天然
 *    不是 A 的键。
 * 2. **身份一变就清**：登录时清掉不属于当前账号的内容键（含旧版本没带账号段的键），退出时
 *    全清。见 `purgeContentStorage` / `clearAppStorage`。
 *
 * 为什么不是「把敏感正文挪到服务端就完事」：报告已经在服务端存了一份（消息 metadata），本地
 * 这份是「本机刷新立刻能看到完整报告」的加速通道；删掉它就要退化成先看到占位卡再点开。首版
 * 选择保留体验，用「按账号分键 + 换人清空」把边界画出来。
 *
 * 没设置账号时（身份还没确认），内容键一律不读不写：宁可少一份缓存，也不把内容落到一个
 * 下一个账号可能读到的位置。
 */

/** 本应用写进 `localStorage` 的两种前缀。别的键不归这里管，也不该被这里删掉。 */
const APP_PREFIXES = ['bank_agent.', 'bank-agent.'] as const

/**
 * 带账号段的内容存档。一处定义，`conversations.ts` 与 `reportStore.ts` 都从这里取，
 * 清理时也只认这里列出的基名 —— 三个地方各写一份字符串，迟早会有一份漏改。
 */
export const CONTENT_STORAGE = {
  /** 已结算的深度分析报告，按 `账号:会话`。 */
  reports: 'bank_agent.settled_reports.v2',
  /** 尚未跑完的那一次提交（含用户原话），按 `账号:会话`。 */
  pending: 'bank-agent.pending-submission',
} as const

/** 这些键是偏好，不含正文：不按账号分键，也不在换人时被内容清理碰到。 */
const PREFERENCE_KEYS = ['bank-agent.last-conversation'] as const

/** 内容存档的基名，清理时逐个前缀比对。 */
const CONTENT_BASES: string[] = Object.values(CONTENT_STORAGE)

let owner: string | null = null

/** 认定当前账号。`null` 表示身份未知 —— 内容存档从此读写都当没有。 */
export function setStorageOwner(userId: string | null): void {
  owner = userId && userId.length > 0 ? userId : null
}

export function currentStorageOwner(): string | null {
  return owner
}

/**
 * 一个内容存档键：`<base>:<账号>:<id>`。没有账号时返回 `null`，调用方据此不读不写。
 *
 * 账号段做 URL 编码，`:` 不会出现在里面，所以「切出账号段」就是按 `:` 分一次，不会因为
 * 用户号里恰好带冒号而串到别人的键上。
 */
export function scopedStorageKey(base: string, id: string): string | null {
  if (!owner) return null
  return `${base}:${encodeURIComponent(owner)}:${id}`
}

/** 同一账号下某类存档的前缀，用于枚举（例如「还有哪些会话欠服务端一次请求」）。 */
export function scopedStoragePrefix(base: string): string | null {
  if (!owner) return null
  return `${base}:${encodeURIComponent(owner)}:`
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

function appKeys(store: Storage): string[] {
  const keys: string[] = []
  for (let index = 0; index < store.length; index += 1) {
    const key = store.key(index)
    if (key && APP_PREFIXES.some(prefix => key.startsWith(prefix))) keys.push(key)
  }
  return keys
}

/**
 * 删掉所有不属于当前账号的内容存档。
 *
 * 认不出来的内容键也删：旧版本写下的键没有账号段（`bank_agent.settled_reports.v1:…`、
 * `bank-agent.pending-submission:…`），按设计 §7.3 的要求它们不迁移给任何账号 —— 上一个版本
 * 留下的报告与草稿，与其猜它属于谁，不如让它消失。
 */
export function purgeContentStorage(): void {
  const store = storage()
  if (!store) return
  const keep = (Object.keys(CONTENT_STORAGE) as Array<keyof typeof CONTENT_STORAGE>)
    .map(kind => scopedStoragePrefix(CONTENT_STORAGE[kind]))
    .filter((prefix): prefix is string => prefix !== null)
  // A key that is exactly the base name is the oldest layout of all: one slot shared by every
  // conversation, written before the records were split. Nothing proves who it belongs to, so it
  // goes with the rest — matching only `base:` would leave that one key behind forever.
  const doomed = appKeys(store).filter(
    key => CONTENT_BASES.some(base => key === base || key.startsWith(`${base}:`))
      && !keep.some(prefix => key.startsWith(prefix)),
  )
  for (const key of doomed) store.removeItem(key)
}

/**
 * 删掉本应用在这个源上留下的全部东西：内容、偏好、旧版本残留。
 *
 * 退出登录、改密、掉线都走这里 —— 这些时候「下一个用这台电脑的人」可能是任何人，而偏好键
 * （最近打开的会话）虽不含正文，却指向别人的历史，没有理由留下。
 */
export function clearAppStorage(): void {
  const store = storage()
  if (!store) return
  for (const key of appKeys(store)) store.removeItem(key)
}

/** 某个键是不是偏好键（供测试与清理逻辑自查）。 */
export function isPreferenceKey(key: string): boolean {
  return (PREFERENCE_KEYS as readonly string[]).includes(key)
}

/** 测试用：把账号作用域复位到「身份未知」。 */
export function resetStorageScope(): void {
  owner = null
}
