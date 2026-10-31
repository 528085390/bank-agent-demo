/**
 * The conversation view, mounted.
 *
 * `conversations.test.ts` covers the pure half -- the stored key, the ledger readers, the
 * filters. What can only be checked with the component on screen is the *view state*: that
 * switching conversations carries nothing across, that a question the server confirms is
 * actually rendered and actually locks the composer, and that the two ledger reads (a page
 * and the earlier page) merge instead of duplicating.
 *
 * The server is a stub that routes by URL, so the requests the page makes are observable and
 * the answers are exactly the ones the real endpoints return.
 */

import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import { resetCache } from './conversationCache'
import { loadPending, savePending, PENDING_SUBMISSION_KEY } from './conversations'
import { demoStore } from './demoStore'
import { resetIdentity } from './identity'
import { resetSessions } from './sessions'
import { resetStorageScope, setStorageOwner } from './storageScope'
import type { Conversation, ConversationDetail, ConversationMessage, TaskNode } from './types'
import type { SubscriptionReminder } from './api'

// --- a tiny fake server -------------------------------------------------------------------

interface Recorded {
  method: string
  url: string
  key: string | null
  body: unknown
}

const USER_ID = 'user_0001'

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

interface FakeServer {
  /** Whether this browser already holds a demo session; false is a browser that has never entered. */
  hasSession: boolean
  /** Whether /health reports demo mode; false is a session where the demo bar never mounts. */
  demoMode: boolean
  conversations: Conversation[]
  messages: Record<string, ConversationMessage[]>
  details: Record<string, ConversationDetail>
  /** Pages older than the first one, keyed by conversation and the `after` cursor. */
  earlier: Record<string, ConversationMessage[]>
  /** Frames a first submission streams back, keyed by conversation. Absent means a receipt. */
  streams: Record<string, Array<[string, unknown]>>
  /** Frames a submission streams and then leaves the response open on, keyed by conversation. */
  openStreams: Record<string, Array<[string, unknown]>>
  /** Frames a button event streams back, keyed by conversation. Absent means a completed turn. */
  decisions: Record<string, Array<[string, unknown]>>
  /** Overrides for one request, so a failure path can be driven deliberately. */
  failures: Record<string, { status: number; code: string; message: string }>
  /** 服务端说「我是谁」时用的用户号；不填就是本文件默认的那一个。 */
  meUserId?: string
  /** 同上，角色；默认管理员，因为多数用例要摸全局控制面。 */
  meRole?: string
  /** 让 `GET /api/auth/me` 以这个状态失败，用来驱动「复核时后端不可达」。 */
  meStatus?: number
  /** 顶栏续费提醒在挂载时自己读一次；默认没有。 */
  reminders?: SubscriptionReminder[]
  requests: Recorded[]
}

let server: FakeServer

function envelope<T>(data: T): string {
  return JSON.stringify({ request_id: 'req_test', data, error: null })
}

function errorEnvelope(code: string, message: string): string {
  return JSON.stringify({
    request_id: 'req_test',
    data: null,
    error: { code, message, retryable: false, details: {} },
  })
}

function json(body: string, status = 200): Response {
  return new Response(body, { status, headers: { 'Content-Type': 'application/json' } })
}

function sse(frames: Array<[string, unknown]>): Response {
  const text = frames
    .map(([name, data]) => `event: ${name}\ndata: ${envelope(data)}\n\n`)
    .join('')
  return new Response(text, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

/**
 * An SSE response that delivers its frames and then stays open.
 *
 * The page clears the in-flight turn the moment the stream ends, so a test that wants to see
 * what is rendered *while* the turn is running has to keep the response open -- which is what
 * a real stream does until the turn is over.
 */
function openStream(frames: Array<[string, unknown]>, signal?: AbortSignal): Response {
  const text = frames
    .map(([name, data]) => `event: ${name}\ndata: ${envelope(data)}\n\n`)
    .join('')
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text))
      if (signal?.aborted) controller.error(new DOMException('Aborted', 'AbortError'))
      else signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')), { once: true })
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
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

    for (const [needle, failure] of Object.entries(server.failures)) {
      if (url.includes(needle) && method !== 'GET') {
        return json(errorEnvelope(failure.code, failure.message), failure.status)
      }
    }

    if (url.endsWith('/api/health')) {
      return json(envelope({ status: 'ok', model_configured: true, demo_mode: server.demoMode }))
    }
    // demo_mode is on, so the bar mounts and reads the clock once -- before anyone opens it.
    if (url.endsWith('/api/demo/clock')) {
      return json(envelope({ current_time: '2026-10-22T09:00:00+08:00', run_id: '7', due_task_count: 2 }))
    }
    // 登录态只有服务端说了算：页面启动时读 `/api/auth/me`，401 就是「还没登录」。
    if (url.endsWith('/api/auth/me')) {
      // 读身份这一条是 GET，`failures` 只拦非 GET，所以它有一个自己的失败开关。
      if (server.meStatus) return json(errorEnvelope('INTERNAL', '服务暂时不可用'), server.meStatus)
      if (!server.hasSession) {
        return json(errorEnvelope('SESSION_REQUIRED', '登录已失效，请重新登录'), 401)
      }
      return json(envelope({
        // 管理员：时钟控制条与模型设置都只对管理员开放，这一套测试关心的是它们的行为。
        user_id: server.meUserId ?? USER_ID, login_name: 'demo',
        role: server.meRole ?? 'admin', email: null, bank_ready: true,
      }))
    }
    if (url.endsWith('/api/auth/login') && method === 'POST') {
      server.hasSession = true
      return json(envelope({
        csrf_token: 'csrf_1',
        user: { user_id: USER_ID, login_name: 'demo', role: 'admin' },
      }))
    }
    if (url.endsWith('/api/auth/logout') && method === 'POST') {
      server.hasSession = false
      return json(envelope({ logged_out: true }))
    }
    // 改密与「退出全部设备」都会撤销服务端的全部登录态，包括这一台 —— 桩也照这个语义答。
    if (url.endsWith('/api/auth/password/change') && method === 'POST') {
      server.hasSession = false
      return json(envelope({ changed: true }))
    }
    if (url.endsWith('/api/auth/logout-all') && method === 'POST') {
      server.hasSession = false
      return json(envelope({ revoked: 2 }))
    }
    // 账号设置区要显示的两块：收件邮箱（每个人自己的）与模型配置（只有管理员读）。
    if (url.endsWith('/api/settings/email')) {
      return json(envelope({ email: null }))
    }
    if (url.endsWith('/api/settings/llm')) {
      return json(envelope({
        base_url: 'https://example.com/v1', model: 'demo-model', configured: true,
        api_key_masked: 'sk-***', mode: 'model',
      }))
    }
    // 续费提醒条在登录后自己读一次；默认空架子，需要时由用例填 server.reminders。
    if (url.endsWith('/api/reminders/subscriptions')) {
      const items = server.reminders ?? []
      return json(envelope({
        items, count: items.length,
        total_minor: items.reduce((sum, item) => sum + item.amount_minor, 0), horizon_days: 14,
      }))
    }
    if (url.endsWith('/api/session')) {
      return json(envelope({ session_id: 'sess_1', user_id: USER_ID, login_name: 'demo' }))
    }
    if (url.includes('/pending-actions/') && url.endsWith('/decide')) {
      // The button route resumes the paused turn, so it really persists what it produced:
      // the answer joins the ledger and so does the reply.
      const match = /\/api\/conversations\/([^/]+)\/pending-actions\/([^/]+)\/decide$/.exec(url)
      const id = decodeURIComponent(match?.[1] ?? '')
      const body = JSON.parse(String(init.body)) as { decision: string }
      const frames = server.decisions[id]
        ?? [['final', { text: '已按您的确认提交。', business_ids: [] }]] as Array<[string, unknown]>
      const ledger = server.messages[id] ?? (server.messages[id] = [])
      ledger.push(
        message({
          message_id: `msg_${ledger.length + 1}`,
          sequence: ledger.length + 1,
          content: body.decision === 'confirm' ? '确认本次操作' : '取消本次操作',
          message_kind: 'interrupt_answer',
        }),
      )
      const final = frames.find(([name]) => name === 'final')
      if (final) {
        ledger.push(
          message({
            message_id: `msg_${ledger.length + 1}`,
            sequence: ledger.length + 1,
            role: 'assistant',
            content: String((final[1] as { text: string }).text),
          }),
        )
      }
      // The card is consumed by the click -- unless the turn re-pauses on it (a rejected code
      // re-offers the same pending), in which case the box the server confirms is that one.
      const reopened = frames.find(([name]) => name === 'interrupt')?.[1]
      if (server.details[id]) {
        server.details[id].pending_interrupt = reopened
          ? (reopened as Record<string, unknown>)
          : null
      }
      return sse(frames)
    }
    if (url.includes('/api/conversations')) {
      const match = /\/api\/conversations\/([^/?]+)(\/[a-z]+)?(\?.*)?$/.exec(url)
      if (!match) {
        if (method === 'POST') {
          const created = conversation({ conversation_id: `conv_new_${server.conversations.length}` })
          server.conversations.push(created)
          return json(envelope(created))
        }
        return json(envelope({ items: server.conversations, next_cursor: null }))
      }
      const id = decodeURIComponent(match[1])
      const action = match[2]
      const query = match[3] ?? ''
      if (action === '/messages') {
        if (method === 'POST') {
          const open = server.openStreams[id]
          if (open) {
            const ledger = server.messages[id] ?? (server.messages[id] = [])
            ledger.push(
              message({
                message_id: `msg_${ledger.length + 1}`,
                sequence: ledger.length + 1,
                content: (JSON.parse(String(init.body)) as { message: string }).message,
              }),
            )
            return openStream(open, init.signal ?? undefined)
          }
          const frames = server.streams[id]
          if (frames) {
            // A first submission really persists: the ledger gains the user message and, if
            // the turn ended, the reply. The page re-reads the ledger afterwards, so a stub
            // that did not store them would model a server that forgets what it answered.
            const body = JSON.parse(String(init.body)) as { message: string }
            const ledger = server.messages[id] ?? (server.messages[id] = [])
            ledger.push(
              message({
                message_id: `msg_${ledger.length + 1}`,
                sequence: ledger.length + 1,
                content: body.message,
              }),
            )
            const final = frames.find(([name]) => name === 'final')
            if (final) {
              ledger.push(
                message({
                  message_id: `msg_${ledger.length + 1}`,
                  sequence: ledger.length + 1,
                  role: 'assistant',
                  content: String((final[1] as { text: string }).text),
                }),
              )
            }
            // An interrupt frame is the server pausing the turn: from then on the card it
            // stopped on is what the conversation detail reports, exactly as the real backend
            // does. The page re-reads that detail when the turn settles, so a stub which forgot
            // to record the pause would wipe the card the stream had just delivered.
            const interrupted = frames.find(([name]) => name === 'interrupt')?.[1]
            if (interrupted && server.details[id]) {
              server.details[id].pending_interrupt = interrupted as Record<string, unknown>
            }
            return sse(frames)
          }
          // A repeat of a stored request: the server answers with a receipt and runs nothing.
          return json(
            envelope({
              submission_id: 'sub_1',
              message_id: 'msg_receipt',
              run_id: null,
              duplicate: true,
            }),
          )
        }
        if (query.includes('after=')) {
          return json(envelope({ items: server.earlier[id] ?? [], next_cursor: null }))
        }
        return json(
          envelope({
            items: server.messages[id] ?? [],
            next_cursor: (server.messages[id] ?? []).length > 2 ? '2' : null,
          }),
        )
      }
      if (action === '/restore') {
        const found = server.conversations.find(item => item.conversation_id === id)
        if (found) found.deleted = false
        return json(envelope(found))
      }
      if (method === 'DELETE') {
        const found = server.conversations.find(item => item.conversation_id === id)
        if (found) found.deleted = true
        return json(envelope(found))
      }
      if (method === 'PATCH') {
        const found = server.conversations.find(item => item.conversation_id === id)
        Object.assign(found ?? {}, JSON.parse(String(init.body)))
        return json(envelope(found))
      }
      return json(envelope(server.details[id] ?? { conversation: conversation({ conversation_id: id }), pending_interrupt: null, recovery_required: false }))
    }
    throw new Error(`unstubbed request: ${method} ${url}`)
  })
}

function interruptPayload(questionId = 'q_1') {
  return {
    kind: 'input',
    question_id: questionId,
    agent_id: 'transfer',
    fields: [{ name: 'amount', label: '金额', type: 'text', required: true, options: [] }],
    display: '请提供转账金额',
    attempt: 1,
  }
}

/**
 * 同源广播的替身。
 *
 * `identity.ts` 用 `BroadcastChannel` 让「另一个标签页退出了」传遍同源页面。jsdom 没有这个
 * 类，所以测试里装一个最小的：它能记录本页发出去的消息，也能把一条消息投递给本页的订阅者
 * （相当于另一个标签页发出的那一条）。
 */
class FakeBroadcastChannel {
  static all: FakeBroadcastChannel[] = []

  private listeners = new Set<(event: MessageEvent) => void>()
  readonly posted: unknown[] = []

  constructor(public readonly name: string) { FakeBroadcastChannel.all.push(this) }

  postMessage(data: unknown): void { this.posted.push(data) }

  addEventListener(_type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.add(listener)
  }

  removeEventListener(_type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.delete(listener)
  }

  close(): void { this.listeners.clear() }

  /** 模拟另一个标签页发来的一条消息。 */
  deliver(data: unknown): void {
    for (const listener of [...this.listeners]) listener({ data } as MessageEvent)
  }
}

