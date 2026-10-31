<script setup lang="ts">
/**
 * 登录 / 注册。
 *
 * 只有一件事需要解释：注册成功**不会**自动登录。理由在 `auth.ts` 里——注册只证明账号建好
 * 了，不能证明用户记住了刚设的密码，自动登录会把一次输错的密码藏在一个可用的会话后面。
 *
 * 「首版暂无自助找回」这句提示是产品要求（见认证规划 A20）：注册页不写这句，用户会默认
 * 填过的收件邮箱能用来找回密码，而那个邮箱只用于发报告。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Landmark } from 'lucide-vue-next'

import { login, register } from './auth'
import { RequestError } from './api'
import PasswordInput from './PasswordInput.vue'

const props = defineProps<{
  /**
   * 为什么回到了登录页（过期、退出、改密、被别处退出）。它是页面给的一句说明，不是错误：
   * 用户需要知道「我被退出来了」和「我输错了」是两件事。
   *
   * 这句话只当一次性提示用：浮在页面上停几秒就走，不占表单的位置。常驻的横幅会一直挂在
   * 那里，用户处理完别的事回来还以为自己刚刚才被踢下线。
   */
  reason?: string
}>()

const emit = defineEmits<{ authenticated: [] }>()

/** 提示停留的时间。够读完一句话，又不至于让人等它消失。 */
const REASON_VISIBLE_MS = 3200

const mode = ref<'login' | 'register'>('login')
const loginName = ref('')
const password = ref('')
const confirmPassword = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')

const reasonVisible = ref(false)
let reasonTimer: ReturnType<typeof setTimeout> | null = null

function showReason(): void {
  if (!props.reason) return
  reasonVisible.value = true
  if (reasonTimer !== null) clearTimeout(reasonTimer)
  reasonTimer = setTimeout(() => {
    reasonVisible.value = false
    reasonTimer = null
  }, REASON_VISIBLE_MS)
}

onMounted(showReason)
// 同一个页面实例上换了原因（例如先过期、再被别处退出）时重新计时。
watch(() => props.reason, showReason)
onBeforeUnmount(() => {
  if (reasonTimer !== null) clearTimeout(reasonTimer)
  reasonTimer = null
})

const title = computed(() => (mode.value === 'login' ? '登录' : '创建账号'))
const submitLabel = computed(() => {
  if (busy.value) return '处理中…'
  return mode.value === 'login' ? '登录' : '注册'
})

function switchMode(next: 'login' | 'register'): void {
  mode.value = next
  error.value = ''
  notice.value = ''
  password.value = ''
  confirmPassword.value = ''
}

