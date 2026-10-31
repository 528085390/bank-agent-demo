/**
 * What the activity feed shows for a tool result.
 *
 * The envelope has four fields and only one of them is for the page: `summary`.
 * `display` is the structured channel and its full block protocol is deferred, so only
 * `text` blocks render today; anything else degrades to the summary rather than blanking
 * the page. `facts` is module-private and must never reach the DOM -- the backend
 * projection already drops it, and this module ignores it even if a payload still
 * carries it.
 */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

export interface ToolResultView {
  text: string
  blocks: string[]
}

/**
 * The failure codes this build has a better sentence for than the server's raw text.
 *
 * Keyed by code, never by the Chinese: the wording may change without touching the branch.
 */
export const ERROR_HINTS: Record<string, string> = {
  NO_CUSTOMER_BINDING: '当前演示用户还没有绑定银行客户号，请先在设置里完成绑定。',
  CONFIRMATION_REQUIRED: '已拦截：该高风险操作必须通过确认卡完成安全验证。',
  IDEMPOTENCY_KEY_REQUIRED: '系统繁忙，请稍后重试。',
  UPSTREAM_UNAVAILABLE: '银行服务暂时不可用，请稍后重试。',
  TOOL_DENIED: '已拦截：该操作未获授权或已被安全策略网关阻断。',
}

/**
 * 错误文本解析：
 * 1. 若为 TOOL_DENIED，坚决采用统一安全拦截提示；
 * 2. 若命中既有错误码映射表（如 NO_CUSTOMER_BINDING 等），采用前端友好提示；
 * 3. 其它具体业务错误（如 VALIDATION 对应的“转账金额必须大于 0”）优先原样展示，不盲目覆盖；
 * 4. 兜底返回服务端 error 或空串。
 */
export function toolErrorText(errorCode: string | null, error: string | null): string {
  if (errorCode === 'TOOL_DENIED') {
    return ERROR_HINTS.TOOL_DENIED
  }
  if (errorCode !== null && Object.hasOwn(ERROR_HINTS, errorCode)) {
    return ERROR_HINTS[errorCode]
  }
  return error ?? ''
}

/**
 * 判断文本是否属于安全网关拦截回复
 */
export function isInterceptedReply(text: string | null | undefined): boolean {
  if (!text) return false
  return text.startsWith('已拦截：') || text.includes('已被安全策略网关阻断')
}

export function renderToolResult(data: unknown): ToolResultView {
  if (!isRecord(data)) return { text: '', blocks: [] }

  const text = typeof data.summary === 'string' ? data.summary : ''
  const display = data.display
  if (!isRecord(display) || !Array.isArray(display.blocks)) return { text, blocks: [] }

  const blocks: string[] = []
  for (const raw of display.blocks) {
    if (!isRecord(raw)) continue
    // Only `text` is rendered this build. `fields`, `table` and `notice` arrive with the
    // full protocol; an unknown type is skipped on purpose (forward compatibility).
    if (raw.type === 'text' && typeof raw.text === 'string') blocks.push(raw.text)
  }
  return { text, blocks }
}