beforeEach(() => {
  localStorage.clear()
  FakeBroadcastChannel.all = []
  vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
  // 本地存档按账号分键：这一套测试里的账号就是 `USER_ID`，种子数据也写在它的键下。
  resetStorageScope()
  setStorageOwner(USER_ID)
  resetIdentity()
  // The cache outlives a mount, and every test here starts from the same two conversations.
  resetCache()
  server = {
    hasSession: true,
    demoMode: true,
    conversations: [
      // Distinct activity, so the order the sidebar shows is also the order this array states.
      conversation({
        conversation_id: 'conv_a', title: '甲的会话', last_message_at: '2026-09-28T02:00:00Z',
      }),
      conversation({
        conversation_id: 'conv_b', title: '乙的会话', last_message_at: '2026-09-28T01:00:00Z',
      }),
    ],
    messages: {
      conv_a: [
        message({ message_id: 'a1', sequence: 1, content: '甲的问题' }),
        message({ message_id: 'a2', sequence: 2, role: 'assistant', content: '甲的回答' }),
      ],
      conv_b: [
        message({ message_id: 'b1', sequence: 1, content: '乙的问题' }),
        message({ message_id: 'b2', sequence: 2, role: 'assistant', content: '乙的回答' }),
      ],
    },
    details: {
      conv_a: {
        conversation: conversation({ conversation_id: 'conv_a', title: '甲的会话' }),
        pending_interrupt: interruptPayload(),
        recovery_required: false,
      },
      conv_b: {
        conversation: conversation({ conversation_id: 'conv_b', title: '乙的会话' }),
        pending_interrupt: null,
        recovery_required: false,
      },
    },
    earlier: {},
    streams: {},
    openStreams: {},
    decisions: {},
    failures: {},
    requests: [],
  }
  installFetch()
  // jsdom has no layout, and the page scrolls itself after every event.
  Element.prototype.scrollTo = () => {}
  // The sessions outlive a mount too, and every test starts from the same two conversations.
  resetSessions()
  // The task graph is a store of its own: a graph left by one case must not be read by the next.
  demoStore.resetTasks()
})

afterEach(() => {
  vi.unstubAllGlobals()
  demoStore.resetTasks()
  resetStorageScope()
  resetIdentity()
})

async function open() {
  const wrapper = mount(App)
  await flushPromises()
  await flushPromises()
  return wrapper
}

function rows(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.conversation-item')
}

function shownText(wrapper: ReturnType<typeof mount>): string {
  return wrapper.find('.conversation').text()
}

/**
 * Holds every response whose URL contains one of `needles`, so a test can look at what the
 * page shows *while* a request is still in flight. `started` counts the requests that have
 * reached the gate; `release` lets them all through.
 */
function holdRequests(needles: string[]) {
  const waiting: Array<() => void> = []
  let reached = 0
  let released = false
  const inner = globalThis.fetch
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
    if (!released && needles.some(needle => String(input).includes(needle))) {
      reached += 1
      await new Promise<void>(resolve => waiting.push(resolve))
    }
    return inner(input, init)
  })
  return {
    started: () => reached,
    release: () => {
      // Nothing is held after this: a later request to the same URL -- the PATCH that follows
      // a read, the write that ends a turn -- is not what the test was looking at.
      released = true
      while (waiting.length) waiting.shift()!()
    },
  }
}

/**
 * Holds the next read of one conversation's detail and lets everything else through.
 *
 * The race this exists for needs one specific GET to answer late while the write that
 * triggered it -- a PATCH, a POST -- has already gone through, which a URL-substring gate
 * cannot express: those requests carry the same path as the read.
 */
function holdGetDetail(conversationId: string) {
  const waiting: Array<() => void> = []
  let reached = 0
  let released = false
  const inner = globalThis.fetch
  const target = `/api/conversations/${conversationId}`
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const method = (init.method ?? 'GET').toUpperCase()
    if (!released && method === 'GET' && String(input).endsWith(target)) {
      reached += 1
      await new Promise<void>(resolve => waiting.push(resolve))
    }
    return inner(input, init)
  })
  return {
    started: () => reached,
    release: () => {
      released = true
      while (waiting.length) waiting.shift()!()
    },
  }
}

/**
 * Lets a turn the fake server answered with a closed stream run to its end. The global 处理中
 * overlay that used to mark this is gone; the pane being usable again means the same thing.
 *
 * A turn that never drains is exactly the stuck-running regression this feature is about, so the
 * timeout throws rather than letting the calling test carry on against a page that is still locked.
 */
async function drainTurn(wrapper: VueWrapper): Promise<void> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await flushPromises()
    if (wrapper.find('.composer textarea').attributes('disabled') === undefined) return
  }
  throw new Error('the turn never drained: the composer is still disabled after 30 flushes')
}

// --- the tests ----------------------------------------------------------------------------


describe('subscription reminders in the header', () => {
  it('prefills the composer once with the cancellation sentence', async () => {
    server.reminders = [{
      subscription_id: 'sub_1', name: '演示视频会员', merchant_name: '演示视频平台',
      amount_minor: 2500, next_due_date: '2026-11-04', days_until_due: 5,
    }]
    const wrapper = await open()
    await flushPromises()

    expect(wrapper.find('[data-test="reminder-count"]').text()).toBe('1')
    await wrapper.find('[data-test="reminder-bell"]').trigger('click')
    await wrapper.find('[data-test="reminder-cancel"]').trigger('click')

    const composer = wrapper.find('textarea').element as HTMLTextAreaElement
    expect(composer.value).toBe('帮我取消订阅「演示视频会员」')
  })
})

describe('the demo control bar', () => {
  it('shows the bank’s simulated time on first paint, without opening the drawer', async () => {
    const wrapper = await open()
    await flushPromises()

    // /health reported demo_mode, so the bar is mounted and has already read the clock once.
    const label = wrapper.find('[data-test="clock-label"]')
    expect(label.text()).toBe('2026-10-22 09:00')
    expect(label.text()).not.toContain('点击查看')

    const clockReads = server.requests.filter(row => row.url.endsWith('/api/demo/clock'))
    expect(clockReads).toHaveLength(1)
    expect(clockReads[0].method).toBe('GET')
    // The drawer is still closed: the one clock read is all the demo asked for.
    expect(server.requests.some(row => row.url.includes('/api/audit/business-events'))).toBe(false)
    expect(wrapper.find('[data-test="demo-drawer"]').attributes('aria-hidden')).toBe('true')
  })
})

describe('conversation switching', () => {
  it('opens the last-used conversation and shows its ledger', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    const wrapper = await open()

    expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')
    expect(shownText(wrapper)).toContain('乙的问题')
    expect(shownText(wrapper)).toContain('乙的回答')
    expect(shownText(wrapper)).not.toContain('甲的问题')
  })

  it('opens at the newest message, not the top of the ledger', async () => {
    // jsdom 没有布局，滚没滚到底看不出来；能看出来的是「打开会话时确实把滚动容器推到过
    // 底部」——以前这条路径一次都不滚，切过去就停在最早那几条上。
    const scrolled: Element[] = []
    const spy = vi.spyOn(Element.prototype, 'scrollTo').mockImplementation(function (this: Element) {
      scrolled.push(this)
    })

    try {
      const wrapper = await open()
      expect(scrolled.at(-1)?.classList.contains('conversation')).toBe(true)

      scrolled.length = 0
      await rows(wrapper)[1].find('.conversation-row').trigger('click')
      await flushPromises()
      await flushPromises()

      expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')
      expect(scrolled.at(-1)?.classList.contains('conversation')).toBe(true)
    } finally {
      spy.mockRestore()
    }
  })

  it('carries no view state across a switch', async () => {
    const wrapper = await open()
    // Conversation A is open, with a confirmed question and answers being typed.
    expect(shownText(wrapper)).toContain('甲的问题')
    expect(wrapper.find('.interrupt-card').exists()).toBe(true)
    await wrapper.find('.interrupt-field input').setValue('100')
    await wrapper.find('.composer textarea').setValue('还没发出去的话')
    // And an error banner from A, so the switch has something to clear.
    server.failures['conv_a'] = { status: 409, code: 'CONVERSATION_NOT_WRITABLE', message: '该会话已归档' }
    await rows(wrapper)[0].find('[aria-label="置顶"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.error-banner').exists()).toBe(true)

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    // B is on screen, and nothing of A came with it.
    expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')
    expect(shownText(wrapper)).toContain('乙的问题')
    expect(shownText(wrapper)).not.toContain('甲的问题')
    expect(shownText(wrapper)).not.toContain('甲的回答')
    // A's question is not B's question: the card is gone, and so are the typed answers.
    expect(wrapper.find('.interrupt-card').exists()).toBe(false)
    expect(wrapper.find('.interrupt-field input').exists()).toBe(false)
    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).value).toBe('')
    // And A's banner is not left hanging over B.
    expect(wrapper.find('.error-banner').exists()).toBe(false)
  })

  it('does not leave the turn that was in flight over the conversation switched to', async () => {
    // This is the state nothing else overwrites. The ledger is re-read on a switch, so a
    // stale `messages` would be replaced anyway -- but an in-flight turn is not in the ledger
    // yet, and if it survived the switch it would render as if it belonged to the new
    // conversation, with a user message the server never received for it.
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [['agent_start', { agent_id: 'transfer' }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('乙这边没跑完的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙这边没跑完的话')
    expect(wrapper.find('.unconfirmed').exists()).toBe(true)

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.pane-title h1').text()).toBe('甲的会话')
    expect(shownText(wrapper)).not.toContain('乙这边没跑完的话')
    expect(shownText(wrapper)).not.toContain('结果未确认')
    expect(shownText(wrapper)).toContain('甲的问题')
  })

  it('opens a conversation without taking the page over', async () => {
    const wrapper = await open()
    expect(wrapper.find('.pane-title h1').text()).toBe('甲的会话')

    const gate = holdRequests(['/api/conversations/conv_b'])
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()

    // Opening a conversation is a read, not an operation that owns the page: the name is up
    // straight away, and the sidebar stays usable -- no row is held shut for it.
    expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')
    expect(rows(wrapper)[0].find('.conversation-actions button').attributes('disabled')).toBeUndefined()
    // The pane says what it is doing instead of going blank.
    expect(shownText(wrapper)).toContain('正在载入会话')

    gate.release()
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的问题')
  })

  it('reads the ledger and the conversation at the same time', async () => {
    const wrapper = await open()

    const gate = holdRequests(['/api/conversations/conv_b'])
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()

    // Neither read waits for the other. Against a database a round trip away, the two of them
    // run one after the other is the whole cost of the switch.
    expect(gate.started()).toBe(2)

    gate.release()
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的问题')
    expect(shownText(wrapper)).toContain('乙的回答')
  })

  it('renders a conversation it has already read without waiting for the server', async () => {
    const wrapper = await open()
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    // Back to A, whose ledger and detail were read on the way in, with every read of it held:
    // whatever appears on screen now came from the cache, not from the server.
    const gate = holdRequests(['/api/conversations/conv_a'])
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()

    expect(shownText(wrapper)).toContain('甲的问题')
    // The pane has something to show, so it does not claim to be loading it.
    expect(shownText(wrapper)).not.toContain('正在载入会话')

    gate.release()
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('甲的问题')
  })

  it('re-reads a conversation it already had, rather than trusting the cache', async () => {
    const wrapper = await open()
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    const gate = holdRequests(['/api/conversations/conv_a'])
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()

    // The cache fills the pane immediately, but it is never the last word: the server runs
    // background work (the automatic title, a turn that is still finishing), so both reads
    // still go out, and they still travel together.
    expect(gate.started()).toBe(2)

    gate.release()
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('甲的回答')
  })

  it('does not let a slow read of the conversation that was left paint over the one now open', async () => {
    const wrapper = await open()
    // The open conversation drops out of the list, which is what makes the page re-read its
    // detail in the background. Nothing waits for that read, so a switch can start over it.
    server.conversations = server.conversations.filter(item => item.conversation_id !== 'conv_a')
    const gate = holdGetDetail('conv_a')
    await wrapper.findAll('.sidebar-filters input')[0].setValue(true)
    await flushPromises()
    expect(gate.started()).toBe(1)

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')

    gate.release()
    await flushPromises()
    await flushPromises()

    // A's answer arrives while B is on screen. It belongs to the cache; the pane is B's.
    expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')
    expect(shownText(wrapper)).toContain('乙的问题')
    expect(shownText(wrapper)).not.toContain('甲的问题')
  })

  it('reads a conversation again once a turn has started in it', async () => {
    const wrapper = await open()
    // A is the one with a question waiting, so the turn below is B's: only a conversation with
    // nothing pending can be written to.
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    // B was read on the way here, so it comes back from the cache...
    const warm = holdRequests(['/api/conversations/conv_b'])
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的回答')
    warm.release()
    await flushPromises()
    await flushPromises()

    // ...until a turn starts in it. That turn is not in the ledger yet, so the ledger the
    // cache holds is already out of date and must not be handed back.
    server.streams.conv_b = [['agent_start', { agent_id: 'transfer' }]]
    await wrapper.find('.composer textarea').setValue('再问一句')
    await wrapper.find('.composer').trigger('submit')
    await drainTurn(wrapper)

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    const gate = holdRequests(['/api/conversations/conv_b'])
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()

    // The turn invalidated the cached ledger, so the read happens...
    expect(gate.started()).toBeGreaterThan(0)
    // ...and the pane keeps the conversation's own ledger instead of going blank, then shows the
    // fresh one -- which now holds the turn that started here.
    expect(shownText(wrapper)).toContain('乙的回答')

    gate.release()
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('再问一句')
  })

  it('keeps the cached metadata in step with the sidebar', async () => {
    const wrapper = await open()
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    // Back to A from the cache, with the reads held: the pane is filled from what was stored.
    const warm = holdRequests(['/api/conversations/conv_a'])
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    expect(wrapper.find('.pane-title h1').text()).toBe('甲的会话')
    expect(shownText(wrapper)).not.toContain('正在载入会话')
    warm.release()
    await flushPromises()
    await flushPromises()

    // Rename it. The cached detail must not keep handing the old name back.
    await rows(wrapper)[0].find('[aria-label="重命名"]').trigger('click')
    await wrapper.find('.rename-input').setValue('甲的新名字')
    await wrapper.find('.rename-input').trigger('keydown.enter')
    await flushPromises()

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    const gate = holdRequests(['/api/conversations/conv_a'])
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()

    // The cached detail carries the name the rename gave it: were it stale, filling the pane
    // from the cache would put the old name back over the one the sidebar is showing.
    expect(wrapper.find('.pane-title h1').text()).toBe('甲的新名字')

    gate.release()
    await flushPromises()
    await flushPromises()
  })

  it('closes the rename input when the title is submitted unchanged', async () => {
    const wrapper = await open()
    await rows(wrapper)[0].find('[aria-label="重命名"]').trigger('click')
    expect(wrapper.find('.rename-input').exists()).toBe(true)

    // Enter with the same title is a no-op write: the editor still has to close.
    await wrapper.find('.rename-input').trigger('keydown.enter')
    await flushPromises()

    expect(wrapper.find('.rename-input').exists()).toBe(false)
  })

  it('reads a conversation ahead of time when the pointer reaches its row', async () => {
    const wrapper = await open()
    await rows(wrapper)[1].find('.conversation-row').trigger('pointerenter')
    await new Promise(resolve => setTimeout(resolve, 200))
    await flushPromises()
    await flushPromises()

    const urls = server.requests.map(item => item.url)
    expect(urls.some(url => url.includes('/api/conversations/conv_b/messages'))).toBe(true)
    expect(urls.some(url => url.endsWith('/api/conversations/conv_b'))).toBe(true)
    // Reading ahead is not opening: the pane is still the conversation the user is in.
    expect(wrapper.find('.pane-title h1').text()).toBe('甲的会话')

    const gate = holdRequests(['/api/conversations/conv_b'])
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的问题')

    gate.release()
    await flushPromises()
    await flushPromises()
  })

  it('does not let a slow ledger from the conversation the page left paint over the one now open', async () => {
    const wrapper = await open()
    // B has nothing open, so its composer accepts a message; A is the one holding a question.
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的问题')

    // A refused interrupt answer makes the view re-read itself in the background
    // (showError -> void refreshHistory()), and that is the one ledger read nobody waits for:
    // the sidebar is clickable again while it is still in flight.
    server.failures['/messages'] = { status: 409, code: 'INVALID_INTERRUPT_ANSWER', message: '确认的问题已失效' }
    const gate = holdRequests(['/api/conversations/conv_b/messages?'])
    await wrapper.find('.composer textarea').setValue('再问一句')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()
    await flushPromises()
    expect(gate.started()).toBe(1)

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('甲的问题')

    gate.release()
    await flushPromises()
    await flushPromises()

    // B's late ledger belongs to B: it goes to the cache and stops there.
    expect(shownText(wrapper)).toContain('甲的问题')
    expect(shownText(wrapper)).not.toContain('乙的问题')
  })

  it('opens the conversation clicked while another one is still on its way', async () => {
    // A third row, so the second click is neither the conversation on screen nor the one
    // already being opened.
    server.conversations.push(conversation({ conversation_id: 'conv_c', title: '丙的会话' }))
    server.messages.conv_c = [message({ message_id: 'c1', sequence: 1, content: '丙的问题' })]
    server.details.conv_c = {
      conversation: conversation({ conversation_id: 'conv_c', title: '丙的会话' }),
      pending_interrupt: null,
      recovery_required: false,
    }
    const wrapper = await open()
    const gate = holdRequests(['/api/conversations/conv_b'])

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')

    // B is still loading and the user has already changed their mind: the row they clicked
    // last is the one they are looking at.
    await rows(wrapper)[2].find('.conversation-row').trigger('click')
    await flushPromises()
    expect(wrapper.find('.pane-title h1').text()).toBe('丙的会话')

    gate.release()
    await flushPromises()
    await flushPromises()

    // B's late reads answer a conversation that is no longer open, so the pane keeps C.
    expect(wrapper.find('.pane-title h1').text()).toBe('丙的会话')
    expect(shownText(wrapper)).toContain('丙的问题')
  })

  it('does not read the same conversation twice when the pointer got there first', async () => {
    const wrapper = await open()
    const gate = holdRequests(['/api/conversations/conv_b'])

    await rows(wrapper)[1].find('.conversation-row').trigger('pointerenter')
    await new Promise(resolve => setTimeout(resolve, 200))
    await flushPromises()
    expect(gate.started()).toBe(2)

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    // The click waits on the read the pointer already started instead of asking again.
    expect(gate.started()).toBe(2)

    gate.release()
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的问题')
  })

  it('shows the half a read-ahead has already brought back', async () => {
    const wrapper = await open()
    // The detail is the slower of the two reads, so a click can arrive while the ledger is
    // already here and the detail is not.
    const detailGate = holdGetDetail('conv_b')
    await rows(wrapper)[1].find('.conversation-row').trigger('pointerenter')
    await new Promise(resolve => setTimeout(resolve, 200))
    await flushPromises()
    expect(detailGate.started()).toBe(1)

    // The click re-reads the ledger, and that read is held as well: what the pane shows now can
    // only come from what the read-ahead already brought back.
    const ledgerGate = holdRequests(['/api/conversations/conv_b/messages'])
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的问题')
    expect(shownText(wrapper)).not.toContain('正在载入会话')

    ledgerGate.release()
    detailGate.release()
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('乙的问题')
  })
})

