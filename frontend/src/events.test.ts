import { describe, expect, it } from 'vitest'

import { answerText, asInterruptFields, interruptComplete, toEvent } from './events'
import type { InterruptEvent } from './types'

/**
 * The narrowing between the wire and the template. Nothing else can fail if this breaks:
 * the event is dropped or mis-shaped, the card renders blank, and no test notices.
 */

const question = (overrides: Record<string, unknown> = {}) => ({
  kind: 'input',
  question_id: 'q_1',
  agent_id: 'transfer',
  display: '请补充收款人信息',
  attempt: 1,
  fields: [{ name: 'recipient_name', label: '收款人姓名', type: 'text', required: true }],
  ...overrides,
})

describe('asInterruptFields', () => {
  it('keeps the label the module declared, and the name as the key', () => {
    const [field] = asInterruptFields([
      { name: 'recipient_name', label: '收款人姓名', type: 'text', required: true },
    ])

    expect(field).toEqual({
      name: 'recipient_name', label: '收款人姓名', type: 'text', required: true, options: [],
    })
  })

  it('falls back to the name as the label when no metadata was declared', () => {
    const [field] = asInterruptFields([{ name: 'amount' }])

    expect(field.label).toBe('amount')
    expect(field.type).toBe('text')
    expect(field.required).toBe(true)
  })

  it('drops a field with no name, because it has no key to answer under', () => {
    expect(asInterruptFields([{ label: '没有名字' }, { name: 'ok' }])).toHaveLength(1)
  })

  it('replaces an unknown type with text rather than trusting it', () => {
    expect(asInterruptFields([{ name: 'a', type: 'textarea' }])[0].type).toBe('text')
  })

  it('treats required as true unless the server says otherwise', () => {
    expect(asInterruptFields([{ name: 'a' }])[0].required).toBe(true)
    expect(asInterruptFields([{ name: 'a', required: false }])[0].required).toBe(false)
  })

  it('keeps only string options', () => {
    expect(asInterruptFields([{ name: 'a', type: 'select', options: ['x', 2, null] }])[0].options)
      .toEqual(['x'])
  })

  it('returns nothing for a payload that is not a list', () => {
    expect(asInterruptFields(undefined)).toEqual([])
    expect(asInterruptFields('recipient_name')).toEqual([])
  })
})

