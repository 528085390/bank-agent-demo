/**
 * 账号状态：当前登录的是谁，以及登录/注册/退出这些动作。
 *
 * 这里只保存「服务端说我是谁」，不保存任何凭据：登录 token 在 HttpOnly Cookie 里，页面读不到
 * 也不需要读；CSRF token 从可读 Cookie 取（见 `csrf.ts`）。
 *
 * 启动时用 `GET /api/auth/me` 判断是否已登录，而不是读 localStorage。上一版把「已进入演示」
 * 当成前端状态，于是刷新页面和换账号都可能留下别人的界面；现在唯一可信的来源是服务端。
 *
 * 退出只有一个出口（`signOut`）：退出、掉线、改密、退出全部设备，四条路都从这里走 ——
 * 清账号状态、注销本地存档的作用域、清空本机残留、世代 +1（在飞请求作废）、广播给同源
 * 其它标签页，最后通知页面把界面收干净。少走一步，就是把上一个人的东西留给下一个人。
 */

import { reactive } from 'vue'

import { RequestError, request } from './api'
import { setCsrfToken } from './csrf'
import { identityChanged, type IdentityChangeReason } from './identity'
import { clearAppStorage, setStorageOwner } from './storageScope'

export interface AuthUser {
  user_id: string
  /** 账号唯一的名称就是登录名；没有凭据的档案账号（user_0001）为 null。 */
  login_name: string | null
  role: string
}

export interface Me extends AuthUser {
  email: string | null
  bank_ready: boolean
}

export interface AuthState {
  user: AuthUser | null
  me: Me | null
  /** 首次判断是否已登录是否完成。未完成时页面不渲染工作台，避免闪一下登录框。 */
  ready: boolean
}

export const auth = reactive<AuthState>({ user: null, me: null, ready: false })

/**
 * 为什么这个人离开了工作台。页面据此给登录页一句话：说「登录已过期」和说「已退出登录」，
 * 对用户是两件事 —— 前者要提醒他可能被别人动过，后者是他自己做的。
 */
export type SignOutReason = Extract<IdentityChangeReason, 'expired' | 'logout' | 'password' | 'logout-all'>

let signedOutHandler: ((reason: SignOutReason) => void) | null = null

/** 页面注册它，用来把界面切回登录页并清掉正文。 */
export function onSignedOut(handler: ((reason: SignOutReason) => void) | null): void {
  signedOutHandler = handler
}

export function isAuthError(exc: unknown): boolean {
  return exc instanceof RequestError
    && (exc.code === 'SESSION_REQUIRED' || exc.code === 'SESSION_MISMATCH')
}

export function clearAuth(): void {
  auth.user = null
  auth.me = null
  setCsrfToken(null)
  setStorageOwner(null)
}

/**
 * 记下服务端说的「我是谁」。
 *
 * 本地存档的作用域跟着这里走，不另设一处：只要 `auth.user` 有值，存档就一定按它的账号分键。
 * 两处各写一遍，迟早会出现「界面已经换人、存档还按旧账号写」的那一刻。
 */
function applyUser(me: Me): void {
  auth.user = { user_id: me.user_id, login_name: me.login_name, role: me.role }
  auth.me = me
  setStorageOwner(me.user_id)
}

/**
 * 把服务端的「我是谁」读进 store。返回 null 表示没有有效登录态。
 *
 * 只有 `SESSION_REQUIRED` 被当作「未登录」；其它错误照常抛出，否则后端挂了会显示成
 * 「请重新登录」，用户会一直重试一件永远失败的事。
 */
export async function refreshAuth(): Promise<Me | null> {
  try {
    const me = await request<Me>('/auth/me')
    applyUser(me)
    return me
  } catch (exc) {
    if (isAuthError(exc)) {
      clearAuth()
      return null
    }
    throw exc
  } finally {
    auth.ready = true
  }
}

export async function login(loginName: string, password: string): Promise<Me> {
  const issued = await request<{ csrf_token: string; user: AuthUser }>('/auth/login', {
    login_name: loginName,
    password,
  })
  setCsrfToken(issued.csrf_token)
  const me = await refreshAuth()
  if (!me) throw new RequestError('SESSION_REQUIRED', '登录后未能读取账号信息', '')
  return me
}

export async function register(loginName: string, password: string): Promise<void> {
  // 注册不签发登录态：注册成功只证明账号建好了，不能证明用户记住了刚设的密码。
  await request('/auth/register', { login_name: loginName, password })
}

/**
 * 退出这一个登录态。
 *
 * 服务端拒绝（cookie 早已失效）也算成功：用户想要的结果已经成立。真正的网络故障照常抛出 ——
 * 那种情况下服务端的登录态还在，页面假装退出只会让人以为自己在登录页后面已经安全了。
 */
export async function logout(): Promise<void> {
  try {
    await request('/auth/logout', {})
  } catch (exc) {
    if (!isAuthError(exc)) throw exc
  }
  signOut('logout')
}

/** 退出全部设备：改密之外的另一种「我不信任这些登录态了」。 */
export async function logoutEverywhere(): Promise<void> {
  await request('/auth/logout-all', {})
  signOut('logout-all')
}

/**
 * 改密。服务端会把所有登录态一起撤销（包括当前这个），所以这里不是「保存设置」而是一次
 * 登出：页面必须回到登录页，重新用新密码进来。
 */
export async function changePassword(current: string, next: string): Promise<void> {
  await request('/auth/password/change', {
    current_password: current,
    new_password: next,
  })
  signOut('password')
}

/**
 * 401 的统一处理：页面已经被服务端否掉了。
 *
 * 没登录过就不当作「掉线」—— 冷启动时 `GET /api/auth/me` 本来就会 401，那不是过期，页面
 * 只需要停在登录框。
 */
export function expireSession(): void {
  if (!auth.user) {
    clearAuth()
    return
  }
  signOut('expired')
}

/** 同源另一个标签页退出了：本页跟着走同一条出口，但不再广播回去（`broadcast: false`）。 */
export function adoptRemoteSignOut(reason: SignOutReason): void {
  signOut(reason, { broadcast: false })
}

/**
 * 清场的唯一实现。
 *
 * `clearStorage` 在没有账号状态时也照样清：掉线时 `auth.user` 可能已经被别处清掉，本机残留
 * 却还在，而这正是要清的东西。
 */
function signOut(
  reason: SignOutReason,
  options: { broadcast?: boolean; clearStorage?: boolean } = {},
): void {
  clearAuth()
  if (options.clearStorage !== false) clearAppStorage()
  // 世代 +1 让所有在飞请求的结果作废；广播让同源其它标签页立刻跟上。
  identityChanged(reason, { broadcast: options.broadcast })
  signedOutHandler?.(reason)
}
