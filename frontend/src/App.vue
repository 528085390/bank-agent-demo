<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  Archive, ArchiveRestore, ArrowRight, ArrowUp, CircleAlert, History, Landmark, LoaderCircle, LogOut,
  MessageSquare, Pencil, Pin, PinOff, Plus, Receipt, RefreshCw, RotateCcw, Settings, ShieldAlert, ShieldCheck, Trash2,
  UserRound, X,
} from 'lucide-vue-next'
import {
  createConversation, decidePendingAction, deleteConversation, bulkDeleteConversations, getConversation, listConversations,
  listMessages, onUnauthorized, request, RequestError, restoreConversation, resumeFor, submitMessage,
  updateConversation,
} from './api'
import AuthView from './AuthView.vue'
import {
  adoptRemoteSignOut, changePassword, expireSession, logout, logoutEverywhere, onSignedOut,
  refreshAuth, type Me, type SignOutReason,
} from './auth'
import {
  identityChanged, isStale, listenIdentityChange, newIdentityScope, trackRequest,
} from './identity'
import { purgeContentStorage } from './storageScope'
import { forget, patch as patchCached, recall, remember, resetCache } from './conversationCache'
import { share } from './inflight'
import { formatAbsolute, formatDay, formatMessageTime, stampOf } from './times'
import {
  DEFAULT_CONVERSATION_TITLE, businessIdsOf, conversationLabel, historyTurns, isWritable, listFilter,
  listPendingConversationIds, loadLastConversation, loadPending, mergeHistory, newIdempotencyKey, pendingBody, pendingInterruptOf,
  orderConversations, pendingMatches, runSubmission as runSubmissionTurn, saveLastConversation,
  savePending, unwritableHint, type HistoryTurn,
} from './conversations'
import { answerText, interruptComplete as interruptCompleteWith, toEvent } from './events'
import { renderMarkdown } from './markdown'
import { formatMinor } from './money'
import { EMPTY_SESSION, focusSession, peekSession, resetSessions, sessionOf, type ConversationSession, type LiveTurn } from './sessions'
import { isInterceptedReply, renderToolResult, toolErrorText } from './toolResult'
const AnalysisResult = defineAsyncComponent(() => import('./AnalysisResult.vue'))
import DemoControlBar from './DemoControlBar.vue'
import SubscriptionReminders from './SubscriptionReminders.vue'
import EmailSettings from './EmailSettings.vue'
import ImConnect from './ImConnect.vue'
import { demoStore } from './demoStore'
import LockBanner from './LockBanner.vue'
import PasswordInput from './PasswordInput.vue'
import QueryResultTable from './QueryResultTable.vue'
import BillAnalysisPrompt from './BillAnalysisPrompt.vue'
import ReportPlaceholder from './ReportPlaceholder.vue'
import { isAnalysisReport } from './analysisResult'
import {
  hydrateSettledReports, markSendPrompted, rememberSettledReports, resetSettledReports,
  reportFingerprint, settledReportsOf as storedReportsOf, type SettledReportEntry,
} from './reportStore'
import { clampDay, composeDate, daysInMonth, MONTHS, todayParts, yearOptions } from './dateFields'
import type { DateParts } from './dateFields'
import {
  BILL_RUNNING, billApi, formatBytes, remainingPasswordAttempts,
} from './bill'
import type { BillTaskView, BillTransactions } from './bill'
import type {
  AgentEvent, ClientContext, Conversation, ConversationMessageRequest,
  FinalEvent, InterruptEvent, LlmSettings, Session, SubtaskEndEvent, ToolResultEvent,
} from './types'

const session = ref<Session | null>(null)
/**
 * 服务端对这个账号的完整说明：角色、邮箱绑定、银行数据是否就绪。
 *
 * `session` 只有「是谁」，账号设置区还要显示「这个账号现在是什么状态」，所以两者分开存。
 * 它和 `session` 同生同灭 —— 清场时一起清，否则设置面板会替上一个人继续显示银行状态。
 */
const me = ref<Me | null>(null)
/** 登录页上的那句话：为什么回到了这里（过期、退出、改密、被别处退出）。 */
const loginNotice = ref('')
const connected = ref(false)
/** 后端 `demo_mode` 打开时才挂演示控制条；关掉它，页面和以前一模一样。 */
const demoMode = ref(false)
// 后端说演示模式开着，和「当前账号能看演示控制条」是两件事：时钟是全局的，只有管理员能拨，
// 所以这个 ref 先记后端的事实，登录之后再按角色收敛到 demoMode。
const demoAvailable = ref(false)
const modelConfigured = ref(false)
// 全局模型配置只有管理员能读写，普通用户看到按钮只会收到 403。
const isAdmin = ref(false)
/** The page is doing something that is about no conversation: entering the demo, or creating one. */
const starting = ref(false)
/** Opening a conversation, which is a read. It says so in the pane it is filling and leaves
 *  the rest of the page where it was: a switch that took the whole page over is what made
 *  every click feel like a freeze. */
const switching = ref(false)
/** A failure that belongs to no conversation, so the banner cannot be attributed to a pane. */
const pageError = ref('')
const pageTrace = ref('')
const draft = ref('')
const composerInput = ref<HTMLTextAreaElement | null>(null)
/** The abort handle of the turn currently streaming in each conversation. */
const turnStops = new Map<string, () => void>()
/** Conversations where the user has asked to stop the current ordinary turn. */
const stoppedTurns = new Set<string>()
// Passwords stay outside conversation caches and persisted submission bodies.
const transactionPassword = reactive({ old_password: '', new_password: '', confirm_password: '' })
function clearTransactionPassword() {
  transactionPassword.old_password = transactionPassword.new_password = transactionPassword.confirm_password = ''
}
/** The conversations whose row is mid-write, so one row's write does not lock the sidebar. */
const busyRows = reactive(new Set<string>())
const settingsOpen = ref(false)
/** 「IM 接入」面板（飞书扫码）。挂在这里而不是当页组件，是为了关闭时彻底停轮询。 */
const imOpen = ref(false)
const settingsBusy = ref(false)
const settingsError = ref('')
const settingsNotice = ref('')
/** 当前绑定的收件邮箱；`undefined` 表示未绑定。设置面板打开时读一次。 */
const userEmail = ref<string | undefined>(undefined)
const billOpen = ref(false)
/*
 * 上传流水卡片。它是一个纯 API 流程，不是聊天流程：附件密码绝不能经过模型，所以上传不走
 * graph，直接跟 `/api/bill/*` 打交道，卡片渲染的就是上一次响应说的那个状态。
 *
 * 入库完成后的收支统计不再展示（那读的是 `bill_summary` 的单文档缓存，与深度分析的口径
 * 不一致，常显示 0.00），改成自动请 agent 跑一次深度分析 —— 报告出现在对话里。
 */
const bill = ref<BillTaskView | null>(null)
const billForm = reactive({ bank_code: 'CMB', period_start: '', period_end: '' })
const billFile = ref<File | null>(null)
/**
 * 这份任务解析出来的流水，入库完成后直接显示在卡片里。
 *
 * 不经过模型：表格的第一页由卡片自己取，翻页也走同一个只读接口。用户传完一份流水先看到
 * 的是「这份账单里到底有什么」，再决定要不要分析 —— 而不是被自动塞一份分析报告。
 */
const billTransactions = ref<BillTransactions | null>(null)
/**
 * 用户对「要不要分析」的选择。选完**不销毁卡片**：流水表继续留在页面上，
 * 分析报告出现在对话里；只有一个明确的「关闭」才收起。
 */
const billDecision = ref<'analyzed' | 'skipped' | null>(null)
/** 还没去手机银行申请的那条支路。主路是直接上传。 */
const billGuide = ref(false)
const billPassword = ref('')
const billBusy = ref(false)
const billError = ref('')
/**
 * 解压在服务端后台线程里跑，这条流程没有 SSE 通道可推；卡片只能自己轮询，任务终态或
 * 卡片关掉时停。计时器只属于这一份任务，不是页面级的。
 */
let billPoll: number | null = null
const llmSettings = ref<LlmSettings | null>(null)
const llmForm = reactive({
  base_url: '', api_key: '', model: '', response_format: 'json_schema' as 'json_schema' | 'json_object',
  timeout_seconds: 30,
})
const messageList = ref<HTMLElement | null>(null)

// --- conversation view state -------------------------------------------------------------

const conversations = ref<Conversation[]>([])
const conversationCursor = ref<string | null>(null)
/**
 * The two list filters, one per server flag. They are independent -- neither implies the other --
 * so each toggle reveals exactly its own kind of conversation, and a row that is listed always
 * carries the marker for why it is not live.
 */
const showArchived = ref(false)
const showDeleted = ref(false)
const listBusy = ref(false)
const current = ref<Conversation | null>(null)
const renamingId = ref('')
const renameDraft = ref('')
/**
 * Multi-select for batch deletion. A plain object keyed by id is reactive under `ref`, so adding
 * or removing a key (and toggling a value) both re-render the sidebar. Only `true` entries count.
 *
 * Nothing is shown until the user enters multi-select: the rows carry no checkbox, so the ordinary
 * sidebar keeps the plain list it always was. `selectMode` is that mode and nothing else -- selection
 * itself stays in `selectedIds`, which is cleared whenever the mode is left.
 */
const selectMode = ref(false)
const selectedIds = ref<Record<string, boolean>>({})
const selectedCount = computed(
  () => Object.keys(selectedIds.value).filter(id => selectedIds.value[id]).length)
const allSelected = computed(
  () => conversations.value.length > 0 && selectedCount.value === conversations.value.length)

function isSelected(item: Conversation): boolean {
  return !!selectedIds.value[item.conversation_id]
}

function toggleSelect(item: Conversation): void {
  const next = { ...selectedIds.value }
  if (next[item.conversation_id]) delete next[item.conversation_id]
  else next[item.conversation_id] = true
  selectedIds.value = next
}

function toggleSelectAll(): void {
  if (allSelected.value) { selectedIds.value = {}; return }
  const next: Record<string, boolean> = {}
  for (const item of conversations.value) next[item.conversation_id] = true
  selectedIds.value = next
}

function clearSelection(): void {
  selectedIds.value = {}
}

/** Reveals the row checkboxes. Nothing is selected yet, and nothing has been deleted. */
function enterSelectMode(): void {
  clearSelection()
  selectMode.value = true
}

/** Leaves multi-select, dropping any selection that had not been committed. */
function exitSelectMode(): void {
  selectMode.value = false
  clearSelection()
}



const writable = computed(() => isWritable(current.value))
/** What the pane shows: the open conversation's own view state, or nothing at all. */
const pane = computed(() =>
  current.value ? peekSession(current.value.conversation_id) ?? EMPTY_SESSION : EMPTY_SESSION)
const paneError = computed(() => pane.value.error || pageError.value)
const paneTrace = computed(() => pane.value.trace || pageTrace.value)
/** The pane is working, or being read; either way nothing may be sent into it yet. */
const paneBusy = computed(() => pane.value.running || switching.value)
/**
 * The turns the pane draws: the ledger the conversation had when the live turn started, then the
 * live turn itself. Without the cut the user message and the reply would be drawn twice -- once
 * from the ledger the turn has since been written into, once from the stream still on screen.
 */
const paneTurns = computed(() => {
  const session = pane.value
  const turn = session.live
  if (!turn) return historyTurns(session.messages)
  const cut = turn.watermark
  // The ledger was empty when the turn started, so everything in it now came from this turn and the
  // live block below already draws it. `watermark === null` cannot mean "not read yet": a turn does
  // not start before its ledger is here (see `submitTurn`).
  if (cut === null) return []
  const at = session.messages.findIndex(item => item.message_id === cut)
  // A watermark that is gone must not hide history: draw everything rather than nothing.
  return historyTurns(at === -1 ? session.messages : session.messages.slice(0, at + 1))
})
function isRunning(conversation: Conversation): boolean {
  return peekSession(conversation.conversation_id)?.running === true
}
function isUnseen(conversation: Conversation): boolean {
  return peekSession(conversation.conversation_id)?.unseen === true
}
function unseenHint(conversation: Conversation): string {
  const session = peekSession(conversation.conversation_id)
  if (!session) return '已回复'
  if (session.pending) return '等你确认'
  // A turn that ended without a confirmed reply is not "replied": it is the one state a user has
  // to open to know whether anything happened at all.
  if (session.error || session.live?.unconfirmed) return '未完成'
  return '已回复'
}
function rowBusy(conversation: Conversation): boolean {
  return busyRows.has(conversation.conversation_id)
}
const composerDisabled = computed(() =>
  !session.value || paneBusy.value || !!pane.value.pending || pane.value.recoveryRequired
  || (!!current.value && !writable.value))
const composerHint = computed(() => {
  if (!session.value) return '尚未登录'
  // Writability comes first. An archived or deleted conversation accepts nothing at all,
  // including an answer to the question it is still showing, so telling the user to fill that
  // card in would be advice the server refuses.
  if (current.value && !writable.value) return unwritableHint(current.value)
  if (pane.value.pending) {
    // A confirmation box is answered by its own two buttons; a card that asks for fields is
    // answered by filling them in. The hint says which one is on screen -- and when the box
    // also needs a code, it says that too, because the confirm button is disabled until then.
    const card = pane.value.pending.confirmation
    if (!card) return '请先在上方补充信息'
    return card.requires_code
      ? `请先在上方填写${card.code_label}并确认，或取消本次操作`
      : '请先在上方确认或取消本次操作'
  }
  if (pane.value.running) return '本会话正在处理中，结束后才能发下一条'
  if (pane.value.recoveryRequired) return '上一次执行无法安全恢复，请新建会话'
  return '模拟环境'
})
/**
 * The fields a card asks the user to fill in.
 *
 * A confirmation box has none: its two buttons are the whole interaction, and the one
 * `decision` field it carries is the protocol's, not something to type into.
 */
const pendingFields = computed(() => (pane.value.pending?.confirmation ? [] : pane.value.pending?.fields ?? []))
const compact = (value: unknown) => JSON.stringify(value) ?? 'null'

/**
 * What the activity feed says a delegated sub-agent ended as.
 *
 * The event's own `status` speaks a three-value vocabulary with no word for "neither
 * succeeded nor failed", so a fund write whose outcome the bank never confirmed would be
 * shown as a failure there. The module's own status travels alongside it, and 待核实 is the
 * honest reading of it: the transaction may have gone through.
 */
function subtaskStatus(event: SubtaskEndEvent): string {
  return event.turn_status === 'unknown' ? '待核实' : event.status
}

/** When a confirmation box stops being valid, in the reader's own time zone. */
function expiresText(value: string | null): string {
  if (!value) return ''
  const at = new Date(value)
  return Number.isNaN(at.getTime()) ? '' : at.toLocaleString()
}

/** The day a conversation row shows: its latest message, or when it was created. */
function rowDay(conversation: Conversation): string {
  return formatDay(stampOf(conversation), new Date())
}

/** The same moment spelled out, for the row's tooltip. */
function rowStamp(conversation: Conversation): string {
  return formatAbsolute(stampOf(conversation))
}

/** The clock under one message, read against today. */
function messageTime(iso: string | null | undefined): string {
  return formatMessageTime(iso, new Date())
}

/** The activity feed's view of one tool result. Pure, so calling it per render is safe. */
function toolResultView(result: ToolResultEvent) {
  return renderToolResult(result.data)
}

/** Whether the view has anything to show: an empty result renders no box, not a blank one. */
/**
 * 判断是不是深度分析结果。
 *
 * `analyze_bill_comprehensive` 把整份报告放在信封的 `display` 里，`renderToolResult` 认不出
 * 这种形状 —— 少了这一支，报告只会显示成一行摘要。
 */
function analysisResultOf(result: ToolResultEvent) {
  return isAnalysisReport(result.data?.display)
}

/**
 * Whether the user's own message already asked for the report to be emailed.
 *
 * This is only a UI hint: it suppresses the automatic "send to email?" modal so the user
 * is not asked twice. The actual send still goes through the bill confirmation card.
 */
function wantsEmailSend(text: string | null | undefined): boolean {
  const value = String(text ?? '').toLowerCase()
  if (!value) return false
  // 否定表达优先：「不用发邮箱」「别发送到邮件」不能被当成要发送。
  if (/(不要|不用|不需要|别|不发|不发送).{0,12}(邮箱|邮件)/.test(value)) return false
  return (
    /(发送|发到|发至|发给|发我|发).{0,16}(邮箱|邮件)/.test(value)
    || /(邮箱|邮件).{0,16}(发送|发到|发我)/.test(value)
    || /send.{0,20}(email|mail)/.test(value)
  )
}

/**
 * 这一轮只要「把报告发到邮箱」：有发信意图，但没有分析意图。
 *
 * 「分析报告」是报告的名字，不是分析请求 —— 判断前先把它从文本里去掉，否则每一句
 * 「把账单分析报告发我邮箱」都会被当成要分析。命中这里的那一轮，页面不再铺分析报告：
 * 用户要的是收一封邮件，不是再看一遍已经看过的结果。反过来，「分析一下并发送到邮箱」
 * 里留着「分析」，仍会正常展示。
 */
function isSendOnlyRequest(text: string | null | undefined): boolean {
  if (!wantsEmailSend(text)) return false
  const cleaned = String(text ?? '').replace(/分析报告/g, '报告').replace(/报告分析/g, '报告')
  return !/(分析|统计|趋势|分类汇总)/.test(cleaned)
}

