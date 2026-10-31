<script setup lang="ts">
/**
 * 流水明细查询结果的分页表格。
 *
 * 只负责 `bill_query_transactions` 这一种工具结果的展示：模型上下文里只有条数，
 * 明细由本表直接渲染。翻页走只读 HTTP 接口，不再经过模型。
 */

import { computed, ref, watch } from 'vue'

import { request } from './api'
import { formatMinor } from './money'

interface QueryItem {
  booked_date: string
  amount_minor: number
  direction: 'credit' | 'debit' | string
  transaction_type: string
  counterparty: string
  category: string
}

interface QueryDisplay {
  total: number
  page: number
  page_size: number
  items: unknown[]
  period_start?: string
  period_end?: string
  /** 按文件查询的结果带着它：恢复后翻页不必重新问模型。 */
  task_id?: string | null
}

const props = defineProps<{
  display: QueryDisplay | null
  persisted?: boolean
  /**
   * 给了就按「这份任务解析出来的流水」翻页（`/bill/tasks/{id}/transactions`）。
   * 上传卡片用的是这一种：它没有用户选的日期区间，只有文件。
   */
  taskId?: string
}>()

const page = ref(Number(props.display?.page) || 1)
const pageSize = ref(Number(props.display?.page_size) || 20)
const total = ref(Number(props.display?.total) || 0)
const items = ref<QueryItem[]>([...(props.display?.items ?? [])] as QueryItem[])
const busy = ref(false)
const error = ref('')

watch(
  () => props.display,
  (display) => {
    if (!display) return
    page.value = Number(display.page) || 1
    pageSize.value = Number(display.page_size) || 20
    total.value = Number(display.total) || 0
    items.value = [...display.items] as QueryItem[]
    error.value = ''
  },
)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / Math.max(1, pageSize.value))))
/** 按哪份任务翻页：显式 prop 优先，其次用结果自己带的（恢复后的表格靠这一条）。 */
const taskId = computed(() => props.taskId ?? props.display?.task_id ?? '')
const canPage = computed(
  () => Boolean(taskId.value)
    || Boolean(props.display?.period_start && props.display?.period_end),
)
const showPager = computed(() => !props.persisted && canPage.value && totalPages.value > 1)

function directionText(direction: string): string {
  return direction === 'credit' ? '收入' : '支出'
}

function amountText(item: QueryItem): string {
  const sign = item.direction === 'credit' ? '+' : '-'
  return `${sign}${formatMinor(item.amount_minor)}`
}

async function go(next: number): Promise<void> {
  const periodStart = props.display?.period_start
  const periodEnd = props.display?.period_end
  if (!canPage.value || busy.value || next < 1 || next > totalPages.value) return
  // 两种翻页来源：按这份账单的文档，或按日期区间。
  let path: string
  if (taskId.value) {
    const params = new URLSearchParams({
      page: String(next),
      page_size: String(pageSize.value),
    })
    path = `/bill/tasks/${taskId.value}/transactions?${params.toString()}`
  } else if (periodStart && periodEnd) {
    const params = new URLSearchParams({
      period_start: periodStart,
      period_end: periodEnd,
      page: String(next),
      page_size: String(pageSize.value),
    })
    path = `/bill/transactions?${params.toString()}`
  } else {
    return
  }
  busy.value = true
  error.value = ''
  try {
    const data = await request<QueryDisplay>(path)
    page.value = data.page
    pageSize.value = data.page_size
    total.value = data.total
    items.value = data.items as QueryItem[]
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '读取明细失败，请重试'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="txn-table">
    <div class="txn-head">
      <strong>流水明细</strong>
      <span v-if="persisted">共 {{ total }} 笔 · 已保留当前页 {{ items.length }} 条</span>
      <span v-else>共 {{ total }} 笔 · 第 {{ page }} / {{ totalPages }} 页</span>
    </div>

    <table v-if="items.length">
      <thead>
        <tr>
          <th>日期</th>
          <th>类型</th>
          <th>对手方</th>
          <th>收支</th>
          <th class="num">金额</th>
          <th>分类</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(item, index) in items" :key="`${item.booked_date}-${index}`">
          <td>{{ item.booked_date }}</td>
          <td>{{ item.transaction_type || '—' }}</td>
          <td>{{ item.counterparty || '—' }}</td>
          <td :class="item.direction === 'credit' ? 'credit' : 'debit'">{{ directionText(item.direction) }}</td>
          <td class="num" :class="item.direction === 'credit' ? 'credit' : 'debit'">{{ amountText(item) }}</td>
          <td>{{ item.category || '未分类' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="txn-empty">没有匹配的交易。</p>

    <div v-if="showPager" class="txn-pager">
      <button type="button" class="txn-page-btn" :disabled="busy || page <= 1" @click="go(page - 1)">上一页</button>
      <span>第 {{ page }} / {{ totalPages }} 页</span>
      <button type="button" class="txn-page-btn" :disabled="busy || page >= totalPages" @click="go(page + 1)">下一页</button>
      <span v-if="busy" class="txn-busy">读取中…</span>
    </div>
    <p v-if="persisted" class="txn-persisted-note">刷新后仅保留当前页（最多 20 条）；查看完整明细请再次查询。</p>
    <p v-if="error" class="txn-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.txn-table {
  margin-top: 6px;
  padding: 12px 14px;
  border: 1px solid #dfe5e2;
  border-radius: 6px;
  background: #fff;
}

.txn-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.txn-head strong { font-size: 13px; color: #24312b; }
.txn-head span { font-size: 11px; color: #7a867e; }

.txn-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.txn-table th,
.txn-table td {
  padding: 6px 8px;
  border-bottom: 1px solid #e6ebe8;
  text-align: left;
  white-space: nowrap;
}
.txn-table th {
  font-size: 11px;
  font-weight: 550;
  color: #7a867e;
  background: #f7f9f8;
}
.txn-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.txn-table .credit { color: #2e8763; }
.txn-table .debit { color: #a2514b; }

.txn-pager {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  font-size: 11px;
  color: #7a867e;
}
.txn-page-btn {
  padding: 3px 10px;
  border: 1px solid #cfd8d3;
  border-radius: 3px;
  background: #fff;
  color: #24312b;
  cursor: pointer;
  font-size: 11px;
}
.txn-page-btn:disabled { cursor: default; opacity: 0.45; }
.txn-busy { color: #8c9990; }
.txn-empty { margin: 8px 0 0; color: #8c9990; font-size: 12px; }
.txn-persisted-note { margin: 8px 0 0; color: #8a6d1f; font-size: 11px; }
.txn-error { margin: 8px 0 0; color: #a2514b; font-size: 12px; }
</style>
