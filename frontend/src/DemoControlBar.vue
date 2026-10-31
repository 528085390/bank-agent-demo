<script setup lang="ts">
/**
 * 演示控制条：当前模拟时间 + 前进一天 + 打开面板。
 *
 * 它同时是抽屉的宿主 —— `App.vue` 只挂这一个组件，抽屉跟着它一起出现。
 *
 * **这个组件自己挂载时不发任何请求。** 首屏那个时间是 `App.vue` 在 `/health` 说出
 * `demo_mode: true` 之后读的一次（`demoStore.loadClock()`）；由条自己发会让三套既有用例的
 * 路径白名单桩全红（见 `DemoControlBar.test.ts` 的第一条）。抽屉关着的时候，演示条一共也就
 * 只有这一次读。
 */

import DemoSidebar from './DemoSidebar.vue'
import { clockLabel, demoStore } from './demoStore'

const store = demoStore

function togglePanel(): void {
  if (store.open) store.closePanel()
  else void store.openPanel()
}

/** 没数据时那一行写着「点击查看」，所以它真的可以点。 */
function openPanel(): void {
  if (!store.open) void store.openPanel()
}

function advanceOneDay(): void {
  void store.advance({ days: 1 })
}
</script>

<template>
  <div class="demo-bar">
    <button
      type="button"
      class="demo-clock"
      data-test="clock-label"
      title="打开演示面板"
      @click="openPanel"
    >
      {{ clockLabel(store.clock) }}
    </button>
    <button
      type="button"
      class="demo-action"
      data-test="advance-1d"
      :disabled="store.busy"
      @click="advanceOneDay"
    >
      前进 1 天
    </button>
    <button
      type="button"
      class="demo-action"
      data-test="toggle-panel"
      :disabled="store.busy"
      @click="togglePanel"
    >
      {{ store.open ? '收起面板' : '演示面板' }}
    </button>
    <span v-if="store.error" class="demo-error" role="alert">{{ store.error }}</span>
    <DemoSidebar />
  </div>
</template>

<style scoped>
.demo-bar {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
}

.demo-clock {
  max-width: 220px;
  overflow: hidden;
  padding: 7px 12px;
  border: 1px solid #dce2de;
  border-radius: 999px;
  background: #f4f8f6;
  color: #176649;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

.demo-clock:hover {
  border-color: #2e8763;
  background: #e8f3ee;
}

.demo-action {
  padding: 7px 12px;
  border: 1px solid #dce2de;
  border-radius: 8px;
  background: #fff;
  color: #2e8763;
  font-size: 13px;
  cursor: pointer;
}

.demo-action:hover:not(:disabled) {
  border-color: #2e8763;
  background: #e8f3ee;
}

.demo-action:disabled {
  color: #9aa8a1;
  cursor: progress;
}

.demo-error {
  max-width: 240px;
  overflow: hidden;
  color: #a2514b;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
