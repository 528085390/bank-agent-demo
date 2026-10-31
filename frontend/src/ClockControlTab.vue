<script setup lang="ts">
/**
 * 时钟页：当前模拟时间、到期任务数、run_id，加拨钟和「跑到期任务」。
 *
 * 拨钟有四个入口：+1 / +2 / +7 天三个快捷键，和一个自己填天数的输入框。全部走
 * `store.advance({ days })` 这一个动作 —— 银行只认 `{"days": N}`，没有别的端点。
 *
 * 跑到期任务之后的结果（处理 / 完成 / 失败）留在这一页上，演示时看得见刚才那一下干了什么。
 */

import { computed, ref } from 'vue'

import { clockLabel, demoStore } from './demoStore'

const store = demoStore

/**
 * 自定义天数的输入框。
 *
 * `<input type="number">` 上的 `v-model` 会把能解析的值给成 number（Vue 对 number 类型输入框
 * 自动加 `.number`），清空时给回空串 —— 所以这里两种类型都要接，显示和校验都先转成字符串。
 */
const days = ref<string | number>('2')
/** 输入不合法时那一行字；提交合法值时清掉。 */
const notice = ref('')

/** 输入框里现在读得出来的那串字；空输入框是空串。 */
const daysText = computed(() => String(days.value).trim())

function runTasks(): void {
  void store.runTasks()
}

function advanceBy(daysToGo: number): void {
  notice.value = ''
  void store.advance({ days: daysToGo })
}

/**
 * 自定义天数：只收 1-365 的整数。
 *
 * 不合法就不发请求，留一行能读懂的提示 —— 交给后端 422 只会变成一句笼统的失败文案。
 * 用正则判整数而不是靠 `Number()` 的宽容：`type="number"` 在浏览器里可能给出空串、
 * 小数或指数写法，这些都不该被当成一个合法的天数。
 */
function advanceCustom(): void {
  const raw = daysText.value
  if (!/^\d+$/.test(raw) || Number(raw) < 1 || Number(raw) > 365) {
    notice.value = '请输入 1 到 365 之间的整数天数'
    return
  }
  advanceBy(Number(raw))
}
</script>

<template>
  <section class="demo-panel" data-test="panel-clock">
    <p class="demo-panel-clock" data-test="current-time">{{ clockLabel(store.clock) }}</p>
    <dl class="demo-facts">
      <div>
        <dt>到期任务</dt>
        <dd data-test="due-count">{{ store.clock?.due_task_count ?? '—' }}</dd>
      </div>
      <div>
        <dt>run_id</dt>
        <dd data-test="run-id">{{ store.clock?.run_id ?? '—' }}</dd>
      </div>
    </dl>
    <div class="demo-steps">
      <button
        type="button"
        class="demo-step"
        data-test="step-1d"
        :disabled="store.busy"
        @click="advanceBy(1)"
      >
        +1 天
      </button>
      <button
        type="button"
        class="demo-step"
        data-test="step-2d"
        :disabled="store.busy"
        @click="advanceBy(2)"
      >
        +2 天
      </button>
      <button
        type="button"
        class="demo-step"
        data-test="step-7d"
        :disabled="store.busy"
        @click="advanceBy(7)"
      >
        +7 天
      </button>
    </div>
    <!-- novalidate：min/max 交给下面那行中文提示，不让浏览器的原生气泡抢先拦住提交。 -->
    <form class="demo-custom" novalidate @submit.prevent="advanceCustom">
      <label class="demo-custom-label" for="demo-advance-days">前进天数</label>
      <input
        id="demo-advance-days"
        v-model="days"
        class="demo-custom-input"
        data-test="step-days"
        type="number"
        min="1"
        max="365"
        step="1"
        inputmode="numeric"
      />
      <button type="submit" class="demo-step" data-test="step-custom" :disabled="store.busy">
        前进 {{ daysText || 'N' }} 天
      </button>
    </form>
    <p v-if="notice" class="demo-notice" data-test="step-error" role="alert">{{ notice }}</p>
    <button
      type="button"
      class="demo-run"
      data-test="run-tasks"
      :disabled="store.busy"
      @click="runTasks"
    >
      跑到期任务
    </button>
    <p v-if="store.lastRun" class="demo-run-result" data-test="run-result">
      已处理 {{ store.lastRun.processed_count }} · 完成 {{ store.lastRun.completed_count }} · 失败
      {{ store.lastRun.failed_count }}
    </p>
  </section>
</template>

<style scoped>
.demo-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.demo-panel-clock {
  margin: 0;
  color: #176649;
  font-size: 22px;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
}

.demo-facts {
  display: flex;
  gap: 28px;
  margin: 0;
}

.demo-facts dt {
  color: #5d6b64;
  font-size: 12px;
}

.demo-facts dd {
  margin: 4px 0 0;
  color: #1f2b25;
  font-size: 15px;
  font-variant-numeric: tabular-nums;
}

.demo-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.demo-step {
  padding: 7px 12px;
  border: 1px solid #cfe0d8;
  border-radius: 8px;
  background: #f7fbf9;
  color: #176649;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}

.demo-step:hover:not(:disabled) {
  border-color: #2e8763;
  background: #e8f3ee;
}

.demo-step:disabled {
  border-color: #e4e9e6;
  background: #f1f4f2;
  color: #9aa8a1;
  cursor: progress;
}

.demo-custom {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.demo-custom-label {
  color: #5d6b64;
  font-size: 12px;
}

.demo-custom-input {
  width: 76px;
  padding: 7px 8px;
  border: 1px solid #dce2de;
  border-radius: 8px;
  color: #1f2b25;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.demo-notice {
  margin: 0;
  color: #a8442e;
  font-size: 12px;
}

.demo-run {
  align-self: flex-start;
  padding: 8px 14px;
  border: 1px solid #2e8763;
  border-radius: 8px;
  background: #176649;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}

.demo-run:hover:not(:disabled) {
  background: #2e8763;
}

.demo-run:disabled {
  border-color: #dce2de;
  background: #f1f4f2;
  color: #9aa8a1;
  cursor: progress;
}

.demo-run-result {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #f4f8f6;
  color: #2e8763;
  font-size: 13px;
}
</style>
