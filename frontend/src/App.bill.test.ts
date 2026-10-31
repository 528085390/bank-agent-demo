/**
 * 账单外壳：报告从 tool_result 里渲染出来，日期字段用「年 / 月 / 日」回答。
 *
 * `AnalysisResult` 和 `dateFields` 各自的单测盯的是纯函数；这个文件盯的是把整个 App
 * 挂起来之后才存在的那几根线 —— 流里的 tool_result 有没有真的走到报告组件、日期字段的
 * 答案以什么形状离开页面、以及**同一轮里已经发出去**的报告还会不会再问一次。
 *
 * 假服务器与 `App.test.ts` 同源，但只留这三条路要用的那几个路由：**不能 import 它** ——
 * 那会把那边的整个套件在本文件里再跑一遍。
 */

import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AnalysisResult from './AnalysisResult.vue'
import App from './App.vue'
import { resetCache } from './conversationCache'
import { resetSettledReports } from './reportStore'
import { resetSessions } from './sessions'
import type { Conversation, ConversationDetail, ConversationMessage } from './types'

// --- 一个很小的假服务器 -------------------------------------------------------------------

interface Recorded {
  method: string
  url: string
  key: string | null
  body: unknown
}

interface FakeServer {
  conversations: Conversation[]
  messages: Record<string, ConversationMessage[]>
  details: Record<string, ConversationDetail>
  /** 一次提交流回来的帧；某个会话没有这一项时，服务器答的是「重复请求」的收据。 */
  streams: Record<string, Array<[string, unknown]>>
  requests: Recorded[]
}

const USER_ID = 'user_0001'

let server: FakeServer

function conversation(overrides: Partial<Conversation> & { conversation_id: string }): Conversation {
  return {
    title: overrides.conversation_id,
    pinned: false,
    archived: false,
    deleted: false,
    created_at: '2026-09-28T00:00:00Z',
    updated_at: '2026-09-28T00:00:00Z',
    last_message_at: '2026-09-28T00:00:00Z',
    message_count: 0,
    serial_no: 1,
    ...overrides,
  }
}

function message(
  overrides: Partial<ConversationMessage> & { message_id: string; sequence: number },
): ConversationMessage {
  return {
    role: 'user',
    content: '',
    message_kind: 'message',
    question_id: null,
    run_id: null,
    created_at: '2026-09-28T00:00:00Z',
    metadata: {},
    ...overrides,
  }
}

function envelope<T>(data: T): string {
  return JSON.stringify({ request_id: 'req_test', data, error: null })
}

function json(body: string, status = 200): Response {
  return new Response(body, { status, headers: { 'Content-Type': 'application/json' } })
}

