<script setup lang="ts">
/**
 * 审计时间线：业务审计的第一页，新的在上。
 *
 * 后端按 oldest first 返回，倒序只发生在下面这个 computed 里，`store.audit` 本身不动 ——
 * 翻页游标是跟着后端顺序走的，本地把数组重排了，下一页就会对不上。
 *
 * 面板开着的时候抽屉每 3 秒自动重读一次；「刷新」是给不想等那一拍的人一个立刻读的按钮，
 * 读取期间禁用，免得连点堆出几个并发请求。
 */

import { computed } from 'vue'

import { demoStore, timeOf } from './demoStore'

const store = demoStore

const rows = computed(() =>
  [...store.audit].sort((left, right) => String(right.created_at).localeCompare(String(left.created_at))),
)

function resultClass(result: string): string {
  return ['ok', 'failed', 'rejected'].includes(result) ? result : 'other'
}

/** 立刻重读，不等下一拍轮询。 */
function refresh(): void {
  void store.loadAudit()
}
</script>

<template>
  <section class="demo-panel" data-test="panel-audit">
    <div class="demo-audit-head">
      <span class="demo-audit-hint">{{ store.busy ? '更新中…' : '每 3 秒自动刷新' }}</span>
      <button
        type="button"
        class="demo-refresh"
        data-test="refresh-audit"
        :disabled="store.busy"
        @click="refresh"
      >
        刷新
      </button>
    </div>
    <p v-if="!rows.length" class="demo-empty">{{ store.busy ? '读取中…' : '暂无审计事件' }}</p>
    <ol v-else class="demo-audit">
      <li v-for="row in rows" :key="row.event_id" class="demo-audit-row">
        <span class="demo-audit-time">{{ timeOf(row.created_at) }}</span>
        <span class="demo-audit-action">{{ row.action }}</span>
        <span :class="['demo-audit-result', resultClass(row.result)]">{{ row.result }}</span>
        <span v-if="row.reason" class="demo-audit-reason">{{ row.reason }}</span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.demo-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.demo-audit-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.demo-audit-hint {
  color: #5d6b64;
  font-size: 12px;
}

.demo-refresh {
  padding: 6px 12px;
  border: 1px solid #dce2de;
  border-radius: 8px;
  background: #fff;
  color: #2e8763;
  font-size: 13px;
  cursor: pointer;
}

.demo-refresh:hover:not(:disabled) {
  border-color: #2e8763;
  background: #e8f3ee;
}

.demo-refresh:disabled {
  color: #9aa8a1;
  cursor: progress;
}

.demo-empty {
  margin: 0;
  color: #5d6b64;
  font-size: 13px;
}

.demo-audit {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.demo-audit-row {
  display: grid;
  grid-template-columns: 44px 1fr auto;
  gap: 4px 10px;
  padding: 10px 12px;
  border: 1px solid #dce2de;
  border-radius: 8px;
  font-size: 13px;
}

.demo-audit-time {
  color: #5d6b64;
  font-variant-numeric: tabular-nums;
}

.demo-audit-action {
  overflow: hidden;
  color: #1f2b25;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.demo-audit-result {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  text-align: center;
}

.demo-audit-result.ok {
  background: #e8f3ee;
  color: #176649;
}

.demo-audit-result.failed {
  background: #fbeeed;
  color: #a2514b;
}

.demo-audit-result.rejected {
  background: #fbf5e6;
  color: #836627;
}

.demo-audit-result.other {
  background: #f1f4f2;
  color: #5d6b64;
}

.demo-audit-reason {
  grid-column: 2 / -1;
  color: #5d6b64;
  font-size: 12px;
}
</style>