/** `bill_query_transactions` 的明细结果；只有它可以走分页表格。 */
interface QueryDisplay {
  total: number
  page: number
  page_size: number
  items: unknown[]
  period_start?: string
  period_end?: string
  /** 按「刚导入的这份账单」查出来的结果：翻页与后续分析都靠它。 */
  task_id?: string | null
}

function asQueryDisplay(value: unknown): QueryDisplay | null {
  if (typeof value !== 'object' || value === null) return null
  const display = value as Partial<QueryDisplay>
  if (!Array.isArray(display.items)) return null
  return display as QueryDisplay
}

function queryDisplayOf(result: ToolResultEvent): QueryDisplay | null {
  if (result.tool_name !== 'bill_query_transactions') return null
  return asQueryDisplay(result.data?.display)
}

function queryKey(display: QueryDisplay): string {
  // 任务号进键：两份不同账单的同一页、同一总数不能被当成同一份结果去重掉。
  return [display.task_id, display.period_start, display.period_end, display.page, display.total]
    .join('|')
}

/** 一条已经落到页面上的查询结果：它来自哪一次工具调用（没有 call_id 时退回内容）。 */
function queryEntryKey(entry: { callId: string; display: QueryDisplay }): string {
  return entry.callId || queryKey(entry.display)
}

/** Two tool results for the same page of the same period are one result, not two tables. */
function isSameQueryResult(event: AgentEvent, key: string): boolean {
  if (event.name !== 'tool_result' || event.data.tool_name !== 'bill_query_transactions') {
    return false
  }
  const display = queryDisplayOf(event.data)
  return display !== null && queryKey(display) === key
}

interface ReportPeriod {
  start: string
  end: string
}

function reportPeriodOf(display: unknown): ReportPeriod | null {
  if (typeof display !== 'object' || display === null) return null
  const range = (display as { report_meta?: { date_range?: { start?: unknown; end?: unknown } } })
    .report_meta?.date_range
  if (typeof range?.start !== 'string' || typeof range.end !== 'string') return null
  return { start: range.start, end: range.end }
}

/** The period carried by a send-report tool result. */
function sentPeriodOf(display: unknown): ReportPeriod | null {
  if (typeof display !== 'object' || display === null) return null
  const value = display as { period?: unknown; period_start?: unknown; period_end?: unknown }
  if (typeof value.period === 'string') {
    const [start, end] = value.period.split('~').map(part => part.trim())
    if (start && end) return { start, end }
  }
  if (typeof value.period_start === 'string' && typeof value.period_end === 'string') {
    return { start: value.period_start, end: value.period_end }
  }
  return null
}

/**
 * A successful email send means the analysis report for that period has already been asked
 * about (and sent). The auto "send to email?" modal must not open afterwards just because
 * the execute happened in a later turn than the analysis.
 */
function markReportsSentForPeriod(
  conversationId: string,
  session: ConversationSession,
  result: ToolResultEvent,
): void {
  const sent = sentPeriodOf(result.data?.display)
  if (!sent) return
  const matches = (display: unknown) => {
    const period = reportPeriodOf(display)
    return period?.start === sent.start && period?.end === sent.end
  }
  for (const event of session.live?.activity ?? []) {
    if (event.name !== 'tool_result' || event.data.error !== null) continue
    if (analysisResultOf(event.data) && matches(event.data.data?.display)) {
      markSendPrompted(event.data.data?.display)
    }
  }
  for (const entry of storedReportsOf(conversationId)) {
    if (matches(entry.display)) markSendPrompted(entry.display)
  }
}

function toolResultHasContent(result: ToolResultEvent) {
  // 分析结果和查询明细都从 `display` 绘制，不是文本块 —— 摘要为空时照样有东西可画。
  if (analysisResultOf(result) || queryDisplayOf(result)) return true
  const view = toolResultView(result)
  return view.text !== '' || view.blocks.length > 0
}

/** The server's reason for ending a turn, as one line for the user. `null` for a normal end. */
function terminalNotice(reply: FinalEvent): string | null {
  switch (reply.terminal_reason) {
    case 'api_error':
      return '回复未完整生成，请重试。'
    case 'max_turns':
      return '已达本轮处理上限，请分几次提出。'
    case 'aborted_streaming':
      return '本次回复已中断。'
    default:
      return null
  }
}

/** Reports a failure on the conversation it was about, or on the page when it was about neither. */
function showError(exc: unknown, conversationId?: string): void {
  // 换人时被掐断的请求不是错误，是安排：把它画成「请求失败」只会让新登录的人莫名其妙。
  if (exc instanceof DOMException && exc.name === 'AbortError') return
  const message = exc instanceof Error ? exc.message : '请求失败，请稍后重试'
  const requestId = exc instanceof RequestError ? exc.requestId : ''
  const target = conversationId ? sessionOf(conversationId) : null
  if (target) {
    target.error = message
    target.trace = requestId
  } else {
    pageError.value = message
    pageTrace.value = requestId
  }
  if (!(exc instanceof RequestError)) return
  switch (exc.code) {
    case 'SESSION_REQUIRED':
    case 'SESSION_MISMATCH':
      // 401 也由 `api.ts` 的统一出口报了同一次；这里只是保证「错误码到了就一定有反应」。
      expireSession()
      break
    case 'CONVERSATION_NOT_WRITABLE':
    case 'NOT_FOUND':
      void refreshConversations()
      break
    case 'INVALID_INTERRUPT_ANSWER':
      void refreshHistory(conversationId ?? current.value?.conversation_id ?? '')
      break
    case 'EXECUTION_RECOVERY_REQUIRED':
      if (target) target.recoveryRequired = true
      break
  }
}
/**
 * Runs one operation and reports a failure where it belongs: on the conversation it was about, or
 * on the page when it was about the page itself. There is no lock here any more -- the lock was
 * what stopped a running conversation from being switched away from.
 *
 * 身份在这一步也可能已经变了（退出、掉线、换号）。那种情况下失败属于上一个人：既不该写进
 * 会话，也不该出现在新登录的人眼前。
 */
async function attempt(target: string | null, work: () => Promise<void>): Promise<void> {
  const identity = newIdentityScope()
  if (target) {
    const view = sessionOf(target)
    view.error = ''
    view.trace = ''
  } else {
    pageError.value = ''
    pageTrace.value = ''
  }
  try {
    await work()
  } catch (exc) {
    if (isStale(identity)) return
    showError(exc, target ?? undefined)
  }
}
function scrollToEnd() {
  void nextTick(() => messageList.value?.scrollTo({ top: messageList.value.scrollHeight }))
}

// --- per-view state ----------------------------------------------------------------------

/**
 * Clears the page: there is no conversation open any more, so the draft, the row being renamed
 * and the page's own error all go with it. A conversation's own view is not here to clear -- it
 * lives in that conversation's session, which a switch leaves untouched.
 */
function resetView(): void {
  current.value = null
  focusSession(null)
  draft.value = ''
  renamingId.value = ''
  pageError.value = ''
  pageTrace.value = ''
}

function setPending(conversationId: string, card: InterruptEvent | null): void {
  const session = sessionOf(conversationId)
  const samePasswordAction = ['apply', 'change_password'].includes(card?.confirmation?.operation ?? '')
    && session.pending?.confirmation?.operation === card?.confirmation?.operation
    && session.pending?.question_id === card?.question_id
    && session.pending?.confirmation?.pending_id === card?.confirmation?.pending_id
  // The stream and its final detail read can deliver the same form twice while typing.
  if (!samePasswordAction && current.value?.conversation_id === conversationId) clearTransactionPassword()
  session.pending = card
  session.cardVerificationCode = ''
  session.cardAuthorizationError = ''
  if (!samePasswordAction) session.confirmationCode = ''
  for (const name of Object.keys(session.answers)) delete session.answers[name]
  if (card) {
    for (const field of card.fields) {
      // 日期字段一上来就是今天：三个下拉框默认选中的就是它，用户不动也能直接提交。
      if (field.type !== 'date') { session.answers[field.name] = ''; continue }
      const parts = todayParts()
      dateParts[field.name] = parts
      session.answers[field.name] = composeDate(parts)
    }
  }
}

// --- the conversation list ---------------------------------------------------------------

async function loadConversationList(options: { append?: boolean } = {}) {
  listBusy.value = true
  try {
    const page = await listConversations({
      limit: 30,
      cursor: options.append ? conversationCursor.value : null,
      // One flag per toggle, always sent: the server's default never decides what the list shows.
      ...listFilter({ archived: showArchived.value, deleted: showDeleted.value }),
    })
    conversations.value = options.append ? [...conversations.value, ...page.items] : page.items
    conversationCursor.value = page.next_cursor
  } finally { listBusy.value = false }
}

/**
 * A secondary read: whatever failed first is already on screen, so a failure here is not
 * repeated. It also re-syncs the open conversation, so a refusal caused by archiving or
 * deletion immediately closes the composer instead of leaving it looking writable.
 */
async function refreshConversations() {
  try {
    await loadConversationList()
    const open = current.value?.conversation_id
    if (!open) return
    const updated = conversations.value.find(item => item.conversation_id === open)
    if (updated) { current.value = updated; return }
    // An archived conversation is filtered out of the default list, so its absence there is
    // not evidence that it is gone: re-read it, and let its own flags decide. Whatever was
    // cached for it goes either way -- its flags are exactly what may have changed.
    forget(open)
    try { if (current.value) await loadDetail(current.value.conversation_id, { syncPending: false }) } catch { /* the banner already explains it */ }
  } catch { /* the banner already explains it */ }
}

/**
 * The naming call is the response's background work, so the list read that ends a turn happens
 * before the title exists. A few bounded re-reads close that window: the poll stops the moment
 * the title moves, and gives up rather than repeating forever (design spec D-3 / A-18).
 */
const TITLE_POLL_INTERVAL_MS = 1000
const TITLE_POLL_ATTEMPTS = 5

/** Re-reads the list while the server still shows the default title, then stops for good. */
async function pollForAutoTitle(conversationId: string): Promise<void> {
  for (let attempt = 0; attempt < TITLE_POLL_ATTEMPTS; attempt += 1) {
    const item = conversations.value.find(row => row.conversation_id === conversationId)
    if (!item || item.title.trim() !== DEFAULT_CONVERSATION_TITLE) return
    await new Promise(resolve => setTimeout(resolve, TITLE_POLL_INTERVAL_MS))
    await refreshConversations()
  }
}

async function loadMoreConversations() {
  await attempt(null, () => loadConversationList({ append: true }))
}

function applyConversation(updated: Conversation) {
  // The sidebar is the server's order, so a change has to be replayed through it: a conversation
  // that was just pinned belongs at the top now, and a rename has to land on its own row.
  // Whether a row still belongs in this list at all stays the server's call: an archived or
  // soft-deleted conversation keeps its row under its mark -- the way it always has -- so the same
  // row can undo the change, and the next refresh is what drops it.
  const known = conversations.value.some(item => item.conversation_id === updated.conversation_id)
  const items = known
    ? conversations.value.map(item =>
        item.conversation_id === updated.conversation_id ? updated : item)
    : [...conversations.value, updated]
  conversations.value = orderConversations(items)
  if (current.value?.conversation_id === updated.conversation_id) current.value = updated
  // The pane title and the sidebar name the same conversation, so a rename or an archive has
  // to reach the cache as well: otherwise the next switch back would paint the old name over
  // the one the sidebar is already showing.
  patchCached(updated.conversation_id, updated)
}

/**
 * Opens one conversation, letting a later click take over from an earlier one.
 *
 * A switch is a read, and reads may overlap: the row the user clicked last is the one they are
 * looking at, so it must not be dropped because the row before it is still loading. A write holds
 * only the row it is writing to shut, never the list. The generation keeps two overlapping
 * switches from fighting over the loading hint: only the newest one clears it.
 */
let switchGeneration = 0

async function switchConversation(conversation: Conversation): Promise<void> {
  const conversationId = conversation.conversation_id
  if (conversationId === current.value?.conversation_id) return
  const previous = current.value
  if (previous && previous.conversation_id !== conversationId) {
    const left = peekSession(previous.conversation_id)
    // Three things must survive the switch away: a turn still running, a result nobody has looked
    // at, and a turn that ended without a confirmed outcome. The last one has nothing in the
    // ledger to fall back on -- the server never sent a final -- so its streamed answer and its
    // 「结果未确认」 notice exist only in the live block, and dropping it would lose both.
    if (left && !left.running && !left.unseen && !left.live?.unconfirmed) left.live = null
  }
  const generation = ++switchGeneration
  switching.value = true
  try {
    await openConversation(conversation)
  } catch (exc) {
    showError(exc, conversationId)
  } finally {
    if (generation === switchGeneration) switching.value = false
  }
}

/**
 * Reads a conversation ahead of the click.
 *
 * A pointer resting on a row is a good guess at what comes next, and that read is the whole
 * cost of a switch. A read-ahead writes the cache and nothing else: the pane, the pending
 * question and the recovery banner belong to the conversation the user is actually in.
 */
const PREFETCH_DELAY_MS = 120
let prefetchTimer: ReturnType<typeof setTimeout> | null = null
let prefetching = false

function cancelPrefetch() {
  if (prefetchTimer === null) return
  clearTimeout(prefetchTimer)
  prefetchTimer = null
}

function prefetchConversation(conversation: Conversation) {
  const conversationId = conversation.conversation_id
  if (conversationId === current.value?.conversation_id) return
  // Already read, or already on its way here: the delay is not worth restarting.
  if (recall(conversationId)) return
  cancelPrefetch()
  prefetchTimer = setTimeout(() => {
    prefetchTimer = null
    void readAhead(conversationId)
  }, PREFETCH_DELAY_MS)
}

async function readAhead(conversationId: string) {
  // One at a time: a second read-ahead would only queue behind the first, holding a pool
  // connection that the conversation on screen may be waiting for.
  if (prefetching || recall(conversationId)) return
  prefetching = true
  try {
    // Each half is handed over the moment it lands, so a click that arrives while the other one
    // is still on its way can show the half that is already here.
    await Promise.all([
      share(firstPageKey(conversationId), () => listMessages(conversationId, { limit: 50 }))
        .then(page => remember(conversationId, { messages: page.items, historyCursor: page.next_cursor })),
      share(detailKey(conversationId), () => getConversation(conversationId))
        .then(detail => remember(conversationId, { detail })),
    ])
  } catch { /* a read-ahead that fails costs nothing: the click reads it again */ }
  finally { prefetching = false }
}

/** Opens one conversation: its ledger, and the interrupt the server is willing to confirm. */
async function openConversation(conversation: Conversation): Promise<void> {
  const conversationId = conversation.conversation_id
  current.value = conversation
  const session = sessionOf(conversationId)
  // Reports live in browser storage on purpose (the ledger never had them); load them before
  // the pane renders so a reload shows them attached to the turns they came from.
  hydrateSettledReports(conversationId)
  // The pane's own error is deliberately *not* cleared here: a turn that failed while this
  // conversation was in the background has to be visible when the user switches to it. It is
  // cleared when the next operation on this conversation starts (`attempt`).
  // The draft is the page's, and a switch clears it.
  renamingId.value = ''
  draft.value = ''
  // 图也是这一条会话的：换一条就不能留着上一条的图。planId 一起清（空串 = 没有图）。
  demoStore.resetTasks()
  // Looking at it is what the mark was for.
  session.unseen = false
  saveLastConversation(conversationId)

  focusSession(conversationId)

  const cached = recall(conversationId)
  if (cached?.messages) {
    session.messages = cached.messages
    session.historyCursor = cached.historyCursor ?? null
    session.historyLoaded = true
  }
  if (cached?.detail) {
    current.value = cached.detail.conversation
    session.recoveryRequired = cached.detail.recovery_required
    setPending(conversationId, pendingInterruptOf(cached.detail))
  }

  await Promise.all([loadHistory(conversationId), loadDetail(conversationId)])
  // 打开一条会话就停在最新一条。账本灌进来之前页面先空过一屏（切会话时尤其明显），
  // 浏览器把滚动位置钳在 0；不推到底的话，回来看到的永远是最早的那几条。
  // 只在它还是当前会话时滚：慢请求回来时人可能已经切走了，不能把别人的页面拽到底。
  if (current.value?.conversation_id === conversationId) scrollToEnd()
}

async function newConversation(): Promise<void> {
  // 新会话是一张白纸：旧图在创建请求飞着的时候就得消失，不能等 openConversation 回来。
  demoStore.resetTasks()
  starting.value = true
  try {
    await attempt(null, async () => {
      const created = await createConversation()
      await loadConversationList()
      await openConversation(created)
    })
  } finally {
    starting.value = false
  }
}

/** The in-flight keys of one conversation: its first page, one per earlier page, and its detail. */
const firstPageKey = (conversationId: string) => 'messages:' + conversationId + ':first'
const earlierPageKey = (conversationId: string, cursor: string) =>
  'messages:' + conversationId + ':after:' + cursor
const detailKey = (conversationId: string) => 'detail:' + conversationId

