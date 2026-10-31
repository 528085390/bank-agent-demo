<script setup lang="ts">
/**
 * 任务图页：supervisor 为这一轮画出来的节点，按 store 里的顺序竖着排。
 *
 * 纯视图：没有 props、没有 emits、挂载时不发请求 —— 节点由 SSE 帧写进 demoStore.taskNodes，
 * 这里只负责画。planId 也不上屏：图的名字对看演示的人没有意义。
 *
 * 产物抽屉是每个节点自己的开关：expanded 按「图 id + 节点 id」记，一个节点展开不会把别的
 * 节点也打开。默认收起 —— 产物是「想深看一眼」时才点开的东西，不是列表的一部分。记上图 id
 * 是因为节点 id 每张图都从 node_1 开始：换图后同名的那个是另一件事，不该继承上一张图的抽屉。
 */

import { reactive } from 'vue'

import { demoStore } from './demoStore'
import type { TaskNode, TaskNodeStatus } from './types'

const store = demoStore

/** 六个状态各自的中文标签。后端要是加了新状态而这里没跟上，就退回原始值，不留一片空白。 */
const STATUS_LABELS: Record<TaskNodeStatus, string> = {
  pending: '等待中',
  running: '正在执行',
  waiting_confirm: '等待确认',
  done: '已完成',
  failed: '失败',
  skipped: '已跳过',
}

function statusLabel(status: TaskNodeStatus): string {
  return STATUS_LABELS[status] ?? status
}

/**
 * 六个领域 agent 的中文名（spec §5.4:348）。这是本组件自己的展示表：store 和解析层都不认识
 * 中文名，也不该认识。表里没有的 agent 退回原始 id —— 宁可显示 bill_agent_2，也不要空一块。
 */
const OWNER_LABELS: Record<string, string> = {
  bill: '账单助手',
  life_event: '生日管家',
  transfer: '转账助手',
  wealth: '理财助手',
  card: '信用卡助手',
  subscription: '订阅助手',
}

function ownerLabel(owner: string): string {
  return OWNER_LABELS[owner] ?? owner
}

/**
 * 这个节点有没有可展开的产物。
 *
 * 只有「非 null 的对象」才算：null 是「跑完了但没有产物」，undefined 是「没这个键」，
 * 两种都不该长出一个点开是 null 的按钮。
 */
function hasArtifact(node: TaskNode): boolean {
  return typeof node.artifact === 'object' && node.artifact !== null
}

/** 哪些节点的产物是展开的，按「图 id + 节点 id」记；没记过的就是收起。 */
const expanded = reactive<Record<string, boolean>>({})

/** 抽屉的键：planId 每张图不同，节点 id 每张图又都从 node_1 开始，两者一起才唯一。 */
function keyFor(node: TaskNode): string {
  return store.planId + '::' + node.id
}

function isExpanded(node: TaskNode): boolean {
  return expanded[keyFor(node)] === true
}

function toggleArtifact(node: TaskNode): void {
  const key = keyFor(node)
  expanded[key] = !expanded[key]
}
</script>

<template>
  <section class="demo-panel" data-test="panel-tasks">
    <p class="demo-hint">按执行顺序排列，产物可展开查看</p>
    <p v-if="!store.taskNodes.length" class="demo-empty" data-test="tasks-empty">当前无后台任务（对话为直接答复或尚未开始）</p>
    <ol v-else class="demo-graph">
      <li
        v-for="node in store.taskNodes"
        :key="node.id"
        class="demo-graph-node"
        data-test="task-node"
      >
        <div class="node-head">
          <span :class="['node-owner', 'owner-' + node.owner]">{{ ownerLabel(node.owner) }}</span>
          <span class="node-label">{{ node.label }}</span>
          <span :class="['node-status', 'status-' + node.status]">{{ statusLabel(node.status) }}</span>
        </div>
        <p v-if="node.status === 'failed' && node.error" class="node-error">{{ node.error }}</p>
        <template v-if="hasArtifact(node)">
          <button
            type="button"
            class="node-artifact-toggle"
            data-test="artifact-toggle"
            :aria-expanded="isExpanded(node) ? 'true' : 'false'"
            @click="toggleArtifact(node)"
          >
            产物
          </button>
          <pre v-if="isExpanded(node)" class="node-artifact" data-test="task-artifact">{{ JSON.stringify(node.artifact, null, 2) }}</pre>
        </template>
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

