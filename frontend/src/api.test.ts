import { afterEach, expect, test, vi } from 'vitest'
import {
  createConversation, deleteConversation, getConversation, listConversations, listMessages,
  request, RequestError, restoreConversation, resumeFor, submitMessage, updateConversation,
} from './api'
import { listFilter } from './conversations'
import type { Conversation, ConversationMessageRequest, InterruptEvent } from './types'

afterEach(() => vi.unstubAllGlobals())

interface Call { url: string; init: RequestInit }

/** Every request the client makes, so a test can pin the URL, the verb and the headers. */
function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return handler(url, init)
  })
  return calls
}

function headerOf(init: RequestInit, name: string): string | undefined {
  return (init.headers as Record<string, string> | undefined)?.[name]
}

function streamResponse(chunks: string[]) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) { for (const chunk of chunks) controller.enqueue(encoder.encode(chunk)); controller.close() },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify({ request_id: 'req_1', data, error: null }), {
    status, headers: { 'Content-Type': 'application/json' },
  })
}

function errorResponse(status: number, code: string, message = '请求失败') {
  return new Response(JSON.stringify({
    request_id: 'req_boom', data: null,
    error: { code, message, retryable: false, details: {} },
  }), { status, headers: { 'Content-Type': 'application/json' } })
}

/** A well-formed frame, as produced by the backend. */
function frame(event: string, data: unknown, requestId = 'req_1') {
  return `event: ${event}\ndata: ${JSON.stringify({ request_id: requestId, data, error: null })}\n\n`
}

const conversation: Conversation = {
  conversation_id: 'c-1', title: '转账', pinned: false, archived: false, deleted: false,
  created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T00:00:00Z',
  last_message_at: null, message_count: 0, serial_no: 1,
}

test('A first submission streams the agent events, keyed by its idempotency key', async () => {
  const body: ConversationMessageRequest = { message: '转账 100 元给张三' }
  const stream = [
    'event: agent_start\r\ndata: {"request_id":"req_1","data":{"agent_id":"transfer"},"error":null}\r\n\r\n',
    'event: tool_call\r\ndata: {"request_id":"req_1","data":{"agent_id":"transfer","tool_name":"transfer.send","call_id":"call_1","arguments":{"amount":"100","to":"张三"}},"error":null}\n\n',
    'event: tool_result\r\ndata: {"request_id":"req_1","data":{"agent_id":"transfer","tool_name":"transfer.send","call_id":"call_1","status":"SUCCEEDED","data":{"summary":"转账已受理。","display":{},"business_ids":{}},"error":null,"error_code":null},"error":null}\n\n',
    'event: subtask_end\r\ndata: {"request_id":"req_1","data":{"agent_id":"transfer","status":"SUCCEEDED","summary":"已处理"},"error":null}\n\n',
    'event: final\r\ndata: {"request_id":"req_1","data":{"text":"已为你完成转账。","business_ids":["BIZ-2024-0001"]},"error":null}\n\n',
  ].join('')
  // Split the whole stream into small chunks so frames, CRLF pairs and JSON bodies break mid-way.
  const chunks: string[] = []
  for (let index = 0; index < stream.length; index += 17) chunks.push(stream.slice(index, index + 17))
  const calls = stubFetch(() => streamResponse(chunks))

  const received: { name: string; data: Record<string, unknown> }[] = []
  const outcome = await submitMessage('c-1', body, 'key-1', (name, data) => received.push({ name, data }))

  expect(received).toEqual([
    { name: 'agent_start', data: { agent_id: 'transfer' } },
    {
      name: 'tool_call',
      data: {
        agent_id: 'transfer', tool_name: 'transfer.send', call_id: 'call_1',
        arguments: { amount: '100', to: '张三' },
      },
    },
    {
      name: 'tool_result',
      data: {
        agent_id: 'transfer', tool_name: 'transfer.send', call_id: 'call_1',
        status: 'SUCCEEDED',
        data: { summary: '转账已受理。', display: {}, business_ids: {} },
        error: null, error_code: null,
      },
    },
    { name: 'subtask_end', data: { agent_id: 'transfer', status: 'SUCCEEDED', summary: '已处理' } },
    { name: 'final', data: { text: '已为你完成转账。', business_ids: ['BIZ-2024-0001'] } },
  ])
  // A stream is not a receipt: the graph ran for this request.
  expect(outcome.receipt).toBeNull()
  // The client must not add anything (session_id, timezone, task_id, ...) to the documented body.
  expect(calls[0].url).toBe('/api/conversations/c-1/messages')
  expect(calls[0].init.method).toBe('POST')
  expect(headerOf(calls[0].init, 'Idempotency-Key')).toBe('key-1')
  expect(JSON.parse(String(calls[0].init.body))).toEqual({ message: '转账 100 元给张三' })
})