async function loadHistory(conversationId: string, options: { fresh?: boolean } = {}): Promise<void> {
  const session = sessionOf(conversationId)
  const identity = newIdentityScope()
  session.historyBusy = true
  try {
    // `share` 会把同键的并发读并成一个。一轮跑完时那次重读必须拿到「这一轮之后」的账本，
    // 并进一个更早的请求就会把用户刚说的话读没了 —— `fresh` 用一个不同的键绕开它。
    const key = options.fresh
      ? `${firstPageKey(conversationId)}#${Date.now()}`
      : firstPageKey(conversationId)
    const page = await share(key, () => listMessages(conversationId, { limit: 50 }))
    // 读回来时人已经换了：这份账本属于上一个账号，连缓存都不该进。
    if (isStale(identity)) return
    remember(conversationId, { messages: page.items, historyCursor: page.next_cursor })
    session.messages = page.items
    session.historyCursor = page.next_cursor
    session.historyLoaded = true
  } finally {
    session.historyBusy = false
  }
}

async function loadEarlier(): Promise<void> {
  const conversation = current.value
  if (!conversation) return
  const conversationId = conversation.conversation_id
  const session = sessionOf(conversationId)
  const cursor = session.historyCursor
  if (!cursor) return
  await attempt(conversationId, async () => {
    session.historyBusy = true
    try {
      const page = await share(earlierPageKey(conversationId, cursor), () => listMessages(conversationId, { limit: 50, after: cursor }))
      const merged = mergeHistory(session.messages, page.items)
      session.messages = merged
      session.historyCursor = page.next_cursor
      remember(conversationId, { messages: merged, historyCursor: page.next_cursor })
    } finally {
      session.historyBusy = false
    }
  })
}

async function loadDetail(conversationId: string, options: { syncPending?: boolean } = {}): Promise<void> {
  const session = sessionOf(conversationId)
  const identity = newIdentityScope()
  const detail = await share(detailKey(conversationId), () => getConversation(conversationId))
  if (isStale(identity)) return
  remember(conversationId, { detail })
  if (current.value?.conversation_id === conversationId) current.value = detail.conversation
  session.recoveryRequired = detail.recovery_required
  if (options.syncPending !== false) setPending(conversationId, pendingInterruptOf(detail))
}

async function refreshHistory(conversationId: string): Promise<void> {
  try {
    await loadHistory(conversationId)
    await loadDetail(conversationId)
  } catch {
    // The banner already explains it.
  }
}

/**
 * Holds one row's lock for the length of one write, and drops a second write to the same row.
 *
 * The sidebar's writes lock the row they write to and nothing else: one conversation's pin or
 * rename is no reason to hold the rest of the list shut. The lock is held only around the
 * request, so a failure releases it exactly like a success does.
 */
async function withRowLock<T>(conversationId: string, work: () => Promise<T>): Promise<T | undefined> {
  if (busyRows.has(conversationId)) return
  busyRows.add(conversationId)
  try {
    return await work()
  } finally {
    busyRows.delete(conversationId)
  }
}

async function togglePinned(conversation: Conversation) {
  const conversationId = conversation.conversation_id
  await withRowLock(conversationId, async () => {
    await attempt(conversationId, async () => {
      applyConversation(await updateConversation(conversationId, { pinned: !conversation.pinned }))
    })
  })
}

async function toggleArchived(conversation: Conversation) {
  // Archiving does not cancel a turn in flight, so a running conversation may not be archived:
  // it would keep working with nowhere to show its result.
  if (isRunning(conversation)) return
  const conversationId = conversation.conversation_id
  await withRowLock(conversationId, async () => {
    await attempt(conversationId, async () => {
      applyConversation(await updateConversation(conversationId, { archived: !conversation.archived }))
    })
  })
}

/** Soft delete: the conversation and its messages are kept, only visibility changes. */
async function removeConversation(conversation: Conversation) {
  // Soft deletion does not cancel a turn in flight either, so it is refused for the same reason.
  if (isRunning(conversation)) return
  const conversationId = conversation.conversation_id
  await withRowLock(conversationId, async () => {
    await attempt(conversationId, async () => {
      applyConversation(await deleteConversation(conversationId))
    })
  })
}

async function undeleteConversation(conversation: Conversation) {
  const conversationId = conversation.conversation_id
  await withRowLock(conversationId, async () => {
    await attempt(conversationId, async () => {
      applyConversation(await restoreConversation(conversationId))
    })
  })
}

/**
 * Deletes every checked conversation in one request. A running conversation is left to finish:
 * the server soft-deletes the row without disturbing an in-flight turn, which is the same
 * behaviour the single-delete button has always had.
 */
async function bulkRemoveSelected(): Promise<void> {
  const ids = Object.keys(selectedIds.value).filter(id => selectedIds.value[id])
  if (ids.length === 0) return
  if (!window.confirm(`确定删除选中的 ${ids.length} 个会话吗？删除后可在“显示已删除”中恢复。`)) return
  await attempt(null, async () => {
    await bulkDeleteConversations({ ids })
  })
  // Only a successful delete leaves the mode; a cancelled one keeps the selection on screen so
  // the user can reconsider rather than re-picking every row.
  exitSelectMode()
  await refreshConversations()
}

/** Wipes every conversation the user owns in one request. */
async function bulkRemoveAll(): Promise<void> {
  if (!window.confirm('确定删除全部会话吗？此操作不可撤销，但删除后可在“显示已删除”中恢复。')) return
  await attempt(null, async () => {
    await bulkDeleteConversations({ all: true })
  })
  exitSelectMode()
  await refreshConversations()
}

function startRename(conversation: Conversation) {
  renamingId.value = conversation.conversation_id
  renameDraft.value = conversation.title
}

async function commitRename(conversation: Conversation) {
  if (renamingId.value !== conversation.conversation_id) return
  const title = renameDraft.value.trim()
  if (!title || title === conversation.title) {
    // An unchanged or emptied title is a no-op write, but the editor still has to close.
    renamingId.value = ''
    return
  }
  const conversationId = conversation.conversation_id
  await withRowLock(conversationId, async () => {
    // The input closes only now that the lock is in hand: a rename whose blur fires while a
    // same-row write is still going keeps the edit on screen instead of dropping it silently.
    renamingId.value = ''
    await attempt(conversationId, async () => {
      applyConversation(await updateConversation(conversationId, { title }))
    })
  })
}

// --- submitting a turn -------------------------------------------------------------------

/**
 * Sends one message the user asked for.
 *
 * A new message always gets a new key: only a byte-identical repeat of the stored, unfinished
 * request -- the same conversation, the same text, the same answer -- reuses the stored key,
 * because that is the one case where the server would otherwise append the message twice and
 * run the graph twice. The record is stored with the exact request so a reload can repeat it.
 */
function prefillDraft(text: string): void {
  draft.value = text
  nextTick(() => composerInput.value?.focus())
}

/** Stops the ordinary reasoning turn in the open conversation; a confirmed bank write is not stoppable. */
function stopCurrentTurn(): void {
  const conversationId = current.value?.conversation_id
  if (!conversationId) return
  const abort = turnStops.get(conversationId)
  if (!abort) return
  stoppedTurns.add(conversationId)
  abort()
}

async function send(text: string, context?: ClientContext, target?: string): Promise<void> {
  const message = text.trim()
  if (!message || !session.value) return

  if (target) {
    // A card authorized while the user was looking elsewhere resumes *its own* conversation: the
    // target is named, because by the time the authorization comes back the pane may hold another.
    const named = peekSession(target)
    if (named && (named.running || named.pending || named.recoveryRequired)) return
    // The named conversation may have been archived while the authorization was in flight; the pane
    // refuses to write into one, and so does this path.
    const row = conversations.value.find(item => item.conversation_id === target)
    if (row && !isWritable(row)) return
  } else {
    // The guards are the open pane's own: another conversation's turn is none of this one's business.
    if (current.value && (paneBusy.value || pane.value.pending || pane.value.recoveryRequired || !writable.value)) return
    // A turn started before the ledger arrives would record an empty watermark and hide the ledger.
    if (switching.value) return

    if (!current.value) {
      await attempt(null, async () => {
        const created = await createConversation()
        await loadConversationList()
        current.value = created
        saveLastConversation(created.conversation_id)
      })
      if (!current.value) return
    }
  }

  const conversationId = target ?? current.value?.conversation_id
  if (!conversationId) return
  await attempt(conversationId, async () => {
    const body = context ? { message, client_context: context } : { message }
    const stored = loadPending(conversationId)
    const key = stored && pendingMatches(stored, conversationId, body) ? stored.key : newIdempotencyKey()
    savePending({ key, conversation_id: conversationId, message, ...(context ? { client_context: context } : {}) })
    await submitTurn(conversationId, body, key, message)
  })
}

/**
 * Enter sends the message; Shift+Enter starts a new line.
 *
 * The composition check is the load-bearing part, not a nicety: every message here is typed
 * through an input method editor, and while the IME is open Enter picks the candidate the user
 * is looking at. Treating that as "send" would submit a half-finished sentence -- and with
 * pinyin, often before a single Chinese character had been committed. `keyCode === 229` is the
 * older spelling of the same signal, still emitted by some engines when `isComposing` is not
 * set. A duplicate Enter is held off by the pane's own busy gate and by the disabled composer,
 * not by `send`, which has no re-entrancy guard of its own: the one window it leaves open is a
 * first turn still waiting for its ledger, because `submitTurn` sets `running` only after
 * `loadHistory` returns.
 */
function onComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey) return
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  void send(draft.value)
}

/**
 * Submits one already-decided request. Nothing here invents a key or a body: a repeat is
 * whatever the caller stored, and `runSubmissionTurn` keeps the stored record until the server
 * confirms an end to the request.
 */
async function submitTurn(
  conversationId: string,
  body: ConversationMessageRequest,
  key: string,
  userText: string,
): Promise<void> {
  // 这一轮属于「现在这个人」。流可能跑好几分钟，中途退出/掉线/换号之后它回来的每一帧都
  // 不能写进页面，更不能把报告写进下一个账号的本地存档。
  const identity = newIdentityScope()
  // A turn is not in the ledger, so whatever the cache holds for this conversation is stale.
  forget(conversationId)
  const session = sessionOf(conversationId)
  // 上一轮的 live 块马上就要被这一轮顶掉。它要是还没被 `settle` 收走过（那一轮没拿到
  // final、或账本没读到，所以仍留在台上），里面的分析报告会**凭空消失** —— 先留下来。
  keepSettledReports(conversationId, session.live)
  keepSettledQueries(conversationId, session.live)
  // The cut is recorded against the ledger, so the ledger has to be here first: `null` means "the
  // ledger was empty", and a ledger that merely has not arrived must not be read as empty -- that
  // would hide the whole of it once it does arrive. A replay after a reload starts a turn in a
  // conversation nobody has opened, which is exactly that case.
  if (!session.historyLoaded) await loadHistory(conversationId)
  const turn: LiveTurn = {
    user: userText,
    at: new Date().toISOString(),
    activity: [],
    reply: null,
    replyDraft: '',
    unconfirmed: false,
    // Everything the ledger already holds stays in the ledger: the live block draws only what
    // came after the turn started.
    watermark: session.messages.length ? session.messages[session.messages.length - 1].message_id : null,
  }
  session.live = turn
  session.running = true
  session.stoppable = true
  session.replyNotice = null
  stoppedTurns.delete(conversationId)
  draft.value = ''
  // A replay after a reload starts a turn in a conversation the user may not be looking at.
  if (current.value?.conversation_id === conversationId) scrollToEnd()

  const stream = trackRequest()
  turnStops.set(conversationId, stream.abort)
  const result = await runSubmissionTurn({
    conversationId,
    submit: onEvent => submitMessage(conversationId, body, key, onEvent, stream.signal),
    onEvent: (name, data) => {
      if (isStale(identity)) return
      const event = toEvent(name, data)
      if (!event) return
      if (event.name === 'tool_result' && event.data.error_code?.toUpperCase() === 'LOCKED') {
        demoStore.markLockedFromEvent()
      }
      // 任务图的两帧不进活动区：模板里没有它们的行，塞进去就是一条空白的 activity-item。
      // 也不按 demoMode 门控：抽屉关着时 store 只是个空壳，丢帧会让切到任务页的人看不见图。
      if (event.name === 'plan') {
        // 没有 plan_id 的图是「换一张」，不是「并进上一张」：旧图的状态对新图没有意义。
        if (!event.data.plan_id) demoStore.resetTasks()
        demoStore.setPlan(event.data.plan_id ?? '', event.data.nodes)
        return
      }
      if (event.name === 'task_update') {
        demoStore.updateTask(event.data)
        return
      }
      const view = sessionOf(conversationId)
      if (event.name === 'interrupt') view.pending = event.data
      else if (event.name === 'reply_delta' && view.live) view.live.replyDraft += event.data.text
      else if (event.name === 'narration' && view.live) {
        view.live.activity.push(event)
        view.live.replyDraft = ''
      } else if (event.name === 'final' && view.live) {
        view.live.reply = event.data
        view.replyNotice = terminalNotice(event.data)
      } else if (view.live) {
        if (event.name === 'tool_result') {
          const display = queryDisplayOf(event.data)
          if (display) {
            const key = queryKey(display)
            // 模型有时会为同一页连续调两次（例如先 page_size=100 再 20）；
            // 只保留最后一次，避免一页显示两张表。
            view.live.activity = view.live.activity.filter(
              existing => !isSameQueryResult(existing, key),
            )
          }
        }
        view.live.activity.push(event)
      }
      if (
        event.name === 'tool_result'
        && event.data.tool_name === 'bill_send_report_execute'
        && event.data.error === null
      ) {
        markReportsSentForPeriod(conversationId, view, event.data)
      }
      // A background turn must not scroll the pane the user is reading.
      if (current.value?.conversation_id === conversationId) scrollToEnd()
    },
  }).finally(() => stream.release())

  // 这一轮跑完了，但页面已经换人：账本、报告、重试标记都归上一个人，什么都不写。
  if (isStale(identity)) return

  session.canRetry = loadPending(conversationId) !== null
  try {
    if (result.kind === 'settled') {
      await settle(conversationId)
      return
    }
    // A stop the user asked for is a deliberate ending, not a transport failure: it gets a
    // plain notice and no error banner. If the server already settled, the answer above wins.
    if (stoppedTurns.delete(conversationId)) {
      if (session.live) session.live.unconfirmed = false
      session.replyNotice = '已停止本次任务。'
      return
    }
    if (result.kind === 'unfinished' && session.live) session.live.unconfirmed = true
    if (result.kind === 'refused' || result.kind === 'failed') throw result.error
  } finally {
    if (turnStops.get(conversationId) === stream.abort) turnStops.delete(conversationId)
    session.stoppable = false
    // Nothing may leave a conversation marked as running. `settle` has already cleared it on the
    // settled path; every other exit -- including a throw from the stream itself -- lands here.
    if (session.running) {
      session.running = false
      if (current.value?.conversation_id !== conversationId) session.unseen = true
    }
  }
}

/**
 * The turn is over: the record can go, and the ledger -- not this page -- becomes what the
 * view shows. A failed refresh keeps the streamed answer on screen rather than replacing it
 * with an error, because the answer is real even when the re-read is not.
 */
async function settle(conversationId: string): Promise<void> {
  const session = sessionOf(conversationId)
  const identity = newIdentityScope()
  let read = false
  try {
    // Read into the conversation the turn belonged to, whether or not it is on screen. Every read
    // writes its own conversation, so there is nothing left to race against.
    await Promise.all([loadHistory(conversationId, { fresh: true }), loadDetail(conversationId)])
    read = true
  } catch {
    // A failed re-read keeps the streamed answer on screen rather than replacing it with an error:
    // the answer is real even when the re-read is not. The sidebar refresh still has to run.
  } finally {
    session.running = false
  }
  // 重读期间换人了：报告属于上一个人，不能写进这一台机器上属于下一个人的存档。
  if (isStale(identity)) return
  // The mark does not depend on the re-read: a turn that ended while the user was somewhere else
  // still ended, and that is what the mark is for. Only dropping the live block depends on the read
  // having worked -- a failed one must not throw the streamed answer away.
  if (current.value?.conversation_id === conversationId) {
    // 账本里只有文本，工具结果不落库 —— `live` 一丢，分析报告卡片就跟着没了。
    // 报告是**内容**、不是进度，所以先留下来再丢 live，见 `reportStore.ts`。
    //
    // 但只在账本确实读到了这一轮用户说的那句话时才丢：读到旧账本（并发去重拿了更早的
    // 那一份）还照样丢，用户刚发的话和刚流出来的回答会一起从眼前消失，只剩报告。
    const arrived = !session.live
      || session.messages.some(message => message.role === 'user' && message.content === session.live?.user)
    if (read && arrived) {
      keepSettledReports(conversationId, session.live)
      keepSettledQueries(conversationId, session.live)
      session.live = null
    }
  } else {
    session.unseen = true
  }
  await refreshConversations()
  void pollForAutoTitle(conversationId)
}

/**
 * 已结算回合里留下的深度分析结果。
 *
 * 为什么不交给账本：`GET /api/conversations/{id}/messages` 只返回文本（`metadata` 里只有
 * `business_ids`），工具结果根本没有落库。所以一轮跑完、`settle` 丢掉 live 块之后，报告
 * 就消失了 —— 用户看到的就是「结果先出现，agent 一认定办完就没了」。
 *
 * 只留分析结果这一类：它是内容；其余工具事件是进度，留着只会越堆越乱。每条报告带
 * `anchorMessageId`，渲染时回到它所属的那一问下面，而不是永远沉在页面最底部。内存与浏览器
 * 存储都由 `reportStore.ts` 管：刷新后报告恢复，但不会再次弹「发到邮箱」。
 */
