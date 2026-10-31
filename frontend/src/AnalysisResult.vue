<script setup lang="ts">
/**
 * 深度分析报告（专业版）。
 *
 * 后端 `bill_analysis` 每个模块产出的字段都要在这里露面 —— 之前只挑了其中几个，
 * 风险标签的证据、账户的收支净额、对手的收支拆分、每个模块的风控批注都被丢掉了。
 *
 * 配色跟随项目其他部分的商务风（金色 #c9a94f + 墨绿 #2e3c34），而不是警告红。
 */

import { computed, ref } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'
import { analysisReportView, isAnalysisReport, reportCashflow } from './analysisResult'
import { formatReportTime } from './times'
import SendAnalysisModal from './SendAnalysisModal.vue'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
])

const props = defineProps<{
  display: unknown
  /** 父组件这一轮还在跑（此时它的 `send` 会把新请求静默丢掉）。 */
  busy?: boolean
  /** 有一个没答的问题在等用户：这时也不该能发信。 */
  locked?: boolean
  /** 这一轮里 agent 已经把报告发成功了：按钮显示「已发送」并禁用。 */
  reportSent?: boolean
  /** 已绑定的收件邮箱；为空时点发送才会让用户填一个。 */
  boundEmail?: string
}>()

const emit = defineEmits<{
  /**
   * 用户确认要发到邮箱。`email` 为空表示用已绑定的那个地址；非空是先填后绑的新地址。
   * 落库和发信都由父组件按顺序做完 —— 绑定必须赶在发信前面。
   */
  (e: 'send-report', payload: {
    periodStart: string
    periodEnd: string
    email: string | null
    /** 这份报告是按文件分析出来的：带着任务号，发信时才不会退回按日期重算。 */
    taskId: string | null
  }): void
}>()

const isAnalysis = computed(() => isAnalysisReport(props.display))
const report = computed(() => props.display as any)
const view = computed(() => (isAnalysis.value ? analysisReportView(props.display as any) : null))

// 发送入口是一个**按钮**，不再自动弹窗：分析完成后先让用户看结果，想发再点。
// 点下去之后才走「未绑邮箱则先填地址 → 发信请求」，服务端仍会用确认卡二次确认。
const showSendModal = ref(false)
const periodStart = ref('')
const periodEnd = ref('')

/** 没有绑定邮箱时才需要这个弹窗；它现在只负责收地址。 */
const needsEmail = computed(() => !props.boundEmail)
const sendDisabled = computed(() => Boolean(props.busy || props.locked || props.reportSent))
const sendLabel = computed(() => (props.reportSent ? '已发送' : '发送到邮箱'))

function startSend(): void {
  if (sendDisabled.value) return
  const range = report.value?.report_meta?.date_range
  if (!range?.start || !range?.end) return
  periodStart.value = range.start
  periodEnd.value = range.end
  if (needsEmail.value) {
    showSendModal.value = true
    return
  }
  emit('send-report', {
    periodStart: periodStart.value,
    periodEnd: periodEnd.value,
    email: null,
    taskId: (report.value?.report_meta?.task_id ?? null) as string | null,
  })
}

function handleSendConfirm(email: string) {
  showSendModal.value = false
  // 期间由这里带上：`send_analysis_report` 是按时间段生成报告的。
  //
  // `email` 非空表示用户是在弹窗里**现填**的地址（之前没绑过）—— 那要先由父组件落库再发信，
  // 因为工具是从库里取收件人的，不先存下来它会回「您还没有绑定邮箱」。
  emit('send-report', {
    periodStart: periodStart.value,
    periodEnd: periodEnd.value,
    email: email || null,
    // 按文件分析出来的报告带着任务号：带上它，发信才会只重算这一份，
    // 而不是把同期间的另一份账单也算进去。
    taskId: (report.value?.report_meta?.task_id ?? null) as string | null,
  })
}

