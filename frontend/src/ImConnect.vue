<script setup lang="ts">
/**
 * 飞书 IM 接入面板：出码 → 轮询 → 接入成功，外加两条退路（取消扫码、取消接入）。
 *
 * 扫码走飞书的设备流：后端向飞书要一次授权，把短链自己渲染成 SVG 二维码（前端没有 QR 依赖），
 * 用户用手机飞书扫一下并确认。凭据在**服务端**落盘并热启动长连接，所以这里看到的是状态，
 * 不是凭据本身 —— app_id 也是后端打过码的。
 *
 * 轮询节奏：前端每秒醒一次，但只在服务端说的「可以再问」时刻之后才真的发请求；飞书让慢下来时
 * 是服务端记的间隔，前端照 retry_after 走。
 */

import { computed, onMounted, onUnmounted, ref } from 'vue'
import { CircleAlert, LoaderCircle, QrCode, ShieldCheck, Unplug } from 'lucide-vue-next'

import {
  cancelFeishuSetup,
  disconnectFeishu,
  feishuStatus,
  isFeishuSetupSettled,
  pollFeishuSetup,
  startFeishuSetup,
  type FeishuSetup,
  type FeishuSetupProgress,
  type FeishuStatus,
} from './api'

/** 服务端说可以再问之前，前端至少等这么久；真正的节流在服务端。 */
const POLL_INTERVAL_MS = 2000
/** 成功提示留多久：它是给人看的，不是状态。 */
const NOTICE_MS = 4000

const status = ref<FeishuStatus | null>(null)
const setup = ref<FeishuSetup | null>(null)
const progress = ref<FeishuSetupProgress | null>(null)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const now = ref(Date.now())
const expiresAt = ref(0)

let timer: ReturnType<typeof setInterval> | null = null
let noticeTimer: ReturnType<typeof setTimeout> | null = null
let nextPollAt = 0
let polling = false

/** 已配置**且**连上了才算接好：配置还在、长连接断了，得让用户看得见。 */
const connected = computed(() => Boolean(status.value?.configured && status.value?.running))

/** 还在等人在飞书里点确认：二维码要一直摆着。 */
const waiting = computed(() => {
  if (!setup.value) return false
  const state = progress.value?.state
  return state !== 'denied' && state !== 'expired' && state !== 'cancelled' && state !== 'authorized'
})

const remaining = computed(() => {
  if (!expiresAt.value) return ''
  const left = Math.max(0, Math.round((expiresAt.value - now.value) / 1000))
  const mm = Math.floor(left / 60)
  const ss = String(left % 60).padStart(2, '0')
  return String(mm) + ':' + ss
})

const failureText = computed(() => {
  const state = progress.value?.state
  if (state === 'cancelled') return '已取消扫码。需要时可以重新生成一张二维码。'
  if (state === 'expired') return progress.value?.message || '二维码已过期，请重新生成。'
  if (state === 'denied') return progress.value?.message || '你在飞书里取消了授权。'
  return progress.value?.message || '这次授权没有完成。'
})

function fail(reason: unknown) {
  error.value = reason instanceof Error ? reason.message : '请求失败，请重试'
}

function flash(text: string) {
  notice.value = text
  if (noticeTimer) clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => {
    notice.value = ''
  }, NOTICE_MS)
}

async function loadStatus() {
  try {
    status.value = await feishuStatus()
  } catch (reason) {
    fail(reason)
  }
}

function stopTimer() {
  if (timer) clearInterval(timer)
  timer = null
}

function startTimer() {
  stopTimer()
  timer = setInterval(() => {
    void tick()
  }, 1000)
}

/** 每秒醒一次：更新倒计时，并（到点才）问一次授权进展。 */
async function tick() {
  now.value = Date.now()
  if (!waiting.value || polling || now.value < nextPollAt) return
  polling = true
  try {
    const data = await pollFeishuSetup(setup.value!.setup_id)
    progress.value = data
    nextPollAt = Date.now() + Math.max(POLL_INTERVAL_MS, data.retry_after * 1000)
    if (data.state === 'authorized') {
      // 授权到手：凭据已在服务端落盘、长连接在热启动，这里只要把状态刷新过来。
      stopTimer()
      setup.value = null
      await loadStatus()
      flash('接入成功，回到飞书给机器人发一句话就能开始')
    } else if (isFeishuSetupSettled(data.state)) {
      stopTimer()
    }
  } catch (reason) {
    // 网络抖动不该把二维码收走：留着，等下一次。
    fail(reason)
  } finally {
    polling = false
  }
}

async function begin() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const data = await startFeishuSetup()
    setup.value = data
    progress.value = { setup_id: data.setup_id, state: 'pending', message: '', retry_after: 0 }
    expiresAt.value = Date.now() + data.expires_in * 1000
    now.value = Date.now()
    nextPollAt = 0
    startTimer()
  } catch (reason) {
    fail(reason)
  } finally {
    busy.value = false
  }
}

async function cancelScan() {
  if (!setup.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    progress.value = await cancelFeishuSetup(setup.value.setup_id)
    stopTimer()
  } catch (reason) {
    fail(reason)
  } finally {
    busy.value = false
  }
}

async function disconnect() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await disconnectFeishu()
    setup.value = null
    progress.value = null
    expiresAt.value = 0
    stopTimer()
    await loadStatus()
    flash(result.disconnected ? '已取消接入：长连接已断开，凭据已从本机删除' : '当前没有接入记录，已清理残留绑定')
  } catch (reason) {
    fail(reason)
  } finally {
    busy.value = false
  }
}

