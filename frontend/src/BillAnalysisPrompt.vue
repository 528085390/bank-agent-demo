<script setup lang="ts">
/**
 * 「这段流水刚查出来，要分析吗？」—— 挂在对话里的流水表下面。
 *
 * 两种来源共用这一行：按文件查的带任务号，按日期查的带起止日期。选过之后由父组件
 * 按 `key` 记住，不再重复出现。
 */

interface Scope {
  /** 记住「问过了」的键：任务号优先，否则用期间。 */
  key: string
  taskId: string | null
  periodStart: string
  periodEnd: string
}

const props = defineProps<{
  scope: Scope
  /** 对话正在跑、或有个问题没答：这时发不出新消息，按钮置灰。 */
  disabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'analyze', scope: Scope): void
  (e: 'dismiss', key: string): void
}>()

const text = () =>
  props.scope.taskId
    ? '这份流水已解析完成，要分析吗？分析只包含这个文件里的交易。'
    : `要分析 ${props.scope.periodStart} ~ ${props.scope.periodEnd} 的这段流水吗？`
</script>

<template>
  <div class="bill-analysis-prompt" role="group" aria-label="是否分析这段流水">
    <span class="prompt-text">{{ text() }}</span>
    <div class="prompt-actions">
      <button type="button" class="secondary" :disabled="props.disabled" @click="emit('dismiss', props.scope.key)">
        先不用
      </button>
      <button type="button" class="primary" :disabled="props.disabled" @click="emit('analyze', props.scope)">
        分析流水
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 贴着上面那张流水表：同样的边距和圆角，看上去是查询结果自己的尾巴，而不是另一张卡。 */
.bill-analysis-prompt {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  margin-top: 6px;
  border: 1px solid #dfe5e2;
  border-radius: 6px;
  background: #f7f9f8;
}

.prompt-text {
  flex: 1 1 240px;
  font-size: 13px;
  color: #374151;
}

.prompt-actions {
  display: flex;
  gap: 8px;
}

.prompt-actions button {
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid transparent;
}

.prompt-actions .secondary {
  background: #fff;
  color: #374151;
  border-color: #d1d5db;
}

.prompt-actions .primary {
  background: #2e3c34;
  color: #fff;
}

.prompt-actions button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
