import { reactive } from 'vue'

import { RequestError, request } from './api'
import type { TaskNode, TaskUpdateEvent } from './types'

/**
 * 演示面板的共享状态。
 *
 * 为什么是模块级单例而不是 pinia：本轮只有一处消费者（`DemoControlBar` 挂出来的那条抽屉），
 * 为了它引一个 store 库不划算 —— 但状态又必须跨组件共享（条、抽屉、两个页签都是不同文件），
 * 所以用 `reactive` 建一个模块级对象，谁 import 谁拿到同一份。
 *
 * 所有动作都自己吞异常：`request()` 在非 2xx 或信封里带 error 时会 throw `RequestError`，
 * 组件里一律 `void store.xxx()` 触发，一旦漏接就是一个未捕获的 rejection，vitest 会直接判红。
 */

/** 抽屉的三个页签。任务图本轮只有占位文案，所以它没有对应的组件。 */
export type DemoTab = 'clock' | 'audit' | 'tasks'

/** 银行 `GET /clock` 的视图；`/api/demo/*` 三个端点原样透传的就是它。 */
export interface DemoClock {
  current_time: string
  run_id: string
  due_task_count: number
}

/** 跑到期任务的结果（`POST /clock/run-tasks`）。 */
export interface TaskRunResult {
  processed_count: number
  completed_count: number
  failed_count: number
  items?: unknown[]
}

/** 一条业务审计。后端按 oldest first 返回，倒序是渲染那一层的事。 */
export interface AuditEvent {
  event_id: string
  action: string
  resource_type?: string
  resource_id?: string
  result: string
  reason?: string | null
  created_at: string
}

/** 审计的一页：`{items, next_cursor}`。本轮只读第一页，游标留给 L2 的翻页。 */
export interface AuditPage {
  items: AuditEvent[]
  next_cursor?: string | null
}

/** 拨钟的步长。两个都可以省，但不能都为 0 —— 银行那边是 422。 */
export interface AdvanceStep {
  days?: number
  hours?: number
}

export interface SecurityToolState {
  user_id: string
  tool_name: string
  failure_count: number
  locked_until: string | null
  last_reason: string | null
}

export interface SecurityStatusPayload {
  user_id: string
  tool_name: string | null
  failure_count: number
  locked_until: string | null
  last_reason: string | null
  tools: SecurityToolState[]
}

/**
 * 把异常压成一行给人看的话。
 *
 * `RequestError` 带的是后端的 message（比如「银行模拟时钟暂不可用」）；非 JSON 响应体在
 * `response.json()` 那一步就炸成 `SyntaxError`，也得变成一行字而不是一个没人接的 rejection。
 */
function messageOf(exc: unknown): string {
  if (exc instanceof RequestError) return exc.message
  if (exc instanceof Error) return exc.message
  return '请求失败'
}

