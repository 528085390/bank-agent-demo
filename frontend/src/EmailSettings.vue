<script setup lang="ts">
/**
 * 邮箱设置组件
 * 允许用户绑定邮箱，用于接收账单分析报告
 */

import { ref, computed, watch } from 'vue'
import { Mail, Check, AlertCircle } from 'lucide-vue-next'

const props = defineProps<{
  currentEmail?: string
}>()

const emit = defineEmits<{
  (e: 'update', email: string | null): void
  (e: 'close'): void
}>()

const email = ref(props.currentEmail || '')
const isEditing = ref(!props.currentEmail)
const isSaving = ref(false)
const error = ref('')
/** 成功提示的文案：绑定时和清除时说的不是一件事。 */
const success = ref('')

/**
 * 跟着 prop 走，别只在 setup 里读一次。
 *
 * 面板是「先打开、再异步取邮箱」的（见 `App.openLlmSettings`），所以这个组件挂载时
 * `currentEmail` 常常还是 undefined —— 只读一次的话，面板会一直停在空表单上，用户
 * 关掉再打开才看见邮箱其实早就绑着，看着就像「什么都没填就自动绑定了」。
 */
watch(
  () => props.currentEmail,
  (value) => {
    if (isSaving.value) return
    // 用户已经在输入框里打了字，就别覆盖他的输入。
    if (isEditing.value && email.value.trim() !== '') return
    email.value = value || ''
    isEditing.value = !value
  },
)

const isValidEmail = computed(() => {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
  return emailRegex.test(email.value)
})

async function saveEmail() {
  if (!isValidEmail.value) {
    error.value = '请输入有效的邮箱地址'
    return
  }

  isSaving.value = true
  error.value = ''
  success.value = ''

  try {
    const response = await fetch('/api/settings/email', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ email: email.value })
    })

    const result = await response.json()

    if (!response.ok) {
      throw new Error(result.error?.message || '保存失败')
    }

    success.value = '邮箱设置成功'
    isEditing.value = false
    emit('update', email.value)

    setTimeout(() => {
      success.value = ''
    }, 3000)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败，请重试'
  } finally {
    isSaving.value = false
  }
}

/**
 * 解除绑定。
 *
 * 报告只发到绑定地址，地址填错时得有路可退 —— 只能「改成另一个」不算路可退。
 * 失败时留在原地显示错误：`isEditing` 不动，用户看得见那条报错。
 */
async function clearEmail() {
  isSaving.value = true
  error.value = ''
  success.value = ''

  try {
    const response = await fetch('/api/settings/email', {
      method: 'DELETE',
      credentials: 'include',
    })

    const result = await response.json()

    if (!response.ok) {
      throw new Error(result.error?.message || '清除失败')
    }

    email.value = ''
    isEditing.value = true
    success.value = '已清除邮箱绑定'
    emit('update', null)

    setTimeout(() => {
      success.value = ''
    }, 3000)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '清除失败，请重试'
  } finally {
    isSaving.value = false
  }
}

function startEdit() {
  // 以服务端那份为准：面板开着的时候邮箱可能被别处改过。
  email.value = props.currentEmail || ''
  isEditing.value = true
  error.value = ''
  success.value = ''
}

function cancelEdit() {
  if (props.currentEmail) {
    email.value = props.currentEmail
    isEditing.value = false
    error.value = ''
  } else {
    emit('close')
  }
}
</script>

