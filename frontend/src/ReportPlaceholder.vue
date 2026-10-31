<script setup lang="ts">
/**
 * 其他设备上的深度分析报告占位卡。
 *
 * 报告本体随助手消息存在服务端，但别的设备没有本机缓存。这里只显示一个“文件消息”样子：
 * 文件名、期间、笔数、生成时间，右下角一个加载按钮。点击后由父组件把报告从消息 metadata
 * 载入并缓存到本机，卡片随即换成完整报告。
 */

import { computed } from 'vue'
import { Download, FileText } from 'lucide-vue-next'

import { analysisReportView, isAnalysisReport } from './analysisResult'
import { formatReportTime } from './times'

const props = defineProps<{ display: unknown }>()
const emit = defineEmits<{ (e: 'open'): void }>()

const view = computed(() =>
  isAnalysisReport(props.display) ? analysisReportView(props.display) : null)

const meta = computed(() => {
  const report = props.display as { report_meta?: { generated_at?: unknown } } | null
  return {
    period: view.value?.meta.period ?? '—',
    total: view.value?.meta.totalTransactions ?? 0,
    generatedAt: formatReportTime(report?.report_meta?.generated_at as string | undefined),
  }
})
</script>

<template>
  <div class="report-file" role="group" aria-label="账单深度分析报告文件">
    <div class="file-icon"><FileText :size="22" :stroke-width="1.6" /></div>
    <div class="file-body">
      <p class="file-title">账单深度分析报告<span class="file-ext">.pdf</span></p>
      <p class="file-meta">
        {{ meta.period }} · {{ meta.total }} 笔交易
        <template v-if="meta.generatedAt"> · {{ meta.generatedAt }}</template>
      </p>
      <p class="file-hint">报告已在云端，点击右下角按钮加载</p>
    </div>
    <button
      class="file-download"
      type="button"
      title="加载报告"
      aria-label="加载报告"
      @click="emit('open')"
    >
      <Download :size="16" />
    </button>
  </div>
</template>

<style scoped>
.report-file {
  position: relative;
  display: flex;
  gap: 12px;
  align-items: flex-start;
  max-width: 560px;
  padding: 14px 52px 16px 14px;
  border: 1px solid #d8ded9;
  border-radius: 12px;
  background: linear-gradient(135deg, #fbfaf6, #f4f6f3);
  color: #24312b;
}

.file-icon {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  flex: 0 0 40px;
  border-radius: 10px;
  background: #2e3c34;
  color: #c9a94f;
}

.file-body { min-width: 0; }

.file-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.4;
}

.file-ext {
  margin-left: 2px;
  color: #8a948d;
  font-weight: 400;
}

.file-meta,
.file-hint {
  margin: 4px 0 0;
  color: #6b756e;
  font-size: 12px;
  line-height: 1.5;
}

.file-hint { color: #98a19b; }

.file-download {
  position: absolute;
  right: 10px;
  bottom: 10px;
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border: 1px solid #c9a94f;
  border-radius: 50%;
  background: #fff;
  color: #7a6320;
  cursor: pointer;
  transition: background 120ms ease, color 120ms ease;
}

.file-download:hover {
  background: #c9a94f;
  color: #fff;
}
</style>
