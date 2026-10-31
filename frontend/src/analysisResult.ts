/**
 * 深度分析结果的数据模型和视图转换。
 *
 * 深度分析返回多个模块的结果，每个模块有自己的数据和洞察。
 * 为了避免页面过长，我们将结果分页展示。
 */

export interface AnalysisInsight {
  text: string
  type: '事实' | '推断' | '建议'
  confidence: number
}

export interface AnalysisModule {
  module_id: string
  title: string
  data: Record<string, unknown>
  insights: AnalysisInsight[]
}

export interface CashflowTotals {
  /** 金额单位：分。 */
  income_minor: number
  expense_minor: number
  net_minor: number
}

export interface ReportCashflow {
  /** 银行流水合计：包含理财申赎、信用卡还款、内部转账等全部行。 */
  all: CashflowTotals
  /** 对外收支：剔除内部资金往来，用于观察真实收支。 */
  external: CashflowTotals
}

export interface AnalysisReportMeta {
  date_range: { start: string; end: string }
  accounts: string[]
  total_transactions: number
  generated_at: string
  /** 新版后端提供；旧报告没有时由 `reportCashflow()` 从模块回退计算。 */
  cashflow?: ReportCashflow
}

export interface AnalysisReport {
  report_meta: AnalysisReportMeta
  modules: AnalysisModule[]
  text_report: string
}

const EMPTY_FLOW: CashflowTotals = { income_minor: 0, expense_minor: 0, net_minor: 0 }

function normalizedFlow(value: Partial<CashflowTotals> | null | undefined): CashflowTotals {
  const income = Number(value?.income_minor ?? 0)
  const expense = Number(value?.expense_minor ?? 0)
  return {
    income_minor: income,
    expense_minor: expense,
    net_minor: Number(value?.net_minor ?? income - expense),
  }
}

/**
 * 报告要同时展示两套收支口径：
 *
 * - `all`：银行流水合计，包含理财申赎、信用卡还款、内部转账等；
 * - `external`：对外收支，剔除上述内部资金往来。
 *
 * 新后端直接在 `report_meta.cashflow` 里给两组数；旧报告没有这个字段时，分别从
 * `account_usage`（全部）和 `monthly_cashflow`（对外）回退汇总。
 */
export function reportCashflow(report: AnalysisReport): ReportCashflow {
  const supplied = report.report_meta?.cashflow
  if (supplied?.all && supplied?.external) {
    return {
      all: normalizedFlow(supplied.all),
      external: normalizedFlow(supplied.external),
    }
  }

  const moduleOf = (id: string) => report.modules.find(module => module.module_id === id)?.data
  const sumRows = (rows: unknown, key: 'income_minor' | 'expense_minor'): number => {
    if (!Array.isArray(rows)) return 0
    return rows.reduce((total: number, row) => total + Number((row as Record<string, unknown>)?.[key] ?? 0), 0)
  }

  const accounts = moduleOf('account_usage')?.accounts
  const monthly = moduleOf('monthly_cashflow')?.monthly_data
  const all = normalizedFlow({
    income_minor: sumRows(accounts, 'income_minor'),
    expense_minor: sumRows(accounts, 'expense_minor'),
  })
  const external = normalizedFlow({
    income_minor: sumRows(monthly, 'income_minor'),
    expense_minor: sumRows(monthly, 'expense_minor'),
  })
  return {
    all: { ...EMPTY_FLOW, ...all },
    external: { ...EMPTY_FLOW, ...external },
  }
}

/**
 * 分页配置
 */
export const ITEMS_PER_PAGE = 2 // 每页显示2个模块

/**
 * 格式化模块数据为可读的行列表
 */
