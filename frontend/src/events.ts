import type {
  AgentEvent, AgentEventName, ConfirmationCard, InterruptEvent, InterruptField,
  TaskNode, TaskNodeStatus,
} from './types'

/**
 * The stream hands back loosely typed payloads. Each event is narrowed to its documented
 * contract here rather than asserted: an unconditional cast would let a payload that does
 * not match the backend reach the template, and the failure would surface as a blank card
 * or a silently missing answer instead of an error.
 *
 * This lives outside `App.vue` so it can be tested directly. The narrowing is the part that
 * has to be right -- a component test would need a DOM and a live stream to reach it.
 */

export const FIELD_TYPES = ['text', 'number', 'date', 'select'] as const
export type FieldType = (typeof FIELD_TYPES)[number]

export const asText = (value: unknown) => (typeof value === 'string' ? value : '')
export const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
export const asTextList = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

/** Keep only entries that can actually be rendered: a field with no name has no key. */
export function asInterruptFields(value: unknown): InterruptField[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item): InterruptField[] => {
    if (!item || typeof item !== 'object') return []
    const raw = item as Record<string, unknown>
    const name = asText(raw.name)
    if (!name) return []
    const type = asText(raw.type) as FieldType
    return [{
      name,
      label: asText(raw.label) || name,
      type: FIELD_TYPES.includes(type) ? type : 'text',
      required: raw.required !== false,
      options: asTextList(raw.options),
    }]
  })
}

/**
 * The confirmation projection, narrowed field by field.
 *
 * A card that does not name a pending operation cannot be clicked -- the click has nothing to
 * bind to -- so it is dropped rather than rendered as buttons that can only fail. Everything
 * else has a safe fallback: an unreadable amount renders as no amount, and the labels fall
 * back to the two words the flow has always used.
 */
export function asConfirmation(value: unknown): ConfirmationCard | undefined {
  const raw = asRecord(value)
  const pendingId = asText(raw.pending_id)
  if (!pendingId) return undefined
  const amount = raw.amount_minor
  return {
    pending_id: pendingId,
    operation: asText(raw.operation),
    title: asText(raw.title) || '待确认操作',
    // `null` 是服务端在说「这次操作没有金额」（账单发邮件、冻结银行卡）。读不懂的金额
    // 也落成 `null` —— 它与「真的 0 元」是两件事：0 是一个金额，会照常显示。
    amount_minor: typeof amount === 'number' && Number.isInteger(amount) ? amount : null,
    product_name: asText(raw.product_name),
    account_display: asText(raw.account_display),
    confirm_label: asText(raw.confirm_label) || '确认',
    cancel_label: asText(raw.cancel_label) || '取消',
    expires_at: typeof raw.expires_at === 'string' ? raw.expires_at : null,
    // Anything but an explicit `true` reads as "no code needed": a card that asks for a
    // secret it does not need is worse than one that asks for none, and the server refuses a
    // confirm that is missing a code it does need.
    requires_code: raw.requires_code === true,
    code_label: asText(raw.code_label) || '模拟验证码',
  }
}

/** The statuses the graph can draw, in the order the backend's `TaskNodeStatus` names them. */
export const TASK_NODE_STATUSES = [
  'pending', 'running', 'waiting_confirm', 'done', 'failed', 'skipped',
] as const

/** `null` for anything that is not one of the six: the page has no word for any other state. */
const asTaskNodeStatus = (value: unknown): TaskNodeStatus | null => {
  const status = asText(value) as TaskNodeStatus
  return TASK_NODE_STATUSES.includes(status) ? status : null
}

/** A plain object is an artifact; a list or a scalar is not one, and reads as absent. */
const asArtifact = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? asRecord(value) : null

/**
 * The task graph, narrowed node by node.
 *
 * A node with no id cannot be matched by the `task_update` that moves it, so it is dropped
 * rather than drawn as a node that never moves. Everything else has a safe default: a status
 * the page cannot draw reads as `pending` -- a snapshot may safely default -- and an artifact
 * or error the frame did not carry reads as `null`.
 */
export function asTaskNodes(value: unknown): TaskNode[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item): TaskNode[] => {
    if (!item || typeof item !== 'object') return []
    const raw = item as Record<string, unknown>
    const id = asText(raw.id)
    if (!id) return []
    return [{
      id,
      label: asText(raw.label),
      owner: asText(raw.owner),
      depends_on: asTextList(raw.depends_on),
      status: asTaskNodeStatus(raw.status) ?? 'pending',
      artifact: asArtifact(raw.artifact),
      error: typeof raw.error === 'string' ? raw.error : null,
    }]
  })
}