.demo-hint {
  margin: 0;
  color: #5d6b64;
  font-size: 12px;
}

.demo-empty {
  margin: 0;
  color: #5d6b64;
  font-size: 13px;
}

.demo-graph {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.demo-graph-node {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid #dce2de;
  border-radius: 8px;
  font-size: 13px;
}

/*
 * 节点之间的连接线是伪元素，不是 DOM：一个节点在列表里就是一个 li，再塞一个「连接器」div
 * 会让 DOM 变成两倍长，[data-test="task-node"] 也会数出多余的元素。
 *
 * 行距 18px，线从上一行的下边框画到这一行的上边框：伪元素定位基准是 padding box（边框内侧
 * 1px），所以 top 取 -19px、高度 19px，正好盖住那 18px 空隙加 1px 边框。
 */
.demo-graph-node + .demo-graph-node {
  margin-top: 18px;
}

.demo-graph-node + .demo-graph-node::before {
  position: absolute;
  top: -19px;
  left: 22px;
  width: 1px;
  height: 19px;
  background: #dce2de;
  content: '';
}

/*
 * 线的下端补一个朝下的箭头（spec §5.4:347）：同样是伪元素，DOM 里还是每节点一个 li。
 * 线在 left:22px、宽 1px，中心是 22.5px；三角形宽 8px，所以从 18.5px 起，顶点正好落在
 * 这一行的上边框上。
 */
.demo-graph-node + .demo-graph-node::after {
  position: absolute;
  top: -5px;
  left: 18.5px;
  width: 0;
  height: 0;
  border-top: 5px solid #dce2de;
  border-right: 4px solid transparent;
  border-left: 4px solid transparent;
  content: '';
}

.node-head {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
}

/* 徽标写中文名，所以用正文字体；不认识的 agent 退回原始 id，落到默认的灰底。 */
.node-owner {
  padding: 1px 8px;
  border-radius: 999px;
  background: #f1f4f2;
  color: #5d6b64;
  font-size: 12px;
}

/* 每个领域一个主题色（spec §5.4:348），都取面板调色板的低饱和底 + 深色字。 */
.owner-bill {
  background: #e8f3ee;
  color: #176649;
}

.owner-life_event {
  background: #fbf5e6;
  color: #836627;
}

.owner-transfer {
  background: #e9eff8;
  color: #35558c;
}

.owner-wealth {
  background: #f2eef8;
  color: #6a4b8c;
}

.owner-card {
  background: #fdeef1;
  color: #9c4a5c;
}

.owner-subscription {
  background: #e9f2f5;
  color: #3f6b78;
}

.node-label {
  overflow: hidden;
  color: #1f2b25;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-status {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  text-align: center;
  white-space: nowrap;
}

.status-pending {
  background: #f1f4f2;
  color: #5d6b64;
}

/* 执行中的小转圈：纯 CSS，不加图标库（spec §5.4:351 的旋转图标）。 */
.status-running {
  background: #e8f3ee;
  color: #2e8763;
}

.status-running::before {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 5px;
  border: 1.5px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  content: '';
  vertical-align: -1px;
  animation: node-spin 0.9s linear infinite;
}

@keyframes node-spin {
  to {
    transform: rotate(360deg);
  }
}

.status-waiting_confirm {
  background: #fbf5e6;
  color: #836627;
}

.status-done {
  background: #cfe0d8;
  color: #176649;
}

.status-failed {
  background: #fbeeed;
  color: #a2514b;
}

.status-skipped {
  background: #f1f4f2;
  color: #9aa8a1;
  text-decoration: line-through;
}

.node-error {
  margin: 0;
  color: #a2514b;
  font-size: 12px;
}

.node-artifact-toggle {
  align-self: flex-start;
  padding: 4px 10px;
  border: 1px solid #dce2de;
  border-radius: 8px;
  background: #fff;
  color: #2e8763;
  font-size: 12px;
  cursor: pointer;
}

.node-artifact-toggle:hover {
  border-color: #2e8763;
  background: #e8f3ee;
}

.node-artifact {
  margin: 0;
  padding: 10px 12px;
  overflow-x: auto;
  border-radius: 8px;
  background: #f4f8f6;
  color: #1f2b25;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