function sse(frames: Array<[string, unknown]>): Response {
  const text = frames
    .map(([name, data]) => `event: ${name}\ndata: ${envelope(data)}\n\n`)
    .join('')
  return new Response(text, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function installFetch(): void {
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    const method = (init.method ?? 'GET').toUpperCase()
    const headers = (init.headers ?? {}) as Record<string, string>
    server.requests.push({
      method,
      url,
      key: headers['Idempotency-Key'] ?? null,
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    })

    if (url.endsWith('/api/health')) {
      return json(envelope({ status: 'ok', model_configured: true, demo_mode: true }))
    }
    if (url.endsWith('/api/session')) {
      return json(envelope({ session_id: 'sess_1', user_id: USER_ID, login_name: 'demo' }))
    }
    // 页面启动时先问「我是谁」，这一条不答，整个工作台都不渲染，下面所有用例都会挂在
    // 「找不到 .composer / .interrupt-date」上 —— 那是登录门，不是账单逻辑坏了。
    if (url.endsWith('/api/auth/me')) {
      return json(envelope({
        // 管理员：时钟控制条只对管理员开放，而演示条挂载时会读一次时钟。
        user_id: USER_ID, login_name: 'demo', role: 'admin', email: null, bank_ready: true,
      }))
    }
    // 没绑邮箱：弹窗里因此多一个收件地址输入框，但它弹不弹跟这件事无关。
    if (url.endsWith('/api/settings/email')) {
      return json(envelope({ email: null }))
    }
    // demo_mode 为真，演示条挂载时读一次时钟。
    if (url.endsWith('/api/demo/clock')) {
      return json(envelope({ current_time: '2026-10-22T09:00:00+08:00', run_id: '7', due_task_count: 2 }))
    }

    // 续费提醒条在登录后自己读一次；这三套工作台用例只关心讨论，给它一个空架子。
    if (url.endsWith('/api/reminders/subscriptions')) {
      return json(envelope({ items: [], count: 0, total_minor: 0, horizon_days: 14 }))
    }
    if (!url.includes('/api/conversations')) {
      throw new Error(`unstubbed request: ${method} ${url}`)
    }
    const match = /\/api\/conversations\/([^/?]+)(\/[a-z]+)?(\?.*)?$/.exec(url)
    if (!match) return json(envelope({ items: server.conversations, next_cursor: null }))
    const id = decodeURIComponent(match[1])
    const action = match[2]
    const query = match[3] ?? ''

    if (action === '/messages') {
      if (method === 'POST') {
        const detail = server.details[id] ?? (server.details[id] = {
          conversation: conversation({ conversation_id: id }),
          pending_interrupt: null,
          recovery_required: false,
        })
        // A new submission consumes the open question; a stream may then open a new one.
        detail.pending_interrupt = null
        const frames = server.streams[id]
        if (!frames) {
          // 一次没有帧可放的提交：服务器答的是收据，而不是一条流。
          return json(envelope({
            submission_id: 'sub_1', message_id: 'msg_receipt', run_id: null, duplicate: true,
          }))
        }
        const interrupt = frames.find(([name]) => name === 'interrupt')
        if (interrupt) detail.pending_interrupt = interrupt[1] as Record<string, unknown>
        // 真提交真落库：账本里留下用户那句话，答完了再留下回答。页面随后会重读账本，
        // 一个不记账的桩模型的是一个会忘事的服务器。
        const ledger = server.messages[id] ?? (server.messages[id] = [])
        ledger.push(message({
          message_id: `msg_${ledger.length + 1}`,
          sequence: ledger.length + 1,
          content: (JSON.parse(String(init.body)) as { message: string }).message,
        }))
        const final = frames.find(([name]) => name === 'final')
        if (final) {
          ledger.push(message({
            message_id: `msg_${ledger.length + 1}`,
            sequence: ledger.length + 1,
            role: 'assistant',
            content: String((final[1] as { text: string }).text),
          }))
        }
        return sse(frames)
      }
      if (query.includes('after=')) return json(envelope({ items: [], next_cursor: null }))
      return json(envelope({ items: server.messages[id] ?? [], next_cursor: null }))
    }

    return json(envelope(server.details[id] ?? {
      conversation: conversation({ conversation_id: id }),
      pending_interrupt: null,
      recovery_required: false,
    }))
  })
}

// --- 三件事要用的素材 ---------------------------------------------------------------------

/**
 * 一份工具结果里的报告展示。
 *
 * `modules` 是空的：这份报告渲染出来的是报告头（期间、交易数、生成时间），
 * 而那些图表模块一个都不挂 —— 这个文件要证明的是「报告被当成报告渲染了」，
 * 不是 ECharts 会不会画图。
 */
function reportDisplay() {
  return {
    report_meta: {
      date_range: { start: '2026-08-01', end: '2026-08-31' },
      total_transactions: 128,
      accounts: [],
      generated_at: '2026-09-01T10:00:00Z',
    },
    modules: [],
    text_report: '2026 年 8 月流水分析报告',
  }
}

function toolResult(overrides: {
  tool_name: string
  display?: unknown
  summary?: string | null
  error?: string | null
}) {
  return {
    agent_id: 'bill',
    tool_name: overrides.tool_name,
    call_id: 'call_1',
    status: overrides.error ? 'error' : 'ok',
    data: {
      summary: overrides.summary ?? null,
      display: overrides.display ?? null,
      business_ids: null,
    },
    error: overrides.error ?? null,
    error_code: null,
  }
}