function retry() {
  setup.value = null
  progress.value = null
  expiresAt.value = 0
  void begin()
}

onMounted(() => {
  void loadStatus()
})

onUnmounted(() => {
  stopTimer()
  if (noticeTimer) clearTimeout(noticeTimer)
})
</script>

<template>
  <div class="im-connect">
    <p v-if="!status" class="im-hint"><LoaderCircle :size="15" class="spin" />正在读取接入状态…</p>

    <!-- 已配置：说清配的是哪个应用、连没连上、几个人在用，并给两条退路。 -->
    <section v-else-if="status.configured" class="im-card">
      <header class="im-card-head">
        <ShieldCheck :size="18" :class="connected ? 'im-on' : 'im-off'" />
        <strong>{{ connected ? '已接入飞书' : '已配置，长连接未连上' }}</strong>
      </header>
      <dl class="im-facts">
        <div><dt>应用</dt><dd>{{ status.app_id || '未配置' }}</dd></div>
        <div><dt>绑定用户</dt><dd>{{ status.bound_users }} 人</dd></div>
        <div><dt>长连接</dt><dd>{{ status.running ? '已连接' : '未连接' }}</dd></div>
      </dl>
      <p class="im-hint">
        {{ connected
          ? '在飞书里给机器人发一句话就能开始；转账这类敏感操作会在卡片上让你确认。'
          : '凭据还在，但长连接没起来。可以点「重新配置」重新扫码，或者重启后端进程再试。' }}
      </p>
      <footer class="im-actions">
        <button class="secondary danger" :disabled="busy" @click="disconnect">
          <Unplug :size="14" />取消接入
        </button>
        <button class="secondary" :disabled="busy" @click="begin">
          <QrCode :size="14" />重新配置
        </button>
      </footer>
    </section>

    <!-- 扫码中：二维码 + 配对码 + 倒计时 + 取消扫码。 -->
    <section v-else-if="waiting && setup" class="im-card">
      <header class="im-card-head"><QrCode :size="18" class="im-on" /><strong>用手机飞书扫码授权</strong></header>
      <!-- 二维码是后端渲染的 SVG：不含脚本、不含外链，见 app/channels/feishu/setup.py。 -->
      <div class="im-qr" v-html="setup.qr_svg"></div>
      <dl class="im-facts">
        <div><dt>配对码</dt><dd class="im-code">{{ setup.user_code }}</dd></div>
        <div><dt>剩余</dt><dd>{{ remaining }}</dd></div>
      </dl>
      <p class="im-hint">
        扫不了码就在飞书里打开这个链接：
        <a :href="setup.link" target="_blank" rel="noreferrer">{{ setup.link }}</a>
      </p>
      <footer class="im-actions">
        <button class="secondary" :disabled="busy" @click="cancelScan">取消扫码</button>
      </footer>
    </section>

    <!-- 没走完（取消 / 过期 / 被拒）：说清原因，留一次重来的入口。 -->
    <section v-else-if="progress" class="im-card">
      <header class="im-card-head"><CircleAlert :size="18" class="im-off" /><strong>本次扫码没有完成</strong></header>
      <p class="im-hint">{{ failureText }}</p>
      <footer class="im-actions">
        <button class="secondary" :disabled="busy" @click="retry"><QrCode :size="14" />重新生成二维码</button>
      </footer>
    </section>

    <!-- 还没配过：一句话说明接入是干什么的，然后给一个按钮。 -->
    <section v-else class="im-card">
      <header class="im-card-head"><QrCode :size="18" class="im-on" /><strong>接入飞书，用手机和助手对话</strong></header>
      <p class="im-hint">
        点下面的按钮会生成一张二维码，用手机飞书扫一下并确认，这台机器就会开一条到达飞书的长连接。
        不需要公网 IP、域名或证书：连接是往飞书那边发起的。
      </p>
      <footer class="im-actions">
        <button class="primary" :disabled="busy" @click="begin">
          <QrCode :size="14" />{{ busy ? '正在申请…' : '扫码接入' }}
        </button>
      </footer>
    </section>

    <p v-if="error" class="im-message im-error" role="alert">{{ error }}</p>
    <p v-else-if="notice" class="im-message im-success" role="status">{{ notice }}</p>
  </div>
</template>

<style scoped>
.im-connect {
  padding: 20px 22px 22px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.im-card {
  border: 1px solid #e0e7e2;
  border-radius: 6px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.im-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: #2e3c34;
}

.im-on { color: #4f8f6f; }
.im-off { color: #c2803a; }

.im-facts {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  font-size: 13px;
}

.im-facts div {
  display: flex;
  gap: 6px;
  align-items: baseline;
}

.im-facts dt { color: #6b7a70; }

.im-facts dd {
  margin: 0;
  color: #2e3c34;
}

.im-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 0.06em;
}

.im-qr {
  display: grid;
  place-items: center;
  padding: 10px;
  background: #fff;
  border: 1px solid #eef2ef;
  border-radius: 6px;
}

.im-qr :deep(svg) {
  width: 220px;
  height: 220px;
}

.im-hint {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: #65746a;
  word-break: break-all;
}

.im-hint a { color: #4f8f6f; }

.im-actions {
  display: flex;
  gap: 9px;
  justify-content: flex-end;
}

.im-actions button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.im-message {
  margin: 0;
  font-size: 13px;
  padding: 9px 11px;
  border-radius: 5px;
}

.im-error { background: #fdf2f2; color: #a03c3c; }
.im-success { background: #f1f8f3; color: #3d7a58; }

.spin { animation: im-spin 1s linear infinite; }

@keyframes im-spin {
  to { transform: rotate(360deg); }
}
</style>
