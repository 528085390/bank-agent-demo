/**
 * A screenshot harness, not a test suite.
 *
 * It mounts the real `App.vue` against a conversation captured from the running stack
 * (`frontend/.e2e/capture.json`, written by
 * `backend/scripts/e2e_confirmation_chain.py --capture <conversation_id>`) and writes a
 * self-contained page with the real `style.css` inlined, so a headless browser can show what
 * the page actually renders. The conversation section is the component's own output; the
 * second section is the same renderer fed a fixture, labelled as such, so the block types the
 * captured reply happens not to contain are visible too.
 *
 * Run with:  npx vitest run src/e2ePreview.test.ts
 * (Skipped when no capture exists, which is the normal state of a fresh checkout.)
 */

import { flushPromises, mount } from '@vue/test-utils'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeAll, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import { renderMarkdown } from './markdown'
import type { Conversation, ConversationDetail, ConversationMessage } from './types'

const ROOT = resolve(__dirname, '..')
const CAPTURE = resolve(ROOT, '.e2e', 'capture.json')
const OUTPUT = resolve(ROOT, '.e2e', 'preview.html')

/**
 * The capture is produced by a manual run against the live stack, so it is absent in a fresh
 * checkout and in CI. The suite then has nothing to render and is skipped rather than failed.
 */
const captured = existsSync(CAPTURE)

/** A fixture for the renderer only. It is not a model reply, and the page says so. */
const RENDERER_FIXTURE = `## 申购确认后的成交话术

成交后页面会显示 **产品名称**、\`订单号\` 和收益，例如：

> 模拟申购已完成：产品 p_demo_cash，本金 2,000.00 元。

- 订单号：\`order_9f2c\`
- 持仓号：\`h_9f2c\`
- 详情见 [产品说明](https://example.com/products)

\`\`\`json
{"tool": "wealth.purchase", "arguments": {"amount_minor": 200000}}
\`\`\``

interface Capture {
  conversation: Conversation
  pending_interrupt: Record<string, unknown> | null
  messages: ConversationMessage[]
}

const envelope = (data: unknown) => JSON.stringify({ request_id: 'req_e2e', data, error: null })
const json = (data: unknown) =>
  new Response(envelope(data), { status: 200, headers: { 'Content-Type': 'application/json' } })

let capture: Capture

beforeAll(() => {
  capture = JSON.parse(readFileSync(CAPTURE, 'utf8')) as Capture
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/health')) {
      return json({ status: 'ok', model_configured: true, demo_mode: true })
    }
    if (url.endsWith('/api/session')) {
      return json({ session_id: 'sess_e2e', user_id: 'user_0001', login_name: 'demo' })
    }
    // demo_mode 为真，演示条挂载时读一次时钟。
    if (url.endsWith('/api/demo/clock')) {
      return json({ current_time: '2026-10-22T09:00:00+08:00', run_id: '7', due_task_count: 2 })
    }
    // 续费提醒条在登录后自己读一次；这三套工作台用例只关心讨论，给它一个空架子。
    if (url.endsWith('/api/reminders/subscriptions')) {
      return json(envelope({ items: [], count: 0, total_minor: 0, horizon_days: 14 }))
    }
    if (/\/api\/conversations\/[^/?]+$/.test(url)) {
      const detail: ConversationDetail = {
        conversation: capture.conversation,
        pending_interrupt: capture.pending_interrupt,
        recovery_required: false,
      }
      return json(detail)
    }
    if (url.includes('/messages')) {
      return json({ items: capture.messages, next_cursor: null })
    }
    return json({ items: [capture.conversation], next_cursor: null })
  })
  localStorage.clear()
  localStorage.setItem('bank-agent.last-conversation', capture.conversation.conversation_id)
  Element.prototype.scrollTo = () => {}
})

describe.runIf(captured)('the rendered page', () => {
  it('writes a self-contained preview of the real conversation', async () => {
    const wrapper = mount(App)
    await flushPromises()
    await flushPromises()

    // The two things this round is about are really on screen: the confirmation box, and a
    // reply rendered as Markdown (this captured one is a real table from the live model).
    expect(wrapper.find('.confirmation').exists()).toBe(true)
    const reply = wrapper.find('.chat-message.assistant .markdown-body')
    expect(reply.find('table').exists()).toBe(true)
    expect(reply.text()).not.toContain('| --- |')

    const css = readFileSync(resolve(ROOT, 'src', 'style.css'), 'utf8')
    // The page scrolls its own panes; a screenshot wants the whole thing in one flow.
    const flatten = `
      .workspace{max-width:none}
      .main-grid{height:auto;max-height:none;min-height:0}
      .conversation{overflow:visible;min-height:0}
      .chat-pane{height:auto}
      .conversation-sidebar{height:auto}
      .composer{position:static}
      .e2e-note{margin:24px 32px;padding:10px 14px;border:1px dashed #c9d4cd;border-radius:6px;
                font-size:12px;color:#66746b;background:#fff}
      .e2e-section{margin:24px 32px;padding:20px 24px;border:1px solid #dce2de;border-radius:6px;background:#fff}
      .e2e-section h2{margin:0 0 14px;font-size:15px}
    `
    writeFileSync(
      OUTPUT,
      `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>银行智能助理 · 实际渲染</title>
<style>body{margin:0;background:#f6f8f7}${css}${flatten}</style></head>
<body>
<p class="e2e-note">下面第一段是 <b>真实链路</b> 抓下来的对话（浏览器 HTTP → 主智能体 → 理财 → 银行），
由 <code>App.vue</code> 自己渲染；第二段是同一渲染器喂一份 fixture，用来展示抓下来的回复里恰好没有的块类型。</p>
${wrapper.html()}
<section class="e2e-section">
  <h2>Markdown 渲染器 fixture（标题 / 列表 / 加粗 / 行内代码 / 引用 / 链接 / 围栏代码块）</h2>
  <div class="chat-message assistant"><span class="message-role">银行助理</span>
    <div class="markdown-body">${renderMarkdown(RENDERER_FIXTURE)}</div></div>
</section>
</body></html>`,
      'utf8',
    )
    console.log('wrote', OUTPUT)
  })
})
