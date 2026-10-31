<script setup lang="ts">
/**
 * 深度分析报告邮件发送确认弹窗
 */

import { computed, ref } from 'vue'
import { Mail, X } from 'lucide-vue-next'

const props = defineProps<{
  periodStart: string
  periodEnd: string
  analysisResult: any
  /**
   * 已绑定的收件邮箱。为空表示还没绑 —— **只有这时才显示输入框**。
   * 已经绑过就一直发到那个地址，不再让人把地址重复输一遍。
   */
  boundEmail?: string
}>()

const emit = defineEmits<{
  /** `email` 为空串 = 用已绑定的那个；非空 = 先把这个地址绑定下来再发。 */
  (e: 'confirm', email: string): void
  (e: 'cancel'): void
}>()

/** 只在没绑定时才要地址。 */
const needsEmail = computed(() => !props.boundEmail)
const email = ref('')
const error = ref('')
const isValidEmail = computed(() => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value.trim()))

function handleConfirm() {
  if (!needsEmail.value) {
    emit('confirm', '')   // 已经绑过：确定就是确定，不再问地址
    return
  }
  if (!isValidEmail.value) {
    error.value = '请输入有效的邮箱地址'
    return
  }
  emit('confirm', email.value.trim())
}

function handleCancel() {
  emit('cancel')
}
</script>

<template>
  <div class="modal-overlay" @click.self="handleCancel">
    <div class="modal-card">
      <div class="modal-header">
        <div class="header-title">
          <Mail :size="20" />
          <h3>发送分析报告</h3>
        </div>
        <button class="icon-btn" @click="handleCancel">
          <X :size="18" />
        </button>
      </div>

      <div class="modal-body">
        <div class="prompt-text">
          <p>{{ needsEmail ? '是否将此分析报告发送到您的邮箱？请先填写收件地址：' : '是否将此分析报告发送到您的邮箱？' }}</p>
          <p class="period-info">分析区间：{{ periodStart }} ~ {{ periodEnd }}</p>
        </div>

        <!-- 只在还没绑定邮箱时出现；绑过就一直发到那个地址。 -->
        <div v-if="needsEmail" class="bind-form">
          <input
            v-model="email"
            type="email"
            name="bind-email"
            autocomplete="off"
            placeholder="example@email.com"
            aria-label="收件邮箱"
            @keydown.enter.prevent="handleConfirm"
          />
          <p v-if="error" class="bind-error">{{ error }}</p>
          <p class="bind-hint">发送前会把它存成你的收件邮箱，下次不用再填。</p>
        </div>
        <p v-else class="bound-note">将发送到 <strong>{{ boundEmail }}</strong></p>
      </div>

      <div class="modal-footer">
        <button class="btn btn-secondary" @click="handleCancel">
          不了
        </button>
        <button
          class="btn btn-primary"
          :disabled="needsEmail && !isValidEmail"
          @click="handleConfirm"
        >
          是，发送
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 20px;
}

.modal-card {
  background: white;
  border-radius: 12px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  width: 100%;
  max-width: 480px;
  overflow: hidden;
  animation: slideUp 0.2s ease-out;
}

@keyframes slideUp {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 24px;
  border-bottom: 1px solid #e5e7eb;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-title h3 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #1a1a1a;
}

.icon-btn {
  padding: 4px;
  background: transparent;
  border: none;
  color: #6b7280;
  cursor: pointer;
  border-radius: 4px;
  transition: all 0.2s;
}

.icon-btn:hover {
  background: #f3f4f6;
  color: #374151;
}

.modal-body {
  padding: 24px;
}

.prompt-text {
  text-align: center;
}

.prompt-text p {
  margin: 0 0 12px 0;
  font-size: 15px;
  color: #374151;
  line-height: 1.6;
}

.period-info {
  font-size: 13px;
  color: #6b7280;
  background: #f9fafb;
  padding: 8px 12px;
  border-radius: 6px;
  display: inline-block;
}

/* 没绑定邮箱时的收件地址输入。绑定过就不出现。 */
.bind-form {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.bind-form input {
  width: 100%;
  height: 38px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 0 12px;
  font-size: 14px;
  color: #1a1a1a;
  outline: none;
}

.bind-form input:focus {
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
}

.bind-error {
  margin: 0;
  font-size: 12px;
  color: #dc2626;
}

.bind-hint {
  margin: 0;
  font-size: 12px;
  color: #9ca3af;
}

.bound-note {
  margin: 14px 0 0;
  font-size: 13px;
  color: #6b7280;
}

.bound-note strong {
  color: #1a1a1a;
}

.status-message {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  border-radius: 8px;
  font-size: 14px;
}

.status-message.success {
  background: #f0fdf4;
  color: #16a34a;
  border: 1px solid #dcfce7;
}

.status-message.error {
  background: #fef2f2;
  color: #dc2626;
  border: 1px solid #fee2e2;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding: 16px 24px;
  border-top: 1px solid #e5e7eb;
  background: #f9fafb;
}

.btn {
  padding: 10px 24px;
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

.btn-secondary {
  background: white;
  color: #374151;
  border: 1px solid #d1d5db;
}

.btn-secondary:hover:not(:disabled) {
  background: #f9fafb;
  border-color: #9ca3af;
}

.btn-primary {
  background: #3b82f6;
  color: white;
}

.btn-primary:hover:not(:disabled) {
  background: #2563eb;
}

@media (max-width: 600px) {
  .modal-card {
    max-width: 100%;
  }

  .modal-header {
    padding: 16px 20px;
  }

  .modal-body {
    padding: 20px;
  }

  .modal-footer {
    padding: 12px 20px;
  }
}
</style>
