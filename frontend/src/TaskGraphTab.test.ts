import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import { nextTick } from 'vue'

import TaskGraphTab from './TaskGraphTab.vue'
import { demoStore } from './demoStore'
import type { TaskNode, TaskNodeStatus } from './types'

/**
 * 任务图页：空态、六个状态、错误行、产物抽屉，以及「store 变了页面自己跟着变」。
 *
 * 这一页没有 props、没有 emits、挂载时也不取数：所有输入都是 `demoStore.taskNodes`。所以
 * 用例只要摆好 store 再挂载，连 fetch 桩都不需要 —— 需要桩，就说明组件偷偷发了请求。
 */

/** 一条最小节点：只写用例关心的字段，其余给默认值。 */
function node(overrides: Partial<TaskNode> & Pick<TaskNode, 'id' | 'label' | 'owner'>): TaskNode {
  return { depends_on: [], status: 'pending', ...overrides }
}

/** 六个状态和它们的中文标签，一一对应；少一个或多一个都算错。 */
const STATUSES: Array<[TaskNodeStatus, string]> = [
  ['pending', '等待中'],
  ['running', '正在执行'],
  ['waiting_confirm', '等待确认'],
  ['done', '已完成'],
  ['failed', '失败'],
  ['skipped', '已跳过'],
]

/** 六个领域的 agent_id 和徽标上的中文名，一一对应。 */
const OWNERS: Array<[string, string]> = [
  ['bill', '账单助手'],
  ['life_event', '生日管家'],
  ['transfer', '转账助手'],
  ['wealth', '理财助手'],
  ['card', '信用卡助手'],
  ['subscription', '订阅助手'],
]

/** 空态文案（spec §5.4:345），一个字都不能差。 */
const EMPTY_TEXT = '当前无后台任务（对话为直接答复或尚未开始）'

