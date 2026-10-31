<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ShieldAlert } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    lockedUntil: string
    bankCurrentTime?: string
    busy?: boolean
  }>(),
  {
    bankCurrentTime: '',
    busy: false,
  },
)

const emit = defineEmits<{
  (e: 'unlock'): void
  (e: 'close'): void
  (e: 'expired'): void
}>()

const remainingSeconds = ref<number>(computeInitialSeconds())
let timer: ReturnType<typeof setInterval> | null = null

function computeInitialSeconds(): number {
  if (!props.lockedUntil) return 900
  const target = new Date(props.lockedUntil).getTime()
  if (isNaN(target)) return 900

  if (props.bankCurrentTime) {
    const bankBase = new Date(props.bankCurrentTime).getTime()
    if (!isNaN(bankBase)) {
      const diff = Math.floor((target - bankBase) / 1000)
      if (diff <= 0) return 0
      if (diff <= 900) return diff
      return 900
    }
  }

  const wallDiff = Math.floor((target - Date.now()) / 1000)
  if (wallDiff > 0 && wallDiff <= 900) return wallDiff

  return 900
}

function startTimer() {
  stopTimer()
  remainingSeconds.value = computeInitialSeconds()
  if (remainingSeconds.value <= 0) {
    emit('expired')
    return
  }
  timer = setInterval(() => {
    if (remainingSeconds.value > 0) {
      remainingSeconds.value -= 1
      if (remainingSeconds.value === 0) {
        stopTimer()
        emit('expired')
      }
    }
  }, 1000)
}

function stopTimer() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

watch(
  () => [props.lockedUntil, props.bankCurrentTime],
  () => {
    startTimer()
  },
)

onMounted(() => {
  startTimer()
})

onBeforeUnmount(() => {
  stopTimer()
})

const countdownFormatted = computed(() => {
  const s = Math.max(0, remainingSeconds.value)
  const mm = String(Math.floor(s / 60)).padStart(2, '0')
  const ss = String(s % 60).padStart(2, '0')
  return `${mm}:${ss}`
})
</script>

<template>
  <div class="lock-banner" role="alert" aria-live="assertive">
    <div class="lock-banner-content">
      <ShieldAlert :size="18" class="lock-banner-icon" />
      <span class="lock-banner-title">资金工具已锁定，查询仍可用</span>
      <span class="lock-banner-countdown">剩余时间：{{ countdownFormatted }}</span>
    </div>
    <div class="lock-banner-actions">
      <button
        type="button"
        class="lock-unlock-btn"
        :disabled="busy"
        @click="$emit('unlock')"
      >
        {{ busy ? '正在解除...' : '解除锁定' }}
      </button>
      <button
        type="button"
        class="lock-close-btn"
        aria-label="关闭横幅"
        @click="$emit('close')"
      >
        ×
      </button>
    </div>
  </div>
</template>

<style scoped>
.lock-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background-color: #fff7ed;
  border: 1px solid #fdba74;
  border-radius: 6px;
  padding: 8px 14px;
  margin: 8px 12px 4px 12px;
  color: #9a3412;
  font-size: 13px;
  animation: slide-down 0.2s ease-out;
}

@keyframes slide-down {
  from {
    opacity: 0;
    transform: translateY(-6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.lock-banner-content {
  display: flex;
  align-items: center;
  gap: 8px;
}

.lock-banner-icon {
  color: #ea580c;
  flex-shrink: 0;
}

.lock-banner-title {
  font-weight: 600;
  color: #9a3412;
}

.lock-banner-countdown {
  color: #ea580c;
  font-feature-settings: 'tnum';
  font-variant-numeric: tabular-nums;
  margin-left: 4px;
}

.lock-banner-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.lock-unlock-btn {
  background-color: #ea580c;
  color: #ffffff;
  border: none;
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.lock-unlock-btn:hover:not(:disabled) {
  background-color: #c2410c;
}

.lock-unlock-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.lock-close-btn {
  background: transparent;
  border: none;
  color: #c2410c;
  font-size: 18px;
  line-height: 1;
  padding: 0 4px;
  cursor: pointer;
}

.lock-close-btn:hover {
  color: #7c2d12;
}
</style>