test('The text frames reach the caller as they arrive, ahead of final', async () => {
  const stream = [
    frame('reply_delta', { text: '正在' }),
    frame('narration', { text: '我看看持仓' }),
    frame('final', { text: '持仓查好了。', business_ids: [] }),
  ].join('')
  stubFetch(() => streamResponse([stream]))

  const received: { name: string; data: Record<string, unknown> }[] = []
  await submitMessage('c-1', { message: '看看持仓' }, 'key-2', (name, data) =>
    received.push({ name, data }),
  )

  // The order is the contract: the two text frames are not folded into final.
  expect(received.map(item => item.name)).toEqual(['reply_delta', 'narration', 'final'])
  expect(received[0].data).toEqual({ text: '正在' })
  expect(received[1].data).toEqual({ text: '我看看持仓' })
})

test('A JSON receipt is read as a receipt and never parsed as a stream', async () => {
  const receipt = { submission_id: 's-1', message_id: 'm-1', run_id: null, duplicate: true as const }
  const calls = stubFetch(() => jsonResponse(receipt))

  let emitted = 0
  const outcome = await submitMessage('c-1', { message: '转账' }, 'key-1', () => { emitted += 1 })

  expect(outcome.receipt).toEqual(receipt)
  expect(emitted).toBe(0)
  expect(headerOf(calls[0].init, 'Idempotency-Key')).toBe('key-1')
})

test('A repeat that conflicts on its key surfaces as IDEMPOTENCY_CONFLICT', async () => {
  const calls = stubFetch(() => errorResponse(409, 'IDEMPOTENCY_CONFLICT', '同一个幂等键被用于了不同的请求'))

  await expect(submitMessage('c-1', { message: '转账' }, 'key-1', () => { throw new Error('must not emit') }))
    .rejects.toMatchObject({
      code: 'IDEMPOTENCY_CONFLICT', message: '同一个幂等键被用于了不同的请求', requestId: 'req_boom',
    } satisfies Partial<RequestError>)
  expect(headerOf(calls[0].init, 'Idempotency-Key')).toBe('key-1')
})

test('A missing idempotency key is refused with IDEMPOTENCY_KEY_REQUIRED', async () => {
  stubFetch(() => errorResponse(400, 'IDEMPOTENCY_KEY_REQUIRED', '请求缺少 Idempotency-Key，无法识别重复提交'))

  await expect(submitMessage('c-1', { message: '转账' }, 'key-1', () => { throw new Error('must not emit') }))
    .rejects.toMatchObject({
      code: 'IDEMPOTENCY_KEY_REQUIRED', requestId: 'req_boom',
    } satisfies Partial<RequestError>)
})

test('A retry after a transport failure repeats the identical request under the identical key', async () => {
  const body: ConversationMessageRequest = { message: '转账 100' }
  const calls: Call[] = []
  let attempt = 0
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    attempt += 1
    if (attempt === 1) throw new TypeError('网络中断')
    return jsonResponse({ submission_id: 's-1', message_id: 'm-1', run_id: 'r-1', duplicate: true })
  })

  // A transport failure is not a receipt: it must reach the caller, which keeps the record.
  await expect(submitMessage('c-1', body, 'key-1', () => {})).rejects.toThrow('网络中断')

  const outcome = await submitMessage('c-1', body, 'key-1', () => {})

  expect(outcome.receipt).not.toBeNull()
  expect(calls).toHaveLength(2)
  expect(calls[1].url).toBe(calls[0].url)
  expect(calls[1].init.body).toBe(calls[0].init.body)
  expect(headerOf(calls[1].init, 'Idempotency-Key')).toBe(headerOf(calls[0].init, 'Idempotency-Key'))
})