<template>
  <div class="email-settings">
    <div class="settings-header">
      <Mail :size="20" class="header-icon" />
      <h3 class="settings-title">邮箱设置</h3>
    </div>

    <div class="settings-body">
      <p class="settings-description">
        绑定邮箱后，系统可以将账单分析报告（PDF）发送到您的邮箱
      </p>

      <div v-if="!isEditing && currentEmail" class="current-email">
        <div class="email-display">
          <Mail :size="16" class="email-icon" />
          <span class="email-text">{{ currentEmail }}</span>
        </div>
        <div class="email-actions">
          <button class="btn-edit" :disabled="isSaving" @click="startEdit">
            修改
          </button>
          <button class="btn-edit" :disabled="isSaving" @click="clearEmail">
            清除绑定
          </button>
        </div>
      </div>

      <div v-else class="email-form">
        <div class="form-group">
          <label class="form-label">邮箱地址</label>
          <input
            v-model="email"
            type="email"
            name="bind-email"
            autocomplete="off"
            class="form-input"
            :class="{ 'input-error': error }"
            placeholder="example@email.com"
            :disabled="isSaving"
            @keyup.enter="saveEmail"
          />
        </div>

        <div class="form-actions">
          <button
            class="btn btn-primary"
            :disabled="!isValidEmail || isSaving"
            @click="saveEmail"
          >
            <span v-if="isSaving">保存中...</span>
            <span v-else>保存</span>
          </button>
          <button
            class="btn btn-secondary"
            :disabled="isSaving"
            @click="cancelEdit"
          >
            取消
          </button>
        </div>
      </div>

      <!-- 提示放在两个分支之外：清除失败时页面停在「已绑定」那一支，错误也得看得见。 -->
      <div v-if="error" class="alert alert-error">
        <AlertCircle :size="16" />
        <span>{{ error }}</span>
      </div>

      <div v-if="success" class="alert alert-success">
        <Check :size="16" />
        <span>{{ success }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.email-settings {
  background: white;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.settings-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 2px solid #f0f0f0;
}

.header-icon {
  color: #3b82f6;
}

.settings-title {
  font-size: 18px;
  font-weight: 600;
  color: #1a1a1a;
  margin: 0;
}

.settings-body {
  padding-top: 8px;
}

.settings-description {
  font-size: 14px;
  color: #666;
  line-height: 1.6;
  margin: 0 0 20px 0;
}

.current-email {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: #f9fafb;
  border-radius: 6px;
  border: 1px solid #e5e7eb;
}

.email-display {
  display: flex;
  align-items: center;
  gap: 8px;
}

.email-icon {
  color: #6b7280;
}

.email-text {
  font-size: 14px;
  color: #1a1a1a;
  font-weight: 500;
}

.email-actions {
  display: flex;
  gap: 8px;
}

.btn-edit {
  padding: 6px 16px;
  background: white;
  border: 1px solid #d1d5db;
  border-radius: 4px;
  color: #374151;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-edit:hover:not(:disabled) {
  background: #f9fafb;
  border-color: #9ca3af;
}

.btn-edit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.email-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-label {
  font-size: 13px;
  font-weight: 500;
  color: #374151;
}

.form-input {
  padding: 10px 12px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 14px;
  color: #1a1a1a;
  transition: all 0.2s;
}

.form-input:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
}

.form-input:disabled {
  background: #f9fafb;
  color: #9ca3af;
  cursor: not-allowed;
}

.form-input.input-error {
  border-color: #ef4444;
}

.alert {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 13px;
  /* 提示挂在外层，不再是表单那一列 flex 的子项，间距得自己带。 */
  margin-top: 16px;
}

.alert-error {
  background: #fef2f2;
  color: #dc2626;
  border: 1px solid #fee2e2;
}

.alert-success {
  background: #f0fdf4;
  color: #16a34a;
  border: 1px solid #dcfce7;
}

.form-actions {
  display: flex;
  gap: 12px;
}

.btn {
  padding: 10px 20px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  border: none;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: #3b82f6;
  color: white;
}

.btn-primary:hover:not(:disabled) {
  background: #2563eb;
}

.btn-secondary {
  background: white;
  color: #374151;
  border: 1px solid #d1d5db;
}

.btn-secondary:hover:not(:disabled) {
  background: #f9fafb;
  border-color: #9ca3af;
}
</style>
