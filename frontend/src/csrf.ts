/**
 * CSRF token：从可读 Cookie 里取。
 *
 * 服务端在登录时同时下发两个 Cookie——`bank_session`（HttpOnly，浏览器凭证）和
 * `bank_csrf`（可读，CSRF token）。token 之所以必须可读，是因为页面要把它放进请求头；而
 * 放在 Cookie 里而不是内存里，是为了让刷新页面和多标签页都拿到同一个值，不必在启动时再
 * 请求一次、也不会出现「两个标签页各自换发导致一个失效」的竞态。
 *
 * 这不会削弱防护：别的源读不到这个 Cookie（同源策略），而且它单独存在没有用——服务端是拿它
 * 和当前登录态里存的摘要比的，没有 `bank_session` 就无从谈起。
 */

const CSRF_COOKIE = 'bank_csrf'

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const prefix = `${name}=`
  for (const part of document.cookie.split(';')) {
    const entry = part.trim()
    if (entry.startsWith(prefix)) return decodeURIComponent(entry.slice(prefix.length))
  }
  return null
}

/** 登录响应里返回的 token，作为 Cookie 不可用时的兜底（例如测试环境）。 */
let fallback: string | null = null

export function setCsrfToken(value: string | null): void {
  fallback = value && value.length > 0 ? value : null
}

export function currentCsrfToken(): string | null {
  return readCookie(CSRF_COOKIE) ?? fallback
}

export function csrfHeaders(): Record<string, string> {
  const token = currentCsrfToken()
  return token ? { 'X-CSRF-Token': token } : {}
}