function settledReportsOf(): SettledReportEntry[] {
  return storedReportsOf(current.value?.conversation_id ?? '')
}

/** Reports belonging to one ledger turn (`historyTurns` keys turns by their user message id). */
function reportsForTurn(turn: { key: string; user?: { content?: string } | null }): SettledReportEntry[] {
  // 「只发送」的那一轮不铺报告：用户要的是邮件，不是再看一遍结果。
  if (isSendOnlyRequest(turn.user?.content)) return []
  return settledReportsOf().filter(entry => entry.anchorMessageId === turn.key)
}

/**
 * Reports the server stored with this turn's assistant message but this device has not loaded.
 *
 * On the device that generated the report the local copy wins (same fingerprint), so the full
 * report renders directly. Anywhere else these become file placeholders: the report travels
 * with the message, but it is not drawn until the user asks for it.
 */
function serverReportsForTurn(turn: HistoryTurn): unknown[] {
  // 同 `reportsForTurn`：只发送的那一轮，连占位卡也不出现。
  if (isSendOnlyRequest(turn.user?.content)) return []
  const local = new Set(settledReportsOf().map(entry => reportFingerprint(entry.display)))
  const seen = new Set<string>()
  const reports: unknown[] = []
  for (const reply of turn.replies) {
    const stored = reply.metadata?.analysis_reports
    if (!Array.isArray(stored)) continue
    for (const display of stored) {
      if (!isAnalysisReport(display)) continue
      const fingerprint = reportFingerprint(display)
      if (!fingerprint || local.has(fingerprint) || seen.has(fingerprint)) continue
      seen.add(fingerprint)
      reports.push(display)
    }
  }
  return reports
}

/**
 * Loads a cloud report into this device and shows it in place of the placeholder.
 *
 * It is deliberately written to the same local store as a freshly generated report: the user
 * asked for the file, so from now on this device has it. Marking it prompted first keeps the
 * "send to email" dialog from popping up for a historical report.
 */
function openServerReport(turn: HistoryTurn, display: unknown): void {
  const conversationId = current.value?.conversation_id
  if (!conversationId || !isAnalysisReport(display)) return
  markSendPrompted(display)
  rememberSettledReports(conversationId, [display], turn.key)
}

/**
 * Reports we could not tie to a ledger turn. They are still rendered, at the end, rather than
 * dropped: losing a report because its anchor is temporarily missing would be worse than showing
 * it in the old place until the history page that contains it is loaded.
 */
const looseSettledReports = computed(() =>
  settledReportsOf().filter(entry => entry.anchorMessageId === null))

/**
 * The ledger turn a finished report belongs to: the user message that opened it.
 *
 * `turn.watermark` is the last ledger id before the turn starts, so searching after it cannot
 * pick up an earlier identical question. A turn that never made it into the ledger (the rare
 * path where `submitTurn` rescues an unsettled live block) falls back to the last user turn we
 * actually have; with no ledger at all the entry stays loose and renders at the end.
 */
function settledReportAnchor(session: ConversationSession, turn: LiveTurn): string | null {
  const cut = turn.watermark
    ? session.messages.findIndex(message => message.message_id === turn.watermark) + 1
    : 0
  const messages = cut > 0 ? session.messages.slice(cut) : session.messages
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message.role === 'user' && message.content === turn.user) return message.message_id
  }
  const lastUser = [...messages].reverse().find(message => message.role === 'user')
  if (lastUser) return lastUser.message_id
  for (let index = session.messages.length - 1; index >= 0; index -= 1) {
    if (session.messages[index].role === 'user') return session.messages[index].message_id
  }
  return null
}

function keepSettledReports(conversationId: string, turn: LiveTurn | null): void {
  if (!turn) return
  // 「只发送」的那一轮不留下报告：它不该在结算后又被铺出来，也不该在别的设备上
  // 变成占位卡。模型即便仍调了分析（提示词已经要求它别调），页面也不展示。
  if (isSendOnlyRequest(turn.user)) return
  const displays = turn.activity.flatMap(event =>
    event.name === 'tool_result' && event.data.error === null && analysisResultOf(event.data)
      ? [event.data.data?.display]
      : [])
  if (!displays.length) return
  // 这一轮里报告**已经真的发出去了**：等 live 块丢掉之后，副本不该再问一次。
  // 看的是**这一轮自己**的 activity，不是当前面板的 —— 收尾时那一轮可能不在台前。
  // 认的是执行工具 `bill_send_report_execute`，不是准备工具 `bill_send_report`：
  // 后者的 tool_result 在用户确认**之前**就记下了，拿它当「已发送」会让用户一取消
  // 就永远等不到弹窗（见 `backend/app/modules/bill/workflow.py:402-410`）。
  const sent = turn.activity.some(event =>
    event.name === 'tool_result'
    && event.data.tool_name === 'bill_send_report_execute'
    && event.data.error === null)
  if (sent) displays.forEach(markSendPrompted)

  const anchorMessageId = settledReportAnchor(sessionOf(conversationId), turn)
  rememberSettledReports(conversationId, displays, anchorMessageId)
}

/**
 * 查询明细工具的结果。它和深度分析一样是“内容”，但只需要留在当前页面会话里：
 * 翻页可以按期间重新请求 HTTP 接口，不需要把明细写进消息账本或 localStorage。
 */
interface SettledQuery {
  callId: string
  anchorMessageId: string | null
  display: QueryDisplay
}

const settledQueries = reactive<Record<string, SettledQuery[]>>({})

/**
 * 已经决定过的「这份流水要不要分析」——记在**某一张查询结果**上，并且每个会话各记各的。
 *
 * 以前按「任务号 / 期间」记、还不分会话：只要点过一次「分析」或「先不用」，同一个期间
 * 之后在任何会话里查出来都不再问，屏幕上就是「按钮时有时无」。查询结果是一份一份出来的，
 * 选择也该跟着这一份走：这一份点过就不再问，下一次查出来的新表下面照旧有。
 */
const handledAnalysisPrompts = reactive(new Set<string>())

/** 「会话 + 这一份结果」拼成一个键：切回旧会话不重复问，换个会话问的是另一份结果。 */
function promptKeyOf(conversationId: string, resultId: string): string {
  // 用 `\u0000` 分隔：会话 id 与结果 id 都是服务端给的普通字符串，这个字符不会出现。
  return `${conversationId}\u0000${resultId}`
}

/**
 * 已经出过报告的账单任务号。刷新之后本地的「处理过」记录没了，但只要对话里有那份报告，
 * 就不该再问一次「要分析吗」。
 */
const analyzedTaskIds = computed(() => {
  const ids = new Set<string>()
  const add = (display: unknown) => {
    const taskId = (display as { report_meta?: { task_id?: unknown } } | null)
      ?.report_meta?.task_id
    if (typeof taskId === 'string' && taskId) ids.add(taskId)
  }
  for (const entry of settledReportsOf()) add(entry.display)
  for (const turn of paneTurns.value) {
    for (const reply of turn.replies ?? []) {
      const stored = reply.metadata?.analysis_reports
      if (Array.isArray(stored)) stored.forEach(add)
    }
  }
  return ids
})

/** 一次「要分析吗」的范围：按文件查的带任务号，按日期查的带起止日期。 */
interface AnalysisPromptScope {
  /** 记住「问过了」的键：会话 + 这一份查询结果。 */
  key: string
  taskId: string | null
  periodStart: string
  periodEnd: string
}

/**
 * 这张表要不要显示「分析」那一行；不需要就返回 null。
 *
 * `resultId` 指的是**这一份查询结果**（工具调用的 call_id，刷新后恢复的表用位置），
 * 不是它覆盖的期间 —— 同一个期间查两次就是两份结果，各自可以问一次。
 */
function analysisPromptFor(
  display: QueryDisplay | null | undefined,
  resultId: string,
): AnalysisPromptScope | null {
  if (!display) return null
  const taskId = typeof display.task_id === 'string' && display.task_id ? display.task_id : null
  const periodStart = display.period_start ?? ''
  const periodEnd = display.period_end ?? ''
  if (!taskId && (!periodStart || !periodEnd)) return null
  const key = promptKeyOf(current.value?.conversation_id ?? '', resultId)
  if (handledAnalysisPrompts.has(key)) return null
  // 已经有这份账单的报告了，就不再问。
  if (taskId && analyzedTaskIds.value.has(taskId)) return null
  return { key, taskId, periodStart, periodEnd }
}

/** 实时活动里的那一条（工具一返回就画出来的表）。 */
function liveAnalysisPrompt(result: ToolResultEvent): AnalysisPromptScope | null {
  return analysisPromptFor(queryDisplayOf(result), `call:${result.call_id}`)
}

/** 结算之后留在页面上的那一份。 */
function settledAnalysisPrompt(entry: SettledQuery): AnalysisPromptScope | null {
  return analysisPromptFor(entry.display, `call:${entry.callId}`)
}

/** 刷新后从消息 metadata 恢复的那一份：没有 call_id，用回合 + 位置认它。 */
function restoredAnalysisPrompt(
  turnKey: string,
  index: number,
  display: QueryDisplay,
): AnalysisPromptScope | null {
  return analysisPromptFor(display, `${turnKey}#query-${index}`)
}

/** 点「分析」：按文件查的用任务号，按日期查的把期间说清楚。 */
function analyzeQueryScope(scope: AnalysisPromptScope): void {
  if (paneBusy.value || pane.value.pending) return
  handledAnalysisPrompts.add(scope.key)
  void send(
    scope.taskId
      ? `分析刚导入的流水（任务 ${scope.taskId}）`
      : `分析 ${scope.periodStart} 到 ${scope.periodEnd} 的流水`,
  )
}

function dismissAnalysisPrompt(key: string): void {
  handledAnalysisPrompts.add(key)
}

function queriesForTurn(turn: { key: string }): SettledQuery[] {
  return (settledQueries[current.value?.conversation_id ?? ''] ?? [])
    .filter(entry => entry.anchorMessageId === turn.key)
}