/**
 * Two conversations at once. The page used to own one turn at a time -- one global busy flag, one
 * live turn, one lock over every sidebar button -- so a turn in flight made the rest of the page
 * unusable. Each conversation now owns its own state, and the pane shows the open one's.
 */
describe('parallel sessions', () => {
  beforeEach(() => {
    // The fixture gives A a question waiting; these tests need a conversation that can be written
    // to, and they make their own interrupts when they want one.
    server.details.conv_a.pending_interrupt = null
  })

  it('switches to another conversation while a turn is still streaming', async () => {
    server.openStreams.conv_a = [['agent_start', { agent_id: 'transfer' }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('先跑一个')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    // A is working, and the page says so where the work is -- not over the whole page: B's row is
    // still writable. (The old global `.pending` overlay is gone, so asserting its absence would be
    // vacuously true; the sidebar being usable is the real evidence.)
    expect(shownText(wrapper)).toContain('先跑一个')
    expect(rows(wrapper)[1].find('.conversation-actions button').attributes('disabled')).toBeUndefined()
    expect(rows(wrapper)[0].find('[aria-label="正在运行"]').exists()).toBe(true)
    expect(rows(wrapper)[1].find('[aria-label="正在运行"]').exists()).toBe(false)

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.pane-title h1').text()).toBe('乙的会话')
    expect(shownText(wrapper)).toContain('乙的问题')
    expect(shownText(wrapper)).not.toContain('先跑一个')
  })

  it('keeps each conversation’s turn in its own pane', async () => {
    server.openStreams.conv_a = [['agent_start', { agent_id: 'transfer' }]]
    server.streams.conv_b = [
      ['agent_start', { agent_id: 'card' }],
      ['final', { text: '乙这边的回答', business_ids: [] }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    await wrapper.find('.composer textarea').setValue('乙这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    // B's pane shows B's turn, and nothing of A's.
    expect(shownText(wrapper)).toContain('乙这边的话')
    expect(shownText(wrapper)).toContain('乙这边的回答')
    expect(shownText(wrapper)).not.toContain('甲这边的话')

    // A's turn is still running, in A.
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(shownText(wrapper)).toContain('甲这边的话')
    expect(shownText(wrapper)).not.toContain('乙这边的话')
  })

  it('marks a conversation that finished while it was not on screen, and keeps the whole process', async () => {
    server.streams.conv_a = [
      ['agent_start', { agent_id: 'transfer' }],
      ['tool_call', { tool: 'get_balance', arguments: { account: 'A' } }],
      ['final', { text: '甲这边跑完了', business_ids: [] }],
    ]
    const wrapper = await open()
    // Held so the turn is still running when the user leaves: a closed stream would otherwise run
    // to its end inside the first flushPromises, while A is still the pane, and nothing would be
    // marked. Installed after open() because the needle also matches the ledger read.
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    expect(gate.started()).toBe(1)

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    // The turn ends while B is the pane.
    gate.release()
    await flushPromises()
    await flushPromises()
    await flushPromises()

    expect(rows(wrapper)[0].find('[aria-label="需要你看"]').exists()).toBe(true)
    expect(rows(wrapper)[0].find('[aria-label="需要你看"]').attributes('title')).toBe('已回复')
    expect(rows(wrapper)[1].find('[aria-label="需要你看"]').exists()).toBe(false)

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    // The process is still there, and the reply is drawn exactly once.
    expect(wrapper.find('.live-turn').exists()).toBe(true)
    expect(wrapper.findAll('.chat-message.user').filter(item => item.text().includes('甲这边的话'))).toHaveLength(1)
    expect(wrapper.findAll('.chat-message.assistant').filter(item => item.text().includes('甲这边跑完了'))).toHaveLength(1)
  })

  it('marks a conversation that stopped on a card while it was not on screen', async () => {
    server.streams.conv_a = [
      ['agent_start', { agent_id: 'transfer' }],
      ['tool_call', { tool: 'get_balance', arguments: { account: 'A' } }],
      ['interrupt', interruptPayload('q_bg')],
    ]
    const wrapper = await open()
    // The same hold as the finished-turn case: a closed stream would run to its end inside the
    // first flushPromises, while A is still the pane, and nothing would be marked.
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    expect(gate.started()).toBe(1)

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    gate.release()
    await flushPromises()
    await flushPromises()
    await flushPromises()

    // The turn did not finish -- it stopped on a question. A card waiting to be answered is not
    // the same 'come back' as a finished reply, and the sidebar has to say which one it is.
    expect(rows(wrapper)[0].find('[aria-label="需要你看"]').attributes('title')).toBe('等你确认')
    expect(rows(wrapper)[1].find('[aria-label="需要你看"]').exists()).toBe(false)

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    // And the card is still there to answer.
    expect(wrapper.find('.interrupt-card').exists()).toBe(true)
    expect(wrapper.find('.interrupt-card').text()).toContain('请提供转账金额')
  })

  it('refuses a second message into a conversation that is still running', async () => {
    server.openStreams.conv_a = [['agent_start', { agent_id: 'transfer' }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('第一条')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).disabled).toBe(true)
    expect(wrapper.find('.composer').text()).toContain('本会话正在处理中，结束后才能发下一条')

    // The disabled attribute is only the first line of defence: VTU refuses to type into a disabled
    // control (its `trigger`/`setValue` bail out on disabled elements), so re-enable it by hand and
    // take the path a bug would take -- otherwise this assertion is vacuously true.
    const textarea = wrapper.find('.composer textarea').element as HTMLTextAreaElement
    textarea.disabled = false
    await wrapper.find('.composer textarea').setValue('第二条')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    // `send` refused on its own: the second message never reached the server.
    expect(server.requests.filter(item => item.method === 'POST' && item.url.includes('/messages'))).toHaveLength(1)
  })

  it('refuses to send while the pane is still loading', async () => {
    const wrapper = await open()
    const gate = holdRequests(['/api/conversations/conv_b'])
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()

    // The pane is loading, so the composer is really disabled: the `switching` guard inside `send`
    // is the second line of defence, not the only one.
    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).disabled).toBe(true)

    // B's ledger is not here yet, so a turn started now would record an empty watermark and hide
    // whatever the ledger turns out to hold. VTU will not type into a disabled control, so re-enable
    // it by hand: the `switching` guard inside `send` has to refuse on its own.
    const textarea = wrapper.find('.composer textarea').element as HTMLTextAreaElement
    textarea.disabled = false
    await wrapper.find('.composer textarea').setValue('打断载入')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    expect(server.requests.filter(item => item.method === 'POST' && item.url.includes('/messages'))).toHaveLength(0)

    gate.release()
    await flushPromises()
    await flushPromises()
  })

  it('falls back to the whole ledger when the watermark is no longer in it', async () => {
    server.openStreams.conv_a = [['agent_start', { agent_id: 'transfer' }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('这一条还在跑')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    // The ledger is read again and no longer holds the message the turn started from -- an older
    // page, a compaction, anything. Losing the cut must never hide history.
    server.messages.conv_a = [
      message({ message_id: 'a1', sequence: 1, content: '甲的问题' }),
      message({ message_id: 'a9', sequence: 9, content: '更早的一页' }),
    ]
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(shownText(wrapper)).toContain('更早的一页')
    expect(shownText(wrapper)).toContain('这一条还在跑')
  })

  it('leaves the sidebar usable while one conversation is running', async () => {
    server.openStreams.conv_a = [['agent_start', { agent_id: 'transfer' }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    // A is running: A may not be archived or deleted. Everything else is free.
    expect(rows(wrapper)[0].find('[aria-label="归档"]').attributes('disabled')).toBeDefined()
    expect(rows(wrapper)[0].find('[aria-label="删除会话"]').attributes('disabled')).toBeDefined()
    expect(rows(wrapper)[0].find('[aria-label="置顶"]').attributes('disabled')).toBeUndefined()
    expect(rows(wrapper)[1].find('[aria-label="归档"]').attributes('disabled')).toBeUndefined()
    expect(rows(wrapper)[1].find('[aria-label="删除会话"]').attributes('disabled')).toBeUndefined()
    expect(rows(wrapper)[1].find('[aria-label="置顶"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('.sidebar-head [aria-label="新建会话"]').attributes('disabled')).toBeUndefined()

    // And B really can be archived while A runs.
    await rows(wrapper)[1].find('[aria-label="归档"]').trigger('click')
    await flushPromises()
    expect(server.requests.some(item => item.method === 'PATCH' && item.url.includes('conv_b'))).toBe(true)
  })

  it('resumes a card confirmation in the conversation that asked for it', async () => {
    server.details.conv_a.pending_interrupt = {
      kind: 'confirmation',
      question_id: 'q_card',
      agent_id: 'card',
      attempt: 1,
      display: '请确认：挂失银行卡 · 借记卡（尾号 8801）。',
      fields: [{ name: 'decision', label: '操作决定', type: 'select', required: true, options: ['确认', '取消'] }],
      confirmation: {
        pending_id: `CA-${'a'.repeat(32)}`,
        operation: 'report_loss',
        title: '挂失银行卡',
        amount_minor: 0,
        product_name: '借记卡（尾号 8801）',
        account_display: '借记卡（尾号 8801）',
        confirm_label: '确认',
        cancel_label: '取消',
        expires_at: '2030-01-01T00:00:00Z',
        requires_code: true,
        code_label: '模拟验证码',
      },
    }
    const wrapper = await open()
    const gate = holdRequests(['/pending-actions/'])
    await wrapper.find('.interrupt-card [aria-label="模拟验证码"]').setValue('123456')
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    expect(gate.started()).toBe(1)

    // The bank is still deciding when the user leaves. The resume that follows belongs to A --
    // it is what asked for the confirmation -- even though the pane holds B by then, and B must
    // not receive it.
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    gate.release()
    await flushPromises()
    await flushPromises()
    await flushPromises()

    expect(shownText(wrapper)).toContain('乙的问题')
    expect(shownText(wrapper)).not.toContain('确认本次操作')
    const decided = server.requests.filter(item => item.method === 'POST' && item.url.includes('/pending-actions/'))
    expect(decided).toHaveLength(1)
    expect(decided[0].url).toContain('conv_a')
    expect(decided[0].url).not.toContain('conv_b')
  })

  it('keeps filling in a turn’s process after the user comes back to it', async () => {
    server.streams.conv_a = [
      ['agent_start', { agent_id: 'transfer' }],
      ['tool_call', { agent_id: 'transfer', tool_name: 'get_balance', call_id: 'c_1', arguments: { account: 'A' } }],
    ]
    const wrapper = await open()
    // Held, so the turn exists with an empty process: whatever the pane shows next arrived later.
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    // Away and back while the turn is still in flight...
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.live-turn').exists()).toBe(true)
    expect(wrapper.find('.live-turn .activity-tool').exists()).toBe(false)

    // ...and the events keep landing in this conversation's own process.
    gate.release()
    await flushPromises()
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.live-turn .activity-tool').text()).toContain('get_balance')
    expect(wrapper.find('.live-turn .chat-message.user').text()).toContain('甲这边的话')
  })

  it('draws a turn that started from an empty ledger exactly once', async () => {
    // A conversation whose ledger is empty when the turn starts: everything the ledger holds later
    // came from this turn, so drawing the ledger *and* the live block would draw it all twice.
    server.messages.conv_a = []
    server.streams.conv_a = [
      ['agent_start', { agent_id: 'transfer' }],
      ['final', { text: '甲这边跑完了', business_ids: [] }],
    ]
    const wrapper = await open()
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    expect(gate.started()).toBe(1)

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    gate.release()
    await flushPromises()
    await flushPromises()
    await flushPromises()

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.findAll('.chat-message.user').filter(item => item.text().includes('甲这边的话'))).toHaveLength(1)
    expect(wrapper.findAll('.chat-message.assistant').filter(item => item.text().includes('甲这边跑完了'))).toHaveLength(1)
  })

  it('keeps a settled conversation locked while its ledger is re-read', async () => {
    // The confirmation is answered by a button event, and the turn it settles is re-read
    // afterwards. That re-read belongs to the turn: if the lock came down first, a message sent
    // in the gap would start a second turn that the re-read then cleared (running false, live
    // gone) -- so the composer has to stay shut until the re-read is done.
    server.details.conv_a.pending_interrupt = {
      kind: 'confirmation',
      question_id: 'q_w1',
      agent_id: 'wealth',
      attempt: 1,
      display: '请确认这笔模拟申购',
      fields: [{ name: 'decision', label: '操作决定', type: 'select', required: true, options: ['确认', '取消'] }],
      confirmation: {
        pending_id: 'wp_1',
        operation: 'purchase_product',
        title: '确认申购',
        amount_minor: 200000,
        product_name: '演示灵活理财',
        account_display: 'a_wealth_main',
        confirm_label: '确认',
        cancel_label: '取消',
        expires_at: '2030-01-01T00:00:00Z',
        requires_code: false,
        code_label: '模拟验证码',
      },
    }
    const wrapper = await open()
    // Held after open(): the needle matches only the ledger read that follows the click, because
    // the click itself goes to the pending-actions route.
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    await flushPromises()
    expect(gate.started()).toBe(1)

    // The turn has settled but not drained: the conversation is still the one being worked in.
    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).disabled).toBe(true)
    expect(wrapper.find('.composer').text()).toContain('本会话正在处理中，结束后才能发下一条')

    gate.release()
    await flushPromises()
    await flushPromises()
    await flushPromises()
    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).disabled).toBe(false)
  })

  it('locks only the row it is writing to', async () => {
    const wrapper = await open()
    const gate = holdRequests(['/api/conversations/conv_a'])
    await rows(wrapper)[0].find('[aria-label="置顶"]').trigger('click')
    await flushPromises()

    // The write is in flight: A's row is held, B's is not.
    expect(rows(wrapper)[0].find('[aria-label="置顶"]').attributes('disabled')).toBeDefined()
    expect(rows(wrapper)[1].find('[aria-label="置顶"]').attributes('disabled')).toBeUndefined()

    gate.release()
    await flushPromises()
    await flushPromises()
    // The write landed: A is pinned now, so its pin button is the other one -- and it is free again.
    expect(rows(wrapper)[0].find('[aria-label="取消置顶"]').attributes('disabled')).toBeUndefined()
  })

  it('converges to a clean ledger once the result has been seen', async () => {
    server.streams.conv_a = [
      ['agent_start', { agent_id: 'transfer' }],
      ['final', { text: '甲这边跑完了', business_ids: [] }],
    ]
    const wrapper = await open()
    // Held so the turn ends while B is the pane; see the same gate in Task 3's "marks a
    // conversation that finished while it was not on screen".
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    expect(gate.started()).toBe(1)

    // Away while it is still running, then released: it ends with B on screen.
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    gate.release()
    await flushPromises()
    await flushPromises()
    await flushPromises()

    // Back to look at it: the process is still there, and the mark it left is gone.
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.live-turn').exists()).toBe(true)
    expect(rows(wrapper)[0].find('[aria-label="需要你看"]').exists()).toBe(false)

    // Away and back again: the turn is the ledger's now, so there is nothing live left to keep.
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.live-turn').exists()).toBe(false)
    expect(wrapper.findAll('.chat-message.user').filter(item => item.text().includes('甲这边的话'))).toHaveLength(1)
    expect(wrapper.findAll('.chat-message.assistant').filter(item => item.text().includes('甲这边跑完了'))).toHaveLength(1)
  })

  it('keeps a background failure in its own conversation', async () => {
    const wrapper = await open()
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    expect(gate.started()).toBe(1)

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    // A's turn is refused while B is on screen.
    server.failures['/messages'] = { status: 409, code: 'IDEMPOTENCY_CONFLICT', message: '同一个幂等键被用于了不同的请求' }
    gate.release()
    await flushPromises()
    await flushPromises()

    // The refusal belongs to A: B's pane is not interrupted by it...
    expect(wrapper.find('.error-banner').exists()).toBe(false)

    // ...and A shows it when the user goes back.
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.error-banner').text()).toContain('同一个幂等键')
  })

  it('offers the repeat only in the conversation that owes it', async () => {
    const wrapper = await open()
    // A transport failure is the one outcome that keeps the record: a refusal is a verdict, and a
    // refusal clears it. This is the state the repeat button exists for.
    const transport = globalThis.fetch
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
      if (String(input).includes('/messages') && (init.method ?? 'GET').toUpperCase() === 'POST') {
        throw new TypeError('Failed to fetch')
      }
      return transport(input, init)
    })
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    // A is the conversation that owes the request, so the repeat is offered in A...
    expect(wrapper.find('.error-banner button.secondary').text()).toContain('重试发送')

    // ...and nowhere else.
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.error-banner').exists()).toBe(false)

    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.error-banner button.secondary').text()).toContain('重试发送')
  })

  it('keeps an unfinished turn when the user leaves and comes back', async () => {
    // The stream ends without a final: the turn is unfinished, so nothing of it reached the
    // ledger and `unseen` stays false -- the conversation was on screen the whole time.
    server.streams.conv_a = [
      ['agent_start', { agent_id: 'transfer' }],
      ['reply_delta', { text: '甲这边还没跑完' }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.live-turn').exists()).toBe(true)
    expect(wrapper.find('.live-turn').text()).toContain('甲这边还没跑完')

    // Away and back: the ledger holds no reply for this turn, so dropping the block would lose
    // the streamed answer and the notice with it.
    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.live-turn').exists()).toBe(true)
    expect(wrapper.find('.live-turn').text()).toContain('甲这边还没跑完')
    expect(wrapper.find('.live-turn .unconfirmed').text()).toContain('结果未确认')
  })

  it('refuses to archive or delete a running conversation, and still writes elsewhere', async () => {
    const wrapper = await open()
    const gate = holdRequests(['/api/conversations/conv_a/messages'])
    await wrapper.find('.composer textarea').setValue('甲这边的话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    expect(gate.started()).toBe(1)

    const rowOf = (title: string) => rows(wrapper).find(row => row.text().includes(title))!
    const rowWrites = (id: string) => server.requests.filter(
      request => request.url.endsWith(`/api/conversations/${id}`)
        && (request.method === 'PATCH' || request.method === 'DELETE'))

    // A is mid-turn. The template disables both buttons and a disabled control swallows the
    // click, so the attribute is cleared first: what is under test is the guard behind the
    // button, not the attribute in front of it.
    for (const label of ['归档', '删除会话']) {
      const button = rowOf('甲的会话').find(`[aria-label="${label}"]`)
      ;(button.element as HTMLButtonElement).disabled = false
      await button.trigger('click')
      await flushPromises()
    }
    expect(rowWrites('conv_a')).toHaveLength(0)
    expect(rows(wrapper)).toHaveLength(2)

    // Another row's write is not the running row's business.
    await rowOf('乙的会话').find('[aria-label="置顶"]').trigger('click')
    await flushPromises()
    expect(rowWrites('conv_b')).toHaveLength(1)

    // The turn ends, and the same click does write now: the two refusals above were the guard's
    // doing, not the disabled attribute swallowing the event.
    gate.release()
    await flushPromises()
    await flushPromises()
    const archive = rowOf('甲的会话').find('[aria-label="归档"]')
    ;(archive.element as HTMLButtonElement).disabled = false
    await archive.trigger('click')
    await flushPromises()
    expect(rowWrites('conv_a')).toHaveLength(1)
  })

  it('repeats every unfinished submission after a reload, each under its own key', async () => {
    savePending({ key: 'key-a', conversation_id: 'conv_a', message: '甲这边没跑完' })
    savePending({ key: 'key-b', conversation_id: 'conv_b', message: '乙这边没跑完' })
    server.streams.conv_a = [['agent_start', { agent_id: 'transfer' }]]
    server.streams.conv_b = [['agent_start', { agent_id: 'card' }]]
    await open()
    for (let attempt = 0; attempt < 6; attempt += 1) await flushPromises()

    // Pair each POST with the conversation its URL names: a bare multiset of keys would not
    // notice two records swapped across conversations, and "each under its own key" is that.
    const posts = server.requests
      .filter(item => item.method === 'POST' && item.url.includes('/messages'))
      .map(item => [/\/api\/conversations\/([^/?]+)\/messages/.exec(item.url)?.[1] ?? item.url, item.key])
      .sort()
    expect(posts).toEqual([['conv_a', 'key-a'], ['conv_b', 'key-b']])
  })

  it('reports a login failure inside the login form', async () => {
    server.hasSession = false
    // 失败注入只对非 GET 生效，所以这里失败的是登录那一次 POST，而不是页面加载时的读会话。
    server.failures['/api/auth/login'] = { status: 500, code: 'INTERNAL', message: '服务暂时不可用' }
    const wrapper = await open()

    // 没登录时工作台整个不渲染，只剩登录框。
    expect(wrapper.find('.workspace').exists()).toBe(false)
    await wrapper.find('input[name="login_name"]').setValue('demo')
    await wrapper.find('input[name="password"]').setValue('a long enough password')
    await wrapper.find('form.auth-form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.auth-message.error').text()).toContain('服务暂时不可用')
  })

  it('does not replay a legacy single-slot submission, and clears it on login', async () => {
    // Before the records were split every conversation shared one slot, and before P3 nothing in
    // it was scoped to an account. It cannot be proven to belong to whoever logs in now, so the
    // page must not repeat it on their behalf -- design §7.3 requires the key to be discarded.
    localStorage.setItem(PENDING_SUBMISSION_KEY, JSON.stringify({
      key: 'legacy-key', conversation_id: 'conv_a', message: '升级前留下的提交',
    }))
    await open()
    for (let attempt = 0; attempt < 6; attempt += 1) await flushPromises()

    const posts = server.requests
      .filter(item => item.method === 'POST' && item.url.includes('/api/conversations/conv_a/messages'))
    expect(posts).toEqual([])
    expect(localStorage.getItem(PENDING_SUBMISSION_KEY)).toBeNull()
  })
})

describe('the sidebar after a change', () => {
  it('moves a conversation to the top as soon as it is pinned', async () => {
    const wrapper = await open()
    expect(rows(wrapper)[0].text()).toContain('甲的会话')

    await rows(wrapper)[1].find('[aria-label="置顶"]').trigger('click')
    await flushPromises()

    expect(rows(wrapper)[0].text()).toContain('乙的会话')
    expect(rows(wrapper)[1].text()).toContain('甲的会话')
  })

  it('marks a conversation that was just archived without taking its row away', async () => {
    const wrapper = await open()
    expect(rows(wrapper)).toHaveLength(2)

    await rows(wrapper)[1].find('[aria-label="归档"]').trigger('click')
    await flushPromises()

    // The row stays under its mark, the way a soft delete does: the same row undoes it.
    expect(rows(wrapper)).toHaveLength(2)
    expect(rows(wrapper)[1].text()).toContain('已归档')
    expect(rows(wrapper)[1].text()).toContain('乙的会话')
  })
})

describe('the serial number badge', () => {
  it('shows the serial number to the left of the title', async () => {
    const wrapper = await open()
    const row = rows(wrapper)[0]
    const shownTitle = row.find('.conversation-title').text()
    expect(row.find('.conversation-serial').text()).toBe('#1')
    expect(row.text().indexOf('#1')).toBeLessThan(row.text().indexOf(shownTitle))
  })

  it('names the number for a screen reader, which cannot see the hash', async () => {
    const wrapper = await open()
    const badge = rows(wrapper)[0].find('.conversation-serial')
    expect(badge.attributes('aria-label')).toBe('会话编号 1')
  })

  it('keeps the serial number visible while renaming', async () => {
    const wrapper = await open()
    const row = rows(wrapper)[0]
    await row.find('button[aria-label="重命名"]').trigger('click')
    await flushPromises()
    expect(row.find('.rename-input').exists()).toBe(true)
    expect(row.find('.conversation-serial').text()).toBe('#1')
  })
})

describe('the confirmed question', () => {
  it('renders the card the server confirms and locks the composer', async () => {
    const wrapper = await open()

    const card = wrapper.find('.interrupt-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('请提供转账金额')
    expect(card.text()).toContain('金额')
    const composer = wrapper.find('.composer textarea')
    expect((composer.element as HTMLTextAreaElement).disabled).toBe(true)
    expect(wrapper.find('.composer-bottom').text()).toContain('请先在上方补充信息')
  })

  it('does not render a card for a conversation with nothing open', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    const wrapper = await open()

    expect(wrapper.find('.interrupt-card').exists()).toBe(false)
    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).disabled).toBe(false)
  })

  it('does not call a paused turn unconfirmed', async () => {
    // Conversation A ends with a user message and no reply, and the server confirms an open
    // question: that missing reply is the pause, not an unknown outcome. Saying "结果未确认"
    // above the card would read as if the card had already gone wrong.
    const wrapper = await open()

    expect(wrapper.find('.interrupt-card').exists()).toBe(true)
    expect(shownText(wrapper)).not.toContain('结果未确认')
  })

  it('still calls a reply-less turn unconfirmed when nothing is open', async () => {
    server.details.conv_b.pending_interrupt = null
    server.messages.conv_b = [
      message({ message_id: 'b1', sequence: 1, content: '没跑完的话' }),
    ]
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    const wrapper = await open()

    expect(wrapper.find('.interrupt-card').exists()).toBe(false)
    expect(shownText(wrapper)).toContain('结果未确认')
  })

  it('sends the answer as an interrupt answer under a fresh key', async () => {
    const wrapper = await open()
    await wrapper.find('.interrupt-field input').setValue('100')
    await wrapper.find('.interrupt-card').trigger('submit')
    await flushPromises()

    const submitted = server.requests.filter(item => item.method === 'POST' && item.url.includes('/messages'))
    expect(submitted).toHaveLength(1)
    expect(submitted[0].key).toBeTruthy()
    expect(submitted[0].body).toMatchObject({
      client_context: { resume: { question_id: 'q_1', value: { amount: '100' } } },
    })
  })
})

describe('card confirmation via decide', () => {
  const actionId = `CA-${'a'.repeat(32)}`

  function pendingCard() {
    server.details.conv_a.pending_interrupt = {
      kind: 'confirmation',
      question_id: 'q_card',
      agent_id: 'card',
      attempt: 1,
      display: '请确认：挂失银行卡 · 借记卡（尾号 8801）。',
      fields: [{ name: 'decision', label: '操作决定', type: 'select', required: true, options: ['确认', '取消'] }],
      confirmation: {
        pending_id: actionId,
        operation: 'report_loss',
        title: '挂失银行卡',
        amount_minor: 0,
        product_name: '借记卡（尾号 8801）',
        account_display: '借记卡（尾号 8801）',
        confirm_label: '确认',
        cancel_label: '取消',
        expires_at: '2030-01-01T00:00:00Z',
        requires_code: true,
        code_label: '模拟验证码',
      },
    }
  }

  function pendingPassword(operation = 'change_password') {
    pendingCard()
    const confirmation = server.details.conv_a.pending_interrupt!.confirmation as { operation: string; title: string }
    confirmation.operation = operation
    confirmation.title = '修改银行卡交易密码'
  }

  async function fillPassword(wrapper: VueWrapper) {
    if (wrapper.find('[aria-label="旧交易密码"]').exists()) await wrapper.find('[aria-label="旧交易密码"]').setValue('483719')
    await wrapper.find('[aria-label="新交易密码"], [aria-label="设置交易密码"]').setValue('725903')
    await wrapper.find('[aria-label="确认新交易密码"], [aria-label="确认交易密码"]').setValue('725903')
    await wrapper.find('[aria-label="模拟验证码"]').setValue('572809')
  }

  it('requires a matching ASCII initial PIN and submits no old PIN for application', async () => {
    pendingPassword('apply')
    const wrapper = await open()
    const confirm = () => wrapper.findAll('.interrupt-actions button')[1]
    expect(wrapper.find('[aria-label="旧交易密码"]').exists()).toBe(false)
    expect((confirm().element as HTMLButtonElement).disabled).toBe(true)
    await fillPassword(wrapper)
    await wrapper.find('[aria-label="设置交易密码"]').setValue('１２３４５６')
    expect((confirm().element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('[aria-label="设置交易密码"]').setValue('903176')
    expect((confirm().element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('[aria-label="设置交易密码"]').setValue('725903')
    await confirm().trigger('click')
    await flushPromises()
    const body = server.requests.find(item => item.url.includes('/pending-actions/'))!.body
    expect(body).toMatchObject({ transaction_password: {new_password:'725903',confirm_password:'725903'} })
    expect(JSON.stringify(body)).not.toContain('old_password')
    expect(JSON.stringify(localStorage)).not.toContain('725903')
  })

  it('submits PINs only through the independent confirmation endpoint', async () => {
    pendingPassword()
    const wrapper = await open()
    await fillPassword(wrapper)
    expect(JSON.stringify(localStorage)).not.toContain('483719')
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided).toHaveLength(1)
    expect(decided[0].body).toMatchObject({ transaction_password: { old_password: '483719', new_password: '725903', confirm_password: '725903' } })
    expect(JSON.stringify(server.requests.filter(item => item.url.includes('/messages')))).not.toContain('483719')
    expect(JSON.stringify(localStorage)).not.toContain('725903')
  })

  it('blocks malformed or mismatched PINs before submission', async () => {
    pendingPassword()
    const wrapper = await open()
    await fillPassword(wrapper)
    await wrapper.find('[aria-label="确认新交易密码"]').setValue('903176')
    expect(wrapper.find('[role="alert"]').text()).toContain('两次新密码不一致')
    expect((wrapper.findAll('.interrupt-actions button')[1].element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('[aria-label="确认新交易密码"]').setValue('725903')
    await wrapper.find('[aria-label="新交易密码"]').setValue('12345a')
    expect((wrapper.findAll('.interrupt-actions button')[1].element as HTMLButtonElement).disabled).toBe(true)
    expect(server.requests.some(item => item.url.includes('/pending-actions/'))).toBe(false)
  })

  it.each(['change_password', 'apply'])('clears PIN inputs after refresh while restoring %s', async (operation) => {
    pendingPassword(operation)
    const wrapper = await open()
    await fillPassword(wrapper)
    wrapper.unmount()
    resetSessions()
    resetCache()
    const refreshed = await open()
    expect(refreshed.find('.transaction-password-form').exists()).toBe(true)
    for (const label of operation === 'apply' ? ['设置交易密码', '确认交易密码'] : ['旧交易密码', '新交易密码', '确认新交易密码']) {
      expect((refreshed.find(`[aria-label="${label}"]`).element as HTMLInputElement).value).toBe('')
    }
    expect(JSON.stringify(localStorage)).not.toContain('725903')
  })

  it.each(['change_password', 'apply'])('keeps typed PINs when detail repeats the same %s action', async (operation) => {
    pendingPassword(operation)
    const card = server.details.conv_a.pending_interrupt!
    server.details.conv_a.pending_interrupt = null
    const wrapper = await open()
    server.details.conv_a.pending_interrupt = card
    server.streams.conv_a = [['interrupt', card]]
    const inner = globalThis.fetch
    let releaseDetail!: () => void
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
      if (String(input) === '/api/conversations/conv_a' && (init.method ?? 'GET') === 'GET') {
        await new Promise<void>(resolve => { releaseDetail = resolve })
      }
      return inner(input, init)
    })
    await wrapper.find('.composer textarea').setValue('修改尾号 8801 的银行卡密码')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await fillPassword(wrapper)
    releaseDetail()
    await flushPromises()
    await flushPromises()
    if (operation !== 'apply') expect((wrapper.find('[aria-label="旧交易密码"]').element as HTMLInputElement).value).toBe('483719')
    expect((wrapper.find('[aria-label="新交易密码"], [aria-label="设置交易密码"]').element as HTMLInputElement).value).toBe('725903')
    expect((wrapper.find('[aria-label="模拟验证码"]').element as HTMLInputElement).value).toBe('572809')
    expect(JSON.stringify(localStorage)).not.toContain('725903')
  })

  it('clears PINs after rejected verification and preserves the pending form', async () => {
    pendingPassword()
    server.failures['/pending-actions/'] = { status: 422, code: 'VERIFICATION_FAILED', message: '旧交易密码不正确' }
    const wrapper = await open()
    await fillPassword(wrapper)
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.transaction-password-form').exists()).toBe(true)
    expect((wrapper.find('[aria-label="旧交易密码"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.find('.error-banner').text()).toContain('旧交易密码不正确')
  })

  it('cancels a password action without sending any PIN or code', async () => {
    pendingPassword()
    const wrapper = await open()
    await fillPassword(wrapper)
    await wrapper.findAll('.interrupt-actions button')[0].trigger('click')
    await flushPromises()
    expect(server.requests.find(item => item.url.includes('/pending-actions/'))?.body).toEqual({ question_id: 'q_card', decision: 'cancel' })
  })

  it('sends confirm and the code only to pending-actions decide', async () => {
    pendingCard()
    const wrapper = await open()
    await wrapper.find('.interrupt-card [aria-label="模拟验证码"]').setValue('123456')
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    await flushPromises()

    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided).toHaveLength(1)
    expect(decided[0].url).toContain(`/pending-actions/${actionId}/decide`)
    expect(decided[0].body).toEqual({
      question_id: 'q_card', decision: 'confirm', verification_code: '123456',
    })
    expect(server.requests.some(item => item.url.endsWith('/authorize'))).toBe(false)
    expect(server.requests.some(item => item.method === 'POST' && item.url.includes('/messages'))).toBe(false)
    expect(JSON.stringify(decided)).toContain('123456')
    expect(JSON.stringify(localStorage)).not.toContain('123456')
  })

  it('cancels through decide without a verification code', async () => {
    pendingCard()
    const wrapper = await open()
    await wrapper.findAll('.interrupt-actions button')[0].trigger('click')
    await flushPromises()
    await flushPromises()

    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided).toHaveLength(1)
    expect(decided[0].body).toEqual({ question_id: 'q_card', decision: 'cancel' })
    expect(server.requests.some(item => item.url.endsWith('/authorize'))).toBe(false)
  })
})

describe('a confirmation box', () => {
  function pendingConfirmation(requiresCode = true) {
    server.details.conv_a.pending_interrupt = {
      kind: 'confirmation',
      question_id: 'q_w1',
      agent_id: 'wealth',
      attempt: 1,
      display: '请确认这笔模拟申购：\n- 产品：演示灵活理财\n- 金额：2,000.00 元',
      fields: [{ name: 'decision', label: '操作决定', type: 'select', required: true, options: ['确认', '取消'] }],
      confirmation: {
        pending_id: 'wp_1',
        operation: 'purchase_product',
        title: '确认申购',
        amount_minor: 200000,
        product_name: '演示灵活理财',
        account_display: 'a_wealth_main',
        confirm_label: '确认',
        cancel_label: '取消',
        expires_at: '2030-01-01T00:00:00Z',
        requires_code: requiresCode,
        code_label: '模拟验证码',
      },
    }
  }

  function confirmButton(wrapper: ReturnType<typeof mount>) {
    return wrapper.findAll('.interrupt-actions button')[1]
  }

  function codeInput(wrapper: ReturnType<typeof mount>) {
    return wrapper.find('.interrupt-card [aria-label="模拟验证码"]')
  }

  it('shows the operation, the product, the amount and the account, with two buttons', async () => {
    pendingConfirmation()
    const wrapper = await open()

    const box = wrapper.find('.confirmation')
    expect(box.exists()).toBe(true)
    expect(box.text()).toContain('确认申购')
    expect(box.text()).toContain('演示灵活理财')
    expect(box.text()).toContain('2,000.00 元')
    expect(box.text()).toContain('a_wealth_main')
    expect(box.text()).toContain('有效期至')
    const labels = wrapper.findAll('.interrupt-actions button').map(button => button.text())
    expect(labels).toEqual(['取消', '确认'])
  })

  it('never asks for a verification code when the card did not ask for one', async () => {
    pendingConfirmation(false)
    const wrapper = await open()

    expect(wrapper.find('.interrupt-card [aria-label="模拟验证码"]').exists()).toBe(false)
    expect(wrapper.find('.interrupt-card').text()).not.toContain('验证码')
  })

  it('submits confirm as a structured button event, not as chat text', async () => {
    pendingConfirmation()
    const wrapper = await open()
    await codeInput(wrapper).setValue('123456')
    await confirmButton(wrapper).trigger('click')
    await flushPromises()
    await flushPromises()

    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided).toHaveLength(1)
    expect(decided[0].url).toContain('/conversations/conv_a/pending-actions/wp_1/decide')
    expect(decided[0].body).toEqual({
      question_id: 'q_w1', decision: 'confirm', verification_code: '123456',
    })
    // The click is not a message: no POST went to the message route. (The ledger re-read
    // that follows the turn is a GET on that same path, so the method is what tells them
    // apart.)
    expect(server.requests.some(item => item.method === 'POST' && item.url.includes('/messages'))).toBe(false)
    // And the answer is in the ledger, under the conversation it belongs to.
    expect(shownText(wrapper)).toContain('确认本次操作')
    expect(shownText(wrapper)).toContain('已按您的确认提交。')
  })

  it('renders the code input only when the bank asked for one', async () => {
    // The card takes the form the operation needs: a code input appears exactly when the
    // server's projection says the bank will demand one, and never otherwise.
    pendingConfirmation(false)
    const wrapper = await open()

    expect(wrapper.find('.confirmation').exists()).toBe(true)
    expect(codeInput(wrapper).exists()).toBe(false)
    expect((confirmButton(wrapper).element as HTMLButtonElement).disabled).toBe(false)
  })

  it('will not confirm until the code is typed, and sends what was typed', async () => {
    pendingConfirmation()
    const wrapper = await open()

    expect(codeInput(wrapper).exists()).toBe(true)
    // Disabled while the box is empty: the route would refuse the click, and a button that
    // fails after the fact is worse than one that explains itself.
    expect((confirmButton(wrapper).element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper.find('.composer-bottom').text()).toContain('请先在上方填写模拟验证码并确认')

    await codeInput(wrapper).setValue('   ')
    expect((confirmButton(wrapper).element as HTMLButtonElement).disabled).toBe(true)

    await codeInput(wrapper).setValue('246810')
    expect((confirmButton(wrapper).element as HTMLButtonElement).disabled).toBe(false)

    await confirmButton(wrapper).trigger('click')
    await flushPromises()
    await flushPromises()

    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided[0].body).toMatchObject({ decision: 'confirm', verification_code: '246810' })
    // The code is not written anywhere: it is not in the stored request, and the page does
    // not keep it after the click.
    expect(JSON.stringify(localStorage)).not.toContain('246810')
  })

  it('cancels without asking for or sending a code', async () => {
    pendingConfirmation()
    const wrapper = await open()
    await wrapper.findAll('.interrupt-actions button')[0].trigger('click')
    await flushPromises()
    await flushPromises()

    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided[0].body).toEqual({ question_id: 'q_w1', decision: 'cancel' })
  })

  it('submits cancel the same way, and never as a confirmation', async () => {
    pendingConfirmation(false)
    const wrapper = await open()
    await wrapper.findAll('.interrupt-actions button')[0].trigger('click')
    await flushPromises()
    await flushPromises()

    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided).toHaveLength(1)
    expect(decided[0].body).toEqual({ question_id: 'q_w1', decision: 'cancel' })
    expect(shownText(wrapper)).toContain('取消本次操作')
  })

  it('keeps the box open so a rejected code can be retyped', async () => {
    // A wrong code is not a refusal of the click: the module re-offers the same pending, so
    // the box comes back asking for the code again. What must not happen is the retry being
    // deduplicated against the first attempt -- the frontend mints a fresh key per click,
    // because the click looks identical from the outside while the code is different.
    pendingConfirmation()
    const reopened = {
      ...(server.details.conv_a.pending_interrupt as Record<string, unknown>),
      question_id: 'q_w2',
    }
    server.decisions.conv_a = [
      ['agent_start', { agent_id: 'wealth' }],
      ['subtask_end', {
        agent_id: 'wealth', status: 'SUCCEEDED',
        summary: '验证码不正确。请重新输入验证码后再确认。', turn_status: 'waiting_confirmation',
      }],
      ['interrupt', reopened],
    ]
    const wrapper = await open()
    await codeInput(wrapper).setValue('000000')
    await confirmButton(wrapper).trigger('click')
    await flushPromises()
    await flushPromises()

    // The box is back, and it asks for the code again rather than staying filled in.
    expect(wrapper.find('.confirmation').exists()).toBe(true)
    expect(codeInput(wrapper).exists()).toBe(true)
    expect((confirmButton(wrapper).element as HTMLButtonElement).disabled).toBe(true)

    await codeInput(wrapper).setValue('246810')
    await confirmButton(wrapper).trigger('click')
    await flushPromises()
    await flushPromises()

    const decided = server.requests.filter(item => item.url.includes('/pending-actions/'))
    expect(decided.map(item => (item.body as { verification_code?: string }).verification_code))
      .toEqual(['000000', '246810'])
    // Different clicks, different keys: the retry is a new submission, not a replay of the
    // first one. (The server refuses a retry that reuses the key it already spent.)
    expect(decided[0].key).not.toBe(decided[1].key)
  })

  it('locks the composer while the box is open', async () => {
    pendingConfirmation()
    const wrapper = await open()

    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).disabled).toBe(true)
  })

  it('re-reads the ledger and the open card when the server refuses the click', async () => {
    pendingConfirmation(false)
    server.failures['/pending-actions/'] = {
      status: 409, code: 'INVALID_INTERRUPT_ANSWER', message: '确认没有指向当前待确认的操作',
    }
    const wrapper = await open()
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()

    expect(wrapper.find('.error-banner').text()).toContain('确认没有指向当前待确认的操作')
    // The refusal is a verdict about this click, not about the conversation: the card the
    // server still confirms is shown again rather than dropped.
    expect(wrapper.find('.confirmation').exists()).toBe(true)
  })

  it('keeps the card when the server still names the same question it refused', async () => {
    // The click is refused as a stale answer while the server's own projection still reports
    // the very same question as open. The refusal says nothing about that card, so dropping
    // it would hide a pause the server has just said is still there: the re-read, not the
    // 409, is what decides the card's fate.
    pendingConfirmation(false)
    server.failures['/pending-actions/'] = {
      status: 409, code: 'INVALID_INTERRUPT_ANSWER', message: '确认没有指向当前待确认的操作',
    }
    const wrapper = await open()
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.error-banner').text()).toContain('确认没有指向当前待确认的操作')
    expect(wrapper.find('.confirmation').exists()).toBe(true)
    expect(wrapper.find('.confirmation-title').text()).toBe('确认申购')
  })

  it('drops the card only once the server says that question is no longer open', async () => {
    // The card is really on screen when the click goes out -- otherwise the refusal would be
    // answering a click nobody could have made. What changed by the time it lands is the
    // server's own projection: the pause it names is gone, so the re-read reports none.
    pendingConfirmation(false)
    const wrapper = await open()
    expect(wrapper.find('.confirmation').exists()).toBe(true)

    server.failures['/pending-actions/'] = {
      status: 409, code: 'INVALID_INTERRUPT_ANSWER', message: '确认没有指向当前待确认的操作',
    }
    server.details.conv_a.pending_interrupt = null
    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.error-banner').text()).toContain('确认没有指向当前待确认的操作')
    // The server's answer is the verdict: the pause is consumed, so the card goes and stays
    // gone instead of being restored from the page's own memory.
    expect(wrapper.find('.confirmation').exists()).toBe(false)
    expect(wrapper.find('.interrupt-card').exists()).toBe(false)
  })
})

describe('the reply is rendered as Markdown', () => {
  it('renders headings, lists, bold and links instead of their source', async () => {
    server.messages.conv_a = [
      message({ message_id: 'a1', sequence: 1, content: '有哪些产品？' }),
      message({
        message_id: 'a2', sequence: 2, role: 'assistant',
        content: '## 推荐\n\n- **稳健理财**\n- [灵活理财](https://example.com/flex)',
      }),
    ]
    const wrapper = await open()

    const reply = wrapper.find('.chat-message.assistant .markdown-body')
    expect(reply.find('h2').text()).toBe('推荐')
    expect(reply.findAll('li')).toHaveLength(2)
    expect(reply.find('strong').text()).toBe('稳健理财')
    const link = reply.find('a')
    expect(link.attributes('href')).toBe('https://example.com/flex')
    expect(link.attributes('rel')).toBe('noopener noreferrer nofollow')
    // The source is gone: the page shows the rendered text, not the Markdown.
    expect(reply.text()).not.toContain('**')
    expect(reply.text()).not.toContain('##')
  })

  it('renders a table and a code block', async () => {
    server.messages.conv_a = [
      message({ message_id: 'a1', sequence: 1, content: '对比一下' }),
      message({
        message_id: 'a2', sequence: 2, role: 'assistant',
        content: '| 产品 | 收益 |\n| --- | --- |\n| 稳健理财 | 2.5% |\n\n```\n{"tool":"wealth_products"}\n```',
      }),
    ]
    const wrapper = await open()

    const reply = wrapper.find('.chat-message.assistant .markdown-body')
    expect(reply.findAll('table th').map(cell => cell.text())).toEqual(['产品', '收益'])
    expect(reply.findAll('table td').map(cell => cell.text())).toEqual(['稳健理财', '2.5%'])
    expect(reply.find('pre code').text()).toBe('{"tool":"wealth_products"}')
  })

  it('renders the reply while the turn is still streaming', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    // An empty ledger, so the only reply on screen is the one still arriving.
    server.messages.conv_b = []
    server.openStreams.conv_b = [['final', { text: '## 已办完\n\n- 转账 100 元', business_ids: [] }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('转账')
    await wrapper.find('.composer').trigger('submit')
    // Three ticks: the submit call, the response's first chunk, and the re-render it causes.
    await flushPromises()
    await flushPromises()
    await flushPromises()

    // The response is still open, so this is the in-flight turn -- and it is rendered as
    // Markdown, not as its source.
    const live = wrapper.find('.chat-message.assistant .markdown-body')
    expect(live.find('h2').text()).toBe('已办完')
    expect(live.findAll('li').map(item => item.text())).toEqual(['转账 100 元'])
    expect(live.text()).not.toContain('##')
  })

  it('shows raw HTML in a reply as text and never executes it', async () => {
    server.messages.conv_a = [
      message({ message_id: 'a1', sequence: 1, content: '查一下' }),
      message({
        message_id: 'a2', sequence: 2, role: 'assistant',
        content: '<img src=x onerror="alert(1)">\n\n<script>alert(2)</script>',
      }),
    ]
    const wrapper = await open()

    const reply = wrapper.find('.chat-message.assistant .markdown-body')
    expect(reply.find('img').exists()).toBe(false)
    expect(reply.find('script').exists()).toBe(false)
    expect(reply.text()).toContain('<img src=x onerror="alert(1)">')
  })
})

describe('the composer keyboard', () => {
  /** A keydown as the browser would deliver it, with the fields the handler reads. */
  function press(init: KeyboardEventInit) {
    return new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ...init })
  }

  it('sends on Enter', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [['final', { text: '收到', business_ids: [] }]]
    const wrapper = await open()
    const textarea = wrapper.find('.composer textarea')
    await textarea.setValue('帮我转账')

    await textarea.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    const submitted = server.requests.filter(item => item.method === 'POST' && item.url.includes('/messages'))
    expect(submitted).toHaveLength(1)
    expect(submitted[0].body).toMatchObject({ message: '帮我转账' })
  })

  it('starts a new line on Shift+Enter instead of sending', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    const wrapper = await open()
    const textarea = wrapper.find('.composer textarea')
    await textarea.setValue('第一行')

    await textarea.trigger('keydown', { key: 'Enter', shiftKey: true })
    await flushPromises()

    expect(server.requests.some(item => item.method === 'POST' && item.url.includes('/messages'))).toBe(false)
    // The keystroke is left to the textarea, which is what inserts the line break.
    expect((textarea.element as HTMLTextAreaElement).value).toBe('第一行')
  })

  it('does not send on the Enter that commits an input-method candidate', async () => {
    // Every message here is typed through an IME, and Enter is how a candidate is picked.
    // Sending there would submit a half-finished pinyin string.
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [['final', { text: '收到', business_ids: [] }]]
    const wrapper = await open()
    const textarea = wrapper.find('.composer textarea')
    await textarea.setValue('zhuan')

    textarea.element.dispatchEvent(press({ isComposing: true }))
    await flushPromises()
    expect(server.requests.some(item => item.method === 'POST' && item.url.includes('/messages'))).toBe(false)

    // The very same keystroke, without the composition flag, does send: what stops the one
    // above is the flag and not some unrelated reason the request never happened.
    textarea.element.dispatchEvent(press({}))
    await flushPromises()
    expect(server.requests.some(item => item.method === 'POST' && item.url.includes('/messages'))).toBe(true)
  })

  it('does not send on Enter when the composer is disabled', async () => {
    // Conversation A has a confirmed question open, so the composer accepts nothing.
    const wrapper = await open()
    const textarea = wrapper.find('.composer textarea')
    expect((textarea.element as HTMLTextAreaElement).disabled).toBe(true)

    await textarea.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(server.requests.some(item => item.method === 'POST' && item.url.includes('/messages'))).toBe(false)
  })
})