test('interrupt frames carry the whole question, including the id it is answered by', async () => {
  const card: InterruptEvent = {
    kind: 'input', question_id: 'q_7f3', agent_id: 'transfer',
    fields: [{ name: 'amount_minor', label: '转账金额（元）', type: 'number', required: true, options: [] }],
    display: '请补充转账金额与收款人', attempt: 1,
  }
  stubFetch(() => streamResponse([frame('interrupt', card)]))

  const received: { name: string; data: Record<string, unknown> }[] = []
  await submitMessage('c-1', { message: '转账' }, 'key-1', (name, data) => received.push({ name, data }))

  expect(received).toEqual([{ name: 'interrupt', data: card }])
})

test('A resume names the question it answers, not just its fields', () => {
  const question: InterruptEvent = {
    kind: 'input', question_id: 'q_42', agent_id: 'transfer',
    fields: [{ name: 'amount_minor', label: '金额', type: 'number', required: true, options: [] }],
    display: '请补充金额', attempt: 2,
  }

  expect(resumeFor(question, { amount: '100' })).toEqual({
    resume: { question_id: 'q_42', value: { amount: '100' } },
  })
})

test('An error frame without an event line rejects with its code and request ID', async () => {
  stubFetch(() => streamResponse([
    'data: {"request_id":"req_boom","data":null,"error":{"code":"AGENT_FAILED","message":"执行失败","retryable":true,"details":{}}}\n\n',
  ]))

  await expect(submitMessage('c-1', { message: '转账' }, 'key-1', () => { throw new Error('must not emit') }))
    .rejects.toMatchObject({
      code: 'AGENT_FAILED', message: '执行失败', requestId: 'req_boom',
    } satisfies Partial<RequestError>)
})

test('Frames without an event line or with an unknown name are ignored', async () => {
  stubFetch(() => streamResponse([
    'data: {"request_id":"req_1","data":null,"error":null}\n\n',
    'event: unknown_event\ndata: {"request_id":"req_1","data":{"text":"nope"},"error":null}\n\n',
    frame('final', { text: '完成', business_ids: [] }),
  ]))

  const received: { name: string; data: Record<string, unknown> }[] = []
  await submitMessage('c-1', { message: '查询余额' }, 'key-1', (name, data) => received.push({ name, data }))

  expect(received).toEqual([{ name: 'final', data: { text: '完成', business_ids: [] } }])
})

test('An HTTP rejection exposes its code and request ID without starting a stream', async () => {
  stubFetch(() => errorResponse(401, 'SESSION_REQUIRED', '请先进入演示会话'))

  await expect(submitMessage('c-1', { message: '转账' }, 'key-1', () => { throw new Error('must not emit') }))
    .rejects.toMatchObject({
      code: 'SESSION_REQUIRED', requestId: 'req_boom',
    } satisfies Partial<RequestError>)
})

test('The conversation list carries its paging cursor and both filter flags', async () => {
  const calls = stubFetch(() => jsonResponse({ items: [conversation], next_cursor: 'cur-2' }))

  const page = await listConversations({ limit: 30, ...listFilter({ archived: true, deleted: true }) })
  await listConversations({ cursor: 'cur-2', ...listFilter({ archived: false, deleted: false }) })

  expect(page).toEqual({ items: [conversation], next_cursor: 'cur-2' })
  expect(calls[0].url).toBe('/api/conversations?limit=30&archived=true&deleted=true')
  expect(calls[0].init.method).toBe('GET')
  // The default list is the live one, and the filters are sent rather than left to the server.
  expect(calls[1].url).toBe('/api/conversations?limit=20&cursor=cur-2&archived=false&deleted=false')
})

test('Each toggle reaches the query string on its own, so all four combinations work', async () => {
  const calls = stubFetch(() => jsonResponse({ items: [], next_cursor: null }))

  for (const toggles of [
    { archived: false, deleted: false },
    { archived: true, deleted: false },
    { archived: false, deleted: true },
    { archived: true, deleted: true },
  ]) {
    await listConversations({ limit: 20, ...listFilter(toggles) })
  }

  // Neither flag implies the other: ?archived=true must never be how soft-deleted rows are asked
  // for, and ?deleted=true must never be how archived ones are.
  expect(calls.map(call => call.url)).toEqual([
    '/api/conversations?limit=20&archived=false&deleted=false',
    '/api/conversations?limit=20&archived=true&deleted=false',
    '/api/conversations?limit=20&archived=false&deleted=true',
    '/api/conversations?limit=20&archived=true&deleted=true',
  ])
})

