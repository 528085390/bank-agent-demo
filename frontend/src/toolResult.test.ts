import { describe, expect, it } from 'vitest'

import { ERROR_HINTS, isInterceptedReply, renderToolResult, toolErrorText } from './toolResult'

/**
 * The activity feed used to print `compact(event.data.data)` -- the whole envelope as JSON --
 * which put a tool's module-private `facts` (account numbers, internal ids) on the page.
 * These tests pin the replacement: the summary, plus `text` blocks, and nothing else.
 */
describe('renderToolResult', () => {
  it('shows the summary and never the raw envelope', () => {
    const view = renderToolResult({
      summary: '活期账户余额 8,320.00 元。',
      display: {},
      business_ids: {},
    })

    expect(view.text).toBe('活期账户余额 8,320.00 元。')
  })

  it('never surfaces facts even if a payload still carries them', () => {
    // Defence in depth: the backend projection should have removed these, but a stale
    // server or a hand-rolled call must not put an account number on the page.
    const payload = {
      summary: '转账已受理。',
      display: {},
      business_ids: {},
      facts: { account_no: '6222-SECRET-MARKER', internal_id: 991 },
    }
    // Guards the assertion below against being vacuous: the marker really is in the payload.
    expect(JSON.stringify(payload)).toContain('SECRET-MARKER')

    const view = renderToolResult(payload)

    expect(JSON.stringify(view)).not.toContain('SECRET-MARKER')
    expect(view.text).toBe('转账已受理。')
  })

  it('falls back to the summary when display carries no blocks', () => {
    const view = renderToolResult({ summary: '已受理。', display: {}, business_ids: {} })

    expect(view.text).toBe('已受理。')
    expect(view.blocks).toEqual([])
  })

  it('renders text blocks when the module produced them', () => {
    const view = renderToolResult({
      summary: '已受理。',
      display: { blocks: [{ type: 'text', text: '24 小时后到账。' }] },
      business_ids: {},
    })

    expect(view.blocks).toEqual(['24 小时后到账。'])
  })

  it('ignores block types this build does not render yet', () => {
    // The full block protocol is deferred; an unknown or not-yet-implemented type must
    // degrade to the summary rather than blank the page.
    const view = renderToolResult({
      summary: '已受理。',
      display: { blocks: [{ type: 'table', columns: ['a'], rows: [['1']] }, { type: 'chart' }] },
      business_ids: {},
    })

    expect(view.blocks).toEqual([])
    expect(view.text).toBe('已受理。')
  })

  it('keeps the text blocks it can render and skips the rest', () => {
    const view = renderToolResult({
      summary: '已受理。',
      display: {
        blocks: [
          { type: 'text', text: '第一行' },
          { type: 'notice', level: 'info', text: '未实现的块' },
          { type: 'text', text: 42 },
          { type: 'text', text: '第二行' },
        ],
      },
      business_ids: {},
    })

    expect(view.blocks).toEqual(['第一行', '第二行'])
  })

  it('survives a malformed payload without throwing', () => {
    for (const data of [null, undefined, 'text', 7, [], {}, { summary: 5 }, { display: 'nope' }]) {
      expect(() => renderToolResult(data)).not.toThrow()
      // Both axes, so a regression that rendered `undefined` or `[object Object]` cannot slip
      // through: the fallback view is empty.
      expect(renderToolResult(data)).toEqual({ text: '', blocks: [] })
    }
  })
})

describe('toolErrorText', () => {
  it('prefers its own wording for a code it knows', () => {
    // The server text differs on purpose: the hint must win, because it is keyed by the code.
    expect(toolErrorText('NO_CUSTOMER_BINDING', '服务端返回的原文')).toBe(
      '当前演示用户还没有绑定银行客户号，请先在设置里完成绑定。',
    )
    expect(toolErrorText('UPSTREAM_UNAVAILABLE', '')).toBe('银行服务暂时不可用，请稍后重试。')
  })

  it('falls back to the server text for a code it does not know', () => {
    expect(toolErrorText('SOMETHING_NEW', '账户余额不足。')).toBe('账户余额不足。')
    expect(toolErrorText(null, '账户余额不足。')).toBe('账户余额不足。')
  })

  it('never matches on the Chinese text itself', () => {
    const known = ERROR_HINTS.NO_CUSTOMER_BINDING
    const reworded = '当前演示用户还没有绑定银行客户号，请先完成绑定。'
    // An unknown code falls back to the server text, whatever that text says.
    expect(toolErrorText('UNKNOWN_CODE', known)).toBe(known)
    expect(reworded).not.toBe(known)
    // The discriminating case: a *known code* with a *reworded server string* must yield the
    // hint, not the server's sentence. A text-keyed implementation returns `reworded` here.
    expect(toolErrorText('NO_CUSTOMER_BINDING', reworded)).toBe(known)
    // And the same reworded sentence under an unknown code stays exactly as the server wrote it.
    expect(toolErrorText('UNKNOWN_CODE', reworded)).toBe(reworded)
  })

  it('treats a prototype key as a code it does not know', () => {
    // A `Record` lookup would find `toString` on the prototype and put a function on the page.
    expect(toolErrorText('toString', '账户余额不足。')).toBe('账户余额不足。')
  })

  it('renders nothing rather than "null" when there is no text to show', () => {
    expect(toolErrorText(null, null)).toBe('')
    expect(toolErrorText('SOMETHING_NEW', null)).toBe('')
  })
})

describe('toolErrorText injection defense', () => {
  it('returns unified interception hint for TOOL_DENIED', () => {
    expect(toolErrorText('TOOL_DENIED', null)).toBe('已拦截：该操作未获授权或已被安全策略网关阻断。')
    expect(toolErrorText('TOOL_DENIED', '服务端传入的其它错误')).toBe('已拦截：该操作未获授权或已被安全策略网关阻断。')
  })

  it('preserves specific business validation errors when errorCode is not in ERROR_HINTS', () => {
    expect(toolErrorText('VALIDATION', '转账金额必须大于 0。')).toBe('转账金额必须大于 0。')
  })

  it('safely handles empty or whitespace error strings', () => {
    expect(toolErrorText('SOME_CODE', '')).toBe('')
    expect(toolErrorText(null, '   ')).toBe('   ')
  })
})

describe('isInterceptedReply', () => {
  it('identifies intercepted replies by prefix or keyword', () => {
    expect(isInterceptedReply('已拦截：该操作未获授权或已被安全策略网关阻断。')).toBe(true)
    expect(isInterceptedReply('系统通知：已被安全策略网关阻断')).toBe(true)
  })

  it('returns false for normal business messages', () => {
    expect(isInterceptedReply('转账已成功受理。')).toBe(false)
    expect(isInterceptedReply('活期账户余额为 8,320.00 元。')).toBe(false)
  })

  it('safely handles null and undefined without throwing', () => {
    expect(isInterceptedReply(null)).toBe(false)
    expect(isInterceptedReply(undefined)).toBe(false)
    expect(isInterceptedReply('')).toBe(false)
  })
})