describe('history paging', () => {
  it('offers the earlier page only when the server says there is one', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    const wrapper = await open()

    expect(wrapper.find('.history-more').exists()).toBe(false)

    // Three messages, so the server reports a cursor for conversation A.
    server.messages.conv_a.push(
      message({ message_id: 'a3', sequence: 3, content: '甲的第二问' }),
    )
    await rows(wrapper)[0].find('.conversation-row').trigger('click')
    await flushPromises()

    expect(wrapper.find('.history-more').exists()).toBe(true)
  })

  it('requests the earlier page with the server cursor and merges it', async () => {
    server.messages.conv_a.push(
      message({ message_id: 'a3', sequence: 3, content: '甲的第二问' }),
    )
    server.earlier.conv_a = [
      message({ message_id: 'a1', sequence: 1, content: '甲的问题' }),
    ]
    const wrapper = await open()

    await wrapper.find('.history-more button').trigger('click')
    await flushPromises()

    const paged = server.requests.filter(item => item.url.includes('after='))
    expect(paged).toHaveLength(1)
    expect(paged[0].url).toContain('after=2')
    // Merged by message id, so the page that came back is not rendered twice.
    const text = shownText(wrapper)
    expect(text.match(/甲的问题/g)).toHaveLength(1)
    expect(wrapper.find('.history-more').exists()).toBe(false)
  })
})