function messageFor(exc: unknown): string {
  if (!(exc instanceof RequestError)) return '请求失败，请稍后重试'
  // 来源校验失败有三种原因，服务端已经分开返回；这里再把 `details` 里的两个主机念出来，
  // 否则「请刷新页面后重试」既不对症（刷新治不了代理改写 Host），也没法让人去查配置。
  if (exc.code === 'ORIGIN_REJECTED') {
    const details = exc.details as { origin?: unknown; expected?: unknown } | undefined
    const origin = typeof details?.origin === 'string' ? details.origin : ''
    const expected = Array.isArray(details?.expected)
      ? details.expected.filter((item): item is string => typeof item === 'string')
      : []
    if (origin) {
      const expectedText = expected.length > 0 ? `，本站应为 ${expected.join(' / ')}` : ''
      return `${exc.message}：本次来源 ${origin}${expectedText}`
    }
  }
  return exc.message
}

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = ''
  notice.value = ''

  if (mode.value === 'register' && password.value !== confirmPassword.value) {
    error.value = '两次输入的密码不一致'
    return
  }

  busy.value = true
  try {
    if (mode.value === 'login') {
      await login(loginName.value, password.value)
      password.value = ''
      emit('authenticated')
      return
    }
    await register(loginName.value, password.value)
    // 注册完回到登录页，并把登录名留下：让用户重新输一遍密码是对的，让他重新输一遍账号不是。
    const registeredName = loginName.value
    switchMode('login')
    loginName.value = registeredName
    notice.value = '账号已创建，请用刚设置的密码登录'
  } catch (exc) {
    error.value = messageFor(exc)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth-screen">
    <section class="auth-card">
      <header class="auth-head">
        <span class="auth-mark"><Landmark :size="22" /></span>
        <div>
          <strong>银行智能助理</strong>
          <span>模拟工作台</span>
        </div>
      </header>

      <div class="auth-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          :aria-selected="mode === 'login'"
          :class="{ active: mode === 'login' }"
          @click="switchMode('login')"
        >登录</button>
        <button
          type="button"
          role="tab"
          :aria-selected="mode === 'register'"
          :class="{ active: mode === 'register' }"
          @click="switchMode('register')"
        >注册</button>
      </div>

      <form class="auth-form" @submit.prevent="submit">
        <label>
          登录名
          <input
            v-model.trim="loginName"
            name="login_name"
            autocomplete="username"
            placeholder="3-32 位字母、数字或下划线"
            required
          />
        </label>

        <label>
          密码
          <PasswordInput
            v-model="password"
            name="password"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
            :placeholder="mode === 'login' ? '请输入密码' : '至少 12 个字符'"
            required
          />
        </label>

        <label v-if="mode === 'register'">
          确认密码
          <PasswordInput
            v-model="confirmPassword"
            name="confirm_password"
            label="确认密码"
            autocomplete="new-password"
            placeholder="再输入一次"
            required
          />
        </label>

        <p v-if="mode === 'register'" class="auth-hint">
          首版暂不支持自助找回密码，请自行保管好。收件邮箱只用于接收分析报告，不能用来找回密码。
        </p>

        <!-- 上一段会话为什么结束。浮在页面顶部停几秒就走：它解释「你刚才为什么在这里」，
             不是这个页面的一部分，不该占着表单的位置一直待着。 -->
        <p v-if="error" class="auth-message error" role="alert">{{ error }}</p>
        <p v-else-if="notice" class="auth-message success" role="status">{{ notice }}</p>

        <button class="primary auth-submit" type="submit" :disabled="busy">
          {{ submitLabel }}
        </button>
      </form>

      <footer class="auth-foot">
        <span>{{ title }}</span>
        <span class="auth-mode-hint">
          {{ mode === 'login' ? '还没有账号？' : '已有账号？' }}
          <button type="button" @click="switchMode(mode === 'login' ? 'register' : 'login')">
            {{ mode === 'login' ? '去注册' : '去登录' }}
          </button>
        </span>
      </footer>
    </section>

    <Transition name="auth-toast">
      <p v-if="reasonVisible" class="auth-toast" role="status">{{ props.reason }}</p>
    </Transition>
  </div>
</template>

<style scoped>
.auth-screen{min-height:100dvh;display:grid;place-items:center;padding:24px;background:#f3f5f4}
.auth-card{width:min(420px,100%);background:#fff;border:1px solid #dce2de;border-radius:8px;box-shadow:0 18px 60px rgba(28,48,37,.10);padding:28px}
.auth-head{display:flex;align-items:center;gap:12px;margin-bottom:22px}
.auth-mark{display:grid;place-items:center;width:40px;height:40px;color:#fff;background:#176649;border-radius:6px}
.auth-head strong{font-size:18px;display:block;color:#24312b}
.auth-head span{font-size:11px;color:#7d8680}
.auth-tabs{display:flex;gap:4px;padding:3px;background:#f1f4f2;border-radius:5px;margin-bottom:20px}
.auth-tabs button{flex:1;height:32px;border-radius:4px;font-size:13px;color:#66746b}
.auth-tabs button.active{background:#fff;color:#176649;font-weight:600;box-shadow:0 1px 2px rgba(28,48,37,.10)}
.auth-tabs button:hover:not(.active){background:transparent}
.auth-form{display:flex;flex-direction:column;gap:15px}
.auth-form label{display:flex;flex-direction:column;gap:7px;font-size:12px;color:#65746a}
/* `:deep(input)` 而不是 `input`：密码框现在是 PasswordInput 的子节点，作用域属性只落在
   子组件的根元素上，普通后代选择器够不进去 —— 少了这个，输入框会变成无边框的一条线。 */
.auth-form :deep(input){height:38px;border:1px solid #d8e1db;border-radius:4px;background:#fff;padding:0 10px;color:#2e3c34;outline:none;font-size:13px}
.auth-form :deep(input:focus){border-color:#70a68b}
.auth-hint{font-size:11px;line-height:1.7;color:#836627;background:#fbf6e7;border:1px solid #eee0bd;border-radius:4px;padding:9px 11px}
.auth-message{padding:9px 11px;border-radius:4px;font-size:12px;overflow-wrap:anywhere}
.auth-message.error{background:#fff3f1;color:#a64c47;border:1px solid #eaccc8}
.auth-message.success{background:#eef7f0;color:#25704d;border:1px solid #cfe3d5}
/* 一次性提示：固定在最上方，不参与表单布局，所以出现和消失都不会把按钮顶来顶去。 */
.auth-toast{position:fixed;top:24px;left:50%;transform:translate(-50%,0);z-index:20;margin:0;padding:10px 18px;border-radius:999px;background:#24312b;color:#fff;font-size:13px;line-height:1.5;box-shadow:0 10px 30px rgba(28,48,37,.24);overflow-wrap:anywhere;max-width:calc(100vw - 48px)}
.auth-toast-enter-active,.auth-toast-leave-active{transition:opacity .28s ease,transform .28s ease}
.auth-toast-enter-from,.auth-toast-leave-to{opacity:0;transform:translate(-50%,-10px)}
.auth-submit{width:100%;min-height:40px}
.auth-foot{display:flex;align-items:center;justify-content:space-between;margin-top:20px;font-size:11px;color:#94a197}
.auth-mode-hint{display:flex;align-items:center;gap:2px}
.auth-mode-hint button{font-size:11px;color:#176649;padding:2px 4px;border-radius:3px}
</style>