function keepSettledQueries(conversationId: string, turn: LiveTurn | null): void {
  if (!turn) return
  const found = turn.activity.flatMap(event => {
    if (event.name !== 'tool_result' || event.data.error !== null) return []
    const display = queryDisplayOf(event.data)
    return display ? [{ callId: event.data.call_id, display }] : []
  })
  if (!found.length) return

  const anchorMessageId = settledReportAnchor(sessionOf(conversationId), turn)
  const existing = settledQueries[conversationId] ?? []
  // 按**这次工具调用**去重，不按表的内容：同一个期间查两次是两份结果，第二份也要留在
  // 它自己那一问下面（以前按「期间 + 页 + 总数」认，第二张表会被当成重复丢掉，连带
  // 表下面那行「要分析吗」一起没了）。同一份结果被收两遍时 call_id 相同，仍然只留一份。
  const seen = new Set(existing.map(entry => queryEntryKey(entry)))
  const added = found
    .filter(entry => {
      const key = queryEntryKey(entry)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    .map(entry => ({ ...entry, anchorMessageId }))
  if (added.length) settledQueries[conversationId] = [...existing, ...added]
}

/**
 * Query pages persisted with the assistant message. This is what makes the table survive a
 * page reload; only the page that was on screen is stored, so the component renders it as a
 * static snapshot and tells the user to re-query for the full detail.
 */
function serverQueryResultsForTurn(turn: HistoryTurn): QueryDisplay[] {
  const local = new Set(queriesForTurn(turn).map(entry => queryKey(entry.display)))
  const seen = new Set<string>()
  const results: QueryDisplay[] = []
  for (const reply of turn.replies) {
    const stored = reply.metadata?.query_results
    if (!Array.isArray(stored)) continue
    for (const value of stored) {
      const display = asQueryDisplay(value)
      if (!display) continue
      const key = queryKey(display)
      if (local.has(key) || seen.has(key)) continue
      seen.add(key)
      results.push(display)
    }
  }
  return results
}

async function repeatPending(conversationId: string): Promise<void> {
  const record = loadPending(conversationId)
  if (!record || record.conversation_id !== conversationId) return
  await submitTurn(conversationId, pendingBody(record), record.key, record.message)
}

async function retryPending(): Promise<void> {
  const conversationId = current.value?.conversation_id
  if (!conversationId) return
  const record = loadPending(conversationId)
  if (!record) return
  await attempt(conversationId, async () => {
    await submitTurn(conversationId, pendingBody(record), record.key, record.message)
  })
}

async function submitInterrupt() {
  const conversationId = current.value?.conversation_id
  const session = conversationId ? sessionOf(conversationId) : null
  if (!conversationId || !session) return
  const card = session.pending
  if (!card || session.running) return
  // A confirmation box is not answered by this function at all: its two buttons call
  // `submitAction`, which submits a structured button event. Typing an answer here would be
  // a chat message, and a chat message is not an authorization.
  if (card.confirmation) return
  const value: Record<string, unknown> = {}
  const summary: string[] = []
  for (const field of card.fields) {
    const answer = answerText(session.answers[field.name])
    value[field.name] = answer
    summary.push(`${field.label}：${answer || '未填写'}`)
  }
  // The card is consumed by this submission; if the server refuses it, the confirmed question
  // is re-read from the server rather than kept from this page.
  setPending(conversationId, null)
  await send(`补充信息：${summary.join('；')}`, resumeFor(card, value), conversationId)
}

/**
 * Whether the ledger's last turn has no reply *because the turn paused*, not because its
 * outcome is unknown.
 *
 * The ledger alone cannot tell those apart: a turn that ended on a question has no assistant
 * message, exactly like a turn whose stream died. The open card is what tells them apart --
 * and it is conclusive, because the server refuses a new message while a question is open, so
 * a card can only belong to the last turn. Without this, a confirmation box would be shown
 * under a banner saying the result is unconfirmed, which reads as if the click had already
 * gone wrong.
 */
function pausedOnThisTurn(turn: { key: string }): boolean {
  const tail = paneTurns.value[paneTurns.value.length - 1]
  return !!pane.value.pending && tail?.key === turn.key
}

/** A required field left blank would be rejected by the server and burn one of the three
 *  ask attempts, so it is blocked here instead. The server still validates independently. */
function interruptComplete(card: InterruptEvent): boolean {
  return interruptCompleteWith(card, pane.value.answers)
}

/**
 * Whether the open confirmation box still needs something before it can be confirmed.
 *
 * A box whose operation the bank will only authorise with a code cannot be confirmed without
 * one. Blocking it here is advice, not enforcement -- the route refuses the same click with
 * `VERIFICATION_CODE_REQUIRED`, and the module refuses it again -- but it is the difference
 * between a button that explains itself and one that fails after the user clicks it.
 */
const confirmationReady = computed(() => {
  const card = pane.value.pending?.confirmation
  if (!card) return true
  const passwordsReady = !['apply', 'change_password'].includes(card.operation) || (
    /^[0-9]{6}$/.test(transactionPassword.new_password)
    && transactionPassword.new_password === transactionPassword.confirm_password
    && (card.operation === 'apply' || (/^[0-9]{6}$/.test(transactionPassword.old_password)
      && transactionPassword.old_password !== transactionPassword.new_password))
  )
  return passwordsReady && (!card.requires_code || pane.value.confirmationCode.trim() !== '')
})
watch(() => current.value?.conversation_id, clearTransactionPassword)

/**
 * One click on a confirmation box.
 *
 * It is submitted to the button route and never typed into the composer: the click is a
 * structured event that names the pending operation it confirms, while a sentence that says
 * 确认 is an ordinary message and is not an authorization. The code, when the box asked for
 * one, travels with the click and nowhere else.
 *
 * The card is consumed by the click; if the server refuses it -- a stale card, a preview that
 * has since been replaced -- the ledger and the confirmed question are re-read rather than
 * kept from this page. A rejected *code* is not that case: it comes back as an ordinary turn
 * result, so the box stays open and the user can retype.
 */
async function submitAction(decision: 'confirm' | 'cancel') {
  const conversationId = current.value?.conversation_id
  const session = conversationId ? sessionOf(conversationId) : null
  if (!conversationId || !session) return
  const card = session.pending
  const confirmation = card?.confirmation
  if (!card || !confirmation || session.running) return
  const code = decision === 'confirm' ? session.confirmationCode.trim() : ''
  if (decision === 'confirm' && confirmation.requires_code && !code) return
  if (decision === 'confirm' && !confirmationReady.value) return
  const passwords = decision === 'confirm' && ['apply', 'change_password'].includes(confirmation.operation) ? {
    new_password: transactionPassword.new_password, confirm_password: transactionPassword.confirm_password,
    ...(confirmation.operation === 'change_password' ? { old_password: transactionPassword.old_password } : {}),
  } : undefined
  clearTransactionPassword()
  const userText = decision === 'confirm' ? '确认本次操作' : '取消本次操作'
  const identity = newIdentityScope()
  await attempt(conversationId, async () => {
    const turn: LiveTurn = {
      user: userText,
      at: new Date().toISOString(),
      activity: [],
      reply: null,
      replyDraft: '',
      unconfirmed: false,
      // Everything the ledger already holds stays in the ledger: the live block draws only what
      // came after the turn started.
      watermark: session.messages.length ? session.messages[session.messages.length - 1].message_id : null,
    }
    session.live = turn
    session.running = true
    session.stoppable = false
    // A new turn retires the previous turn's notice, exactly as `submitTurn` does.
    session.replyNotice = null
    session.confirmationCode = ''
    setPending(conversationId, null)
    scrollToEnd()
    let settled = false
    const stream = trackRequest()
    try {
      await decidePendingAction(
        conversationId,
        confirmation.pending_id,
        {
          question_id: card.question_id,
          decision,
          ...(code ? { verification_code: code } : {}),
          ...(passwords ? { transaction_password: passwords } : {}),
        },
        newIdempotencyKey(),
        (name, data) => {
          if (isStale(identity)) return
          const event = toEvent(name, data)
          if (!event) return
          if (event.name === 'tool_result' && event.data.error_code?.toUpperCase() === 'LOCKED') {
            demoStore.markLockedFromEvent()
          }
          // 与提交那条流同样的两条：图归 store，活动区不收（模板没有它们的行）。
          if (event.name === 'plan') {
            if (!event.data.plan_id) demoStore.resetTasks()
            demoStore.setPlan(event.data.plan_id ?? '', event.data.nodes)
            return
          }
          if (event.name === 'task_update') {
            demoStore.updateTask(event.data)
            return
          }
          if (event.name === 'interrupt') { setPending(conversationId, event.data); settled = true }
          else if (event.name === 'final') { if (session.live) session.live.reply = event.data; settled = true }
          else if (session.live) session.live.activity.push(event)
          scrollToEnd()
        },
        stream.signal,
      )
      if (!settled) {
        // The stream ended without final, interrupt or receipt: the ledger is the only way to
        // find out what happened, and this page must not claim the click was not submitted.
        if (session.live) session.live.unconfirmed = true
        return
      }
      // The re-read belongs to the turn: `settle` is what clears `running`, so it has to run
      // while the flag is still up. Clearing the flag first left a window in which a message
      // could start a second turn that this re-read then clobbered.
      await settle(conversationId)
    } catch (exc) {
      session.live = null
      // 被中止（换人）的那一次不必再回读：账本属于上一个账号。
      if (!isStale(identity)) await refreshHistory(conversationId)
      throw exc
    } finally {
      stream.release()
      // Nothing may leave a conversation marked as running. `settle` has already cleared it on the
      // settled path; every other exit -- including a throw from the stream itself -- lands here.
      if (session.running) {
        session.running = false
        if (current.value?.conversation_id !== conversationId) session.unseen = true
      }
    }
  })
}

async function handleUnlock() {
  try {
    await demoStore.resetLock()
    if (current.value) {
      const s = sessionOf(current.value.conversation_id)
      s.replyNotice = '已解除资金工具锁定，资金操作已恢复'
    }
  } catch {
    // 错误已由 store 记录
  }
}

// --- session, settings and startup --------------------------------------------------------

/** 四种离场各自给一句话。用户要能分辨「我自己退的」和「我被踢下来了」。 */
const SIGNED_OUT_NOTICE: Record<SignOutReason, string> = {
  expired: '登录已过期，请重新登录',
  logout: '已退出登录',
  password: '密码已修改，请重新登录',
  'logout-all': '已退出全部设备，请重新登录',
}

/**
 * 清空这一个账号在页面上留下的一切：会话、账本、报告、草稿、待答问题、上传卡片、邮箱、
 * 演示状态、模型配置。
 *
 * 为什么是一处集中清、而不是各页自己管：漏一个变量就是把上一个人的内容留给下一个人看，
 * 而这种漏不会有任何报错。身份一变（退出、掉线、改密、被别处退出）也只走这里。
 *
 * 本地存档（`localStorage`）由 `auth.signOut` 负责 —— 那里是唯一知道「这次是哪种离场」的
 * 地方，而且它要在通知页面之前就把键清掉。
 */
function teardownWorkspace(): void {
  // 在飞的切换/读取回来了也不许再写：世代 +1 之后它们的守卫全部失效。
  switchGeneration += 1
  stopBillPolling()
  billOpen.value = false
  bill.value = null
  billFile.value = null
  billPassword.value = ''
  billError.value = ''
  billForm.period_start = ''
  billForm.period_end = ''
  billGuide.value = false
  billBusy.value = false

  session.value = null
  me.value = null
  current.value = null
  conversations.value = []
  conversationCursor.value = null
  draft.value = ''
  renamingId.value = ''
  renameDraft.value = ''
  busyRows.clear()
  pageError.value = ''
  pageTrace.value = ''
  starting.value = false
  switching.value = false
  listBusy.value = false

  settingsOpen.value = false
  settingsBusy.value = false
  settingsError.value = ''
  settingsNotice.value = ''
  accountPassword.current = ''
  accountPassword.next = ''
  accountPassword.confirm = ''
  accountBusy.value = false
  accountError.value = ''
  accountNotice.value = ''
  userEmail.value = undefined
  llmSettings.value = null
  llmForm.api_key = ''

  isAdmin.value = false
  demoMode.value = false
  demoStore.resetTasks()

  // 三个「按会话按键」的内存表：会话视图、读过的缓存、已结算报告与查询明细。
  resetSessions()
  resetCache()
  resetSettledReports()
  for (const key of Object.keys(settledQueries)) delete settledQueries[key]
  // 「问过没有」是上一个账号的判断，换人之后问的是别人的流水。
  handledAnalysisPrompts.clear()
}

/** 服务端说人换了（或第一次说他是谁）：把身份装进页面，必要时先清场。 */
function adoptIdentity(who: Me): void {
  const previous = session.value?.user_id ?? null
  if (previous !== who.user_id) {
    // 换人：上一个人的界面先收干净，再让这个人的东西进来。
    if (previous) teardownWorkspace()
    // 这台机器上不属于当前账号的内容存档（含上一个版本没有账号段的键）一并清掉。
    // 报告本地副本是「本机刷新更快」的加速通道，不是别人的历史，没有理由跨账号留着。
    purgeContentStorage()
    identityChanged('login', { broadcast: false })
  }
  session.value = { session_id: '', user_id: who.user_id, login_name: who.login_name }
  me.value = who
  isAdmin.value = who.role === 'admin'
  demoMode.value = demoAvailable.value && isAdmin.value
  if (demoMode.value) void demoStore.loadClock()
}

/** 任何一条离场路径的落点：收干净，并在登录页说明为什么。 */
function handleSignedOut(reason: SignOutReason): void {
  teardownWorkspace()
  loginNotice.value = SIGNED_OUT_NOTICE[reason]
}

/**
 * After a successful login (or on page load when a session already exists): read who we are,
 * then restore the view.
 *
 * `session` is only ever filled from the server's answer. There is no local "already entered"
 * flag any more — that flag is what let a page reload keep showing the previous account's
 * conversations.
 */
async function enterWorkspace() {
  starting.value = true
  loginNotice.value = ''
  try {
    await attempt(null, async () => {
      const who = await refreshAuth()
      if (!who) {
        session.value = null
        return
      }
      // 身份先落定，再碰任何本地存档：恢复视图会读报告与待提交正文，它们按账号分键。
      adoptIdentity(who)
      await restoreView()
      await loadUserEmail()
    })
  } finally {
    starting.value = false
  }
}

async function handleLogout() {
  try {
    // 成功时 `logout()` 已经走完 signOut：账号状态、本地存档、世代、广播、界面清理。
    await logout()
  } catch (exc) {
    // 退出请求没发出去就不假装退出：服务端的登录态还在，刷新一下就会回来。
    showError(exc)
  }
}

async function bootstrap() {
  starting.value = true
  try {
    const health = await request<{ model_configured: boolean; demo_mode?: boolean }>('/health')
    connected.value = true; modelConfigured.value = health.model_configured
    // 演示开关跟着 health 走：后端说开着才挂控制条，没说就当它关着。
    demoAvailable.value = health.demo_mode === true
    // 条一挂出来就得有银行的时间，否则它先是「点击查看」、要等用户点开抽屉才变成真时间。
    // 这是挂载时唯一为演示条发的请求：抽屉关着的时候没有任何别的演示请求（见 DemoControlBar）。
  } catch (exc) {
    // 健康检查失败也要把登录框放出来：停在「服务未连接」的登录页，比停在一片空白有用。
    showError(exc)
  } finally {
    starting.value = false
  }
  await enterWorkspace()
}

/**
 * Page load: the list, then the conversation last used, then only the interrupt the server
 * confirms. An unfinished submission is repeated under its stored key -- the server answers a
 * repeat with a receipt and runs no graph, so nothing is re-invoked by reopening the page.
 */
async function restoreView(): Promise<void> {
  await loadConversationList()
  const lastUsed = loadLastConversation()
  const target = conversations.value.find(item => item.conversation_id === lastUsed)
    ?? conversations.value[0]
    ?? null
  if (!target) {
    resetView()
    return
  }
  await openConversation(target)
  // Every conversation that still owes the server a request repeats it, under its own key, in its
  // own session -- one unfinished turn no longer hides the others.
  // The legacy single slot is not in the list, so the conversation on screen joins the sweep.
  for (const conversationId of new Set([target.conversation_id, ...listPendingConversationIds()])) {
    await attempt(conversationId, async () => {
      await repeatPending(conversationId)
    })
  }
  // The line this replaces also set the retry flag for the conversation on screen; keep it, or a
  // reload with a still-pending submission loses its 重试发送 button.
  sessionOf(target.conversation_id).canRetry = loadPending(target.conversation_id) !== null
}

function applyLlmSettings(value: LlmSettings) {
  llmSettings.value = value
  // The header badge reads the same flag the API reports, so saving, clearing or reloading
  // the configuration updates it without a page refresh.
  modelConfigured.value = value.configured
  llmForm.base_url = value.base_url
  llmForm.model = value.model
  llmForm.response_format = value.response_format
  llmForm.timeout_seconds = value.timeout_seconds
  llmForm.api_key = ''
}
async function loadLlmSettings() {
  applyLlmSettings(await request<LlmSettings>('/settings/llm'))
}
/**
 * 打开设置面板。
 *
 * 面板里三块东西的可见范围不同：账号与收件邮箱是**每个人自己的**，模型配置只有管理员能动。
 * 所以入口对所有人开放，模型那一块按角色隐藏 —— 以前整个入口只给管理员，普通用户连自己的
 * 邮箱和密码都改不了。
 */
async function openSettings() {
  settingsOpen.value = true; settingsError.value = ''; settingsNotice.value = ''
  settingsBusy.value = true
  try {
    if (isAdmin.value) await loadLlmSettings()
    // 打开时再读一次：面板要显示它，晚一拍会让它先显示成「未绑定」，用户得关掉再打开才看得见。
    await loadUserEmail()
    await loadMe()
  } catch (exc) { settingsError.value = exc instanceof Error ? exc.message : '读取配置失败' }
  finally { settingsBusy.value = false }
}
/**
 * 重读账号状态（银行数据是否就绪、邮箱、角色）。
 *
 * 只更新 `me`，不碰 `session`：身份没变，变的是这个账号的准备进度。
 */
async function loadMe(): Promise<void> {
  try {
    const who = await refreshAuth()
    if (who) me.value = who
  } catch { /* 面板少一行状态，不影响别的 */ }
}

// --- 账号：改密 与 退出全部设备 ---------------------------------------------------------------
//
// 两件事都会让服务端撤销登录态（包括当前这个），所以成功后一律回到登录页 —— 不是「保存
// 成功、请继续使用」，而是一次登出。这是设计 §5.3 的规则：改密验证旧密码，然后重新登录。

const accountPassword = reactive({ current: '', next: '', confirm: '' })
const accountBusy = ref(false)
const accountError = ref('')
const accountNotice = ref('')

async function submitPasswordChange(): Promise<void> {
  if (accountBusy.value) return
  accountError.value = ''
  accountNotice.value = ''
  if (!accountPassword.current) { accountError.value = '请输入当前密码'; return }
  if (accountPassword.next !== accountPassword.confirm) {
    accountError.value = '两次输入的新密码不一致'
    return
  }
  if (accountPassword.next === accountPassword.current) {
    accountError.value = '新密码不能与当前密码相同'
    return
  }
  accountBusy.value = true
  try {
    // 成功时页面已经被 `signOut('password')` 收拾干净并回到登录页。
    await changePassword(accountPassword.current, accountPassword.next)
  } catch (exc) {
    accountError.value = exc instanceof Error ? exc.message : '修改密码失败'
  } finally {
    accountBusy.value = false
    // 无论成败都不留在页面上：这是密码，不该在这里多待一秒。
    accountPassword.current = ''
    accountPassword.next = ''
    accountPassword.confirm = ''
  }
}

async function submitLogoutEverywhere(): Promise<void> {
  if (accountBusy.value) return
  accountError.value = ''
  accountBusy.value = true
  try {
    await logoutEverywhere()
  } catch (exc) {
    accountError.value = exc instanceof Error ? exc.message : '退出全部设备失败'
  } finally {
    accountBusy.value = false
  }
}

/**
 * 读当前绑定的收件邮箱。
 *
 * 进会话时就要读到：发信确认弹窗靠它判断「要不要让用户填地址」—— 没读到就会把一个已经
 * 绑好的用户当成没绑定，又让他输一遍。读失败按未绑定处理（那种情况下让他填一次也能走通）。
 */
async function loadUserEmail(): Promise<void> {
  try {
    const current = await request<{ email: string | null }>('/settings/email')
    userEmail.value = current.email ?? undefined
  } catch { userEmail.value = undefined }
}
function handleEmailUpdate(email: string | null) {
  userEmail.value = email ?? undefined
  settingsNotice.value = email ? '邮箱设置已保存' : '已清除邮箱绑定'
  setTimeout(() => { settingsNotice.value = '' }, 3000)
}
function llmPayload() {
  return {
    base_url: llmForm.base_url, api_key: llmForm.api_key || null, model: llmForm.model,
    response_format: llmForm.response_format, timeout_seconds: llmForm.timeout_seconds,
  }
}
async function testLlmSettings() {
  settingsBusy.value = true; settingsError.value = ''; settingsNotice.value = ''
  try {
    const result = await request<{ message: string }>('/settings/llm/test', llmPayload())
    settingsNotice.value = result.message
  } catch (exc) { settingsError.value = exc instanceof Error ? exc.message : '连接测试失败' }
  finally { settingsBusy.value = false }
}
async function saveLlmSettings() {
  settingsBusy.value = true; settingsError.value = ''; settingsNotice.value = ''
  try {
    applyLlmSettings(await request<LlmSettings>('/settings/llm', llmPayload(), undefined, 'PUT'))
    settingsNotice.value = '模型配置已生效'
  } catch (exc) { settingsError.value = exc instanceof Error ? exc.message : '保存配置失败' }
  finally { settingsBusy.value = false }
}
async function clearLlmSettings() {
  settingsBusy.value = true; settingsError.value = ''; settingsNotice.value = ''
  try {
    applyLlmSettings(await request<LlmSettings>('/settings/llm', undefined, undefined, 'DELETE'))
    settingsNotice.value = '已清除模型配置，下一次对话将提示未配置模型'
  } catch (exc) { settingsError.value = exc instanceof Error ? exc.message : '清除配置失败' }
  finally { settingsBusy.value = false }
}

// --- 追问卡片里的日期字段：年 / 月 / 日三个下拉框 -----------------------------------------

/**
 * 三个下拉框各自选了什么。键是字段名 —— 每次弹出新问题都会重新种一遍（`setPending`）。
 *
 * 为什么不是 `<input type="date">`：原生控件的占位符由浏览器按自己的语言渲染（这台机器上
 * 是 `yyyy/mm/dd`），页面 `lang` 管不了它。三个框里写的就是「2026 年 / 9 月 / 13 日」。
 */
const dateParts = reactive<Record<string, DateParts>>({})
/**
 * 选项字段渲染成选择卡还是原生下拉框。
 *
 * 只对账单领域用选择卡（与我们那份一致）；其他领域的问答卡保持它们原有的下拉框 ——
 * 这是「只动我们核心功能」的边界，不是技术上的取舍。
 */
const choiceCards = computed(() => pane.value.pending?.agent_id === 'bill')
/** 年选项：往前十年到今年。页面开着期间不会变，算一次就够。 */
const dateYears = yearOptions()
const monthOptions = MONTHS

/**
 * 改年、月、日里的某一项。
 *
 * 换年或换月可能让当前的天不存在（1月31日 → 2月），所以跟着收窄 —— 不收的话页面会停在
 * 一个 2 月 31 日上，还会照这个样子发给工具。
 */
function setDatePart(fieldName: string, part: keyof DateParts, event: Event) {
  const current = dateParts[fieldName] ?? todayParts()
  const chosen = Number((event.target as HTMLSelectElement).value)
  const next: DateParts = { ...current, [part]: chosen }
  next.day = clampDay(next.year, next.month, next.day)
  dateParts[fieldName] = next
  pane.value.answers[fieldName] = composeDate(next)
}
/**
 * 点选一个选项。
 *
 * 账单领域只有一个字段的问题（「上传新的流水文件 / 查询已导入的账单」）点一下就答完了，
 * 不该再让人点一次「提交」。选「上传新的流水文件」时还要**直接把上传卡片打开**：
 * 本仓库的底座没有「界面动作」这条通道（`DomainDeclaration` 里已经没有 actions，
 * `ui_action` 事件也不在事件表里），指望 agent 去请界面打开会落空。
 *
 * 其他领域保持它们原有的「选完再提交」，一行不动。
 */
function chooseOption(fieldName: string, option: string) {
  pane.value.answers[fieldName] = option
  if (!choiceCards.value) return
  if (option === '上传新的流水文件') openBill()
  if (pendingFields.value.length === 1) void submitInterrupt()
}

/** 某字段的日下拉框该给几天：跟着它当前选中的年、月走。 */
function dayOptions(fieldName: string): number {
  const parts = dateParts[fieldName] ?? todayParts()
  return daysInMonth(parts.year, parts.month)
}

// --- 上传流水卡片 --------------------------------------------------------------------------

/** 这一轮里 agent 是不是已经把分析报告**真的发出去**了 —— 发出去了就别再弹确认框。
 *
 * 认的是执行工具 `bill_send_report_execute`（只有确认路径会调它），不是准备工具
 * `bill_send_report`：后者的 tool_result 在用户确认之前就记下了，拿它当「已发送」会
 * 让用户一取消就永远等不到弹窗（见 `backend/app/modules/bill/workflow.py:402-410`）。
 */
function reportSentInTurn(): boolean {
  const activity = pane.value.live?.activity ?? []
  return activity.some(
    event => event.name === 'tool_result'
      && event.data.tool_name === 'bill_send_report_execute'
      && event.data.error === null,
  )
}

/**
 * 用户确认把报告发到邮箱。
 *
 * 确认弹窗只在**没绑定**邮箱时才收地址（绑过了就直接发，不再让人重复输入）；填了就先落库
 * 再发信 —— `bill_send_report` 是从库里取收件人的，不先存下来它会回「您还没有绑定邮箱」。
 * 绑定失败就到此为止：那封信注定发不出去，不如让用户先看见错误。
 */
async function sendReport(payload: {
  periodStart: string
  periodEnd: string
  email: string | null
  /** 报告是按文件分析的：带上任务号，邮件才只重算这一份。 */
  taskId?: string | null
}): Promise<void> {
  if (payload.email) {
    try {
      await request('/settings/email', { email: payload.email }, undefined, 'PUT')
      userEmail.value = payload.email
    } catch (exc) {
      pageError.value = exc instanceof Error ? exc.message : '邮箱保存失败，请重试'
      return
    }
  }
  // 末尾那句是给模型看的**意图标记**：这一轮只要发送，不要重新分析、不要在页面再铺一份
  // 报告。不加它，模型会把「分析报告」四个字当成分析请求，于是发信前又跑一遍分析，
  // 用户就在确认后又看到一份一模一样的报告。
  //
  // 报告是按文件分析出来的（带任务号）时把它一并写进去：发信按这份账单重算，
  // 邮件内容于是与屏幕上看到的完全一致；没有任务号的老路径仍按日期区间。
  const scope = payload.taskId ? `；任务 ${payload.taskId}` : ''
  await send(
    `请把 ${payload.periodStart} 到 ${payload.periodEnd} 的账单分析报告发送到我的邮箱（只发送，无需再展示报告${scope}）`,
  )
}

const billRunning = computed(() => bill.value !== null && BILL_RUNNING.includes(bill.value.state))
const billRemainingAttempts = computed(() => (bill.value ? remainingPasswordAttempts(bill.value) : 0))

function stopBillPolling() {
  if (billPoll !== null) { window.clearInterval(billPoll); billPoll = null }
}
function startBillPolling() {
  stopBillPolling()
  billPoll = window.setInterval(() => { void refreshBill() }, 1000)
}
/**
 * 一次刷新。失败保持安静：一次抖动不该把卡片换成报错条，而到了终态的任务本来就会停表。
 */
async function refreshBill() {
  const task = bill.value
  if (!task) { stopBillPolling(); return }
  const identity = newIdentityScope()
  try {
    const next = await billApi.getTask(task.task_id)
    // 轮询是最容易被忘记的一条迟到响应：换人之后它还会回来一次。
    if (isStale(identity)) return
    bill.value = next
    if (!BILL_RUNNING.includes(next.state)) {
      stopBillPolling()
      // 入库完成：把这份流水作为一条查询结果放进对话（和分析、发信同一套任务号范围），
      // 卡片随之收起。卡片里的那张表只是对话忙、发不出消息时的兜底。
      if (next.state === 'COMPLETED') {
        void loadBillTransactions(next.task_id)
        if (showParsedInChat(next)) closeBill()
      }
      scrollToEnd()
    }
  } catch { stopBillPolling() }
}
/**
 * 把刚导入的流水放进对话：发一句带任务号的查询，表格会作为工具结果显示在消息流里。
 *
 * 返回是否真的发出去了 —— 对话正忙时 `send` 会静默丢弃，调用方据此决定要不要收起
 * 卡片（不收起时卡片里的那张表就是兜底）。
 */
function showParsedInChat(task: BillTaskView): boolean {
  if (paneBusy.value || pane.value.pending) return false
  void send(`查询刚导入的流水（任务 ${task.task_id}）`)
  return true
}

/** 取这份任务解析出来的流水（第一页）。失败就留空，卡片上会提示没有可展示的流水。 */
async function loadBillTransactions(taskId: string): Promise<void> {
  try {
    billTransactions.value = await billApi.getTransactions(taskId)
  } catch {
    billTransactions.value = null
  }
}

/**
 * 用户点了「分析这份流水」：把任务号交给对话，由分析工具按**这份账单文档**取数。
 *
 * 不带日期 —— 分析的起止就是解析出来的流水所在的区间。消息里的任务号是给模型原样
 * 转述用的标记，工具据此定位文档；模型不需要也不应该自己换算日期。
 */
function analyzeBill(): void {
  const task = bill.value
  if (!task) return
  if (paneBusy.value || pane.value.pending) {
    billError.value = '对话正在处理上一件事，稍后再点分析。'
    return
  }
  void send(`分析刚导入的流水（任务 ${task.task_id}）`)
  // 卡片**不关**：这份账单解析出来的流水要继续留在页面上，分析报告只是出现在对话里。
  billDecision.value = 'analyzed'
}

/**
 * 账单操作统一走这里：并发闸门、错误落位，以及「换人之后回来的结果一律作废」。
 *
 * 上传、确认、开始处理都是会花几秒的请求，退出或掉线完全可能发生在它们回来之前。没有这道
 * 守卫，上一个账号的流水卡片就会出现在下一个人的页面上（设计 §7.3）。回调拿到
 * `stillCurrent()`，写页面之前先问一次；轮询那条路自己已经这么做了（`refreshBill`）。
 */
async function billAction(work: (stillCurrent: () => boolean) => Promise<void>) {
  if (billBusy.value) return
  const identity = newIdentityScope()
  const stillCurrent = () => !isStale(identity)
  billBusy.value = true; billError.value = ''
  try { await work(stillCurrent) } catch (exc) {
    if (stillCurrent()) billError.value = exc instanceof Error ? exc.message : '操作失败，请稍后重试'
  } finally { if (stillCurrent()) billBusy.value = false }
}
function openBill() {
  billOpen.value = true; billError.value = ''
  bill.value = null; billTransactions.value = null; billDecision.value = null
  billFile.value = null; billPassword.value = ''
  billForm.period_start = ''; billForm.period_end = ''
  billGuide.value = false
}
function closeBill() {
  stopBillPolling()
  billOpen.value = false; billError.value = ''; billPassword.value = ''; billFile.value = null
  billTransactions.value = null; billDecision.value = null; billGuide.value = false
}
async function createBillTask() {
  await billAction(async stillCurrent => {
    const next = await billApi.createTask({
      bank_code: billForm.bank_code,
      period_start: billForm.period_start,
      period_end: billForm.period_end,
    })
    if (!stillCurrent()) return
    bill.value = next
    scrollToEnd()
  })
}
/**
 * 主路径：文件已经在用户手上。
 *
 * 「先去手机银行申请」那一步是故意跳过的 —— 它存在是为了告诉**还没申请**的人去哪拿文件，
 * 而这个用户不是那种人。`confirm_application` 是个状态迁移，不是提问，在这里调它只是「好，
 * 跳过那步」。
 *
 * 处理也直接开始，不问密码：Excel / CSV 流水没有加密，为了发现这一点而把所有人拦在密码框
 * 前是反的。真需要密码的文件会在后台失败并回到 WAITING_PASSWORD，那一步自己会出现。
 */
async function createAndUpload() {
  const file = billFile.value
  if (!file) return
  await billAction(async stillCurrent => {
    // 不传周期：这份账单覆盖哪一段，要等文件解析出来才知道。任务创建后为空，
    // 解析完成时由后端用文档的真实起止回填。
    const created = await billApi.createTask({ bank_code: billForm.bank_code })
    await billApi.confirmApplication(created.task_id)
    await billApi.uploadFile(created.task_id, file)
    const started = await billApi.beginProcessing(created.task_id)
    if (!stillCurrent()) return
    bill.value = started
    billFile.value = null
    scrollToEnd()
    // 处理在后台线程里跑，状态要靠轮询才跟得上；不启动它，卡片会永远停在起手那一步。
    if (BILL_RUNNING.includes(bill.value.state)) startBillPolling()
  })
}
async function confirmBillApplication() {
  const task = bill.value
  if (!task) return
  await billAction(async stillCurrent => {
    const next = await billApi.confirmApplication(task.task_id)
    if (!stillCurrent()) return
    bill.value = next
    scrollToEnd()
  })
}
/** 选文件就是全部：不再从文件名猜周期，也不默认当月 —— 报告期间以解析结果为准。 */
function pickBillFile(event: Event) {
  billFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
}
async function uploadBillFile() {
  const task = bill.value
  const file = billFile.value
  if (!task || !file) return
  await billAction(async stillCurrent => {
    const next = await billApi.uploadFile(task.task_id, file)
    if (!stillCurrent()) return
    bill.value = next
    billFile.value = null
    scrollToEnd()
  })
}
async function submitBillPassword() {
  const task = bill.value
  const password = billPassword.value
  if (!task || !password) return
  await billAction(async stillCurrent => {
    const next = await billApi.submitPassword(task.task_id, password)
    // 密码不留在页面上，换没换人都一样清。
    billPassword.value = ''
    if (!stillCurrent()) return
    bill.value = next
    scrollToEnd()
    if (BILL_RUNNING.includes(next.state)) startBillPolling()
  })
}
async function cancelBill() {
  const task = bill.value
  if (!task) return
  stopBillPolling()
  await billAction(async stillCurrent => {
    const next = await billApi.cancelTask(task.task_id)
    if (!stillCurrent()) return
    bill.value = next
    scrollToEnd()
  })
}

// --- 身份同步：401、其它标签页、窗口重新聚焦 ----------------------------------------------

/** 聚焦复核的最小间隔：在窗口之间来回切不该把 `/auth/me` 打成轮询。 */
const RECHECK_MIN_INTERVAL_MS = 15_000
let lastRecheckAt = 0
let stopBroadcast: (() => void) | null = null

/**
 * 窗口重新拿到焦点时复核一次「现在还是我吗」。
 *
 * 这是第二道网：广播可能到不了（标签页休眠、浏览器没有 `BroadcastChannel`），而页面停在
 * 别人的历史上是这个阶段最不能接受的状态。后端不可达时不当作被退出 —— 网络抖动不是撤权。
 */
async function recheckIdentity(): Promise<void> {
  if (!session.value || document.visibilityState !== 'visible') return
  const now = Date.now()
  if (now - lastRecheckAt < RECHECK_MIN_INTERVAL_MS) return
  lastRecheckAt = now
  const identity = newIdentityScope()
  let who: Me | null
  try {
    who = await refreshAuth()
  } catch { return }
  if (isStale(identity) || !session.value) return
  if (!who) { expireSession(); return }
  if (who.user_id !== session.value.user_id) {
    // 同一台浏览器换了个人：这一页属于上一个账号的东西全部作废。
    expireSession()
    loginNotice.value = '登录身份已变化，请重新登录'
    return
  }
  me.value = who
}

function onVisibilityChange(): void {
  void recheckIdentity()
}

onMounted(() => {
  // 401 的唯一出口。任何一个请求被服务端否掉，都从 `api.ts` 走到这里。
  onUnauthorized(() => { expireSession() })
  onSignedOut(handleSignedOut)
  // 同源另一个标签页退出/改密：本页立刻跟上，不等到下一次请求被拒。
  stopBroadcast = listenIdentityChange(reason => {
    if (reason === 'login' || !session.value) return
    adoptRemoteSignOut(reason)
  })
  document.addEventListener('visibilitychange', onVisibilityChange)
  void bootstrap()
})
onUnmounted(() => {
  stopBillPolling()
  onUnauthorized(null)
  onSignedOut(null)
  if (stopBroadcast) { stopBroadcast(); stopBroadcast = null }
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <!-- 未登录就只渲染登录页：工作台的任何一部分（侧栏、会话、报告）都不该在没确认身份时出现。 -->
  <AuthView v-if="!session" :reason="loginNotice" @authenticated="enterWorkspace" />
  <div v-else class="workspace">
    <header class="topbar">
      <div class="brand"><span class="brand-mark"><Landmark :size="22" /></span><div><strong>银行智能助理</strong><span class="brand-caption">演示工作台</span></div></div>
      <!-- 模拟时钟条 + 审计抽屉。只在后端 `demo_mode` 打开时出现，且挂载时不发请求。 -->
      <DemoControlBar v-if="demoMode" />
      <SubscriptionReminders @ask="prefillDraft" />
      <div class="top-status"><span :class="['status-dot', { online: connected }]"></span>{{ connected ? '服务已连接' : '服务未连接' }}<span class="divider"></span><ShieldCheck :size="16" /><span>模拟账户</span></div>
      <!-- IM 接入：点开出码，扫完自己就连上；取消也在面板里。 -->
      <button class="icon-button" title="IM 接入" aria-label="IM 接入" :disabled="!session" @click="imOpen = true"><MessageSquare :size="18" /></button>
      <!-- 模型设置已并入「设置与账号」面板（账号、模型、邮件都在里面）。 -->
      <button v-if="session" class="icon-button" title="设置与账号" aria-label="设置与账号" @click="openSettings"><Settings :size="18" /></button>
      <button v-if="session && !billOpen" class="secondary bill-entry" :disabled="paneBusy" @click="openBill"><Receipt :size="14" />上传流水</button>
      <div class="user"><UserRound :size="17" /><span>{{ session?.login_name ?? session?.user_id ?? '未登录' }}</span></div>
      <button class="icon-button" title="退出登录" aria-label="退出登录" @click="handleLogout"><LogOut :size="17" /></button>
    </header>

    <div v-if="imOpen" class="settings-overlay" @click.self="imOpen = false">
      <section class="settings-panel" role="dialog" aria-modal="true" aria-labelledby="im-title">
        <header><div><h2 id="im-title">IM 接入</h2><span class="status-label">飞书</span></div><button class="icon-button" title="关闭 IM 接入" aria-label="关闭 IM 接入" @click="imOpen = false"><X :size="18" /></button></header>
        <!-- 面板自己读状态、自己停轮询；父级只管开关。 -->
        <ImConnect />
      </section>
    </div>
    <div v-if="settingsOpen" class="settings-overlay" @click.self="settingsOpen = false">
      <section class="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <header>
          <div>
            <h2 id="settings-title">设置</h2>
            <span v-if="isAdmin" :class="['status-label', { muted: llmSettings?.mode !== 'model' }]">{{ llmSettings?.mode === 'model' ? '模型模式' : '未配置模型' }}</span>
            <span v-else class="status-label">{{ me?.role === 'admin' ? '管理员' : '普通用户' }}</span>
          </div>
          <button class="icon-button" title="关闭设置" aria-label="关闭设置" @click="settingsOpen = false"><X :size="18" /></button>
        </header>

        <!--
          账号区。三件事放在一起：我是谁（登录名 / 角色 / 银行数据是否就绪）、改密码、退出
          全部设备。后两件都会让服务端撤销全部登录态（包括当前这个），所以它们不是「保存设置」
          而是「重新登录」—— 点了就回登录页，这一点在按钮下面写清楚。
        -->
        <section class="settings-account">
          <h3>账号</h3>
          <dl class="account-facts">
            <div><dt>登录名</dt><dd>{{ me?.login_name ?? '—' }}</dd></div>
            <div><dt>角色</dt><dd>{{ me?.role === 'admin' ? '管理员' : '普通用户' }}</dd></div>
            <div><dt>银行数据</dt><dd>{{ me?.bank_ready ? '已就绪' : '准备中' }}</dd></div>
          </dl>

          <form class="account-form" @submit.prevent="submitPasswordChange">
            <label>当前密码<PasswordInput v-model="accountPassword.current" label="当前密码" autocomplete="current-password" placeholder="请输入当前密码" /></label>
            <label>新密码<PasswordInput v-model="accountPassword.next" label="新密码" autocomplete="new-password" placeholder="至少 12 个字符" /></label>
            <label>确认新密码<PasswordInput v-model="accountPassword.confirm" label="确认新密码" autocomplete="new-password" placeholder="再输入一次" /></label>
            <div v-if="accountError" class="settings-message error" role="alert">{{ accountError }}</div>
            <div v-else-if="accountNotice" class="settings-message success" role="status">{{ accountNotice }}</div>
            <div class="account-actions">
              <button type="submit" class="primary" :disabled="accountBusy || !accountPassword.current || !accountPassword.next || !accountPassword.confirm">{{ accountBusy ? '处理中' : '修改密码' }}</button>
              <button type="button" class="secondary danger" :disabled="accountBusy" @click="submitLogoutEverywhere">退出全部设备</button>
            </div>
            <small class="account-note">修改密码或退出全部设备后，所有登录态（包括这一台）都会失效，需要重新登录。</small>
          </form>
        </section>

        <!-- 邮箱绑定：深度分析报告（PDF）的收件地址，也是「发送到邮箱」的唯一目标。 -->
        <div class="settings-email">
          <h3>收件邮箱</h3>
          <EmailSettings :current-email="userEmail" @update="handleEmailUpdate" />
        </div>

        <!-- 全局模型配置只有管理员能读写，普通用户看不到这一块（后端也会 403）。 -->
        <form v-if="isAdmin" @submit.prevent="saveLlmSettings">
          <h3>模型</h3>
          <label>Base URL<input v-model.trim="llmForm.base_url" type="url" placeholder="https://example.com/v1" autocomplete="url" required /></label>
          <label>API Key<PasswordInput v-model="llmForm.api_key" label="API Key" :placeholder="llmSettings?.configured ? `已配置 ${llmSettings.api_key_masked}，留空沿用` : '请输入 API Key'" autocomplete="new-password" /></label>
          <label>模型名<input v-model.trim="llmForm.model" type="text" placeholder="模型名称" autocomplete="off" required /></label>
          <div class="settings-row"><label>响应格式<select v-model="llmForm.response_format"><option value="json_schema">JSON Schema</option><option value="json_object">JSON Object</option></select></label><label>超时（秒）<input v-model.number="llmForm.timeout_seconds" type="number" min="1" max="120" step="1" required /></label></div>
          <div v-if="settingsError" class="settings-message error" role="alert">{{ settingsError }}</div>
          <div v-else-if="settingsNotice" class="settings-message success" role="status">{{ settingsNotice }}</div>
          <footer><button type="button" class="secondary danger" :disabled="settingsBusy || !llmSettings?.configured" @click="clearLlmSettings">清除配置</button><span></span><button type="button" class="secondary" :disabled="settingsBusy || !llmForm.base_url || !llmForm.model" @click="testLlmSettings">测试连接</button><button type="submit" class="primary" :disabled="settingsBusy || !llmForm.base_url || !llmForm.model">{{ settingsBusy ? '处理中' : '保存并启用' }}</button></footer>
        </form>
      </section>
    </div>

    <div v-if="paneError" class="error-banner" role="alert"><div>{{ paneError }}<small v-if="paneTrace">请求编号 {{ paneTrace }}</small></div><button v-if="pane.canRetry" class="secondary" :disabled="pane.running" @click="retryPending">重试发送</button><button class="icon-button" title="重新连接" aria-label="重新连接" @click="bootstrap"><RefreshCw :size="16" /></button></div>

    <main :class="['main-grid', session ? 'with-sidebar' : 'single-pane']">
      <aside v-if="session" class="conversation-sidebar">
        <div class="sidebar-head">
          <div class="title-with-icon"><MessageSquare :size="16" /><h2>会话</h2></div>
          <button class="icon-button" title="新建会话" aria-label="新建会话" :disabled="starting" @click="newConversation"><Plus :size="17" /></button>
        </div>
        <div class="sidebar-filters">
          <label class="sidebar-toggle"><input type="checkbox" v-model="showArchived" @change="refreshConversations" /><span>显示已归档</span></label>
          <label class="sidebar-toggle"><input type="checkbox" v-model="showDeleted" @change="refreshConversations" /><span>显示已删除</span></label>
        </div>
        <!--
          批量删除：平时只有「删除选中｜全删」两个入口，行上没有方框；点「删除选中」才进入多选，
          出现勾选框与「全选 / 删除选中(N) / 取消」。保持和原来一样的简洁，只是把方框藏到模式里。
        -->
        <div v-if="conversations.length" class="sidebar-select">
          <template v-if="selectMode">
            <label class="sidebar-toggle"><input type="checkbox" :checked="allSelected" @change="toggleSelectAll" /><span>全选</span></label>
            <button class="secondary" type="button" :disabled="selectedCount === 0" @click="bulkRemoveSelected">删除选中{{ selectedCount > 0 ? ` (${selectedCount})` : '' }}</button>
            <button class="secondary" type="button" @click="exitSelectMode">取消</button>
          </template>
          <template v-else>
            <button class="secondary" type="button" @click="enterSelectMode">删除选中</button>
          </template>
          <button class="secondary danger" type="button" @click="bulkRemoveAll">全删</button>
        </div>
        <div class="conversation-list">
          <p v-if="!conversations.length && !listBusy" class="sidebar-empty">暂无会话，点右上角新建，或直接输入消息</p>
          <div v-for="item in conversations" :key="item.conversation_id" :class="['conversation-item', { active: item.conversation_id === current?.conversation_id, selected: isSelected(item) }]">
            <div
              class="conversation-row"
              @click="switchConversation(item)"
              @pointerenter="prefetchConversation(item)"
              @pointerleave="cancelPrefetch"
            >
              <input
                v-if="selectMode"
                class="conversation-check"
                type="checkbox"
                :checked="isSelected(item)"
                :aria-label="`选择会话 ${item.serial_no}`"
                @click.stop
                @change="toggleSelect(item)"
              />
              <span
                class="conversation-serial"
                :title="`会话编号 ${item.serial_no}`"
                :aria-label="`会话编号 ${item.serial_no}`"
              >#{{ item.serial_no }}</span>
              <span v-if="isRunning(item)" class="conversation-running" title="正在运行" aria-label="正在运行">
                <LoaderCircle class="spin" :size="14" />
              </span>
              <span v-else-if="isUnseen(item)" class="conversation-unseen" :title="unseenHint(item)" aria-label="需要你看" />
              <input
                v-if="renamingId === item.conversation_id"
                v-model="renameDraft"
                class="rename-input"
                aria-label="会话标题"
                @click.stop
                @keydown.enter.prevent="commitRename(item)"
                @keydown.esc="renamingId = ''"
                @blur="commitRename(item)"
              />
              <span v-else class="conversation-title">{{ conversationLabel(item) }}</span>
              <span v-if="rowDay(item)" class="conversation-stamp" :title="rowStamp(item)">{{ rowDay(item) }}</span>
              <span class="conversation-marks">
                <Pin v-if="item.pinned" :size="12" aria-label="已置顶" />
                <span v-if="item.archived" class="mark">已归档</span>
                <span v-if="item.deleted" class="mark deleted">已删除</span>
              </span>
            </div>
            <div class="conversation-actions">
              <button class="icon-button" :title="item.pinned ? '取消置顶' : '置顶'" :aria-label="item.pinned ? '取消置顶' : '置顶'" :disabled="rowBusy(item)" @click.stop="togglePinned(item)"><PinOff v-if="item.pinned" :size="14" /><Pin v-else :size="14" /></button>
              <button class="icon-button" title="重命名" aria-label="重命名" :disabled="rowBusy(item)" @click.stop="startRename(item)"><Pencil :size="14" /></button>
              <button v-if="!item.deleted" class="icon-button" :title="item.archived ? '取消归档' : '归档'" :aria-label="item.archived ? '取消归档' : '归档'" :disabled="rowBusy(item) || isRunning(item)" @click.stop="toggleArchived(item)"><ArchiveRestore v-if="item.archived" :size="14" /><Archive v-else :size="14" /></button>
              <button v-if="!item.deleted" class="icon-button danger" title="删除会话" aria-label="删除会话" :disabled="rowBusy(item) || isRunning(item)" @click.stop="removeConversation(item)"><Trash2 :size="14" /></button>
              <button v-else class="icon-button" title="恢复会话" aria-label="恢复会话" :disabled="rowBusy(item)" @click.stop="undeleteConversation(item)"><RotateCcw :size="14" /></button>
            </div>
          </div>
          <button v-if="conversationCursor" class="secondary load-more" :disabled="listBusy" @click="loadMoreConversations">加载更多会话</button>
        </div>
      </aside>

      <section class="chat-pane">
        <div class="pane-title"><div class="title-with-icon"><MessageSquare :size="18" /><h1>{{ current ? conversationLabel(current) : '对话' }}</h1></div><span :class="['status-label', { muted: !modelConfigured }]">{{ modelConfigured ? '助理在线' : '尚未配置模型' }}</span></div>
        <LockBanner
          v-if="demoMode && demoStore.isBannerVisible"
          :locked-until="demoStore.security?.locked_until ?? ''"
          :bank-current-time="demoStore.clock?.current_time"
          :busy="demoStore.securityBusy"
          @unlock="handleUnlock"
          @close="demoStore.dismissBanner()"
          @expired="demoStore.dismissBanner()"
        />
        <div ref="messageList" class="conversation" aria-live="polite">
          <!-- 未登录的分支已经在模板最外层处理掉了：这里只会是「已登录但没有会话」。 -->
          <div v-if="!current" class="conversation-empty"><div class="empty-mark"><Landmark :size="32" :stroke-width="1.4" /></div><h2>银行智能助理</h2><p>直接输入消息即可开始一个新会话</p><button class="primary" :disabled="starting" @click="newConversation">新建会话<ArrowRight :size="16" /></button></div>

          <template v-else>
            <div v-if="switching && !pane.messages.length" class="conversation-loading" role="status"><LoaderCircle :size="14" class="spin" />正在载入会话…</div>

            <div v-if="pane.historyCursor" class="history-more"><button class="secondary" :disabled="pane.historyBusy" @click="loadEarlier"><History :size="14" />加载更早的消息</button></div>

            <div v-for="turn in paneTurns" :key="turn.key" class="chat-turn">
              <div v-if="turn.user" class="chat-message user">
                <span class="message-role">{{ turn.user.message_kind === 'interrupt_answer' ? '补充信息' : '你' }}</span>
                <p>{{ turn.user.content }}</p>
                <div class="message-time">{{ messageTime(turn.user.created_at) }}</div>
              </div>
              <div v-for="reply in turn.replies" :key="reply.message_id" class="chat-message assistant">
                <span class="message-role">
                  银行助理
                  <span v-if="isInterceptedReply(reply.content)" class="message-role-badge intercepted">
                    <ShieldAlert :size="11" /> 安全防御拦截
                  </span>
                </span>
                <div class="markdown-body" v-html="renderMarkdown(reply.content)"></div>
                <div v-if="businessIdsOf(reply).length" class="business-ids"><span class="business-label">业务编号</span><code v-for="id in businessIdsOf(reply)" :key="id" class="business-id">{{ id }}</code></div>
                <div class="message-time">{{ messageTime(reply.created_at) }}</div>
              </div>
              <div v-if="turn.unconfirmed && !pausedOnThisTurn(turn)" class="unconfirmed" role="status"><CircleAlert :size="14" />结果未确认：这条消息已提交，但服务端没有返回助手回复。</div>
              <!--
                这一轮里留下的深度分析报告。账本不存工具结果，报告由前端按 `anchorMessageId`
                挂回它所属的那一问下面 —— 后面再发消息，它也不会被挤到页面底部。
              -->
              <div v-if="reportsForTurn(turn).length" class="activity settled-reports">
                <div v-for="(report, index) in reportsForTurn(turn)" :key="`${turn.key}-report-${index}`" class="activity-item">
                  <div class="activity-result">
                    <AnalysisResult :display="report.display" :busy="paneBusy" :locked="!!pane.pending" :bound-email="userEmail" @send-report="sendReport" />
                  </div>
                </div>
              </div>
              <!--
                服务端随消息保存、但本机还没加载过的报告。别的设备看到的是这个文件占位卡；
                点击右下角按钮后由 `openServerReport` 写进本机缓存并换成完整报告。
              -->
              <div v-if="serverReportsForTurn(turn).length" class="activity server-reports">
                <div v-for="(display, index) in serverReportsForTurn(turn)" :key="`${turn.key}-cloud-${index}`" class="activity-item">
                  <ReportPlaceholder :display="display" @open="openServerReport(turn, display)" />
                </div>
              </div>
              <!-- 查询明细：翻页走只读接口，不经过模型，也不写消息账本。 -->
              <div v-if="queriesForTurn(turn).length" class="activity settled-queries">
                <div v-for="query in queriesForTurn(turn)" :key="query.callId" class="activity-item">
                  <!-- `activity-result` 是竖排：表在上、选择在下。少了它，这一行是横排，
                       那行「要分析吗」会跑到表格右边去。 -->
                  <div class="activity-result">
                    <QueryResultTable :display="query.display" />
                    <BillAnalysisPrompt
                      v-if="settledAnalysisPrompt(query)"
                      :scope="settledAnalysisPrompt(query) as AnalysisPromptScope"
                      :disabled="paneBusy || !!pane.pending"
                      @analyze="analyzeQueryScope"
                      @dismiss="dismissAnalysisPrompt"
                    />
                  </div>
                </div>
              </div>
              <!-- 刷新后从消息 metadata 恢复的当前页；完整明细需要重新查询。 -->
              <div v-if="serverQueryResultsForTurn(turn).length" class="activity server-queries">
                <div v-for="(display, index) in serverQueryResultsForTurn(turn)" :key="`${turn.key}-saved-query-${index}`" class="activity-item">
                  <!-- 带任务号的恢复结果可以按任务号重新取页，所以照常给翻页；只有按日期
                       查的那些才退化成静态快照。 -->
                  <div class="activity-result">
                    <QueryResultTable :display="display" :persisted="!display.task_id" />
                    <BillAnalysisPrompt
                      v-if="restoredAnalysisPrompt(turn.key, index, display)"
                      :scope="restoredAnalysisPrompt(turn.key, index, display) as AnalysisPromptScope"
                      :disabled="paneBusy || !!pane.pending"
                      @analyze="analyzeQueryScope"
                      @dismiss="dismissAnalysisPrompt"
                    />
                  </div>
                </div>
              </div>
            </div>

            <div v-if="pane.live" class="chat-turn live-turn">
              <div class="chat-message user"><span class="message-role">你</span><p>{{ pane.live.user }}</p><div class="message-time">{{ messageTime(pane.live.at) }}</div></div>
              <div v-if="pane.live.activity.length" class="activity">
                <div v-for="(event, position) in pane.live.activity" :key="position" class="activity-item">
                  <template v-if="event.name === 'agent_start'">
                    <span class="activity-chip"><LoaderCircle :size="12" class="spin" />{{ event.data.agent_id }} Agent 处理中</span>
                  </template>
                  <template v-else-if="event.name === 'tool_call'">
                    <span class="activity-label">调用工具</span><strong class="activity-tool">{{ event.data.tool_name }}</strong><code class="activity-json">{{ compact(event.data.arguments) }}</code>
                  </template>
                  <template v-else-if="event.name === 'tool_result'">
                    <span class="activity-label">工具返回</span>
                    <strong class="activity-tool">{{ event.data.tool_name }}</strong>
                    <span :class="[
                      'activity-status',
                      {
                        failed: event.data.error !== null && event.data.status !== 'DENIED',
                        denied: event.data.status === 'DENIED' || event.data.error_code === 'TOOL_DENIED'
                      }
                    ]">
                      <ShieldAlert v-if="event.data.status === 'DENIED' || event.data.error_code === 'TOOL_DENIED'" :size="12" class="denied-icon" />
                      {{ (event.data.status === 'DENIED' || event.data.error_code === 'TOOL_DENIED') ? '已拦截' : event.data.status }}
                    </span>
                    <code v-if="toolErrorText(event.data.error_code, event.data.error)"
                          :class="['activity-json', { failed: event.data.error !== null, denied: event.data.status === 'DENIED' || event.data.error_code === 'TOOL_DENIED' }]">
                      {{ toolErrorText(event.data.error_code, event.data.error) }}
                    </code>
                    <div v-else-if="toolResultHasContent(event.data)" class="activity-result"><template v-if="queryDisplayOf(event.data)"><QueryResultTable :display="queryDisplayOf(event.data)" /><BillAnalysisPrompt v-if="liveAnalysisPrompt(event.data)" :scope="liveAnalysisPrompt(event.data) as AnalysisPromptScope" :disabled="paneBusy || !!pane.pending" @analyze="analyzeQueryScope" @dismiss="dismissAnalysisPrompt" /></template><AnalysisResult v-else-if="analysisResultOf(event.data) && !isSendOnlyRequest(pane.live.user)" :display="event.data.data?.display" :busy="paneBusy" :locked="!!pane.pending" :report-sent="reportSentInTurn()" :bound-email="userEmail" @send-report="sendReport" /><template v-else><p v-if="toolResultView(event.data).text" class="result-summary">{{ toolResultView(event.data).text }}</p><p v-for="(line, i) in toolResultView(event.data).blocks" :key="i" class="block-text">{{ line }}</p></template></div>
                  </template>
                  <template v-else-if="event.name === 'subtask_end'">
                    <span class="activity-label">子任务</span><strong class="activity-tool">{{ event.data.agent_id }}</strong><span :class="['activity-status', { pending: event.data.turn_status === 'unknown' }]">{{ subtaskStatus(event.data) }}</span><span class="activity-summary">{{ event.data.summary }}</span>
                  </template>
                  <template v-else-if="event.name === 'narration'">
                    <span class="activity-label">说明</span><span class="activity-narration">{{ event.data.text }}</span>
                  </template>
                </div>
              </div>
              <div v-if="pane.live.reply" class="chat-message assistant">
                <span class="message-role">
                  银行助理
                  <span v-if="isInterceptedReply(pane.live.reply.text)" class="message-role-badge intercepted">
                    <ShieldAlert :size="11" /> 安全防御拦截
                  </span>
                </span>
                <div class="markdown-body" v-html="renderMarkdown(pane.live.reply.text)"></div><div v-if="pane.live.reply.business_ids.length" class="business-ids"><span class="business-label">业务编号</span><code v-for="id in pane.live.reply.business_ids" :key="id" class="business-id">{{ id }}</code></div></div>
              <div v-else-if="pane.live.replyDraft" class="chat-message assistant"><span class="message-role">银行助理</span><p>{{ pane.live.replyDraft }}</p></div>
              <div v-if="!pane.live.reply && pane.live.unconfirmed" class="unconfirmed" role="status"><CircleAlert :size="14" />结果未确认：连接中断，服务端没有返回助手回复。可重新加载页面按相同请求重试。</div>
            </div>

            <!--
              找不到所属回合的报告（账本还没读到那一页，或那一轮从未落库）。有锚点的报告都
              已经渲染在各自的回合下面了，这里只兜底，避免它们凭空消失。
            -->
            <div v-if="looseSettledReports.length" class="activity settled-reports">
              <div v-for="(report, index) in looseSettledReports" :key="`loose-${index}`" class="activity-item">
                <div class="activity-result">
                  <AnalysisResult :display="report.display" :busy="paneBusy" :locked="!!pane.pending" :bound-email="userEmail" @send-report="sendReport" />
                </div>
              </div>
            </div>

            <div v-if="pane.replyNotice" class="reply-notice" role="status"><CircleAlert :size="14" />{{ pane.replyNotice }}</div>

            <form v-if="pane.pending" class="interrupt-card" @submit.prevent="submitInterrupt">
              <div class="interrupt-prompt markdown-body" v-html="renderMarkdown(pane.pending.display)"></div>
              <div v-if="pane.pending.confirmation" class="confirmation" role="group" aria-label="待确认操作">
                <div class="confirmation-head"><span class="confirmation-title">{{ pane.pending.confirmation.title }}</span><span v-if="pane.pending.confirmation.amount_minor !== null" class="confirmation-amount">{{ formatMinor(pane.pending.confirmation.amount_minor) }}</span></div>
                <dl class="confirmation-details">
                  <div><dt>产品</dt><dd>{{ pane.pending.confirmation.product_name || '未提供' }}</dd></div>
                  <div><dt>账户</dt><dd>{{ pane.pending.confirmation.account_display || '未提供' }}</dd></div>
                  <div v-if="expiresText(pane.pending.confirmation.expires_at)"><dt>有效期至</dt><dd>{{ expiresText(pane.pending.confirmation.expires_at) }}</dd></div>
                </dl>
              </div>
              <label v-for="field in pendingFields" :key="field.name" class="interrupt-field">
                <span>{{ field.label }}</span>
                <!--
                  账单领域的选项字段是**选择卡**（一排按钮），与我们那份一致：下拉框要多点
                  一次、还要再看一眼展开的列表，而选项只有两三个时它更像一个输入框。
                  其他领域的卡片保持原生下拉框 —— 那是它们既有的交互，不动（card 的授权卡
                  也有一个 select，改它会连带改掉那边的行为和测试）。
                  这里只点选、不自动提交。
                -->
                <div v-if="field.type === 'select' && choiceCards" class="interrupt-choices">
                  <button
                    v-for="option in field.options"
                    :key="option"
                    type="button"
                    :class="['interrupt-choice', { chosen: pane.answers[field.name] === option }]"
                    :disabled="pane.running || !writable"
                    @click="chooseOption(field.name, option)"
                  >{{ option }}</button>
                </div>
                <select v-else-if="field.type === 'select'" v-model="pane.answers[field.name]">
                  <option value="">{{ `请选择${field.label}` }}</option>
                  <option v-for="option in field.options" :key="option" :value="option">{{ option }}</option>
                </select>
                <!--
                  日期用「年 / 月 / 日」三个下拉框，不用 `<input type="date">`：原生控件的
                  占位符由浏览器按自己的语言渲染（这台机器上是 `yyyy/mm/dd`），页面 `lang`
                  管不了它。三个框默认都选今天，拼出来是 `2026年9月13日` —— 工具的日期解析
                  认得这种写法（后端 app/modules/dates.py）。
                -->
                <div v-else-if="field.type === 'date'" class="interrupt-date">
                  <select :value="dateParts[field.name]?.year" @change="setDatePart(field.name, 'year', $event)">
                    <option v-for="year in dateYears" :key="year" :value="year">{{ year }} 年</option>
                  </select>
                  <select :value="dateParts[field.name]?.month" @change="setDatePart(field.name, 'month', $event)">
                    <option v-for="month in monthOptions" :key="month" :value="month">{{ month }} 月</option>
                  </select>
                  <select :value="dateParts[field.name]?.day" @change="setDatePart(field.name, 'day', $event)">
                    <option v-for="day in dayOptions(field.name)" :key="day" :value="day">{{ day }} 日</option>
                  </select>
                </div>
                <input
                  v-else
                  v-model="pane.answers[field.name]"
                  :type="field.type === 'number' ? 'number' : 'text'"
                  :placeholder="`请输入${field.label}`"
                  autocomplete="off"
                />
              </label>
              <fieldset v-if="['apply', 'change_password'].includes(pane.pending.confirmation?.operation ?? '')" class="transaction-password-form">
                <legend>银行卡交易密码（六位数字）</legend>
                <label v-if="pane.pending.confirmation?.operation === 'change_password'" class="interrupt-field"><span>旧交易密码</span><input v-model="transactionPassword.old_password" aria-label="旧交易密码" type="password" inputmode="numeric" maxlength="6" autocomplete="off" /></label>
                <label class="interrupt-field"><span>{{ pane.pending.confirmation?.operation === 'apply' ? '设置交易密码' : '新交易密码' }}</span><input v-model="transactionPassword.new_password" :aria-label="pane.pending.confirmation?.operation === 'apply' ? '设置交易密码' : '新交易密码'" type="password" inputmode="numeric" maxlength="6" autocomplete="off" /></label>
                <label class="interrupt-field"><span>{{ pane.pending.confirmation?.operation === 'apply' ? '确认交易密码' : '确认新交易密码' }}</span><input v-model="transactionPassword.confirm_password" :aria-label="pane.pending.confirmation?.operation === 'apply' ? '确认交易密码' : '确认新交易密码'" type="password" inputmode="numeric" maxlength="6" autocomplete="off" /></label>
                <small>只提交到独立验证接口，不会写入聊天记录。刷新或切换会话会清空输入。</small>
                <p v-if="transactionPassword.confirm_password && transactionPassword.new_password !== transactionPassword.confirm_password" role="alert">两次新密码不一致</p>
              </fieldset>
              <label v-if="pane.pending.confirmation?.requires_code" class="interrupt-field">
                <span>{{ pane.pending.confirmation.code_label }}</span>
                <PasswordInput v-model="pane.confirmationCode" :label="pane.pending.confirmation.code_label" inputmode="numeric" maxlength="32" autocomplete="off" :placeholder="`请输入${pane.pending.confirmation.code_label}`" :aria-label="pane.pending.confirmation.code_label" @keydown.enter.prevent="submitAction('confirm')" />
                <small>验证码只随这次确认提交给银行，不写入聊天记录，也不进检查点。</small>
              </label>
              <div class="interrupt-actions">
                <template v-if="pane.pending.confirmation">
                  <button type="button" class="secondary" :disabled="pane.running || !writable" @click="submitAction('cancel')">{{ pane.pending.confirmation.cancel_label }}</button>
                  <button type="button" class="primary" :disabled="pane.running || !writable || !confirmationReady" @click="submitAction('confirm')">{{ pane.pending.confirmation.confirm_label }}</button>
                </template>
                <template v-else>
                  <button type="button" class="secondary" :disabled="starting" @click="newConversation">新建会话</button>
                  <button type="submit" class="primary" :disabled="pane.running || !writable || !interruptComplete(pane.pending)">提交</button>
                </template>
              </div>
            </form>

            <div v-if="pane.recoveryRequired" class="unconfirmed blocked" role="status">
              <span><CircleAlert :size="14" />结果未确认：上一次执行未完成，服务端无法安全恢复，只能新建会话继续。</span>
              <button class="secondary" :disabled="starting" @click="newConversation">新建会话</button>
            </div>

          </template>

          <!--
            上传流水卡片。上传、密码、本地文件这些都在这里发生，不进对话 —— 它们一个都不
            能经过模型。入库完成后它不展示统计（那是 bill_summary 的单文档缓存，与深度分析
            口径不一致），而是自动请 agent 跑一次深度分析，卡片随即收起。
          -->
          <section v-if="billOpen" class="bill-card" aria-live="polite">
            <header class="bill-head">
              <div class="title-with-icon"><Receipt :size="16" /><strong>流水分析</strong></div>
              <button class="icon-button" title="关闭" aria-label="关闭" @click="closeBill"><X :size="16" /></button>
            </header>

            <div v-if="billError" class="bill-message error" role="alert">{{ billError }}</div>

            <!-- 主路径：用户手上已有文件 → 第一操作就是选文件，日期从文件名推断 -->
            <template v-if="!bill && !billGuide">
              <p class="bill-hint">请上传从手机银行导出的流水文件（支持 ZIP、PDF、Excel、CSV）。</p>
              <input class="bill-file-input" type="file" accept=".zip,.pdf,.xlsx,.csv,.txt" :disabled="billBusy" @change="pickBillFile" />
              <p v-if="billFile" class="bill-file">{{ billFile.name }}（{{ formatBytes(billFile.size) }}）</p>
              <p v-if="billFile" class="bill-note">不用选日期：解析完成后会按文件里的真实起止展示流水。</p>
              <div class="bill-actions">
                <button class="secondary" :disabled="billBusy" @click="billGuide = true">还没有流水文件？</button>
                <button class="primary" :disabled="billBusy || !billFile" @click="createAndUpload">创建并上传</button>
              </div>
            </template>

            <!-- 次路径：用户还没去银行申请 → 先告诉他申请哪一段 -->
            <template v-else-if="!bill">
              <p class="bill-hint">请先在招商银行 APP 申请这段周期的流水，文件会发送到您的邮箱。</p>
              <div class="bill-fields">
                <label>银行<select v-model="billForm.bank_code"><option value="CMB">招商银行</option></select></label>
                <label>开始日期<input v-model="billForm.period_start" type="date" /></label>
                <label>结束日期<input v-model="billForm.period_end" type="date" /></label>
              </div>
              <div class="bill-actions">
                <button class="secondary" :disabled="billBusy" @click="billGuide = false">返回上传</button>
                <button class="primary" :disabled="billBusy || !billForm.period_start || !billForm.period_end" @click="createBillTask">创建任务</button>
              </div>
            </template>

            <template v-else-if="bill.state === 'WAITING_APPLICATION'">
              <p class="bill-hint">请先在招商银行 APP 申请这段周期的流水，文件会发送到您的邮箱。</p>
              <ol class="bill-steps">
                <li>打开招商银行 APP，进入「我的」→「交易明细」</li>
                <li>选择周期 {{ bill.period_start }} ~ {{ bill.period_end }}</li>
                <li>点击「导出」→「发送到邮箱」，稍后在邮箱中下载</li>
              </ol>
              <div class="bill-actions">
                <button class="primary" :disabled="billBusy" @click="confirmBillApplication">已完成申请</button>
                <button class="secondary" :disabled="billBusy" @click="cancelBill">取消</button>
              </div>
            </template>

            <template v-else-if="bill.state === 'WAITING_UPLOAD'">
              <p class="bill-hint">请上传从邮箱下载的流水文件（支持 ZIP、PDF、Excel、CSV）。</p>
              <input class="bill-file-input" type="file" accept=".zip,.pdf,.xlsx,.csv,.txt" :disabled="billBusy" @change="pickBillFile" />
              <p v-if="billFile" class="bill-file">{{ billFile.name }}（{{ formatBytes(billFile.size) }}）</p>
              <div class="bill-actions">
                <button class="primary" :disabled="billBusy || !billFile" @click="uploadBillFile">上传</button>
                <button class="secondary" :disabled="billBusy" @click="cancelBill">取消</button>
              </div>
            </template>

            <template v-else-if="bill.state === 'WAITING_PASSWORD'">
              <!-- 密码不对会把任务退回这个状态而不是判失败，所以原因得显示在这里，
                   否则表单会毫无解释地重新出现。 -->
              <p v-if="bill.error" class="bill-message error" role="alert">{{ bill.error.message }}</p>
              <p class="bill-hint">附件已加密，请输入解压密码。</p>
              <label class="bill-field">密码<PasswordInput v-model="billPassword" label="附件密码" placeholder="请输入附件密码" autocomplete="off" @keydown.enter.prevent="submitBillPassword" /></label>
              <p class="bill-note">密码仅用于本次解压，不会保存，也不会发送给模型。</p>
              <p v-if="bill.password_attempts > 0" class="bill-note">已尝试 {{ bill.password_attempts }} 次，剩余 {{ billRemainingAttempts }} 次。</p>
              <div class="bill-actions">
                <button class="primary" :disabled="billBusy || !billPassword" @click="submitBillPassword">提交</button>
                <button class="secondary" :disabled="billBusy" @click="cancelBill">取消</button>
              </div>
            </template>

            <template v-else-if="billRunning">
              <p class="bill-hint">{{ bill.progress.current_step }}…</p>
              <div class="bill-progress"><span :style="{ width: `${bill.progress.percentage}%` }"></span></div>
              <div class="bill-actions"><button class="secondary" :disabled="billBusy" @click="cancelBill">取消处理</button></div>
            </template>

            <!--
              入库完成：先把这份账单**解析出来的流水**摆出来，再由用户决定要不要分析。
              这里不自动发分析 —— 自动那一问会塞给用户一份他没要过的报告。
            -->
            <template v-else-if="bill.state === 'COMPLETED'">
              <p class="bill-hint">流水已入库{{ bill.period_start && bill.period_end ? `（${bill.period_start} ~ ${bill.period_end}）` : '' }}。</p>
              <QueryResultTable v-if="billTransactions" :display="billTransactions" :task-id="bill.task_id" />
              <p v-else class="bill-note">没有读取到可展示的流水。</p>
              <!--
                选完不收起卡片：这份流水表要一直留在页面上，用户随时能翻页回看。
                只有明确点「关闭」才收起。
              -->
              <p v-if="billDecision === 'analyzed'" class="bill-note">已请求分析，报告会出现在对话里；这份流水仍保留在这里。</p>
              <p v-else-if="billDecision === 'skipped'" class="bill-note">已跳过分析，流水照常留在这里；想分析时随时点下面的按钮。</p>
              <p v-else class="bill-note">要分析这份流水吗？分析只包含这个文件里的交易，不需要填日期。</p>
              <div class="bill-actions">
                <button v-if="!billDecision" class="secondary" :disabled="billBusy" @click="billDecision = 'skipped'">先不用</button>
                <button v-else class="secondary" :disabled="billBusy" @click="closeBill">关闭</button>
                <button
                  v-if="billDecision !== 'analyzed'"
                  class="primary"
                  :disabled="billBusy || !billTransactions?.total"
                  @click="analyzeBill"
                >分析这份流水</button>
              </div>
            </template>

            <template v-else-if="bill.state === 'FAILED'">
              <p class="bill-message error" role="alert">{{ bill.error?.message ?? '处理失败' }}</p>
              <p class="bill-note">错误码 {{ bill.error?.code ?? 'INTERNAL' }}</p>
              <div class="bill-actions"><button class="secondary" :disabled="billBusy" @click="closeBill">关闭</button></div>
            </template>

            <template v-else-if="bill.state === 'CANCELLED'">
              <p class="bill-note">任务已取消。</p>
              <div class="bill-actions"><button class="secondary" @click="closeBill">关闭</button></div>
            </template>
          </section>
        </div>
        <form class="composer" @submit.prevent="send(draft)"><textarea ref="composerInput" v-model="draft" rows="3" :placeholder="composerDisabled ? '当前无法发送消息' : '输入你的银行业务需求，回车发送'" aria-label="银行业务需求" :disabled="composerDisabled" @keydown="onComposerKeydown"></textarea><div class="composer-bottom"><span><ShieldCheck :size="14" />{{ composerHint }}</span><span class="composer-keys">回车发送 · Shift+回车换行</span><button v-if="pane.running && pane.stoppable" type="button" class="stop-button" data-test="stop-turn" @click="stopCurrentTurn()">停止</button><button type="submit" class="send-button" :disabled="!draft.trim() || composerDisabled" title="发送" aria-label="发送"><ArrowUp :size="20" /></button></div></form>
      </section>
    </main>
    <footer><span>AI BANKING · 演示环境</span><span>所有账户与交易均为模拟数据</span></footer>
  </div>
</template>

<style scoped>
.activity-status.denied {
  background: #fef2f2;
  color: #dc2626;
  border: 1px solid #f87171;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 6px;
  border-radius: 4px;
}

.denied-icon {
  color: #dc2626;
  flex-shrink: 0;
}

.activity-json.denied {
  background: #fff1f2;
  color: #9f1239;
  border-left: 3px solid #e11d48;
}

.message-role-badge.intercepted {
  margin-left: 8px;
  padding: 1px 6px;
  background: #fef2f2;
  color: #b91c1c;
  border: 1px solid #fca5a5;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
</style>