describe('toEvent', () => {
  it('narrows a card confirmation through the shared confirmation payload', () => {
    const confirmation = {
      pending_id: `CA-${'a'.repeat(32)}`,
      operation: 'report_loss',
      title: '挂失银行卡',
      // 挂失没有金额：模块现在传 null，而不是拿 0 兼作「没有」。
      amount_minor: null,
      product_name: '借记卡（尾号 8801）',
      account_display: '借记卡（尾号 8801）',
      confirm_label: '确认',
      cancel_label: '取消',
      expires_at: '2030-01-01T00:00:00Z',
      requires_code: true,
      code_label: '模拟验证码',
    }
    const event = toEvent('interrupt', question({
      kind: 'confirmation',
      agent_id: 'card',
      confirmation,
      fields: [{ name: 'decision', label: '操作决定', type: 'select', required: true, options: ['确认', '取消'] }],
    }))
    expect(event?.name === 'interrupt' && event.data.kind).toBe('confirmation')
    expect(event?.name === 'interrupt' && event.data.confirmation).toEqual(confirmation)
    expect(event?.name === 'interrupt' && 'authorization' in (event.data as object)).toBe(false)
  })
  it('narrows an interrupt into a renderable question', () => {
    const event = toEvent('interrupt', question())

    expect(event).toEqual({
      name: 'interrupt',
      data: {
        kind: 'input',
        question_id: 'q_1',
        agent_id: 'transfer',
        display: '请补充收款人信息',
        attempt: 1,
        fields: [
          { name: 'recipient_name', label: '收款人姓名', type: 'text', required: true, options: [] },
        ],
      },
    })
  })

  it('drops an interrupt with no question id, because the answer could not be bound', () => {
    expect(toEvent('interrupt', question({ question_id: undefined }))).toBeNull()
    expect(toEvent('interrupt', question({ question_id: '' }))).toBeNull()
    expect(toEvent('interrupt', question({ question_id: 7 }))).toBeNull()
  })

  it('drops an interrupt with nothing to fill in', () => {
    expect(toEvent('interrupt', question({ fields: [] }))).toBeNull()
    expect(toEvent('interrupt', question({ fields: [{ label: '无名' }] }))).toBeNull()
    expect(toEvent('interrupt', question({ fields: undefined }))).toBeNull()
  })

  it('narrows a confirmation box into what the page renders and submits', () => {
    const event = toEvent('interrupt', question({
      kind: 'confirmation',
      confirmation: {
        pending_id: 'wp_1', operation: 'purchase_product', title: '确认申购',
        amount_minor: 200000, product_name: '演示灵活理财', account_display: 'a_wealth_main',
        expires_at: '2030-01-01T00:00:00Z', requires_code: true, code_label: '模拟验证码',
      },
    }))

    expect(event?.name === 'interrupt' && event.data.confirmation).toEqual({
      pending_id: 'wp_1', operation: 'purchase_product', title: '确认申购',
      amount_minor: 200000, product_name: '演示灵活理财', account_display: 'a_wealth_main',
      confirm_label: '确认', cancel_label: '取消', expires_at: '2030-01-01T00:00:00Z',
      requires_code: true, code_label: '模拟验证码',
    })
  })

  it('reads a card that does not say it needs a code as a plain confirm box', () => {
    // Only an explicit `true` asks the user for a secret. A card that asks for one it does
    // not need is worse than one that asks for none.
    const event = toEvent('interrupt', question({
      kind: 'confirmation',
      confirmation: { pending_id: 'wp_1', title: '确认申购', amount_minor: 200000 },
    }))

    const card = event?.name === 'interrupt' ? event.data.confirmation : undefined
    expect(card?.requires_code).toBe(false)
    expect(card?.code_label).toBe('模拟验证码')
  })

  it('drops a confirmation box with no pending id, because a click has nothing to bind to', () => {
    const event = toEvent('interrupt', question({
      kind: 'confirmation',
      confirmation: { title: '确认申购', amount_minor: 200000 },
    }))

    // The card itself still renders -- it is a question with one field -- but without the
    // projection, so no confirm/cancel buttons are offered.
    expect(event?.name === 'interrupt' && event.data.confirmation).toBeUndefined()
  })

  it('does not trust a confirmation amount that is not an integer number of fen', () => {
    const event = toEvent('interrupt', question({
      kind: 'confirmation',
      confirmation: { pending_id: 'wp_1', title: '确认申购', amount_minor: '200000' },
    }))

    // 读不懂的金额落成「没有金额」，不是 0 —— 0 是一个会显示出来的金额。
    expect(event?.name === 'interrupt' && event.data.confirmation?.amount_minor).toBeNull()
  })

  it('keeps a null amount as "no amount" instead of inventing zero', () => {
    // 账单发邮件没有金额：服务端传 null，卡面不该印出一个 ¥0.00 / ¥0.01。
    const event = toEvent('interrupt', question({
      kind: 'confirmation',
      confirmation: { pending_id: 'bill-report-1', title: '确认发送账单报告', amount_minor: null },
    }))

    expect(event?.name === 'interrupt' && event.data.confirmation?.amount_minor).toBeNull()
  })

  it('keeps a real zero amount, because zero is an amount', () => {
    // 「没有金额」与「金额是 0」不是一回事：后者要照常显示 ¥0.00。
    const event = toEvent('interrupt', question({
      kind: 'confirmation',
      confirmation: { pending_id: 'wp_0', title: '确认申购', amount_minor: 0 },
    }))

    expect(event?.name === 'interrupt' && event.data.confirmation?.amount_minor).toBe(0)
  })

  it('defaults the attempt so the card can always show one', () => {
    const event = toEvent('interrupt', question({ attempt: undefined }))

    expect(event?.name === 'interrupt' && event.data.attempt).toBe(1)
  })

  it('drops a final with no text', () => {
    expect(toEvent('final', {})).toBeNull()
    expect(toEvent('final', { text: 42 })).toBeNull()
  })

  it('keeps only string business ids on a final', () => {
    const event = toEvent('final', { text: '完成', business_ids: ['t_1', 2, null] })

    expect(event?.name === 'final' && event.data.business_ids).toEqual(['t_1'])
  })

  it('keeps the error code of a failed tool call', () => {
    const event = toEvent('tool_result', {
      agent_id: 'transfer', tool_name: 'transfer.send', call_id: 'call_1',
      status: 'FAILED', data: null, error: '账户余额不足。', error_code: 'INSUFFICIENT_BALANCE',
    })

    expect(event?.name === 'tool_result' && event.data.error_code).toBe('INSUFFICIENT_BALANCE')
  })

  it('reports no error code when the server sent none', () => {
    const event = toEvent('tool_result', { status: 'SUCCEEDED' })

    expect(event?.name === 'tool_result' && event.data.error_code).toBeNull()
  })

  it('narrows a tool result to the envelope fields the page may render', () => {
    // `facts` is module-private. Even a stale server that still sends it must not get it
    // past this narrowing and onto the page.
    const event = toEvent('tool_result', {
      agent_id: 'transfer', tool_name: 'transfer.send', call_id: 'call_1', status: 'SUCCEEDED',
      data: {
        summary: '转账已受理。',
        display: { blocks: [{ type: 'text', text: '24 小时后到账。' }] },
        business_ids: { transfer_id: 'T-9' },
        facts: { account_no: '6222-SECRET-MARKER' },
      },
      error: null, error_code: null,
    })

    expect(event).toEqual({
      name: 'tool_result',
      data: {
        agent_id: 'transfer', tool_name: 'transfer.send', call_id: 'call_1', status: 'SUCCEEDED',
        data: {
          summary: '转账已受理。',
          display: { blocks: [{ type: 'text', text: '24 小时后到账。' }] },
          business_ids: { transfer_id: 'T-9' },
        },
        error: null, error_code: null,
      },
    })
    expect(JSON.stringify(event)).not.toContain('SECRET-MARKER')
  })

  it('narrows the activity events without throwing on a malformed payload', () => {
    expect(toEvent('agent_start', {})).toEqual({ name: 'agent_start', data: { agent_id: '' } })
    expect(toEvent('tool_call', { arguments: 'not-an-object' })).toEqual({
      name: 'tool_call',
      data: { agent_id: '', tool_name: '', call_id: '', arguments: {} },
    })
    expect(toEvent('tool_result', {})).toEqual({
      name: 'tool_result',
      data: {
        agent_id: '', tool_name: '', call_id: '', status: '', data: null, error: null,
        error_code: null,
      },
    })
    expect(toEvent('subtask_end', {})).toEqual({
      name: 'subtask_end',
      data: { agent_id: '', status: '', summary: '', turn_status: null },
    })
  })

  it('keeps the module status that says an unknown write is not a failure', () => {
    expect(toEvent('subtask_end', { agent_id: 'wealth', status: 'FAILED', summary: '结果未知。', turn_status: 'unknown' }))
      .toEqual({
        name: 'subtask_end',
        data: { agent_id: 'wealth', status: 'FAILED', summary: '结果未知。', turn_status: 'unknown' },
      })
    // A publisher that is not a whole-turn module reports nothing, and the page then shows
    // the event's own status rather than inventing one.
    expect(toEvent('subtask_end', { turn_status: 7 })).toMatchObject({ data: { turn_status: null } })
  })

  it('narrows the text events, and refuses one that carries no text', () => {
    expect(toEvent('reply_delta', { text: '正在' })).toEqual({
      name: 'reply_delta',
      data: { text: '正在' },
    })
    expect(toEvent('narration', { text: '我看看持仓' })).toEqual({
      name: 'narration',
      data: { text: '我看看持仓' },
    })
    // A frame with no text is not a frame: rendering it would print an empty bubble.
    expect(toEvent('reply_delta', {})).toBeNull()
    expect(toEvent('narration', { text: 42 })).toBeNull()
  })

  it('keeps the two terminal fields of final, and tolerates a server that omits them', () => {
    expect(
      toEvent('final', {
        text: '好了', business_ids: ['T-1'], terminal_reason: 'api_error', error_status: 500,
      }),
    ).toEqual({
      name: 'final',
      data: {
        text: '好了', business_ids: ['T-1'], terminal_reason: 'api_error', error_status: 500,
      },
    })
    // An older server sends neither field, and the page must not invent one.
    expect(toEvent('final', { text: '好了' })).toEqual({
      name: 'final',
      data: { text: '好了', business_ids: [], terminal_reason: null, error_status: null },
    })
    expect(toEvent('final', { text: '好了', error_status: '500' })).toEqual({
      name: 'final',
      data: { text: '好了', business_ids: [], terminal_reason: null, error_status: null },
    })
  })

  it('drops the name this page has no renderer for, instead of falling off the switch', () => {
    // `clock` is in the frozen vocabulary but has no renderer on this page yet. The call sites
    // already do `if (!event) return`, so null is the contract they rely on -- and it is what
    // keeps `toEvent` from returning `undefined` (TS2366, which failed `npm run build`).
    // `plan` and `task_update` used to be dropped here too; they are narrowed in the task graph
    // describe block below now.
    expect(toEvent('clock', { current_time: '2026-10-22T09:00:00+08:00' })).toBeNull()
  })
})