function closeSendModal() {
  showSendModal.value = false
}

// ———————————————————————— 取数助手 ————————————————————————

const modules = computed<any[]>(() => report.value?.modules ?? [])
const moduleOf = (id: string) => modules.value.find((m) => m.module_id === id)
const money = (minor: number | null | undefined) =>
  `¥${((minor ?? 0) / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const pct = (value: unknown) => `${value ?? 0}%`

/** 报告头：期间、交易数、账户数、生成时间。 */
const meta = computed(() => {
  const m = report.value?.report_meta ?? {}
  const range = m.date_range ?? {}
  return {
    period: range.start && range.end ? `${range.start} ~ ${range.end}` : '—',
    totalTransactions: m.total_transactions ?? 0,
    accountCount: (m.accounts ?? []).length,
    generatedAt: formatReportTime(m.generated_at as string | undefined),
    // 整年区间就是年报：标题换成年度报告，与 PDF 封面一致。
    annual: m.report_type === 'annual',
    year: typeof range.start === 'string' ? range.start.slice(0, 4) : '',
  }
})

/** 风险标签：完整字段（tag / 笔数 / 金额 / 置信度 / 证据 / 说明）。 */
const riskTags = computed<any[]>(() => moduleOf('customer_risk_profile')?.data?.risk_tags ?? [])

/** 账单完整性：评分 + 问题清单。 */
const completeness = computed(() => {
  const data = moduleOf('customer_risk_profile')?.data
  if (!data) return null
  return {
    score: data.completeness_score ?? null,
    issues: data.completeness_issues ?? [],
  }
})

/** 时段：24 小时分布 + 夜间统计。 */
const hourly = computed<any[]>(() => moduleOf('hourly_activity')?.data?.hourly_distribution ?? [])
const night = computed(() => moduleOf('hourly_activity')?.data?.night_stats ?? null)

const hourlyOption = computed(() => {
  if (!hourly.value.length) return null
  const hours = hourly.value.map((h: any) => `${h.hour}时`)
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['交易笔数', '交易金额（元）'], top: 0 },
    grid: { left: 60, right: 60, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: hours, axisLine: { lineStyle: { color: '#c8d0c9' } } },
    yAxis: [
      { type: 'value', name: '笔数', axisLine: { lineStyle: { color: '#c8d0c9' } } },
      { type: 'value', name: '金额（元）', axisLine: { lineStyle: { color: '#c8d0c9' } } },
    ],
    series: [
      {
        name: '交易笔数',
        type: 'line',
        smooth: true,
        // 这里以前直接把 `hourly_distribution` 对象数组塞给 series.data，
        // ECharts 认不出来，图是空的。要映射成数值。
        data: hourly.value.map((h: any) => h.count ?? 0),
        itemStyle: { color: '#c9a94f' },
        areaStyle: { color: 'rgba(201, 169, 79, 0.15)' },
      },
      {
        name: '交易金额（元）',
        type: 'line',
        smooth: true,
        yAxisIndex: 1,
        data: hourly.value.map(
          (h: any) => (((h.income ?? 0) + (h.expense ?? 0)) / 100).toFixed(2),
        ),
        itemStyle: { color: '#4a7c6a' },
      },
    ],
  }
})

/** 交易类型：按金额画饼，同时给一张表。 */
const typeData = computed(() => {
  const data = moduleOf('transaction_type_analysis')?.data
  if (!data) return null
  return { categories: data.categories ?? [], p2p: data.p2p_transfers ?? null }
})

const typePieOption = computed(() => {
  const categories = typeData.value?.categories ?? []
  if (!categories.length) return null
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {d}%' },
    legend: { orient: 'vertical', right: 8, top: 'center', textStyle: { fontSize: 12 } },
    series: [
      {
        type: 'pie',
        radius: ['42%', '70%'],
        center: ['38%', '50%'],
        data: categories.map((c: any) => ({ name: c.category, value: c.count })),
        label: { formatter: '{b}\n{d}%', fontSize: 11 },
      },
    ],
  }
})

/** 账户使用：补齐收支与净额。 */
const accountRows = computed<any[]>(() => moduleOf('account_usage')?.data?.accounts ?? [])

/** 月度收支：补齐净额与倒挂标记。 */
const monthlyRows = computed<any[]>(() => moduleOf('monthly_cashflow')?.data?.monthly_data ?? [])

const monthlyOption = computed(() => {
  if (!monthlyRows.value.length) return null
  const months = monthlyRows.value.map((m: any) => m.month)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['收入', '支出', '净额'], top: 0 },
    grid: { left: 70, right: 30, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: months, axisLine: { lineStyle: { color: '#c8d0c9' } } },
    yAxis: { type: 'value', name: '金额（元）', axisLine: { lineStyle: { color: '#c8d0c9' } } },
    series: [
      {
        name: '收入',
        type: 'bar',
        data: monthlyRows.value.map((m: any) => (m.income_minor ?? 0) / 100),
        itemStyle: { color: '#4a7c6a' },
      },
      {
        name: '支出',
        type: 'bar',
        data: monthlyRows.value.map((m: any) => (m.expense_minor ?? 0) / 100),
        itemStyle: { color: '#b5654f' },
      },
      {
        name: '净额',
        type: 'line',
        smooth: true,
        data: monthlyRows.value.map((m: any) => (m.net_minor ?? 0) / 100),
        itemStyle: { color: '#c9a94f' },
      },
    ],
  }
})

/** 高频对手：补齐收入 / 支出 / 标签，并附标签分布。 */
const counterparties = computed<any[]>(
  () => moduleOf('frequent_counterparties')?.data?.time_slices?.all?.by_frequency ?? [],
)
const tagDistribution = computed<any[]>(
  () => moduleOf('frequent_counterparties')?.data?.tag_distribution ?? [],
)

/** 风控清单：补齐方向 / 账户 / 余额 / 命中规则。 */
const riskRows = computed<any[]>(() => moduleOf('risk_transactions')?.data?.transactions ?? [])
const riskSummary = computed<any>(() => moduleOf('risk_transactions')?.data?.summary ?? null)

/** 每个模块的风控批注。专用区块不再过滤掉它们。 */
const insightBlocks = computed(() =>
  modules.value
    .filter((m) => (m.insights ?? []).length)
    .map((m) => ({ title: m.title, insights: m.insights })),
)

/** 核心指标：同时展示银行流水合计与对外收支两套口径。 */
const cashflow = computed(() =>
  isAnalysis.value ? reportCashflow(report.value) : null)

/** 通用兜底：没被专用区块接住的模块，仍按键值对渲染。 */
const SPECIALIZED = new Set([
  'customer_risk_profile',
  'hourly_activity',
  'monthly_cashflow',
  'transaction_type_analysis',
  'account_usage',
  'risk_transactions',
  'frequent_counterparties',
])
const leftoverModules = computed(() =>
  (view.value?.modules ?? []).filter((m: any) => !SPECIALIZED.has(m.module_id)),
)
</script>

<template>
  <div v-if="view" class="report">
    <!-- ── 报告头 ───────────────────────────────────────────── -->
    <header class="report-head">
      <div class="head-main">
        <p class="eyebrow">{{ meta.annual ? `${meta.year} 年度账单报告` : '账户流水深度分析' }}</p>
        <h2 class="head-title">{{ meta.period }}</h2>
        <div v-if="riskTags.length" class="tag-strip">
          <span v-for="tag in riskTags" :key="tag.tag" class="tag" :class="{ severe: (tag.hit_count ?? 0) >= 5 }">
            {{ tag.tag }}<em>{{ tag.hit_count }} 笔</em>
          </span>
        </div>
        <p v-else class="head-note">未识别到风险标签</p>
        <!-- 发送入口：不再自动弹窗，用户想发时点这里；未绑邮箱时会先收一个地址。 -->
        <button
          type="button"
          class="report-send-btn"
          :disabled="sendDisabled"
          @click="startSend"
        >{{ sendLabel }}</button>
      </div>
      <dl class="head-stats">
        <div><dt>交易总数</dt><dd>{{ meta.totalTransactions }}<small>笔</small></dd></div>
        <div><dt>涉及账户</dt><dd>{{ meta.accountCount }}<small>个</small></dd></div>
        <div><dt>生成时间</dt><dd class="time-cell">{{ meta.generatedAt || '—' }}</dd></div>
      </dl>
    </header>

    <!-- ── 核心指标：两套收支口径 ────────────────────────────── -->
    <section
      v-if="cashflow && (
        cashflow.all.income_minor || cashflow.all.expense_minor
        || cashflow.external.income_minor || cashflow.external.expense_minor
      )"
      class="cashflow-block"
    >
      <div class="cashflow-head">
        <h3>流水合计</h3>
        <p>银行账单口径：包含理财申赎、信用卡还款、内部转账等全部交易</p>
      </div>
      <div class="metric-row">
        <div class="metric">
          <span>流水收入</span><strong class="credit">{{ money(cashflow.all.income_minor) }}</strong>
        </div>
        <div class="metric">
          <span>流水支出</span><strong class="debit">{{ money(cashflow.all.expense_minor) }}</strong>
        </div>
        <div class="metric">
          <span>流水净额</span>
          <strong :class="cashflow.all.net_minor >= 0 ? 'credit' : 'debit'">
            {{ cashflow.all.net_minor >= 0 ? '+' : '-' }}{{ money(Math.abs(cashflow.all.net_minor)).slice(1) }}
          </strong>
        </div>
        <div class="metric">
          <span>账单完整性</span>
          <strong>{{ completeness?.score ?? '—' }}<small v-if="completeness?.score !== null">分</small></strong>
        </div>
      </div>

      <div class="cashflow-head">
        <h3>对外收支</h3>
        <p>剔除理财申赎、信用卡还款、内部转账等内部资金往来后的收支</p>
      </div>
      <div class="metric-row">
        <div class="metric">
          <span>对外收入</span><strong class="credit">{{ money(cashflow.external.income_minor) }}</strong>
        </div>
        <div class="metric">
          <span>对外支出</span><strong class="debit">{{ money(cashflow.external.expense_minor) }}</strong>
        </div>
        <div class="metric">
          <span>对外净额</span>
          <strong :class="cashflow.external.net_minor >= 0 ? 'credit' : 'debit'">
            {{ cashflow.external.net_minor >= 0 ? '+' : '-' }}{{ money(Math.abs(cashflow.external.net_minor)).slice(1) }}
          </strong>
        </div>
      </div>
    </section>

    <!-- ── 风险标签明细 + 完整性 ────────────────────────────── -->
    <section v-if="riskTags.length || completeness?.issues?.length" class="card">
      <div class="card-head">
        <h3>风险标签识别</h3>
        <p>命中风控关键词的交易归类，按出现次数排序；证据列为该标签下最早的几笔交易单号</p>
      </div>
      <table v-if="riskTags.length" class="table">
        <thead>
          <tr>
            <th>风险标签</th><th class="num">出现次数</th><th class="num">涉及金额</th>
            <th class="num">占总交易</th><th>证据交易（前 3 笔）</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="tag in riskTags" :key="tag.tag">
            <td><span class="pill" :class="{ severe: (tag.hit_count ?? 0) >= 5 }">{{ tag.tag }}</span></td>
            <td class="num">{{ tag.hit_count }}</td>
            <td class="num">{{ money(tag.amount_minor) }}</td>
            <td class="num">{{ tag.confidence }}</td>
            <td class="mono ellipsis">{{ (tag.evidence || []).join('、') || '—' }}</td>
            <td class="muted">{{ tag.detail || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="completeness?.issues?.length" class="issue-box">
        <strong>账单完整性问题</strong>
        <ul>
          <li v-for="(issue, i) in completeness.issues" :key="i">{{ issue }}</li>
        </ul>
      </div>
    </section>

    <!-- ── 交易时段 ─────────────────────────────────────────── -->
    <section v-if="hourlyOption" class="card">
      <div class="card-head">
        <h3>交易活跃时段</h3>
        <p>作息规律的人交易集中在白天三段；长期夜间（22:00–06:00）交易频繁是需要留意的信号</p>
      </div>
      <v-chart :option="hourlyOption" class="chart" style="height: 320px" />
      <div v-if="night" class="stat-strip">
        <div><span>夜间交易</span><strong>{{ night.count }} 笔（{{ night.percentage }}%）</strong></div>
        <div><span>夜间金额</span><strong>{{ money(night.amount_minor) }}（{{ pct(night.amount_percentage) }}）</strong></div>
        <div><span>连续夜间天数</span><strong>{{ night.consecutive_days }} 天</strong></div>
        <div v-if="(night.top_counterparties || []).length">
          <span>夜间高频对手</span>
          <strong>
            {{ (night.top_counterparties || []).map((c: any) => `${c.name}(${c.count})`).join('、') }}
          </strong>
        </div>
      </div>
    </section>

    <!-- ── 交易类型 + 账户 ──────────────────────────────────── -->
    <div class="grid-2">
      <section v-if="typePieOption" class="card">
        <div class="card-head">
          <h3>交易类型占比</h3>
          <p>长期集中的某一类交易（如红包、理财）本身就是特征</p>
        </div>
        <v-chart :option="typePieOption" class="chart" style="height: 300px" />
        <table class="table compact">
          <thead>
            <tr><th>类型</th><th class="num">笔数</th><th class="num">占比</th><th class="num">金额</th><th class="num">金额占比</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in typeData?.categories ?? []" :key="c.category">
              <td>{{ c.category }}</td>
              <td class="num">{{ c.count }}</td>
              <td class="num">{{ pct(c.count_pct) }}</td>
              <td class="num">{{ money(c.amount_minor) }}</td>
              <td class="num">{{ pct(c.amount_pct) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="typeData?.p2p" class="footnote">
          其中个人对个人转账 {{ typeData.p2p.count }} 笔，合计 {{ money(typeData.p2p.amount_minor) }}
        </p>
      </section>

      <section v-if="accountRows.length" class="card">
        <div class="card-head">
          <h3>账户使用情况</h3>
          <p>资金集中在哪个账户、各账户的收支与净额</p>
        </div>
        <table class="table">
          <thead>
            <tr><th>账户</th><th class="num">笔数</th><th class="num">收入</th><th class="num">支出</th><th class="num">净额</th><th class="num">活跃天数</th></tr>
          </thead>
          <tbody>
            <tr v-for="acc in accountRows" :key="acc.account_no">
              <td class="mono">{{ acc.account_no }}</td>
              <td class="num">{{ acc.count }}</td>
              <td class="num credit">{{ money(acc.income_minor) }}</td>
              <td class="num debit">{{ money(acc.expense_minor) }}</td>
              <td class="num">{{ acc.net_flow }} {{ money(Math.abs(acc.net_minor ?? 0)) }}</td>
              <td class="num">{{ acc.active_days }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <!-- ── 月度收支 ─────────────────────────────────────────── -->
    <section v-if="monthlyOption" class="card">
      <div class="card-head">
        <h3>月度收支趋势</h3>
        <p>逐月收支是否对等；净额为负说明当月入不敷出</p>
      </div>
      <v-chart :option="monthlyOption" class="chart" style="height: 340px" />
      <table class="table compact">
        <thead>
          <tr><th>月份</th><th class="num">收入</th><th class="num">支出</th><th class="num">净额</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="m in monthlyRows" :key="m.month">
            <td class="mono">{{ m.month }}</td>
            <td class="num credit">{{ money(m.income_minor) }}</td>
            <td class="num debit">{{ money(m.expense_minor) }}</td>
            <td class="num">{{ m.net_minor >= 0 ? '+' : '-' }}{{ money(Math.abs(m.net_minor ?? 0)).slice(1) }}</td>
            <td><span v-if="m.is_deficit" class="pill severe">收支倒挂</span><span v-else class="pill ok">正常</span></td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- ── 高频交易对手 ─────────────────────────────────────── -->
    <section v-if="counterparties.length" class="card">
      <div class="card-head">
        <h3>高频交易对手 TOP {{ Math.min(counterparties.length, 10) }}</h3>
        <p>资金集中度：与少数对手反复往来，是判断资金用途的重要线索</p>
      </div>
      <table class="table">
        <thead>
          <tr><th class="num">#</th><th>对手</th><th class="num">笔数</th><th class="num">总收入</th><th class="num">总支出</th><th class="num">总金额</th><th>标签</th></tr>
        </thead>
        <tbody>
          <tr v-for="(cp, i) in counterparties.slice(0, 10)" :key="cp.counterparty">
            <td class="num">{{ i + 1 }}</td>
            <td>{{ cp.counterparty }}</td>
            <td class="num">{{ cp.count }}</td>
            <td class="num credit">{{ money(cp.income) }}</td>
            <td class="num debit">{{ money(cp.expense) }}</td>
            <td class="num">{{ money(cp.total_amount) }}</td>
            <td class="muted">{{ cp.tag || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="tagDistribution.length" class="chip-row">
        <span class="chip-label">对手标签分布</span>
        <span v-for="t in tagDistribution" :key="t.tag" class="chip">
          {{ t.tag }} {{ pct(t.count_pct) }}
        </span>
      </div>
    </section>

    <!-- ── 特殊风控交易清单 ─────────────────────────────────── -->
    <section v-if="riskRows.length" class="card">
      <div class="card-head">
        <h3>特殊风控交易清单</h3>
        <p>
          命中平台风控规则的交易
          <template v-if="riskSummary">，共 {{ riskSummary.total_count }} 笔</template>
        </p>
      </div>
      <table class="table">
        <thead>
          <tr>
            <th class="num">#</th><th>时间</th><th>对手</th><th>摘要</th>
            <th class="num">金额</th><th class="num">余额</th><th>方向</th><th>等级</th><th>命中规则</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(tx, i) in riskRows.slice(0, 30)" :key="tx.tx_id || i">
            <td class="num">{{ i + 1 }}</td>
            <td class="mono">{{ (tx.time || '').slice(0, 19).replace('T', ' ') }}</td>
            <td>{{ tx.counterparty }}</td>
            <td class="muted ellipsis">{{ tx.summary || '—' }}</td>
            <td class="num">{{ money(tx.amount_minor) }}</td>
            <td class="num">{{ tx.balance_minor === null ? '—' : money(tx.balance_minor) }}</td>
            <td>{{ tx.direction }}</td>
            <td><span class="pill">{{ tx.risk_level || '—' }}</span></td>
            <td class="muted ellipsis">{{ tx.reasons || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="riskRows.length > 30" class="footnote">仅展示前 30 笔，共 {{ riskRows.length }} 笔。</p>
    </section>

    <!-- ── 风控解读（每个模块的批注） ───────────────────────── -->
    <section v-if="insightBlocks.length" class="card">
      <div class="card-head">
        <h3>风控解读</h3>
        <p>系统根据以上数据给出的判断，标注了是事实还是推断</p>
      </div>
      <div v-for="block in insightBlocks" :key="block.title" class="insight-block">
        <h4>{{ block.title }}</h4>
        <ul>
          <li v-for="(insight, i) in block.insights" :key="i">
            <span class="pill" :class="insight.type === '推断' ? 'warn' : 'ok'">{{ insight.type }}</span>
            <span>{{ insight.text }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- ── 未特殊处理的模块，兜底展示 ───────────────────────── -->
    <section v-for="mod in leftoverModules" :key="mod.module_id" class="card">
      <div class="card-head"><h3>{{ mod.title }}</h3></div>
      <div v-if="mod.dataRows.length" class="kv-grid">
        <div v-for="(row, i) in mod.dataRows" :key="i" class="kv">
          <span>{{ row.label }}</span><strong>{{ row.value }}</strong>
        </div>
      </div>
      <ul v-if="mod.insights.length" class="insight-list">
        <li v-for="(insight, i) in mod.insights" :key="i">{{ insight.text }}</li>
      </ul>
    </section>

    <!-- 报告末尾的发送入口：报告很长，看完再想发时不用滚回顶部。 -->
    <div class="report-actions">
      <button
        type="button"
        class="report-send-btn"
        :disabled="sendDisabled"
        @click="startSend"
      >{{ sendLabel }}</button>
    </div>

    <SendAnalysisModal
      v-if="showSendModal"
      :period-start="periodStart"
      :period-end="periodEnd"
      :analysis-result="report"
      :bound-email="boundEmail"
      @confirm="handleSendConfirm"
      @cancel="closeSendModal"
    />
  </div>
</template>

<style scoped>
/* 商务风：石墨灰文字 + 墨绿/金色点缀，容器留白，数字右对齐等宽 */
.report {
  --ink: #24312b;
  --muted: #7a867e;
  --line: #e6ebe7;
  --gold: #c9a94f;
  --green: #2e8763;
  --red: #a2514b;
  --deep: #2e3c34;
  --panel: #f7f9f8;

  max-width: 1180px;
  margin: 0 auto;
  color: var(--ink);
  font-size: 13px;
  line-height: 1.6;
}

/* ── 报告头 ── */
.report-head {
  display: flex;
  justify-content: space-between;
  gap: 28px;
  flex-wrap: wrap;
  padding: 22px 24px;
  margin-bottom: 14px;
  background: linear-gradient(135deg, #2e3c34 0%, #3d4f45 100%);
  border-radius: 6px;
  color: #f3f6f4;
}

.eyebrow {
  margin: 0 0 4px;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--gold);
}

.head-title {
  margin: 0 0 12px;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.head-note { margin: 0; font-size: 12px; color: #b8c4bd; }

.tag-strip { display: flex; flex-wrap: wrap; gap: 8px; }

.tag {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid rgba(201, 169, 79, 0.5);
  border-radius: 3px;
  background: rgba(201, 169, 79, 0.12);
  color: #f0e2bd;
  font-size: 12px;
}
.tag em { font-style: normal; font-size: 11px; color: #c9a94f; }
.tag.severe { border-color: rgba(181, 101, 79, 0.7); background: rgba(181, 101, 79, 0.18); color: #f2d6cd; }

.head-stats { display: flex; gap: 34px; margin: 0; flex-wrap: wrap; }
.head-stats dt { font-size: 11px; color: #b8c4bd; margin-bottom: 4px; }
.head-stats dd { margin: 0; font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.head-stats dd small { font-size: 12px; font-weight: 400; margin-left: 3px; color: #b8c4bd; }
.head-stats .time-cell { font-size: 13px; font-weight: 400; }

/* ── 核心指标：两套收支口径 ── */
.cashflow-block { margin-bottom: 14px; }
.cashflow-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
  margin: 0 0 8px;
}
.cashflow-head:not(:first-child) { margin-top: 18px; }
.cashflow-head h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--deep); }
.cashflow-head p { margin: 0; font-size: 11px; color: var(--muted); }

.metric-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.metric {
  padding: 14px 16px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 5px;
}
.metric span { display: block; font-size: 11px; color: var(--muted); margin-bottom: 6px; }
.metric strong { font-size: 19px; font-weight: 650; font-variant-numeric: tabular-nums; }
.metric strong small { font-size: 12px; font-weight: 400; margin-left: 3px; color: var(--muted); }

/* ── 卡片 ── */
.card {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 20px 22px;
  margin-bottom: 14px;
}
.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 14px;
}
.grid-2 .card { margin-bottom: 0; }

.card-head { margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
.card-head h3 { margin: 0 0 6px; font-size: 15px; font-weight: 600; color: var(--deep); }
.card-head p { margin: 0; font-size: 12px; color: var(--muted); }

.chart { width: 100%; }

/* ── 表格 ── */
.table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.table th, .table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--line); }
.table th { font-size: 11px; font-weight: 550; color: var(--muted); background: #fbfcfb; white-space: nowrap; }
.table tbody tr:hover { background: #fafcfa; }
.table .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.table.compact th, .table.compact td { padding: 6px 8px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.muted { color: var(--muted); }
.ellipsis { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.credit { color: var(--green); }
.debit { color: var(--red); }

/* ── 小件 ── */
.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 3px;
  font-size: 11px;
  background: #eef2f0;
  color: #4a5a52;
  white-space: nowrap;
}
.pill.ok { background: #e7f2ec; color: var(--green); }
.pill.warn { background: #fbf3df; color: #8a6d1f; }
.pill.severe { background: #f7eae7; color: var(--red); }

.stat-strip { display: flex; flex-wrap: wrap; gap: 28px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--line); }
.stat-strip span { display: block; font-size: 11px; color: var(--muted); margin-bottom: 3px; }
.stat-strip strong { font-size: 13px; font-weight: 600; }

.chip-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.chip-label { font-size: 11px; color: var(--muted); }
.chip { padding: 3px 9px; border-radius: 3px; background: var(--panel); border: 1px solid var(--line); font-size: 11.5px; }

.issue-box { margin-top: 14px; padding: 12px 14px; background: #fdf8ea; border-left: 3px solid var(--gold); border-radius: 3px; }
.issue-box strong { font-size: 12px; }
.issue-box ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: #6b6050; }

.insight-block { margin-bottom: 16px; }
.insight-block:last-child { margin-bottom: 0; }
.insight-block h4 { margin: 0 0 8px; font-size: 13px; font-weight: 600; color: var(--deep); }
.insight-block ul, .insight-list { margin: 0; padding: 0; list-style: none; }
.insight-block li, .insight-list li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 5px 0;
  font-size: 12.5px;
  color: #4a5a52;
  border-bottom: 1px dashed var(--line);
}
.insight-block li:last-child, .insight-list li:last-child { border-bottom: 0; }

.footnote { margin: 12px 0 0; font-size: 11.5px; color: var(--muted); }

.kv-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.kv { padding: 10px 12px; background: var(--panel); border-radius: 4px; }
.kv span { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
.kv strong { font-size: 14px; font-weight: 600; }

@media (max-width: 760px) {
  .report-head { flex-direction: column; gap: 18px; }
  .head-stats { gap: 22px; }
  .ellipsis { max-width: 130px; }
}

/* 报告上的发送入口：一个按钮，不再自动弹窗。 */
.report-send-btn {
  margin-top: 10px;
  padding: 7px 16px;
  border-radius: 6px;
  border: 1px solid #2e3c34;
  background: #2e3c34;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  transition: opacity 0.2s;
}
.report-send-btn:hover:not(:disabled) { opacity: 0.88; }
.report-send-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* 报告末尾的动作区：按钮靠右，和顶部的那个是同一个入口。 */
.report-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid #e5e7eb;
}
</style>
