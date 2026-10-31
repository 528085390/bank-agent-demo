/**
 * The bill-analysis module's HTTP client and money formatting.
 *
 * The bill flow is deliberately *not* chat-driven: a file upload is a multipart request the
 * agent must not carry (the archive password would end up in the model's context). So it talks
 * to `/api/bill/*` directly, through the shared `request`/`upload` transport rather than its
 * own client.
 *
 * Money is an integer number of fen (`*_minor`) everywhere the API speaks. Dividing by 100 is
 * a *display* concern and lives here, so no caller has to remember the unit.
 */

import { request, upload } from './api'

/** The task state machine, in order. Terminal states are `COMPLETED` / `FAILED` / `CANCELLED`. */
export type BillState =
  | 'WAITING_APPLICATION' | 'WAITING_UPLOAD' | 'WAITING_PASSWORD'
  | 'EXTRACTING' | 'PARSING' | 'SAVING' | 'ANALYZING'
  | 'COMPLETED' | 'FAILED' | 'CANCELLED'

/** States in which the server is working and the card should keep refreshing. */
export const BILL_RUNNING: readonly BillState[] = ['EXTRACTING', 'PARSING', 'SAVING', 'ANALYZING']

export interface BillProgress { current_step: string; percentage: number }

export interface BillTaskView {
  task_id: string
  state: BillState
  bank_code: string
  /**
   * 账单周期。上传时不再要求填写：解析完成前是 `null`，解析完成后由后端用文件里的
   * 真实起止回填。分析与明细都按文档取数，不依赖这两个值。
   */
  period_start: string | null
  period_end: string | null
  source_mode: string
  /** Failed archive-password attempts so far, and the configured limit. */
  password_attempts: number
  max_password_attempts: number
  progress: BillProgress
  allowed_actions: string[]
  error: { code: string; message: string } | null
}

/** 一份任务解析出来的流水（分页结果，字段与查询工具一致）。 */
export interface BillTransactions {
  total: number
  page: number
  page_size: number
  items: Array<{
    booked_date: string
    amount_minor: number
    direction: string
    transaction_type: string
    counterparty: string
    category: string
  }>
}

/** How many password attempts are left before the task refuses further tries. */
export function remainingPasswordAttempts(task: BillTaskView): number {
  return Math.max(0, task.max_password_attempts - task.password_attempts)
}

/** A byte count as the file picker shows it. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// 文件名推断周期（guessPeriodFromFilename / currentMonthRange）已删除：上传卡片不再
// 让用户或文件名决定报告期间。真正的起止来自解析结果（statement_document 的
// period_start/period_end），后端在解析完成时回填到任务上。

export const billApi = {
  /**
   * 创建任务。周期可以不给：手动上传的起止由解析结果决定；「申请流水」那条支路
   * 仍然会带上，因为它要告诉用户去银行申请哪一段。
   */
  createTask: (body: { bank_code: string; period_start?: string; period_end?: string }) =>
    request<BillTaskView>('/bill/tasks', body),
  confirmApplication: (taskId: string) =>
    request<BillTaskView>(`/bill/tasks/${taskId}/application-confirm`, {}),
  uploadFile: (taskId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return upload<BillTaskView>(`/bill/tasks/${taskId}/upload-file`, form)
  },
  /**
   * 不带密码直接开始处理：Excel / CSV 根本不需要密码，不该把用户拦在密码步骤上。
   * 真需要密码的文件会在后台失败并退回 WAITING_PASSWORD，那时前端才提示输入。
   */
  beginProcessing: (taskId: string) =>
    request<BillTaskView>(`/bill/tasks/${taskId}/begin-processing`, {}),
  submitPassword: (taskId: string, password: string) =>
    request<BillTaskView>(`/bill/tasks/${taskId}/password`, { password }),
  getTask: (taskId: string) => request<BillTaskView>(`/bill/tasks/${taskId}`),
  /** 这份任务解析出来的流水（分页）。不走模型：上传卡片自己显示，翻页也走这里。 */
  getTransactions: (taskId: string, page = 1, pageSize = 20) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    return request<BillTransactions>(`/bill/tasks/${taskId}/transactions?${params.toString()}`)
  },
  cancelTask: (taskId: string) => request<BillTaskView>(`/bill/tasks/${taskId}/cancel`, {}),
}
