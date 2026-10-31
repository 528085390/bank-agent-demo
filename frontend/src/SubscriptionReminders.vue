<script setup lang="ts">
/**
 * 续费提醒：登录后自己读一次，不靠用户先问一句。
 *
 * 银行为每个订阅算好 `next_due_date`，这里只做展示层的投影：14 天内会自动扣款的才出现，
 * 暂停的、手动续费的不出现。提醒失败时整块静默消失——工作台不能被一条辅助信息拖垮。
 */
import { computed, onMounted, ref } from 'vue'
import { Bell, X } from 'lucide-vue-next'

import { subscriptionReminders, type SubscriptionReminder } from './api'
import { formatMinor } from './money'

const emit = defineEmits<{ (e: 'ask', text: string): void }>()

const items = ref<SubscriptionReminder[]>([])
const open = ref(false)
const loading = ref(false)

const count = computed(() => items.value.length)
const totalMinor = computed(() => items.value.reduce((sum, item) => sum + item.amount_minor, 0))

function dueLabel(item: SubscriptionReminder): string {
  if (item.days_until_due <= 0) return '今天自动续费'
  if (item.days_until_due === 1) return '明天自动续费'
  return `${item.days_until_due} 天后自动续费`
}

async function load(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    const page = await subscriptionReminders()
    items.value = page.items
  } catch {
    items.value = []
    open.value = false
  } finally {
    loading.value = false
  }
}

function toggle(): void {
  open.value = !open.value
}

function askCancel(item: SubscriptionReminder): void {
  open.value = false
  emit('ask', `帮我取消订阅「${item.name}」`)
}

onMounted(load)
</script>

<template>
  <div v-if="count > 0" class="reminder-shelf">
    <button
      class="icon-button reminder-button"
      data-test="reminder-bell"
      type="button"
      title="续费提醒"
      aria-label="续费提醒"
      :aria-expanded="open"
      @click="toggle"
    >
      <Bell :size="18" />
      <span class="reminder-count" data-test="reminder-count">{{ count }}</span>
    </button>
    <section v-if="open" class="reminder-pop" role="dialog" aria-label="续费提醒">
      <header>
        <div><strong>续费提醒</strong><span>扣款前先看一眼</span></div>
        <button class="icon-button" type="button" title="关闭续费提醒" aria-label="关闭续费提醒" @click="open = false">
          <X :size="16" />
        </button>
      </header>
      <ul>
        <li v-for="item in items" :key="item.subscription_id">
          <div class="reminder-copy">
            <strong>{{ item.name }}</strong>
            <span>{{ dueLabel(item) }}<template v-if="item.merchant_name && item.merchant_name !== item.name"> · {{ item.merchant_name }}</template></span>
          </div>
          <div class="reminder-action">
            <strong>{{ formatMinor(item.amount_minor) }}</strong>
            <button class="secondary" type="button" data-test="reminder-cancel" @click="askCancel(item)">去取消</button>
          </div>
        </li>
      </ul>
      <footer>
        <span>共 {{ count }} 项，合计 {{ formatMinor(totalMinor) }}</span>
        <span>来自银行订阅计划</span>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.reminder-shelf { position: relative; flex-shrink: 0; }
.reminder-button { position: relative; }
.reminder-count {
  position: absolute; top: -4px; right: -4px; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; background: #b8574f; color: #fff; font-size: 10px; line-height: 16px;
  text-align: center; font-weight: 600;
}
.reminder-pop {
  position: absolute; right: 0; top: 38px; z-index: 18; width: min(340px, calc(100vw - 24px));
  background: #fff; border: 1px solid #d8e1db; border-radius: 7px;
  box-shadow: 0 16px 42px rgba(28, 48, 37, .18); overflow: hidden;
}
.reminder-pop > header {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 13px 14px; border-bottom: 1px solid #e8ece9;
}
.reminder-pop > header > div { display: flex; flex-direction: column; gap: 2px; }
.reminder-pop > header strong { font-size: 13px; }
.reminder-pop > header span { color: #8a958e; font-size: 10.5px; }
.reminder-pop ul { margin: 0; padding: 4px 14px; list-style: none; max-height: 260px; overflow-y: auto; }
.reminder-pop li {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 11px 0; border-bottom: 1px solid #f0f3f1;
}
.reminder-pop li:last-child { border-bottom: 0; }
.reminder-copy, .reminder-action { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.reminder-copy strong { font-size: 12.5px; overflow-wrap: anywhere; }
.reminder-copy span { color: #7f8b84; font-size: 10.5px; }
.reminder-action { align-items: flex-end; flex-shrink: 0; }
.reminder-action strong { font-size: 12.5px; color: #9a6a1e; white-space: nowrap; }
.reminder-action .secondary { padding: 3px 7px; min-height: 24px; font-size: 10.5px; }
.reminder-pop > footer {
  display: flex; justify-content: space-between; gap: 10px; padding: 9px 14px;
  background: #f7f9f7; color: #8a958e; font-size: 10px;
}
</style>
