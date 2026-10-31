<script setup lang="ts">
/**
 * 演示抽屉：三个页签 + 面板体。
 *
 * 抽屉容器和页签一直在 DOM 里（关着的时候只是滑出屏幕），面板体才是 `v-if="store.open"` ——
 * 这样关着的时候没有任何页签内容在后台渲染。
 *
 * 滑出屏幕不等于不可达：只用 transform 藏起来，里面的按钮还在 Tab 顺序里，键盘用户会跳进
 * 一个看不见的抽屉。所以关着的时候整个 aside 是 `inert`。
 *
 * 切到审计页才读审计，且审计页开着的时候每 3 秒重读一次 —— 观众把抽屉停在审计页上，
 * 也看得见 agent 刚做完的事，而不是一块冻住的板子。切页签或收起抽屉就停。
 * 任务图页只读 store：图由 App.vue 的 SSE 绑定写进来，切页签不重读、也不丢。
 */

import { onUnmounted, watch } from 'vue'

import AuditTimelineTab from './AuditTimelineTab.vue'
import ClockControlTab from './ClockControlTab.vue'
import TaskGraphTab from './TaskGraphTab.vue'
import { demoStore, type DemoTab } from './demoStore'

const store = demoStore

/** 审计页的刷新间隔。3 秒：够快看见新事件，又不至于把后端刷成压力测试。 */
const AUDIT_POLL_MS = 3000

/**
 * 轮询计时器只属于这个抽屉，不是模块级的：抽屉卸载（页面换了、用例收尾）必须把它停掉，
 * 否则一个已经不在屏幕上的组件还在发请求。
 */
let auditPoll: ReturnType<typeof setInterval> | null = null

function stopAuditPolling(): void {
  if (auditPoll === null) return
  clearInterval(auditPoll)
  auditPoll = null
}

/** 审计页是当前页且抽屉开着 —— 只有这一种状态需要轮询。 */
function syncAuditPolling(): void {
  if (!store.open || store.tab !== 'audit') {
    stopAuditPolling()
    return
  }
  if (auditPoll !== null) return
  auditPoll = setInterval(() => {
    // 上一次还没回来就跳过这一拍：并发两次读，后到的可能是更旧的那一份。
    if (store.busy) return
    void store.loadAudit()
  }, AUDIT_POLL_MS)
}

watch(() => [store.open, store.tab], syncAuditPolling, { immediate: true })
onUnmounted(stopAuditPolling)

const TABS: Array<{ id: DemoTab; label: string }> = [
  { id: 'clock', label: '时钟' },
  { id: 'audit', label: '审计' },
  { id: 'tasks', label: '任务图' },
]

function selectTab(tab: DemoTab): void {
  void store.setTab(tab)
}
</script>

<template>
  <aside
    :class="['demo-drawer', { open: store.open }]"
    data-test="demo-drawer"
    :aria-hidden="!store.open"
    :inert="!store.open"
  >
    <header class="demo-drawer-head">
      <strong>演示控制面</strong>
      <button
        type="button"
        class="demo-close"
        data-test="close-panel"
        title="收起面板"
        aria-label="收起面板"
        @click="store.closePanel()"
      >
        ×
      </button>
    </header>
    <nav class="demo-tabs">
      <button
        v-for="item in TABS"
        :key="item.id"
        type="button"
        :class="['demo-tab-btn', { active: store.tab === item.id }]"
        :data-test="'tab-' + item.id"
        @click="selectTab(item.id)"
      >
        {{ item.label }}
      </button>
    </nav>
    <div v-if="store.open" class="demo-drawer-body">
      <ClockControlTab v-if="store.tab === 'clock'" />
      <AuditTimelineTab v-else-if="store.tab === 'audit'" />
      <TaskGraphTab v-else-if="store.tab === 'tasks'" />
    </div>
  </aside>
</template>

<style scoped>
.demo-drawer {
  position: fixed;
  top: 0;
  right: 0;
  z-index: 60;
  display: flex;
  width: min(420px, 92vw);
  height: 100dvh;
  flex-direction: column;
  border-left: 1px solid #dce2de;
  background: #fff;
  box-shadow: -18px 0 40px rgb(23 102 73 / 10%);
  transform: translateX(105%);
  transition: transform 0.22s ease;
}

.demo-drawer.open {
  transform: none;
}

.demo-drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 20px;
  border-bottom: 1px solid #dce2de;
  color: #176649;
  font-size: 14px;
}

.demo-close {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #5d6b64;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}

.demo-close:hover {
  background: #f1f4f2;
}

.demo-tabs {
  display: flex;
  gap: 6px;
  padding: 12px 20px;
  border-bottom: 1px solid #dce2de;
}

.demo-tab-btn {
  padding: 6px 12px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: #f4f8f6;
  color: #5d6b64;
  font-size: 13px;
  cursor: pointer;
}

.demo-tab-btn.active {
  border-color: #2e8763;
  background: #e8f3ee;
  color: #176649;
}

.demo-drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 18px 20px;
}

.demo-placeholder {
  margin: 0;
  color: #836627;
  font-size: 13px;
}
</style>