export function toEvent(name: AgentEventName, data: Record<string, unknown>): AgentEvent | null {
  switch (name) {
    case 'agent_start':
      return { name, data: { agent_id: asText(data.agent_id) } }
    case 'tool_call':
      return {
        name,
        data: {
          agent_id: asText(data.agent_id), tool_name: asText(data.tool_name),
          call_id: asText(data.call_id), arguments: asRecord(data.arguments),
        },
      }
    case 'tool_result': {
      // The envelope is rebuilt field by field rather than passed through: `data` is narrowed
      // to `{summary, display, business_ids}`, so anything else a payload carries -- `facts`
      // above all -- stops at this line instead of reaching the template.
      const payload = data.data === null || data.data === undefined ? null : asRecord(data.data)
      return {
        name,
        data: {
          agent_id: asText(data.agent_id), tool_name: asText(data.tool_name),
          call_id: asText(data.call_id), status: asText(data.status),
          data: payload === null ? null : {
            summary: typeof payload.summary === 'string' ? payload.summary : null,
            display: payload.display ?? null,
            business_ids: payload.business_ids === null || payload.business_ids === undefined
              ? null
              : asRecord(payload.business_ids) as Record<string, string>,
          },
          error: typeof data.error === 'string' ? data.error : null,
          error_code: typeof data.error_code === 'string' ? data.error_code : null,
        },
      }
    }
    case 'subtask_end':
      return {
        name,
        data: {
          agent_id: asText(data.agent_id), status: asText(data.status),
          summary: asText(data.summary),
          turn_status: typeof data.turn_status === 'string' ? data.turn_status : null,
        },
      }
    case 'interrupt': {
      // A question without its id cannot be answered: the resume would have no binding.
      if (typeof data.question_id !== 'string' || !data.question_id) return null
      const fields = asInterruptFields(data.fields)
      // A card with nothing to fill in is not a question the user can answer.
      if (!fields.length) return null
      const confirmation = asConfirmation(data.confirmation)
      return {
        name,
        data: {
          kind: asText(data.kind) || 'input',
          question_id: data.question_id,
          agent_id: asText(data.agent_id),
          fields,
          display: asText(data.display),
          attempt: typeof data.attempt === 'number' ? data.attempt : 1,
          ...(confirmation ? { confirmation } : {}),
        },
      }
    }
    case 'reply_delta':
      // A text frame with no text is not a frame: it would render an empty bubble.
      if (typeof data.text !== 'string') return null
      return { name, data: { text: data.text } }
    case 'narration':
      if (typeof data.text !== 'string') return null
      return { name, data: { text: data.text } }
    case 'final':
      if (typeof data.text !== 'string') return null
      return {
        name,
        data: {
          text: data.text,
          business_ids: asTextList(data.business_ids),
          terminal_reason: typeof data.terminal_reason === 'string' ? data.terminal_reason : null,
          error_status: typeof data.error_status === 'number' ? data.error_status : null,
        },
      }
    case 'plan': {
      // A plan with no `nodes` list is not a graph: there is nothing to draw, and the drawer
      // would only be cleared for nothing. An empty list is still the graph the supervisor
      // drew, so it is kept.
      if (!Array.isArray(data.nodes)) return null
      const planId = asText(data.plan_id)
      return {
        name,
        data: { ...(planId ? { plan_id: planId } : {}), nodes: asTaskNodes(data.nodes) },
      }
    }
    case 'task_update': {
      // A transition with no node has nothing to move, and a status the page cannot draw would
      // either invent a state or leave the node in its previous one: neither is a frame. The
      // artifact and error are rebuilt here too, so a payload the protocol does not name stops
      // at this line (see the `tool_result` comment above for why).
      const nodeId = asText(data.node_id)
      const status = asTaskNodeStatus(data.status)
      if (!nodeId || !status) return null
      const planId = asText(data.plan_id)
      return {
        name,
        data: {
          ...(planId ? { plan_id: planId } : {}),
          node_id: nodeId,
          status,
          artifact: asArtifact(data.artifact),
          error: typeof data.error === 'string' ? data.error : null,
        },
      }
    }
    default:
      // `clock` is in the frozen vocabulary but has no renderer on this page yet. Returning it
      // would push a shape the activity feed does not know into the feed; dropping it is the
      // behaviour the call sites already assume (`if (!event) return`). Naming the case keeps
      // the return type honest -- the switch used to fall off its end, which `vue-tsc` reports
      // as TS2366 and which fails `npm run build`.
      return null
  }
}

/**
 * One answer, as the string the protocol carries.
 *
 * `v-model` on `<input type="number">` assigns a JS number rather than a string: Vue's
 * `vModelText` casts whenever the element's type is `number`, so the `.number` modifier is
 * not needed for that. An answer is therefore `string | number`, and a reader that calls
 * `.trim()` on it throws -- which takes the render down with it and leaves 提交 disabled
 * forever, with the question unanswerable.
 *
 * A number that is not finite reads as blank rather than as the text "NaN": a value that
 * cannot be meant must fail closed, not be sent to the server as if the user had filled it in.
 */
export function answerText(value: unknown): string {
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : ''
  if (typeof value === 'string') return value.trim()
  return ''
}

/** A required field left blank would be rejected by the server and burn one of the three
 *  ask attempts, so it is blocked here instead. The server still validates independently. */
export function interruptComplete(
  card: InterruptEvent,
  answers: Record<string, unknown>,
): boolean {
  return card.fields.every(field => !field.required || answerText(answers[field.name]) !== '')
}
