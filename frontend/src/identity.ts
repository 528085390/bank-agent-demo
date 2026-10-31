/**
 * 身份世代：页面上的每一次「现在登录的是谁」都带一个编号。
 *
 * 为什么需要它：退出、掉线、换号、改密之后，上一个人发出去的请求还在路上 —— SSE 还在流、
 * 账单还在轮询、历史还在读。它们回来时页面可能已经属于另一个人，照旧写进去就是把 A 的内容
 * 画进 B 的界面（设计 §7.3、验收 A11/A12）。所以每个异步动作在开始时取一个编号，回来时
 * 先比一次：编号变了，整段作废。
 *
 * 这里只有三件事，且都不认识 Vue，也不认识 `fetch`：
 *
 * - 世代编号与订阅：谁关心「人换了」，谁自己注册；
 * - 在飞请求的中止：换人时立刻掐断，而不是等它回来再丢掉；
 * - 跨标签页广播：一个标签页退出，同源的其它标签页跟着清场。
 *
 * 广播用 `BroadcastChannel`，浏览器不支持时静默降级为「只靠聚焦复核」。降级不是漏洞：
 * 另一个标签页仍然会在下一次请求被服务端拒掉（401）或下一次获得焦点时发现。
 */

/** 一次身份变化的来龙去脉，用于给用户一句解释。 */
export type IdentityChangeReason = 'login' | 'logout' | 'expired' | 'password' | 'logout-all'

export interface IdentityChange {
  generation: number
  reason: IdentityChangeReason
}

/** 一次异步动作开始时取到的身份。回来时把它交给 `isStale` 就知道还能不能写页面。 */
export interface IdentityScope {
  generation: number
}

const CHANNEL_NAME = 'bank-agent.identity'

let generation = 0
const listeners = new Set<(change: IdentityChange) => void>()
const controllers = new Set<AbortController>()

let channel: BroadcastChannel | null = null

/** 当前世代。测试与断言用；业务代码应当持有 `newIdentityScope()` 的结果而不是这个数字。 */
export function currentGeneration(): number {
  return generation
}

export function newIdentityScope(): IdentityScope {
  return { generation }
}

/** 取到作用域之后页面已经换过人：这一轮的任何写入都必须放弃。 */
export function isStale(scope: IdentityScope): boolean {
  return scope.generation !== generation
}

/** 订阅身份变化。返回取消订阅的函数，组件卸载时调用。 */
export function onIdentityChange(handler: (change: IdentityChange) => void): () => void {
  listeners.add(handler)
  return () => { listeners.delete(handler) }
}

/**
 * 在飞请求的中止信号。
 *
 * 只给「会持续写入页面」的请求用（一条 SSE 流可能流好几分钟），普通读靠世代编号丢弃结果就
 * 够了：为一个已经没人要的响应多做一次抽象不值得。调用方在请求结束时必须 `release()`，
 * 否则一个长页面会攒下一堆死控制器。
 */
export interface TrackedRequest {
  signal: AbortSignal
  /** Stop this request without waiting for the server, but leave other requests alone. */
  abort: () => void
  release: () => void
}

export function trackRequest(): TrackedRequest {
  const controller = new AbortController()
  controllers.add(controller)
  return {
    signal: controller.signal,
    abort: () => controller.abort(),
    release: () => { controllers.delete(controller) },
  }
}

/** 掐断所有在飞请求。换人时调用；被中止的请求会以 `AbortError` 结束，由调用方静默丢弃。 */
export function abortInflight(): void {
  for (const controller of controllers) controller.abort()
  controllers.clear()
}

/**
 * 宣布「人换了」：世代 +1、掐断在飞请求、通知本页订阅者，必要时广播给同源其它标签页。
 *
 * 广播只在**本标签页发起的**变化上发送（`broadcast: false` 用于收到广播后的自我清理），
 * 否则两个标签页会互相回响。
 */
export function identityChanged(
  reason: IdentityChangeReason,
  options: { broadcast?: boolean } = {},
): IdentityChange {
  generation += 1
  abortInflight()
  const change: IdentityChange = { generation, reason }
  if (options.broadcast !== false) announce(reason)
  for (const listener of [...listeners]) listener(change)
  return change
}

function announce(reason: IdentityChangeReason): void {
  const bus = ensureChannel()
  if (!bus) return
  try {
    bus.postMessage({ reason })
  } catch {
    // 一个已经关闭的频道不值得让退出流程失败：本页的状态已经清完了。
  }
}

function ensureChannel(): BroadcastChannel | null {
  if (channel) return channel
  if (typeof BroadcastChannel === 'undefined') return null
  try {
    channel = new BroadcastChannel(CHANNEL_NAME)
  } catch {
    return null
  }
  return channel
}

/**
 * 订阅其他标签页的身份变化。回调只处理「别人发来的」消息，本标签页自己发的那条不会回到这里。
 */
export function listenIdentityChange(
  handler: (reason: IdentityChangeReason) => void,
): () => void {
  const bus = ensureChannel()
  if (!bus) return () => {}
  const listener = (event: MessageEvent) => {
    const reason = (event.data as { reason?: unknown } | null)?.reason
    if (typeof reason !== 'string') return
    handler(reason as IdentityChangeReason)
  }
  bus.addEventListener('message', listener)
  return () => { bus.removeEventListener('message', listener) }
}

/**
 * 测试用的复位：世代归零、清空订阅者与频道。
 *
 * 模块级状态不随组件卸载消失，而测试会反复挂载页面；不重置的话上一个用例的世代会漏进下一个。
 */
export function resetIdentity(): void {
  generation = 0
  listeners.clear()
  controllers.clear()
  if (channel) {
    try { channel.close() } catch { /* 已经关掉的频道 */ }
    channel = null
  }
}