describe('TaskGraphTab.vue', () => {
  beforeEach(() => {
    demoStore.resetTasks()
  })

  it('renders empty state when there are no tasks', () => {
    const wrapper = mount(TaskGraphTab)
    // 根节点就是抽屉里那一页：data-test 是 Task 8 的接线点，少了它抽屉里找不到这一页。
    const root = wrapper.find('section.demo-panel[data-test="panel-tasks"]')
    expect(root.exists()).toBe(true)
    expect(wrapper.find('[data-test="tasks-empty"]').text()).toBe(EMPTY_TEXT)
  })

  it('renders task nodes with status badge and owner', async () => {
    demoStore.setPlan('plan_1', [
      { id: 'n1', label: '查询账单与结余', owner: 'bill', depends_on: [], status: 'done', artifact: { total: 500 } },
      { id: 'n2', label: '转账确认', owner: 'transfer', depends_on: ['n1'], status: 'waiting_confirm' },
    ])
    const wrapper = mount(TaskGraphTab)
    expect(wrapper.find('[data-test="tasks-empty"]').exists()).toBe(false)
    const nodes = wrapper.findAll('[data-test="task-node"]')
    expect(nodes).toHaveLength(2)

    expect(nodes[0].text()).toContain('账单')
    expect(nodes[0].text()).toContain('查询账单与结余')
    expect(nodes[0].find('.status-done').exists()).toBe(true)

    expect(nodes[1].text()).toContain('转账')
    expect(nodes[1].find('.status-waiting_confirm').exists()).toBe(true)
  })

  it.each(OWNERS)('shows the %s owner as its Chinese domain name, next to the label', (owner, name) => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '查询账单与结余', owner }),
    ])

    const wrapper = mount(TaskGraphTab)

    const row = wrapper.find('[data-test="task-node"]')
    const badge = row.find('.node-owner')
    expect(badge.text()).toBe(name)
    expect(badge.classes()).toEqual(['node-owner', 'owner-' + owner])
    expect(row.find('.node-label').text()).toBe('查询账单与结余')
  })

  it('falls back to the raw agent id when the owner has no domain name', () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '查账', owner: 'bill_agent_2' }),
    ])

    const wrapper = mount(TaskGraphTab)

    expect(wrapper.find('[data-test="task-node"] .node-owner').text()).toBe('bill_agent_2')
  })

  it('keeps the list to exactly one element per node: the connector is CSS, not a div', () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '查账', owner: 'bill' }),
      node({ id: 'n2', label: '转账', owner: 'transfer', depends_on: ['n1'] }),
    ])

    const wrapper = mount(TaskGraphTab)

    const list = wrapper.find('ol.demo-graph')
    expect(list.element.children).toHaveLength(2)
    expect(list.element.children[0].tagName).toBe('LI')
    expect(list.element.children[1].tagName).toBe('LI')
  })

  it('keeps each node row to its own children: head, error and artifact parts only', async () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '查账', owner: 'bill', status: 'done', artifact: { summary: '结余 500' } }),
      node({ id: 'n2', label: '转账', owner: 'transfer', status: 'failed', error: '余额不足' }),
    ])

    const wrapper = mount(TaskGraphTab)
    await wrapper.find('[data-test="artifact-toggle"]').trigger('click')

    const rows = wrapper.findAll('[data-test="task-node"]')
    const children = (index: number): string[] =>
      Array.from(rows[index].element.children).map((child) => child.className)

    // 展开的 done 节点：头部 + 产物开关 + 产物；失败节点：头部 + 错误行。
    // 连接器是伪元素，这里多出任何元素（比如一个 connector div）都会让断言不等。
    expect(children(0)).toEqual(['node-head', 'node-artifact-toggle', 'node-artifact'])
    expect(children(1)).toEqual(['node-head', 'node-error'])
  })

  it('keeps the empty state when a plan exists but holds no nodes', () => {
    demoStore.setPlan('plan_1', [])

    const wrapper = mount(TaskGraphTab)

    expect(demoStore.planId).toBe('plan_1')
    expect(wrapper.find('[data-test="tasks-empty"]').text()).toBe(EMPTY_TEXT)
    expect(wrapper.findAll('[data-test="task-node"]')).toEqual([])
    // 图的名字不上屏：这一页只画节点。
    expect(wrapper.text()).not.toContain('plan_1')
  })

  it.each(STATUSES)('renders a %s node with its own badge class and Chinese label', (status, label) => {
    demoStore.setPlan('plan_1', [node({ id: 'n1', label: '查账', owner: 'bill', status })])

    const wrapper = mount(TaskGraphTab)

    const badge = wrapper.find('[data-test="task-node"] .node-status')
    expect(badge.classes()).toEqual(['node-status', 'status-' + status])
    expect(badge.text()).toBe(label)
  })

  it('shows a failed node error and gives it no artifact toggle', () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '转账', owner: 'transfer', status: 'failed', error: '余额不足' }),
    ])

    const wrapper = mount(TaskGraphTab)

    const row = wrapper.find('[data-test="task-node"]')
    expect(row.find('.node-error').text()).toBe('余额不足')
    expect(row.find('[data-test="artifact-toggle"]').exists()).toBe(false)
    expect(row.find('[data-test="task-artifact"]').exists()).toBe(false)
  })

  it('keeps the error line off a failed node whose error is empty', () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '转账', owner: 'transfer', status: 'failed', error: '' }),
    ])

    const wrapper = mount(TaskGraphTab)

    expect(wrapper.find('[data-test="task-node"] .node-error').exists()).toBe(false)
  })

  it('gives a node with a null artifact no toggle and no artifact block', () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '查账', owner: 'bill', status: 'done', artifact: null }),
    ])

    const wrapper = mount(TaskGraphTab)

    // events.ts 的 asArtifact 把「没有产物」一律变成 null：这是线上真实形状，
    // 不能被当成「有东西可看」而长出一个点开是 null 的按钮。
    expect(wrapper.findAll('[data-test="artifact-toggle"]')).toEqual([])
    expect(wrapper.find('[data-test="task-artifact"]').exists()).toBe(false)
  })

  it('starts a done node artifact collapsed and expands it on click', async () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '查账', owner: 'bill', status: 'done', artifact: { summary: '本月结余 500 元' } }),
    ])

    const wrapper = mount(TaskGraphTab)

    expect(wrapper.find('[data-test="task-artifact"]').exists()).toBe(false)
    const toggle = wrapper.find('[data-test="artifact-toggle"]')
    expect(toggle.text()).toBe('产物')
    expect(toggle.attributes('aria-expanded')).toBe('false')

    await toggle.trigger('click')

    const artifact = wrapper.find('[data-test="task-artifact"]')
    expect(artifact.exists()).toBe(true)
    expect(artifact.text()).toBe(JSON.stringify({ summary: '本月结余 500 元' }, null, 2))
    expect(wrapper.find('[data-test="artifact-toggle"]').attributes('aria-expanded')).toBe('true')

    // 再点一次收回去：这是一个开关，不是单向展开。
    await wrapper.find('[data-test="artifact-toggle"]').trigger('click')

    expect(wrapper.find('[data-test="task-artifact"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="artifact-toggle"]').attributes('aria-expanded')).toBe('false')
  })

  it('opens two nodes independently', async () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'n1', label: '查账', owner: 'bill', status: 'done', artifact: { summary: '结余 500' } }),
      node({ id: 'n2', label: '转账', owner: 'transfer', status: 'done', artifact: { summary: '已转 200' } }),
    ])

    const wrapper = mount(TaskGraphTab)
    const toggles = wrapper.findAll('[data-test="artifact-toggle"]')
    expect(toggles).toHaveLength(2)

    await toggles[0].trigger('click')

    const rows = wrapper.findAll('[data-test="task-node"]')
    expect(rows[0].find('[data-test="task-artifact"]').text()).toBe(
      JSON.stringify({ summary: '结余 500' }, null, 2),
    )
    expect(rows[1].find('[data-test="task-artifact"]').exists()).toBe(false)

    await toggles[1].trigger('click')

    const after = wrapper.findAll('[data-test="task-node"]')
    expect(after[0].find('[data-test="task-artifact"]').text()).toBe(
      JSON.stringify({ summary: '结余 500' }, null, 2),
    )
    expect(after[1].find('[data-test="task-artifact"]').text()).toBe(
      JSON.stringify({ summary: '已转 200' }, null, 2),
    )
  })

  it('collapses a recycled node id after the plan is replaced', async () => {
    demoStore.setPlan('plan_1', [
      node({ id: 'node_1', label: '查账', owner: 'bill', status: 'done', artifact: { summary: 'plan 1' } }),
    ])
    const wrapper = mount(TaskGraphTab)

    await wrapper.find('[data-test="artifact-toggle"]').trigger('click')
    expect(wrapper.find('[data-test="artifact-toggle"]').attributes('aria-expanded')).toBe('true')

    demoStore.resetTasks()
    await nextTick()
    expect(wrapper.find('[data-test="tasks-empty"]').exists()).toBe(true)

    // 新图复用同一个 node_1：它是另一张图里的另一件事，抽屉要回到默认的收起。
    demoStore.setPlan('plan_2', [
      node({ id: 'node_1', label: '查账', owner: 'bill', status: 'done', artifact: { summary: 'plan 2' } }),
    ])
    await nextTick()

    expect(wrapper.find('[data-test="artifact-toggle"]').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-test="task-artifact"]').exists()).toBe(false)
  })

  it('follows the store without remounting: an update flips the badge, a reset empties the page', async () => {
    demoStore.setPlan('plan_1', [node({ id: 'n1', label: '查账', owner: 'bill', status: 'pending' })])
    const wrapper = mount(TaskGraphTab)

    expect(wrapper.find('[data-test="task-node"] .node-status').text()).toBe('等待中')
    expect(wrapper.find('[data-test="task-node"] .node-status').classes()).toEqual([
      'node-status',
      'status-pending',
    ])

    demoStore.updateTask({ node_id: 'n1', status: 'done', artifact: { summary: 'x' } })
    await nextTick()

    expect(wrapper.find('[data-test="task-node"] .node-status').text()).toBe('已完成')
    expect(wrapper.find('[data-test="task-node"] .node-status').classes()).toEqual([
      'node-status',
      'status-done',
    ])
    expect(wrapper.find('[data-test="artifact-toggle"]').exists()).toBe(true)

    demoStore.resetTasks()
    await nextTick()

    expect(wrapper.find('[data-test="tasks-empty"]').text()).toBe(EMPTY_TEXT)
    expect(wrapper.findAll('[data-test="task-node"]')).toEqual([])
  })
})