/** 账单追问里那个要日期的字段。 */
function dateCard(questionId = 'q_date') {
  return {
    kind: 'input',
    question_id: questionId,
    agent_id: 'bill',
    fields: [{ name: 'period_start', label: '开始日期', type: 'date', required: true, options: [] }],
    display: '请提供要分析的周期',
    attempt: 1,
  }
}

async function open(): Promise<VueWrapper> {
  const wrapper = mount(App)
  await flushPromises()
  await flushPromises()
  return wrapper
}

/** 从输入框发一条消息，等这一轮跑完。 */
async function submit(wrapper: VueWrapper, text: string): Promise<void> {
  await wrapper.find('.composer textarea').setValue(text)
  await wrapper.find('.composer').trigger('submit')
  await flushPromises()
  await flushPromises()
  await flushPromises()
}

beforeEach(() => {
  localStorage.clear()
  resetCache()
  resetSettledReports()
  server = {
    conversations: [conversation({ conversation_id: 'conv_a', title: '甲的会话' })],
    messages: {
      conv_a: [
        message({ message_id: 'msg_1', sequence: 1, content: '甲的问题' }),
        message({ message_id: 'msg_2', sequence: 2, role: 'assistant', content: '甲的回答' }),
      ],
    },
    details: {
      conv_a: {
        conversation: conversation({ conversation_id: 'conv_a', title: '甲的会话' }),
        pending_interrupt: null,
        recovery_required: false,
      },
    },
    streams: {},
    requests: [],
  }
  installFetch()
  Element.prototype.scrollTo = () => {}
  resetSessions()
})