test('History paging asks for the cursor the server returned, and only then', async () => {
  const calls = stubFetch(() => jsonResponse({ items: [], next_cursor: 'cur-9' }))

  await listMessages('c-1', { limit: 50, after: 'cur-9' })
  await listMessages('c-1')

  expect(calls[0].url).toBe('/api/conversations/c-1/messages?limit=50&after=cur-9')
  expect(calls[0].init.method).toBe('GET')
  expect(calls[1].url).toBe('/api/conversations/c-1/messages?limit=50')
})

test('A conversation is read whichever way the server wrapped it', async () => {
  const calls = stubFetch(() => jsonResponse(conversation))
  await expect(createConversation('转账')).resolves.toMatchObject({ conversation_id: 'c-1' })
  expect(calls[0].url).toBe('/api/conversations')
  expect(JSON.parse(String(calls[0].init.body))).toEqual({ title: '转账' })

  vi.unstubAllGlobals()
  stubFetch(() => jsonResponse({ conversation }))
  await expect(createConversation()).resolves.toMatchObject({ conversation_id: 'c-1' })
})

test('A conversation without a serial number is refused, not rendered', async () => {
  // The sidebar numbers every row, so a conversation that cannot produce a number would render
  // "#undefined"; it is refused as unrecognizable data instead of reaching the list.
  const withoutSerial: Record<string, unknown> = { ...conversation }
  delete withoutSerial.serial_no
  stubFetch(() => jsonResponse(withoutSerial))

  await expect(createConversation('转账')).rejects.toMatchObject({ code: 'UNEXPECTED_RESPONSE' })
})

test('Rename, pin and archive are one PATCH of the fields that changed', async () => {
  const calls = stubFetch(() => jsonResponse(conversation))

  await updateConversation('c-1', { title: '新标题' })
  await updateConversation('c-1', { pinned: true })

  expect(calls[0].init.method).toBe('PATCH')
  expect(calls[0].url).toBe('/api/conversations/c-1')
  expect(JSON.parse(String(calls[0].init.body))).toEqual({ title: '新标题' })
  expect(JSON.parse(String(calls[1].init.body))).toEqual({ pinned: true })
})

test('Soft delete and restore use the verb the contract names', async () => {
  const calls = stubFetch(() => jsonResponse({ ...conversation, deleted: true }))

  await deleteConversation('c-1')
  await restoreConversation('c-1')

  expect(calls[0].init.method).toBe('DELETE')
  expect(calls[0].url).toBe('/api/conversations/c-1')
  expect(calls[0].init.body).toBeUndefined()
  expect(calls[1].init.method).toBe('POST')
  expect(calls[1].url).toBe('/api/conversations/c-1/restore')
})

test('The detail read returns the confirmed interrupt and the recovery flag', async () => {
  const payload = { conversation, pending_interrupt: null, recovery_required: true }
  const calls = stubFetch(() => jsonResponse(payload))

  await expect(getConversation('c-1')).resolves.toEqual(payload)
  expect(calls[0].url).toBe('/api/conversations/c-1')
})

test('Settings requests can use PUT and DELETE without creating an SSE stream', async () => {
  const calls = stubFetch(() => jsonResponse({ mode: 'unconfigured' }))

  await request('/settings/llm', { model: 'demo' }, undefined, 'PUT')
  await request('/settings/llm', undefined, undefined, 'DELETE')

  expect(calls[0].init.method).toBe('PUT')
  expect(calls[1].init.method).toBe('DELETE')
})

test('A rejected origin keeps the hosts it disagreed about', async () => {
  // 后端把「收到哪个来源、本站认哪几个」放在 details 里。前端原样带出来，页面才说得出是哪
  // 一个地址不对；把 details 丢掉，就只剩一句「请刷新后重试」，而刷新治不了代理改写 Host。
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({
    request_id: 'req_origin', data: null,
    error: {
      code: 'ORIGIN_REJECTED',
      message: '请求来源与本站不一致，已被拒绝（跨站请求会被拦截）',
      retryable: false,
      details: { origin: 'evil.example', expected: ['127.0.0.1:5173'] },
    },
  }), { status: 403, headers: { 'Content-Type': 'application/json' } }))

  await expect(request('/auth/logout', {})).rejects.toMatchObject({
    code: 'ORIGIN_REJECTED',
    details: { origin: 'evil.example', expected: ['127.0.0.1:5173'] },
  } satisfies Partial<RequestError>)
})