describe('toEvent with task graph events', () => {
  it('narrows plan event with safe defaults', () => {
    const raw = {
      plan_id: 'plan_1',
      nodes: [
        { id: 'node_1', label: '查询账单', owner: 'bill', depends_on: [] },
        { id: 'node_2', label: '转账', owner: 'transfer', depends_on: ['node_1'], status: 'pending' },
      ],
    }
    const event = toEvent('plan', raw)
    expect(event).toEqual({
      name: 'plan',
      data: {
        plan_id: 'plan_1',
        nodes: [
          { id: 'node_1', label: '查询账单', owner: 'bill', depends_on: [], status: 'pending', artifact: null, error: null },
          { id: 'node_2', label: '转账', owner: 'transfer', depends_on: ['node_1'], status: 'pending', artifact: null, error: null },
        ],
      },
    })
  })

  it('narrows task_update event and drops invalid node_id', () => {
    const valid = toEvent('task_update', {
      plan_id: 'plan_1',
      node_id: 'node_1',
      status: 'running',
    })
    expect(valid).toEqual({
      name: 'task_update',
      data: {
        plan_id: 'plan_1',
        node_id: 'node_1',
        status: 'running',
        artifact: null,
        error: null,
      },
    })

    // A valid status, so the empty node_id is the only thing that can reject this frame: the
    // node_id guard has to fire on its own, not be shadowed by a missing status.
    expect(toEvent('task_update', { node_id: '', status: 'running' })).toBeNull()
    expect(toEvent('task_update', {})).toBeNull()
  })

  it('drops a plan that carries no graph, because there is nothing to draw', () => {
    // Only a payload whose `nodes` is not a list is not a frame. An empty list is still the
    // graph the supervisor drew, and it is kept -- see the plan_id case below.
    expect(toEvent('plan', { plan_id: 'plan_1' })).toBeNull()
    expect(toEvent('plan', { plan_id: 'plan_1', nodes: 'node_1' })).toBeNull()
    expect(toEvent('plan', { plan_id: 'plan_1', nodes: { node_1: {} } })).toBeNull()
  })

  it('drops a node it cannot identify, and keeps the siblings that can be drawn', () => {
    const event = toEvent('plan', {
      plan_id: 'plan_1',
      nodes: [
        { label: '没有 id' },
        { id: '', label: '空 id' },
        'node_2',
        null,
        { id: 'node_3', label: '保留', owner: 'bill', depends_on: ['node_1', 2, null] },
      ],
    })

    expect(event).toEqual({
      name: 'plan',
      data: {
        plan_id: 'plan_1',
        nodes: [{
          id: 'node_3', label: '保留', owner: 'bill', depends_on: ['node_1'],
          status: 'pending', artifact: null, error: null,
        }],
      },
    })
  })

  it('defaults a status the page cannot render, and a missing artifact or error', () => {
    // A snapshot may safely default: an unreadable status on the graph is still a node the
    // user must see, so it is drawn as pending rather than dropped.
    const event = toEvent('plan', {
      nodes: [{ id: 'node_1', status: 'exploded', artifact: 'not-an-object', error: 7 }],
    })

    expect(event).toEqual({
      name: 'plan',
      data: {
        nodes: [{
          id: 'node_1', label: '', owner: '', depends_on: [],
          status: 'pending', artifact: null, error: null,
        }],
      },
    })
  })

  it('omits a plan_id that is absent, rather than sending an empty one', () => {
    expect(toEvent('plan', { nodes: [] })).toEqual({ name: 'plan', data: { nodes: [] } })
    expect(toEvent('plan', { plan_id: '', nodes: [] })).toEqual({ name: 'plan', data: { nodes: [] } })
    expect(toEvent('task_update', { node_id: 'node_1', status: 'running' })).toEqual({
      name: 'task_update',
      data: { node_id: 'node_1', status: 'running', artifact: null, error: null },
    })
  })

  it('drops a task_update whose status the page cannot render', () => {
    // A transition to a state the graph has no word for is not a frame: drawing it would
    // either invent a status or leave the node in the previous one.
    expect(toEvent('task_update', { node_id: 'node_1' })).toBeNull()
    expect(toEvent('task_update', { node_id: 'node_1', status: 'exploded' })).toBeNull()
    expect(toEvent('task_update', { node_id: 'node_1', status: 7 })).toBeNull()
  })

  it('keeps the artifact and error a task_update actually carries', () => {
    const event = toEvent('task_update', {
      plan_id: 'plan_1', node_id: 'node_1', status: 'failed',
      artifact: { summary: '余额不足。' }, error: '账户余额不足。',
    })

    expect(event).toEqual({
      name: 'task_update',
      data: {
        plan_id: 'plan_1', node_id: 'node_1', status: 'failed',
        artifact: { summary: '余额不足。' }, error: '账户余额不足。',
      },
    })
    // An artifact that is not a plain object is not an artifact, and a non-string error is
    // not a reason: both read as absent.
    expect(toEvent('task_update', { node_id: 'node_1', status: 'done', artifact: ['x'], error: 7 }))
      .toEqual({
        name: 'task_update',
        data: { node_id: 'node_1', status: 'done', artifact: null, error: null },
      })
  })
})