function formatModuleData(moduleId: string, data: Record<string, unknown>): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = []

  switch (moduleId) {
    case 'customer_risk_profile': {
      const tags = data.risk_tags as Array<{ tag: string; hit_count: number; amount_minor: number; confidence: number }> | undefined
      if (tags && tags.length > 0) {
        tags.forEach((tag) => {
          rows.push({
            label: `${tag.tag}交易`,
            value: `${tag.hit_count}笔 / ¥${(tag.amount_minor / 100).toFixed(2)} / 置信度${(tag.confidence * 100).toFixed(0)}%`,
          })
        })
      }
      const score = data.completeness_score as number | undefined
      if (score !== undefined) {
        rows.push({ label: '完整性评分', value: `${score}分` })
      }
      break
    }

    case 'hourly_activity': {
      const nightStats = data.night_stats as { count: number; percentage: number; consecutive_days: number } | undefined
      if (nightStats) {
        rows.push({ label: '夜间交易', value: `${nightStats.count}笔 (${nightStats.percentage}%)` })
        if (nightStats.consecutive_days > 0) {
          rows.push({ label: '连续夜间天数', value: `${nightStats.consecutive_days}天` })
        }
      }
      break
    }

    case 'account_usage': {
      const accounts = data.accounts as Array<{ account_no: string; count: number; net_minor: number; active_days: number }> | undefined
      if (accounts && accounts.length > 0) {
        accounts.forEach((acc, idx) => {
          if (idx < 3) {
            // 只显示前3个账户
            const netFlow = acc.net_minor > 0 ? '流入' : acc.net_minor < 0 ? '流出' : '平衡'
            rows.push({
              label: acc.account_no,
              value: `${acc.count}笔 / 净${netFlow}¥${Math.abs(acc.net_minor / 100).toFixed(2)} / ${acc.active_days}天活跃`,
            })
          }
        })
      }
      break
    }

    case 'transaction_type_analysis': {
      const categories = data.categories as Array<{ category: string; count: number; amount_pct: number }> | undefined
      if (categories && categories.length > 0) {
        categories.slice(0, 5).forEach((cat) => {
          // 只显示前5个分类
          rows.push({
            label: cat.category,
            value: `${cat.count}笔 / ${cat.amount_pct}%`,
          })
        })
      }
      break
    }

    case 'annual_summary': {
      // 年报的年度总览：全年收支、覆盖范围、几张年度榜单。
      const yuan = (minor: unknown) => `¥${(Number(minor ?? 0) / 100).toFixed(2)}`
      rows.push({ label: '全年收入', value: yuan(data.income_minor) })
      rows.push({ label: '全年支出', value: yuan(data.expense_minor) })
      rows.push({ label: '年度结余', value: yuan(data.net_minor) })
      rows.push({
        label: '储蓄率',
        value: data.savings_rate_pct == null ? '—' : `${data.savings_rate_pct}%`,
      })
      const active = (data.active_months as string[] | undefined) ?? []
      const missing = (data.missing_months as string[] | undefined) ?? []
      rows.push({
        label: '数据覆盖',
        value: missing.length
          ? `${active.length} / 12 个月（缺 ${missing.map(m => `${Number(m.slice(5))}月`).join('、')}）`
          : '全年 12 个月',
      })
      const categories = (data.top_categories as Array<{ category: string; count: number; amount_minor: number }> | undefined) ?? []
      categories.slice(0, 3).forEach((item) => {
        rows.push({ label: `支出·${item.category}`, value: `${item.count}笔 / ${yuan(item.amount_minor)}` })
      })
      const counterparts = (data.top_counterparties as Array<{ counterparty: string; count: number; amount_minor: number }> | undefined) ?? []
      counterparts.slice(0, 3).forEach((item) => {
        rows.push({ label: `对手·${item.counterparty}`, value: `${item.count}笔 / ${yuan(item.amount_minor)}` })
      })
      const riskCount = Number(data.risk_hit_count ?? 0)
      if (riskCount) rows.push({ label: '风控命中', value: `${riskCount}笔` })
      break
    }

    case 'monthly_cashflow': {
      const monthly = data.monthly_data as Array<{ month: string; income_minor: number; expense_minor: number; net_minor: number; is_deficit?: boolean }> | undefined
      if (monthly && monthly.length > 0) {
        monthly.forEach((m) => {
          const flag = m.is_deficit ? ' ⚠️' : ''
          rows.push({
            label: m.month,
            value: `收入¥${(m.income_minor / 100).toFixed(2)} / 支出¥${(m.expense_minor / 100).toFixed(2)} / 净额¥${(m.net_minor / 100).toFixed(2)}${flag}`,
          })
        })
      }
      break
    }

    case 'risk_transactions': {
      const summary = data.summary as { total_count: number; by_rule: Record<string, number> } | undefined
      if (summary) {
        rows.push({ label: '风控交易总数', value: `${summary.total_count}笔` })
        if (summary.by_rule) {
          Object.entries(summary.by_rule)
            .slice(0, 5)
            .forEach(([rule, count]) => {
              rows.push({ label: rule, value: `${count}笔` })
            })
        }
      }
      break
    }

    case 'frequent_counterparties': {
      const slices = data.time_slices as Record<string, { by_frequency: Array<{ counterparty: string; count: number; total_amount: number }> }> | undefined
      if (slices && slices.all) {
        const topFreq = slices.all.by_frequency.slice(0, 5)
        topFreq.forEach((cp) => {
          rows.push({
            label: cp.counterparty,
            value: `${cp.count}笔 / ¥${(cp.total_amount / 100).toFixed(2)}`,
          })
        })
      }
      break
    }
  }

  return rows
}