describe('soft delete and restore', () => {
  it('marks a deleted conversation and restores it from the same row', async () => {
    const wrapper = await open()

    await rows(wrapper)[0].find('[aria-label="删除会话"]').trigger('click')
    await flushPromises()

    expect(server.conversations[0].deleted).toBe(true)
    expect(rows(wrapper)[0].text()).toContain('已删除')

    await rows(wrapper)[0].find('[aria-label="恢复会话"]').trigger('click')
    await flushPromises()

    expect(server.conversations[0].deleted).toBe(false)
    expect(rows(wrapper)[0].text()).not.toContain('已删除')
    // The ledger was never dropped by the soft delete.
    expect(shownText(wrapper)).toContain('甲的问题')
  })

  it('disables the composer for a conversation that is not writable', async () => {
    server.conversations[0].archived = true
    server.details.conv_a.conversation.archived = true
    const wrapper = await open()

    expect((wrapper.find('.composer textarea').element as HTMLTextAreaElement).disabled).toBe(true)
    expect(wrapper.find('.composer-bottom').text()).toContain('取消归档后才能继续发送')
  })

  it('refuses the answer too, because an archived conversation accepts nothing', async () => {
    // The card is still shown -- the question is part of what happened -- but the answer
    // cannot be submitted, so the hint says why instead of telling the user to fill it in.
    server.conversations[0].archived = true
    server.details.conv_a.conversation.archived = true
    const wrapper = await open()

    expect(wrapper.find('.interrupt-card').exists()).toBe(true)
    const submit = wrapper.find('.interrupt-card button[type="submit"]')
    expect((submit.element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper.find('.composer-bottom').text()).toContain('取消归档后才能继续发送')
    expect(wrapper.find('.composer-bottom').text()).not.toContain('请先在上方补充信息')
  })
})

describe('a JSON receipt', () => {
  it('refreshes the ledger and reports the result as unconfirmed', async () => {
    // Conversation B has nothing open, so the composer is live and a message can be sent.
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    const wrapper = await open()
    // The message is already stored; the receipt says so and runs nothing.
    server.messages.conv_b.push(
      message({ message_id: 'b3', sequence: 3, content: '已提交的问题' }),
    )
    await wrapper.find('.composer textarea').setValue('已提交的问题')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    const text = shownText(wrapper)
    expect(text).toContain('已提交的问题')
    expect(text).toContain('结果未确认')
    // The stored request is gone: a settled receipt is not something to repeat.
    expect(loadPending('conv_b')).toBeNull()
  })
})

describe('a first submission', () => {
  it('streams the turn and clears the stored request on final', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [
      ['agent_start', { agent_id: 'transfer' }],
      ['final', { text: '已经完成', business_ids: ['T-1'] }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    const submitted = server.requests.filter(item => item.method === 'POST' && item.url.includes('/messages'))
    expect(submitted).toHaveLength(1)
    expect(submitted[0].key).toBeTruthy()
    expect(submitted[0].body).toMatchObject({ message: '帮我转账' })
    // The reply is on screen, and the request is no longer pending: it settled.
    expect(shownText(wrapper)).toContain('已经完成')
    expect(shownText(wrapper)).not.toContain('结果未确认')
    expect(loadPending('conv_b')).toBeNull()
  })

  it('stops an ordinary turn and says so without an error banner', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.openStreams.conv_b = [
      ['agent_start', { agent_id: 'transfer' }],
      ['reply_delta', { text: '正在为你查' }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    const stop = wrapper.find('[data-test="stop-turn"]')
    expect(stop.exists()).toBe(true)
    await stop.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-test="stop-turn"]').exists()).toBe(false)
    expect(shownText(wrapper)).toContain('已停止本次任务。')
    expect(wrapper.find('.error-banner').exists()).toBe(false)
  })

  it('keeps the text that arrived when the stream breaks before final', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    // The stream dies after text has already reached the page. Throwing it away would replace
    // something the user has read with nothing.
    server.streams.conv_b = [
      ['agent_start', { agent_id: 'transfer' }],
      ['reply_delta', { text: '正在为你' }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    expect(shownText(wrapper)).toContain('正在为你')
    expect(shownText(wrapper)).toContain('结果未确认')
  })

  it('replaces the streamed draft with the complete text of final', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    // The fixture seeds conv_b with an older turn; this case counts assistant bubbles, so it
    // starts from an empty ledger and the only bubble on screen is this turn's.
    server.messages.conv_b = []
    server.streams.conv_b = [
      ['reply_delta', { text: '已经' }],
      ['reply_delta', { text: '完成' }],
      ['final', { text: '已经完成。', business_ids: ['T-1'] }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    // One bubble, the ledger's: the draft is gone rather than shown twice.
    const bubbles = wrapper.findAll('.chat-message.assistant')
    expect(bubbles).toHaveLength(1)
    expect(bubbles[0].text()).toContain('已经完成。')
    expect(shownText(wrapper)).not.toContain('结果未确认')
  })

  it('moves a narration into the activity feed and out of the reply', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    // The fixture seeds conv_b with an older reply; this case asserts no assistant bubble at
    // all, so it starts from an empty ledger and only this turn is on screen.
    server.messages.conv_b = []
    // The model said what it was about to do and then delegated: that sentence is activity,
    // not the answer, and the answer never arrives in this stub.
    server.streams.conv_b = [
      ['reply_delta', { text: '我看看持仓' }],
      ['narration', { text: '我看看持仓' }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我看看持仓')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.activity').text()).toContain('我看看持仓')
    expect(wrapper.find('.chat-message.assistant').exists()).toBe(false)
  })

  it('names why a turn ended when the server says so', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [
      ['reply_delta', { text: '部分回答' }],
      ['final', { text: '部分回答', business_ids: [], terminal_reason: 'api_error', error_status: 500 }],
    ]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    // The notice survives the ledger re-read that clears the live turn.
    expect(shownText(wrapper)).toContain('回复未完整生成')
  })

  it('shows no notice for a turn that completed', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [['final', { text: '已经完成', business_ids: [] }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.reply-notice').exists()).toBe(false)
  })

  it('reuses the stored key for a byte-identical repeat and never for a new message', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    // The stream stops with no final and no interrupt: the request is unfinished, so the
    // record survives and the next identical send has to carry the same key.
    server.streams.conv_b = [['agent_start', { agent_id: 'transfer' }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('同一句话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    const stored = loadPending('conv_b')!
    expect(stored).toMatchObject({ message: '同一句话' })
    expect(stored.key).toBeTruthy()

    await wrapper.find('.composer textarea').setValue('同一句话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await wrapper.find('.composer textarea').setValue('换一句话')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    const keys = server.requests
      .filter(item => item.method === 'POST' && item.url.includes('/messages'))
      .map(item => item.key)
    expect(keys).toHaveLength(3)
    expect(keys[1]).toBe(keys[0])
    expect(keys[2]).not.toBe(keys[0])
  })
})

/**
 * The naming call is the response's background work, so the list read that ends a turn happens
 * before the server has written the title. These cases pin the bounded poll that closes that
 * window: it stops the moment the title moves, and it gives up instead of repeating forever.
 */
describe('the automatic title', () => {
  // Only the poll's own timer is faked: the rest of the clock keeps running, so a test only
  // has to advance the poll's one second. Faking `setImmediate` as well would in fact change
  // nothing here -- @vue/test-utils captured the real one at import time
  // (vue-test-utils.cjs.js:8567, and `flushPromises` on :8569-8573 schedules through it),
  // while this file's own import on line 14 runs before this `beforeEach` -- so no
  // `await flushPromises()` below waits on a clock nobody advances. (Measured: a full
  // `vi.useFakeTimers()` also leaves this file green.)
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  /** Reads of the list endpoint, which is the only thing the poll asks for again. */
  function listReads(): number {
    return server.requests.filter(
      item => item.method === 'GET' && /^\/api\/conversations(\?|$)/.test(item.url),
    ).length
  }

  /**
   * Opens B, streams one answered turn, and leaves the server still holding the default title:
   * exactly the window the background naming opens.
   */
  async function submitOneTurn() {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.conversations[1].title = '新会话'
    server.details.conv_b.conversation.title = '新会话'
    server.streams.conv_b = [['final', { text: '已经完成', business_ids: [] }]]
    const wrapper = await open()
    await wrapper.find('.composer textarea').setValue('帮我转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()
    return wrapper
  }

  it('polls the list until the title the server wrote later arrives', async () => {
    const wrapper = await submitOneTurn()

    // The answer is on screen, but the list read that ended the turn ran before the server
    // wrote the title, so the sidebar still shows the default.
    expect(shownText(wrapper)).toContain('已经完成')
    expect(rows(wrapper)[1].text()).toContain('新会话')

    // The background naming finishes after the response, so the next read has the new title.
    server.conversations[1].title = '转账给张三'
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(rows(wrapper)[1].text()).toContain('转账给张三')
  })

  it('stops polling once the title has moved', async () => {
    const wrapper = await submitOneTurn()

    server.conversations[1].title = '转账给张三'
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(rows(wrapper)[1].text()).toContain('转账给张三')

    // The title is no longer the default, so the poll is done: more clock time, no more reads.
    const settled = listReads()
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(listReads()).toBe(settled)
  })

  it('gives up after a bounded number of attempts when the title never moves', async () => {
    await submitOneTurn()
    const baseline = listReads()

    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()

    // It really did poll -- the turn's own refresh is not the whole story -- and it stayed
    // inside its attempt cap instead of reading the list forever.
    expect(listReads()).toBeGreaterThan(baseline)
    expect(listReads()).toBeLessThanOrEqual(baseline + 5)

    // And it is over: the title never moved, so nothing keeps asking.
    const after = listReads()
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(listReads()).toBe(after)
  })
})

describe('登录', () => {
  it('未登录时只显示登录框，不泄露工作台的任何一部分', async () => {
    server.hasSession = false
    const wrapper = await open()

    expect(wrapper.find('.auth-card').exists()).toBe(true)
    expect(wrapper.find('.workspace').exists()).toBe(false)
    expect(rows(wrapper)).toHaveLength(0)
  })

  it('登录成功后直接进入工作台，不需要刷新页面', async () => {
    server.hasSession = false
    const wrapper = await open()

    await wrapper.find('input[name="login_name"]').setValue('demo')
    await wrapper.find('input[name="password"]').setValue('a long enough password')
    await wrapper.find('form.auth-form').trigger('submit')
    for (let i = 0; i < 4; i += 1) await flushPromises()

    // 登录态一到，侧栏就该有内容：它是页面在等的东西，不是刷新之后才有的东西。
    expect(wrapper.find('.workspace').exists()).toBe(true)
    expect(rows(wrapper)).toHaveLength(2)
    expect(shownText(wrapper)).toContain('甲的问题')
  })
})

describe('身份同步：另一个标签页与重新聚焦', () => {
  function bus(): FakeBroadcastChannel {
    const found = FakeBroadcastChannel.all[FakeBroadcastChannel.all.length - 1]
    if (!found) throw new Error('本页没有订阅同源广播')
    return found
  }

  it('另一个标签页退出后，本页立刻回到登录页，而不是等下一次请求被拒', async () => {
    const wrapper = await open()
    expect(wrapper.find('.workspace').exists()).toBe(true)

    bus().deliver({ reason: 'logout' })
    await flushPromises()

    // 用户以为自己已经退出了，另一个窗口却还开着 —— 这正是设计 §7.3 要消掉的状态。
    expect(wrapper.find('.workspace').exists()).toBe(false)
    expect(wrapper.find('.auth-card').exists()).toBe(true)
    expect(wrapper.find('.auth-toast').text()).toContain('已退出登录')
  })

  it('被别处退出时给的是「过期」那句话，不是「你自己退的」', async () => {
    const wrapper = await open()

    bus().deliver({ reason: 'expired' })
    await flushPromises()

    expect(wrapper.find('.auth-toast').text()).toContain('登录已过期')
  })

  it('收到广播后不再把这条消息广播回去', async () => {
    await open()
    const channel = bus()

    channel.deliver({ reason: 'logout' })
    await flushPromises()

    // 两个标签页互相回响会把「清场」变成一条无限循环的消息。
    expect(channel.posted).toEqual([])
  })

  it('本页自己退出时才广播，让别的标签页跟上', async () => {
    const wrapper = await open()
    const channel = bus()

    await wrapper.find('[aria-label="退出登录"]').trigger('click')
    await flushPromises()

    expect(channel.posted).toEqual([{ reason: 'logout' }])
  })

  it('窗口重新获得焦点时复核身份，服务端说已失效就回登录页', async () => {
    const wrapper = await open()
    expect(wrapper.find('.workspace').exists()).toBe(true)

    // 会话在别处被撤销了：本页没有收到广播（标签页休眠、浏览器没有 BroadcastChannel），
    // 唯一能发现它的时机就是重新拿到焦点。
    server.hasSession = false
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()

    expect(wrapper.find('.workspace').exists()).toBe(false)
    expect(wrapper.find('.auth-toast').text()).toContain('登录已过期')
  })

  it('重新聚焦发现浏览器里换了个人，也把这一页收干净', async () => {
    const wrapper = await open()
    expect(shownText(wrapper)).toContain('甲的问题')

    server.meUserId = 'user_0002'
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()

    expect(wrapper.find('.workspace').exists()).toBe(false)
    expect(wrapper.find('.auth-toast').text()).toContain('登录身份已变化')
  })

  it('重新聚焦时后端不可达不算被退出', async () => {
    const wrapper = await open()

    // 网络抖动不是撤权：把用户踢回登录页只会让他重输一遍密码，然后再被踢一次。
    server.meStatus = 500
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()

    expect(wrapper.find('.workspace').exists()).toBe(true)
  })
})

describe('账号设置', () => {
  async function openSettings(wrapper: VueWrapper) {
    await wrapper.find('[aria-label="设置与账号"]').trigger('click')
    for (let i = 0; i < 4; i += 1) await flushPromises()
  }

  it('账号区显示登录名、角色和银行数据是否就绪', async () => {
    const wrapper = await open()
    await openSettings(wrapper)

    const account = wrapper.find('.settings-account')
    expect(account.exists()).toBe(true)
    expect(account.text()).toContain('demo')
    expect(account.text()).toContain('管理员')
    expect(account.text()).toContain('已就绪')
  })

  it('普通用户也能看到自己的账号区（模型配置那一块才按角色隐藏）', async () => {
    // 账号与邮箱是每个人自己的：以前整个设置入口只给管理员，普通用户连密码都改不了。
    server.meRole = 'user'
    const wrapper = await open()
    await openSettings(wrapper)

    expect(wrapper.find('.settings-account').exists()).toBe(true)
    expect(wrapper.find('.account-form').exists()).toBe(true)
    // 全局模型配置仍然只有管理员能动（后端也会 403）。
    expect(wrapper.text()).not.toContain('模型')
  })

  it('两次新密码不一致时不会发出请求', async () => {
    const wrapper = await open()
    await openSettings(wrapper)

    const inputs = wrapper.findAll('.account-form input')
    await inputs[0].setValue('current-password-1')
    await inputs[1].setValue('next-password-1')
    await inputs[2].setValue('next-password-2')
    await wrapper.find('.account-form').trigger('submit')
    await flushPromises()

    expect(server.requests.some(item => item.url.includes('/auth/password/change'))).toBe(false)
    expect(wrapper.find('.settings-message.error').text()).toContain('不一致')
  })

  it('改密成功后回到登录页，并说明要重新登录', async () => {
    const wrapper = await open()
    await openSettings(wrapper)

    const inputs = wrapper.findAll('.account-form input')
    await inputs[0].setValue('current-password-1')
    await inputs[1].setValue('next-password-1')
    await inputs[2].setValue('next-password-1')
    await wrapper.find('.account-form').trigger('submit')
    await flushPromises()
    await flushPromises()

    const sent = server.requests.find(item => item.url.includes('/auth/password/change'))
    expect(sent?.body).toEqual({
      current_password: 'current-password-1',
      new_password: 'next-password-1',
    })
    // 改密会撤销所有登录态（包括这一台），所以它不是「保存成功」而是一次登出。
    expect(wrapper.find('.workspace').exists()).toBe(false)
    expect(wrapper.find('.auth-toast').text()).toContain('密码已修改')
  })

  it('退出全部设备后同样回到登录页', async () => {
    const wrapper = await open()
    await openSettings(wrapper)

    await wrapper.find('.account-actions .danger').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(server.requests.some(item => item.url.includes('/auth/logout-all'))).toBe(true)
    expect(wrapper.find('.workspace').exists()).toBe(false)
    expect(wrapper.find('.auth-toast').text()).toContain('已退出全部设备')
  })
})

describe('the timestamps', () => {
  const DAY = /今天|昨天|\d{2}-\d{2}|\d{4}-\d{2}-\d{2}/
  const CLOCK = /\d{2}:\d{2}/

  it('stamps a conversation row with the day of its latest message', async () => {
    const wrapper = await open()

    expect(rows(wrapper)[0].find('.conversation-stamp').text()).toMatch(DAY)
    expect(rows(wrapper)[1].find('.conversation-stamp').text()).toMatch(DAY)
  })

  it('falls back to the day a conversation was created when nothing was written in it', async () => {
    server.conversations[0] = { ...server.conversations[0], last_message_at: null }
    const wrapper = await open()

    expect(rows(wrapper)[0].find('.conversation-stamp').text()).toMatch(DAY)
  })

  it('stamps each message with the time it was written', async () => {
    const wrapper = await open()

    const times = wrapper.findAll('.conversation .message-time').map(node => node.text())
    // The last-used conversation is A: its question and its answer.
    expect(times).toHaveLength(2)
    for (const time of times) expect(time).toMatch(CLOCK)
  })

  it('stamps the message being sent before the server has answered', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.openStreams.conv_b = [['agent_start', { agent_id: 'transfer' }]]
    const wrapper = await open()

    await wrapper.find('.composer textarea').setValue('再问一句')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()

    // The turn still running is the last one on the page, and it carries the reader's own
    // clock: today, so no day in front of it.
    const live = wrapper.findAll('.chat-turn').at(-1)!.find('.chat-message.user .message-time')
    expect(live.exists()).toBe(true)
    expect(live.text()).toMatch(/^\d{2}:\d{2}$/)
  })
})
describe('the task graph in the stream', () => {
  // The drawer is mounted only in the demo, the store is not: what these cases read is exactly
  // what the two onEvent handlers applied, whichever tab the reader is on.
  const PLAN = 'plan_demo_1'

  function node(id: string, status: TaskNode['status'] = 'pending'): TaskNode {
    return { id, label: id + ' 的步骤', owner: 'transfer', depends_on: [], status }
  }

  function graph(): string[][] {
    return demoStore.taskNodes.map(item => [item.id, item.status])
  }

  function pendingTransfer() {
    server.details.conv_a.pending_interrupt = {
      kind: 'confirmation',
      question_id: 'q_graph',
      agent_id: 'transfer',
      attempt: 1,
      display: '请确认这笔模拟转账：\n- 金额：1,000.00 元',
      fields: [{ name: 'decision', label: '操作决定', type: 'select', required: true, options: ['确认', '取消'] }],
      confirmation: {
        pending_id: 'tp_graph',
        operation: 'transfer',
        title: '确认转账',
        amount_minor: 100000,
        product_name: '',
        account_display: 'a_main',
        confirm_label: '确认',
        cancel_label: '取消',
        expires_at: '2030-01-01T00:00:00Z',
        requires_code: false,
        code_label: '模拟验证码',
      },
    }
  }

  /**
   * Holds the create request of a new conversation, so a test can look at the page *while* the
   * round-trip is still open. Only POST /api/conversations is held; everything else flows.
   */
  function holdCreate() {
    const waiting: Array<() => void> = []
    let reached = 0
    let released = false
    const inner = globalThis.fetch
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const method = (init.method ?? 'GET').toUpperCase()
      if (!released && method === 'POST' && String(input) === '/api/conversations') {
        reached += 1
        await new Promise<void>(resolve => waiting.push(resolve))
      }
      return inner(input, init)
    })
    return {
      started: () => reached,
      release: () => {
        released = true
        while (waiting.length) waiting.shift()!()
      },
    }
  }

  it('applies a plan and its node update from the submission stream', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [
      ['plan', { plan_id: PLAN, nodes: [node('node_1'), node('node_2')] }],
      ['task_update', { plan_id: PLAN, node_id: 'node_1', status: 'running' }],
      ['final', { text: '已经排好两步', business_ids: [] }],
    ]
    const wrapper = await open()

    await wrapper.find('.composer textarea').setValue('查账单再转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    expect(demoStore.planId).toBe(PLAN)
    expect(graph()).toEqual([['node_1', 'running'], ['node_2', 'pending']])
  })

  it('leaves the plan and task_update frames out of the activity feed', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    // The stream stays open: the live turn -- and with it the activity list -- is on screen,
    // which is the state a leaked frame shows up in as an empty row.
    server.openStreams.conv_b = [
      ['agent_start', { agent_id: 'transfer' }],
      ['plan', { plan_id: PLAN, nodes: [node('node_1'), node('node_2')] }],
      ['task_update', { plan_id: PLAN, node_id: 'node_1', status: 'running' }],
    ]
    const wrapper = await open()

    await wrapper.find('.composer textarea').setValue('查账单再转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    // The frames did arrive -- otherwise 'no row' would only mean 'no frames'.
    expect(graph()).toEqual([['node_1', 'running'], ['node_2', 'pending']])
    const activity = wrapper.findAll('.live-turn .activity-item')
    expect(activity.map(item => item.text())).toEqual(['transfer Agent 处理中'])
  })

  it('replaces the graph for a plan frame that carries no plan_id', async () => {
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [
      ['plan', { nodes: [node('node_1', 'running'), node('node_2')] }],
      ['plan', { nodes: [node('node_1'), node('node_3')] }],
      ['final', { text: '已经重排', business_ids: [] }],
    ]
    const wrapper = await open()

    await wrapper.find('.composer textarea').setValue('换个排法')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    // No plan_id means 'this is the graph now', not 'merge into the last one': node_1 is back
    // to pending and node_2 is gone.
    expect(demoStore.planId).toBe('')
    expect(graph()).toEqual([['node_1', 'pending'], ['node_3', 'pending']])
  })

  it('applies a task_update from the decision stream and adds no activity row', async () => {
    pendingTransfer()
    server.decisions.conv_a = [
      ['plan', { plan_id: PLAN, nodes: [node('node_1'), node('node_2')] }],
      ['task_update', { plan_id: PLAN, node_id: 'node_1', status: 'done', artifact: { transfer_id: 't_1' } }],
    ]
    const wrapper = await open()

    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    await flushPromises()

    expect(demoStore.planId).toBe(PLAN)
    expect(graph()).toEqual([['node_1', 'done'], ['node_2', 'pending']])
    expect(demoStore.taskNodes[0].artifact).toEqual({ transfer_id: 't_1' })
    // No final frame: the turn is still on screen, and the two frames left it no rows.
    expect(wrapper.find('.live-turn').exists()).toBe(true)
    expect(wrapper.findAll('.live-turn .activity-item')).toHaveLength(0)
  })

  it('clears the graph when another conversation is opened', async () => {
    const wrapper = await open()
    demoStore.setPlan(PLAN, [node('node_1', 'done')])

    await rows(wrapper)[1].find('.conversation-row').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(demoStore.taskNodes).toEqual([])
    expect(demoStore.planId).toBe('')
  })

  it('clears the graph when a new conversation is started', async () => {
    const wrapper = await open()
    demoStore.setPlan(PLAN, [node('node_1', 'done')])

    await wrapper.find('[aria-label="新建会话"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(demoStore.taskNodes).toEqual([])
    expect(demoStore.planId).toBe('')
  })

  it('clears the graph while the new conversation is still being created', async () => {
    const wrapper = await open()
    // A graph from the turn the reader was just looking at.
    demoStore.setPlan(PLAN, [node('node_1', 'done')])
    const create = holdCreate()

    await wrapper.find('[aria-label="新建会话"]').trigger('click')
    await flushPromises()

    // The create request is in flight. This window belongs to newConversation's own reset:
    // openConversation's reset only runs once the server has answered, so without the former
    // the previous graph would sit here until the round-trip came back.
    expect(create.started()).toBe(1)
    expect(demoStore.taskNodes).toEqual([])
    expect(demoStore.planId).toBe('')

    create.release()
    await flushPromises()
    await flushPromises()

    // The conversation really was created and opened; the graph is still gone.
    expect(server.conversations.map(item => item.conversation_id)).toEqual([
      'conv_a', 'conv_b', 'conv_new_2',
    ])
    expect(demoStore.taskNodes).toEqual([])
    expect(demoStore.planId).toBe('')
  })

  it('replaces the graph when a resumed turn replays a plan without a plan_id', async () => {
    pendingTransfer()
    const wrapper = await open()
    // An earlier turn drew a graph that carried no id either, so the store's planId is '' as
    // well: setPlan alone would merge the replay into it. The reset in the decision handler is
    // what makes a plan without an id a replace.
    demoStore.setPlan('', [node('node_1', 'done'), node('node_2')])
    server.decisions.conv_a = [
      ['plan', { nodes: [node('node_1', 'running'), node('node_3')] }],
    ]

    await wrapper.findAll('.interrupt-actions button')[1].trigger('click')
    await flushPromises()
    await flushPromises()

    // node_1 does not inherit its old 'done' and the old node_2 is gone.
    expect(demoStore.planId).toBe('')
    expect(graph()).toEqual([['node_1', 'running'], ['node_3', 'pending']])
  })

  it('applies a plan even when the session reports demo_mode: false', async () => {
    server.demoMode = false
    localStorage.setItem('bank-agent.last-conversation', 'conv_b')
    server.streams.conv_b = [
      ['plan', { plan_id: PLAN, nodes: [node('node_1'), node('node_2')] }],
      ['task_update', { plan_id: PLAN, node_id: 'node_1', status: 'running' }],
      ['final', { text: '已经排好两步', business_ids: [] }],
    ]
    const wrapper = await open()

    await wrapper.find('.composer textarea').setValue('查账单再转账')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await flushPromises()

    // No demo bar in this session -- and the graph still arrives: the handlers are not gated
    // on the flag. A gate would drop a plan the moment the reader switches tabs mid-turn.
    expect(wrapper.find('[data-test="toggle-panel"]').exists()).toBe(false)
    expect(demoStore.planId).toBe(PLAN)
    expect(graph()).toEqual([['node_1', 'running'], ['node_2', 'pending']])
  })
})