describe('interruptComplete', () => {
  const card = (fields: InterruptEvent['fields']): InterruptEvent => ({
    kind: 'input', question_id: 'q_1', agent_id: 'transfer', display: '', attempt: 1, fields,
  })
  const field = (name: string, required: boolean) =>
    ({ name, label: name, type: 'text' as const, required, options: [] })

  it('blocks submit while a required field is blank', () => {
    expect(interruptComplete(card([field('a', true)]), {})).toBe(false)
    expect(interruptComplete(card([field('a', true)]), { a: '   ' })).toBe(false)
    expect(interruptComplete(card([field('a', true)]), { a: 'x' })).toBe(true)
  })

  it('lets an optional field stay blank', () => {
    expect(interruptComplete(card([field('note', false)]), {})).toBe(true)
    expect(interruptComplete(card([field('a', true), field('note', false)]), { a: 'x' })).toBe(true)
  })

  it('accepts a numeric answer, which is what a number input actually writes', () => {
    // `v-model` on `<input type="number">` assigns a JS number, not a string: Vue's
    // `vModelText` casts whenever the element's type is "number", so `.number` is not
    // needed for that. Reading the answer with `.trim()` threw, which took the render down
    // and left 提交 disabled forever.
    expect(interruptComplete(card([field('amount', true)]), { amount: 10000 })).toBe(true)
    expect(interruptComplete(card([field('amount', true)]), { amount: 0 })).toBe(true)
  })

  it('treats a number that is not a number as blank, rather than as the text "NaN"', () => {
    // An empty `<input type="number">` yields "" and never reaches here, but a bad value
    // must fail closed: "NaN" would read as a filled-in answer and be sent to the server.
    expect(interruptComplete(card([field('amount', true)]), { amount: Number.NaN })).toBe(false)
    expect(interruptComplete(card([field('amount', true)]), { amount: Number.POSITIVE_INFINITY }))
      .toBe(false)
  })
})

describe('answerText', () => {
  it('reads an answer as the string the protocol carries, whatever the input wrote', () => {
    expect(answerText('6222')).toBe('6222')
    expect(answerText(10000)).toBe('10000')
    expect(answerText(0)).toBe('0')
  })

  it('trims, so a field of spaces still counts as blank', () => {
    expect(answerText('   ')).toBe('')
    expect(answerText('  x  ')).toBe('x')
  })

  it('reads a missing answer as blank instead of throwing', () => {
    expect(answerText(undefined)).toBe('')
    expect(answerText(null)).toBe('')
  })

  it('reads a value that is not a number as blank rather than as "NaN"', () => {
    expect(answerText(Number.NaN)).toBe('')
    expect(answerText(Number.POSITIVE_INFINITY)).toBe('')
  })
})