/**
 * 报告出来后，要不要弹「发到邮箱」的确认框。
 *
 * 两个「别问」的理由：
 *
 * 1. 工具结果是在这一轮跑到一半时就出现的（流里的 tool_result），这时父组件的 `send` 还在
 *    用着 —— 它会把新请求静默丢掉，于是弹窗看着能点、点了没反应。所以等这一轮跑完
 *    （`busy` 落回 false）、也没有待答问题时再弹；`prompted` 保证只弹一次。
 * 2. `sent`：用户一开始就说了「分析…并把结果发到邮箱」，agent 在同一轮里已经调成功
 *    `send_analysis_report` —— 事都办完了还弹一次确认，等于把刚做完的事再问一遍。
 * 3. `alreadyPrompted`：**同一份报告**只问一次。一轮跑完时 live 块会被丢弃、报告改由
 *    结算后的副本渲染 —— 那是同一份报告的两个组件实例，各问一次就成了骚扰。父组件按
 *    报告对象本身记账。
 */
export function shouldPromptSend(state: {
  ready: boolean
  busy: boolean
  locked: boolean
  prompted: boolean
  sent: boolean
  alreadyPrompted: boolean
}): boolean {
  return state.ready
    && !state.busy
    && !state.locked
    && !state.prompted
    && !state.sent
    && !state.alreadyPrompted
}

export function isAnalysisReport(display: unknown): display is AnalysisReport {
  if (typeof display !== 'object' || display === null) return false
  const obj = display as Record<string, unknown>
  return (
    typeof obj.report_meta === 'object' &&
    obj.report_meta !== null &&
    Array.isArray(obj.modules) &&
    typeof obj.text_report === 'string'
  )
}

export interface AnalysisModuleView {
  module_id: string
  title: string
  insights: AnalysisInsight[]
  dataRows: Array<{ label: string; value: string }>
}

export interface AnalysisReportView {
  meta: {
    period: string
    totalTransactions: number
  }
  modules: AnalysisModuleView[]
  totalPages: number
}

export function analysisReportView(report: AnalysisReport): AnalysisReportView {
  const modules: AnalysisModuleView[] = report.modules.map((m) => ({
    module_id: m.module_id,
    title: m.title,
    insights: m.insights,
    dataRows: formatModuleData(m.module_id, m.data),
  }))

  const totalPages = Math.ceil(modules.length / ITEMS_PER_PAGE)

  return {
    meta: {
      period: `${report.report_meta.date_range.start} ~ ${report.report_meta.date_range.end}`,
      totalTransactions: report.report_meta.total_transactions,
    },
    modules,
    totalPages,
  }
}