export const demoStore = reactive({
  /** 抽屉是否打开。 */
  open: false,
  /** 当前页签。默认时钟 —— 它是演示时最常看的那一页。 */
  tab: 'clock' as DemoTab,
  /** 最近一次读到的时钟；没读过就是 null。 */
  clock: null as DemoClock | null,
  /** 审计第一页，保持后端给的 oldest first。 */
  audit: [] as AuditEvent[],
  /** 最近一次跑到期任务的结果，没跑过就是 null。 */
  lastRun: null as TaskRunResult | null,
  /** 有请求在飞。组件用它 disable 按钮，避免连点。 */
  busy: false,
  /** 最近一次失败的文案，空串表示没有错误。 */
  error: '',
  /** 当前任务图的节点；没有图就是空数组。 */
  taskNodes: [] as TaskNode[],
  /** 当前任务图的 id；没有图就是空串（不是 null）—— 页签用 falsy 判断「有没有图」。 */
  planId: '',

  /** 安全风控锁定状态 */
  security: null as SecurityStatusPayload | null,
  /** 解锁或拉取安全状态中 */
  securityBusy: false,
  /** 用户手动关闭了横幅 */
  dismissed: false,

  get isLocked(): boolean {
    if (!this.security) return false
    if (this.security.locked_until) return true
    return (this.security.tools ?? []).some((t) => Boolean(t.locked_until))
  },

  get isBannerVisible(): boolean {
    return this.isLocked && !this.dismissed
  },

  dismissBanner(): void {
    this.dismissed = true
  },

  async loadSecurity(): Promise<void> {
    this.securityBusy = true
    try {
      this.security = await request<SecurityStatusPayload>('/demo/security')
    } catch {
      // 演示控制面遵循统一吞异常规范
    } finally {
      this.securityBusy = false
    }
  },

  async resetLock(): Promise<void> {
    this.securityBusy = true
    this.error = ''
    try {
      this.security = await request<SecurityStatusPayload>('/demo/security/reset-lock', {})
      this.dismissed = false
    } catch (exc) {
      this.error = messageOf(exc)
      throw exc
    } finally {
      this.securityBusy = false
    }
  },

  markLockedFromEvent(lockedUntil?: string): void {
    this.dismissed = false
    if (!this.security || !this.security.locked_until) {
      this.security = {
        user_id: '',
        tool_name: 'fund_transfer',
        failure_count: 5,
        locked_until: lockedUntil ?? new Date(Date.now() + 15 * 60 * 1000).toISOString(),
        last_reason: 'locked',
        tools: [],
      }
    }
    if (!this.securityBusy) {
      void this.loadSecurity()
    }
  },

  /**
   * 打开抽屉并读一次时钟与安全状态。
   *
   * 首屏那次读不在这里，也不在控制条自己的 `onMounted` 里：`App.vue` 在 `/health` 报出
   * `demo_mode: true` 之后调用 `loadClock()` 一次，条一挂出来就有时间。由条自己发请求不行 ——
   * `App.test.ts` / `App.bill.test.ts` / `e2ePreview.test.ts` 的 fetch 桩是路径白名单，
   * 挂载即发请求会让 17 个既有测试文件全红。抽屉关着的时候，演示条一共只有那一次读。
   */
  async openPanel(): Promise<void> {
    this.open = true
    await Promise.all([this.loadClock(), this.loadSecurity()])
  },

  closePanel(): void {
    this.open = false
  },

  /**
   * 切页签。只有审计页需要读数据：时钟在打开抽屉时已经读过，任务图是静态占位。
   *
   * 审计页开着的时候还会每 3 秒重读一次（轮询在 `DemoSidebar.vue`，跟着抽屉和页签起停）；
   * 这里只负责「切过来的这一下立刻读」，让用户不用等第一拍。
   */
  async setTab(tab: DemoTab): Promise<void> {
    this.tab = tab
    if (tab === 'audit') await this.loadAudit()
  },

  async loadClock(): Promise<void> {
    this.busy = true
    this.error = ''
    try {
      this.clock = await request<DemoClock>('/demo/clock')
    } catch (exc) {
      this.error = messageOf(exc)
    } finally {
      this.busy = false
    }
  },

  /**
   * 拨钟。
   *
   * 响应体就是拨完之后的时钟，所以直接拿它覆盖 `clock`，不再回读一次 —— 多一次 GET 会让
   * 条上的时间先闪回旧值再跳到新值。
   */
  async advance(step: AdvanceStep): Promise<void> {
    this.busy = true
    this.error = ''
    try {
      this.clock = await request<DemoClock>('/demo/clock/advance', step)
    } catch (exc) {
      this.error = messageOf(exc)
    } finally {
      this.busy = false
    }
  },

  /**
   * 跑到期任务，然后回读时钟。
   *
   * 结果里没有到期数，跑完它必然变了，所以这一处必须再读一次 —— 与 `advance` 相反。
   */
  async runTasks(outcome?: 'success' | 'fail'): Promise<void> {
    this.busy = true
    this.error = ''
    try {
      this.lastRun = await request<TaskRunResult>(
        '/demo/clock/run-tasks',
        outcome ? { birthday_outcome: outcome } : {},
      )
      await this.loadClock()
    } catch (exc) {
      this.error = messageOf(exc)
    } finally {
      this.busy = false
    }
  },

  /** 审计第一页。 */
  async loadAudit(): Promise<void> {
    this.busy = true
    this.error = ''
    try {
      const page = await request<AuditPage>('/audit/business-events')
      this.audit = page.items ?? []
    } catch (exc) {
      this.error = messageOf(exc)
    } finally {
      this.busy = false
    }
  },

  /**
   * 收到一整张任务图（`plan` 事件）。
   *
   * 同一个 planId 而且手里已经有节点时是**合并**：`plan` 在恢复执行时会重播一次，那一份里的
   * status 是旧的，直接覆盖会让已经跑完的节点闪回 pending。所以每个节点只从新来的那份取
   * id/label/owner/depends_on，status/artifact/error 沿用手里已有的；顺序按新来的那份，
   * 新来的那份没提到的节点丢掉 —— 重播的就是整张图，它才是「哪些节点存在」的权威。
   *
   * 换了 planId、或者手里本来就没有图，就是**替换**：新来的那份原样收下。
   *
   * 一律换新数组 + 新节点对象（`depends_on` 也复制一份）：Vue 只看到一次变化，
   * 而且 store 不会和调用方共用一个对象。
   */
  setPlan(planId: string, nodes: TaskNode[]): void {
    const known = new Map<string, TaskNode>()
    // 只有同一个 planId 才谈得上合并；换了图，旧节点的状态对新图没有意义。
    if (planId === this.planId) for (const node of this.taskNodes) known.set(node.id, node)

    this.planId = planId
    this.taskNodes = nodes.map((node) => {
      const existing = known.get(node.id)
      const next: TaskNode = {
        id: node.id,
        label: node.label,
        owner: node.owner,
        depends_on: [...node.depends_on],
        status: existing ? existing.status : node.status,
      }
      const artifact = existing ? existing.artifact : node.artifact
      const error = existing ? existing.error : node.error
      // 没有值就不写这个键：`artifact: undefined` 会让「有没有 artifact」看起来像有。
      if (artifact !== undefined) next.artifact = artifact
      if (error !== undefined) next.error = error
      return next
    })
  },

  /**
   * 收到一个节点的状态变化（`task_update` 事件）。
   *
   * 不认识的 node_id 直接忽略：漏了 `plan` 的页面不能凭空造节点，也不能抛异常把整条流打断。
   *
   * `status` 每次都写（事件里必有）。`artifact` / `error` 只在事件里**带着这个键**时才写：
   * 显式 `null` 是「清空」，而没有这个键是「这次没说」，不能把手里已有的值擦掉。
   *
   * 换新数组、只给被改的那个节点换新对象，别的节点原样留着；planId 不动 —— 图的名字由
   * `plan` 给，更新不负责命名。
   */
  updateTask(update: TaskUpdateEvent): void {
    const index = this.taskNodes.findIndex((node) => node.id === update.node_id)
    if (index < 0) return

    const current = this.taskNodes[index]
    const next: TaskNode = {
      id: current.id,
      label: current.label,
      owner: current.owner,
      depends_on: [...current.depends_on],
      status: update.status,
    }
    const artifact = 'artifact' in update ? update.artifact : current.artifact
    const error = 'error' in update ? update.error : current.error
    if (artifact !== undefined) next.artifact = artifact
    if (error !== undefined) next.error = error

    const nodes = [...this.taskNodes]
    nodes[index] = next
    this.taskNodes = nodes
  },

  /** 新开一个对话：图清掉，planId 回到空串（「没有图」的值是空串，不是 null）。 */
  resetTasks(): void {
    this.taskNodes = []
    this.planId = ''
  },
})

/**
 * 条上那一行：`YYYY-MM-DD HH:mm`。
 *
 * 银行给的是带时区的 ISO 串（`2026-10-22T09:00:00+08:00`），截前 16 个字符正好到分钟。
 * 不引 dayjs：要的就是把 `T` 换成空格。
 */
export function clockLabel(clock: DemoClock | null): string {
  if (!clock?.current_time) return '模拟时钟 · 点击查看'
  return clock.current_time.slice(0, 16).replace('T', ' ')
}

/**
 * 审计行上的时间部分（`HH:mm`）。
 *
 * 认不出 ISO 就原样返回：界面上留一个怪字符串，比留一片空白更容易查。
 */
export function timeOf(value: string): string {
  const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(value ?? '')
  return match ? match[1] : (value ?? '')
}