enableAutoUnmount(afterEach)

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('the bill shell', () => {
  it('renders the analysis report when a tool result carries a report display', async () => {
    // 只拨 `setTimeout`：`flushPromises` 走的是 @vue/test-utils 在 import 时抓住的
    // 真 `setImmediate`，所以这里 await 的每一个 flush 都不会等在一个没人推的钟上。
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
    ]
    const wrapper = await open()
    await submit(wrapper, '帮我分析八月的流水')

    // 报告本身在屏幕上，而不是那段 JSON 被原样抄进活动流里。
    const rendered = wrapper.find('.activity-result .report')
    expect(rendered.exists()).toBe(true)
    expect(rendered.text()).toContain('账户流水深度分析')
    expect(rendered.text()).toContain('2026-08-01 ~ 2026-08-31')
    expect(rendered.text()).toContain('128')
    expect(rendered.text()).not.toContain('report_meta')
    expect(rendered.text()).not.toContain('text_report')

    // 发送入口在报告上：不再自动弹窗，用户点按钮才进入发送流程。
    const sendButton = rendered.find('.report-send-btn')
    expect(sendButton.exists()).toBe(true)
    expect(sendButton.text()).toContain('发送到邮箱')
    expect(sendButton.attributes('disabled')).toBeUndefined()
  })

  it('renders a year-month-day picker for a date field and answers with a composed date', async () => {
    server.details.conv_a.pending_interrupt = dateCard()
    const wrapper = await open()

    const selects = wrapper.findAll('.interrupt-date select')
    expect(selects).toHaveLength(3)

    await selects[0].setValue('2026')
    await selects[1].setValue('9')
    await selects[2].setValue('1')
    await flushPromises()

    // 三个框是受控的：选完显示的就是选的那个，日的选项跟着月份走（9 月 30 天）。
    expect((selects[0].element as HTMLSelectElement).value).toBe('2026')
    expect((selects[1].element as HTMLSelectElement).value).toBe('9')
    expect((selects[2].element as HTMLSelectElement).value).toBe('1')
    expect(wrapper.findAll('.interrupt-date select')[2].findAll('option')).toHaveLength(30)

    await wrapper.find('.interrupt-card').trigger('submit')
    await flushPromises()
    await flushPromises()

    const posted = server.requests.filter(
      item => item.method === 'POST' && item.url === '/api/conversations/conv_a/messages',
    )
    expect(posted).toHaveLength(1)
    const body = posted[0].body as {
      client_context?: { resume?: { question_id?: string; value?: Record<string, unknown> } }
    }
    expect(body.client_context?.resume?.question_id).toBe('q_date')
    // 拼出来的是工具认得的写法（后端 app/modules/dates.py 的 _FULL_CN 认这一种），
    // 不是 ISO 的 `2026-09-01`。
    expect(body.client_context?.resume?.value?.period_start).toBe('2026年9月1日')
  })

  it('does not render a report for a turn that only sends it', async () => {
    // 用户要的是「把报告发我邮箱」，不是「再给我看一遍分析」。前端由发送弹窗发出的
    // 那句话带「只发送，无需再展示报告」；模型即便仍然调了分析，页面也不铺报告 ——
    // 否则确认之后收件人会在同一轮里看到第二份一模一样的报告。
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['tool_result', toolResult({ tool_name: 'bill_send_report', summary: '等待确认后发送' })],
    ]
    const wrapper = await open()
    await submit(
      wrapper,
      '请把 2026-08-01 到 2026-08-31 的账单分析报告发送到我的邮箱（只发送，无需再展示报告）',
    )

    expect(wrapper.find('.activity-result .report').exists()).toBe(false)
  })

  it('still renders the report when the user asked to analyse before sending', async () => {
    // 对照组：句子里有真正的分析意图，报告照常出现 —— 上面的规则不能把这一类也吞掉。
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['tool_result', toolResult({ tool_name: 'bill_send_report', summary: '等待确认后发送' })],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析一下八月的流水，然后发送到我的邮箱')

    expect(wrapper.find('.activity-result .report').exists()).toBe(true)
  })

  it('does not offer the send prompt again once the report was sent in this turn', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    // 同一轮里两件事都发生了：分析报告出来了，**确认之后真的执行了**那次发送。
    // 流不带 final，所以这一轮停在台上（live 块留着），但已经不再 running。
    //
    // fixture 写的是 **execute** 工具：只有确认路径会调它，所以它出现在这一轮里，
    // 就意味着用户已经点过确认（准备工具 `bill_send_report` 成功时用户还没点）。
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['tool_result', toolResult({ tool_name: 'bill_send_report_execute', summary: '报告已发到绑定邮箱' })],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析八月流水')

    // 报告还在：发出去过不等于不用再看。
    expect(wrapper.find('.activity-result .report').exists()).toBe(true)
    // 依据就是这一条 —— 页面确实从这一轮里认出了那次**执行**。
    // 页面要是退回匹配准备工具 `bill_send_report`，这里会变成 false。
    expect(wrapper.findComponent(AnalysisResult).props('reportSent')).toBe(true)

    // 该弹的时间点已经过去了（对照组在同一份报告上会弹）。
    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)
  })

  it('sends from the report button and asks for an address only when none is bound', async () => {
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析八月流水')

    // 没有自动弹窗；报告头和报告末尾各有一个入口，点哪个都一样。
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)
    const buttons = wrapper.findAll('.report-send-btn')
    expect(buttons).toHaveLength(2)
    await buttons[buttons.length - 1].trigger('click')

    // 这个夹具没绑邮箱，所以先收地址。
    expect(wrapper.find('.modal-overlay').exists()).toBe(true)
    await wrapper.find('.modal-overlay input').setValue('someone@example.com')
    await wrapper.find('.modal-overlay .btn-primary').trigger('click')
    await flushPromises()

    const posted = server.requests.filter(
      item => item.method === 'POST' && item.url === '/api/conversations/conv_a/messages',
    )
    expect(posted.length).toBeGreaterThan(0)
    const body = posted[posted.length - 1].body as { message: string }
    expect(body.message).toContain('发送到我的邮箱')
    expect(body.message).toContain('只发送')
  })

  it('does not mark the turn sent when only the prepare tool succeeded', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    // 只有**准备**工具成功：报告生成了、确认卡片也弹过了，但用户还没确认，信没发出去。
    // 这一轮要是被标成「已发送」，用户一取消弹窗就再也不会自动出现 —— 报告就烂在
    // 这一轮里，只能靠手动重开。
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['tool_result', toolResult({ tool_name: 'bill_send_report', summary: '等待确认后发送' })],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析八月流水')

    expect(wrapper.find('.activity-result .report').exists()).toBe(true)
    // 没执行过，就不算发过。
    expect(wrapper.findComponent(AnalysisResult).props('reportSent')).toBe(false)

    // 所以发送按钮仍然可用，不显示「已发送」。
    const button = wrapper.find('.report-send-btn')
    expect(button.exists()).toBe(true)
    expect(button.text()).toContain('发送到邮箱')
    expect(button.attributes('disabled')).toBeUndefined()
  })

  it('still offers the send prompt when a settled turn only prepared the report', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    // 这一轮**真的跑完了**：流里有 final，所以收尾时 live 块被丢掉，报告改由
    // 结算后的副本继续渲染（见 App.vue 的 `settledReports`）。而这一轮里只出现过
    // **准备**工具 `bill_send_report` —— 用户还没确认，信没发出去。
    //
    // 上面两条用例走的都是「没 final、停在台上」的 live 路径；这一条盯的是**结算**那一次
    // `keepSettledReports`：它要是拿准备工具当「已发送」，就会把这份报告记进
    // `promptedSendReports`，结算副本于是永远不再问「要不要发到邮箱」。
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['tool_result', toolResult({ tool_name: 'bill_send_report', summary: '等待确认后发送' })],
      ['final', { text: '报告已生成，等待您确认发送。', business_ids: [] }],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析八月流水')
    await flushPromises()

    // 确实结算了：台上的 live 块没了，报告由结算后的副本渲染 —— 副本也带着报告本身。
    expect(wrapper.find('.live-turn').exists()).toBe(false)
    const settled = wrapper.find('.settled-reports .activity-result .report')
    expect(settled.exists()).toBe(true)
    expect(settled.text()).toContain('账户流水深度分析')
    expect(settled.text()).toContain('2026-08-01 ~ 2026-08-31')

    // 台下只剩结算副本这一个报告组件 —— 它根本**不接** `report-sent`（那是 live 那份
    // 按本轮执行结果算的，见 App.vue:1627）；结算副本唯一会「不问」的理由是父组件那本
    // 「这份报告问过没有」的账。所以能证明 `reportSent` 为 false 的，就是下面那声弹窗。
    expect(wrapper.findAllComponents(AnalysisResult)).toHaveLength(1)
    expect(wrapper.findComponent(AnalysisResult).props('reportSent')).toBeFalsy()

    // 于是结算副本上的发送按钮照常可用。
    const settledButton = settled.find('.report-send-btn')
    expect(settledButton.exists()).toBe(true)
    expect(settledButton.attributes('disabled')).toBeUndefined()
  })

  it('keeps a settled report inside the turn that produced it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['final', { text: '报告已生成。', business_ids: [] }],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析八月流水')
    await flushPromises()

    // 账本里第一轮是 fixture，第二轮才是这次分析；报告挂在第二轮下面。
    let turns = wrapper.findAll('.chat-turn')
    expect(turns).toHaveLength(2)
    expect(turns[1].find('.settled-reports .activity-result .report').exists()).toBe(true)
    // 本机已有报告：直接显示完整报告，不再出现文件占位卡。
    expect(turns[1].find('.report-file').exists()).toBe(false)

    // 再发一条普通消息：报告必须留在自己那一轮，而不是被挤到页面最底部。
    server.streams.conv_a = [['final', { text: '好的。', business_ids: [] }]]
    await submit(wrapper, '再帮我看看别的')
    await flushPromises()

    turns = wrapper.findAll('.chat-turn')
    expect(turns).toHaveLength(3)
    expect(turns[1].find('.settled-reports .activity-result .report').exists()).toBe(true)
    expect(turns[2].find('.settled-reports').exists()).toBe(false)
  })

  it('shows a file placeholder for a server report, then loads it on click', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    // 另一个设备：本机没有任何缓存，报告只随助手消息的 metadata 从服务端下来。
    server.messages.conv_a = [
      message({ message_id: 'msg_1', sequence: 1, content: '分析八月流水' }),
      message({
        message_id: 'msg_2',
        sequence: 2,
        role: 'assistant',
        content: '报告已生成。',
        metadata: { analysis_reports: [reportDisplay()] },
      }),
    ]
    const wrapper = await open()
    await flushPromises()

    // 未加载时只有文件占位卡，不直接渲染完整报告。
    expect(wrapper.find('.report-file').exists()).toBe(true)
    expect(wrapper.find('.report-file').text()).toContain('账单深度分析报告')
    expect(wrapper.find('.report-file').text()).toContain('2026-08-01 ~ 2026-08-31')
    expect(wrapper.find('.activity-result .report').exists()).toBe(false)

    // 右下角的按钮把报告载入本机：占位卡换成完整报告。
    await wrapper.find('.report-file .file-download').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.report-file').exists()).toBe(false)
    const report = wrapper.find('.activity-result .report')
    expect(report.exists()).toBe(true)
    expect(report.text()).toContain('账户流水深度分析')
    expect(Object.keys(localStorage).some(key => key.includes('settled_reports'))).toBe(true)

    // 从云端载入的是历史报告，不该再自动弹「发到邮箱」。
    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)
  })

  it('restores a settled report after a page reload without asking again', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['final', { text: '报告已生成。', business_ids: [] }],
    ]
    const first = await open()
    await submit(first, '分析八月流水')
    await flushPromises()
    expect(first.find('.settled-reports .activity-result .report').exists()).toBe(true)
    first.unmount()

    // 模拟刷新：内存里的会话、缓存与报告视图清空，只有 localStorage 和服务器账本还在。
    resetSessions()
    resetCache()
    resetSettledReports()
    const second = await open()
    await flushPromises()

    const settled = second.find('.chat-turn .settled-reports .activity-result .report')
    expect(settled.exists()).toBe(true)
    expect(settled.text()).toContain('账户流水深度分析')
    expect(settled.text()).toContain('2026-08-01 ~ 2026-08-31')
    expect(second.findAllComponents(AnalysisResult)).toHaveLength(1)

    // 恢复出来的报告已经问过「要不要发到邮箱」，刷新后不该再弹一次。
    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(second.find('.modal-overlay').exists()).toBe(false)
  })

  it('renders a query result as a table and keeps it after the turn settles', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    const display = {
      total: 2,
      page: 1,
      page_size: 20,
      period_start: '2026-05-05',
      period_end: '2026-05-06',
      items: [
        {
          booked_date: '2026-05-05',
          amount_minor: 850000,
          direction: 'credit',
          transaction_type: '工资代发',
          counterparty: '深圳市星辰科技有限公司',
          category: '工资',
        },
        {
          booked_date: '2026-05-06',
          amount_minor: 3250,
          direction: 'debit',
          transaction_type: '消费-餐饮',
          counterparty: '美团点评-肠粉王店',
          category: '餐饮',
        },
      ],
    }
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_query_transactions', display })],
      ['final', { text: '查询完成。', business_ids: [] }],
    ]

    const wrapper = await open()
    await submit(wrapper, '查一下这段时间的流水明细')
    await flushPromises()

    // 回合结算后 live 块被丢掉，但查询表格应保留在所属回合下面。
    expect(wrapper.find('.live-turn').exists()).toBe(false)
    const table = wrapper.find('.settled-queries .txn-table')
    expect(table.exists()).toBe(true)
    expect(table.findAll('tbody tr')).toHaveLength(2)
    expect(table.text()).toContain('2026-05-05')
    expect(table.text()).toContain('深圳市星辰科技有限公司')
  })

  it('restores a persisted query page from the assistant message metadata', async () => {
    const display = {
      total: 25,
      page: 1,
      page_size: 20,
      period_start: '2026-05-05',
      period_end: '2026-05-06',
      items: [
        {
          booked_date: '2026-05-05',
          amount_minor: 850000,
          direction: 'credit',
          transaction_type: '工资代发',
          counterparty: '深圳市星辰科技有限公司',
          category: '工资',
        },
      ],
    }
    server.messages.conv_a = [
      message({ message_id: 'msg_1', sequence: 1, content: '查一下明细' }),
      message({
        message_id: 'msg_2',
        sequence: 2,
        role: 'assistant',
        content: '查询完成。',
        metadata: { query_results: [display] },
      }),
    ]

    const wrapper = await open()
    await flushPromises()

    const table = wrapper.find('.server-queries .txn-table')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('已保留当前页 1 条')
    expect(table.text()).toContain('刷新后仅保留当前页')
    expect(table.text()).toContain('查看完整明细请再次查询')
  })

  it('does not re-prompt for email after the send executes in a later turn', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    // 第一回合：分析出报告，然后停在待答卡片上 —— 此时报告不能弹“发邮箱”。
    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['interrupt', dateCard()],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析八月流水')
    await flushPromises()
    expect(wrapper.find('.live-turn').exists()).toBe(false)
    expect(wrapper.find('.interrupt-card').exists()).toBe(true)

    // 第二回合：用户确认后，代码执行发送。
    const selects = wrapper.findAll('.interrupt-date select')
    await selects[0].setValue('2026')
    await selects[1].setValue('8')
    await selects[2].setValue('1')
    server.streams.conv_a = [
      ['tool_result', toolResult({
        tool_name: 'bill_send_report_execute',
        display: { period: '2026-08-01 ~ 2026-08-31', recipient: '3118400178@qq.com', format: 'PDF' },
      })],
      ['final', { text: '报告已发送。', business_ids: [] }],
    ]
    await wrapper.find('.interrupt-card').trigger('submit')
    await flushPromises()
    await flushPromises()
    await flushPromises()

    // 发送成功后，同期间的分析报告已经问过/发过，不该再自动弹邮箱确认。
    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)
  })

  it('does not auto-prompt when the user already asked to send to email', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['final', { text: '报告已生成。', business_ids: [] }],
    ]
    const wrapper = await open()
    await submit(wrapper, '帮我分析八月流水并发送到邮箱')
    await flushPromises()

    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)
  })

  it('cancels a scheduled send prompt when the send happens in a later turn', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_analyze_comprehensive', display: reportDisplay() })],
      ['final', { text: '报告已生成。', business_ids: [] }],
    ]
    const wrapper = await open()
    await submit(wrapper, '分析八月流水')
    await flushPromises()

    server.streams.conv_a = [
      ['tool_result', toolResult({
        tool_name: 'bill_send_report_execute',
        display: { period: '2026-08-01 ~ 2026-08-31', recipient: '3118400178@qq.com', format: 'PDF' },
      })],
      ['final', { text: '报告已发送。', business_ids: [] }],
    ]
    await submit(wrapper, '把报告发到我的邮箱')
    await flushPromises()

    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)
  })

  it('offers to analyse the period a query covered', async () => {
    const item = (index: number) => ({
      booked_date: '2026-05-01',
      amount_minor: 100 + index,
      direction: 'debit',
      transaction_type: '商户消费',
      counterparty: `商户${index}`,
      category: '商户消费',
    })
    server.streams.conv_a = [
      ['tool_result', toolResult({
        tool_name: 'bill_query_transactions',
        display: {
          total: 2,
          page: 1,
          page_size: 20,
          period_start: '2026-05-01',
          period_end: '2026-05-31',
          task_id: null,
          items: [item(0), item(1)],
        },
      })],
      ['final', { text: '查询完成。', business_ids: [] }],
    ]
    const wrapper = await open()
    await submit(wrapper, '查询五月流水')

    // 按日期查的表下面也有「要不要分析」这一行，而且在同一个竖排容器里 ——
    // 挂在横排的 activity-item 上它会跑到表格右边去。
    const prompt = wrapper.find('.settled-queries .activity-result .bill-analysis-prompt')
    expect(prompt.exists()).toBe(true)
    expect(prompt.text()).toContain('2026-05-01 ~ 2026-05-31')
    expect(prompt.element.parentElement?.querySelector('.txn-table')).not.toBeNull()

    await prompt.find('.primary').trigger('click')
    await flushPromises()

    const posted = server.requests.filter(
      entry => entry.method === 'POST' && entry.url === '/api/conversations/conv_a/messages',
    )
    const body = posted[posted.length - 1].body as { message: string }
    expect(body.message).toBe('分析 2026-05-01 到 2026-05-31 的流水')
  })

  it('offers the analysis row under every query of the same period, not just the first', async () => {
    const item = (index: number) => ({
      booked_date: '2026-05-01',
      amount_minor: 100 + index,
      direction: 'debit',
      transaction_type: '商户消费',
      counterparty: `商户${index}`,
      category: '商户消费',
    })
    const display = {
      total: 2,
      page: 1,
      page_size: 20,
      period_start: '2026-05-01',
      period_end: '2026-05-31',
      task_id: null,
      items: [item(0), item(1)],
    }
    /** 同一个期间的又一次查询：内容一样，但那是另一次工具调用。 */
    const queryTurn = (callId: string): Array<[string, unknown]> => [
      ['tool_result', { ...toolResult({ tool_name: 'bill_query_transactions', display }), call_id: callId }],
      ['final', { text: '查询完成。', business_ids: [] }],
    ]

    server.streams.conv_a = queryTurn('call_a')
    const wrapper = await open()
    await submit(wrapper, '查询五月流水')
    expect(wrapper.findAll('.bill-analysis-prompt')).toHaveLength(1)

    // 查两次同一个期间：两张表、两行选择。第二张表不能因为「内容一样」被当成重复丢掉，
    // 它下面那行「要分析吗」也不能因为第一份点过「先不用」就跟着消失。
    await wrapper.find('.bill-analysis-prompt .secondary').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.bill-analysis-prompt')).toHaveLength(0)

    server.streams.conv_a = queryTurn('call_b')
    await submit(wrapper, '再查一次五月流水')

    expect(wrapper.findAll('.txn-table')).toHaveLength(2)
    const prompts = wrapper.findAll('.bill-analysis-prompt')
    expect(prompts).toHaveLength(1)
    expect(prompts[0].text()).toContain('2026-05-01 ~ 2026-05-31')
  })

  it('shows one paginated query table when the model repeats the same page', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    const item = (index: number) => ({
      booked_date: '2026-10-01',
      amount_minor: 100 + index,
      direction: 'debit',
      transaction_type: '商户消费',
      counterparty: `商户${index}`,
      category: '商户消费',
    })
    const page = (pageSize: number, count: number) => ({
      total: 50,
      page: 1,
      page_size: pageSize,
      period_start: '2026-10-01',
      period_end: '2026-10-31',
      items: Array.from({ length: count }, (_, index) => item(index)),
    })

    server.streams.conv_a = [
      ['tool_result', toolResult({ tool_name: 'bill_query_transactions', display: page(100, 50) })],
      ['tool_result', toolResult({ tool_name: 'bill_query_transactions', display: page(20, 20) })],
      ['final', { text: '查询完成。', business_ids: [] }],
    ]
    const wrapper = await open()
    await submit(wrapper, '查询十月流水')
    await flushPromises()

    // 同一期间、同一页、同一总数算一份结果；第二次调用替换第一次，不并排两张表。
    expect(wrapper.findAll('.txn-table')).toHaveLength(1)
    expect(wrapper.find('.txn-pager').exists()).toBe(true)
    expect(wrapper.text()).toContain('第 1 / 3 页')
  })
})
