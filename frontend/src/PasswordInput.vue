<script setup lang="ts">
/**
 * 密码输入框：右侧一个「显示 / 隐藏」按钮。
 *
 * 为什么值得单独一个组件：这套界面里有八处密码框（登录、注册、确认密码、改密三格、API Key、
 * 支付验证码、附件解压密码）。逐处手写会出现「有的能看、有的不能看」，而「看一眼自己刚敲的
 * 密码」恰好是输错时唯一的自救手段 —— 尤其改密码那三格，用户没法用「粘贴到记事本」核对。
 *
 * 三个决定：
 * 1. 按钮是 `type="button"`。默认的 `submit` 会让「点一下眼睛」直接提交表单，在登录页就是
 *    拿着半截密码去换 401。
 * 2. 属性全部透传（`inheritAttrs: false` + `v-bind="$attrs"`）。`placeholder`、`autocomplete`、
 *    `name`、`inputmode`、`@keydown.enter` 这些每处都不一样，包一层不能把它们吞掉。
 * 3. `:type` 写在 `v-bind="$attrs"` 之后，由组件说了算 —— 否则调用方传个 `type` 就把
 *    显示/隐藏架空了。
 *
 * 切换不清空输入、不重置焦点位置，也不在提交后自动隐藏：用户按下眼睛是他自己的选择，
 * 替他改回来只会让人以为没生效。
 */
import { computed, ref } from 'vue'
import { Eye, EyeOff } from 'lucide-vue-next'

defineOptions({ inheritAttrs: false })

const model = defineModel<string>({ default: '' })

const props = withDefaults(
  defineProps<{
    /** 读屏念的名字。默认「密码」，验证码那处要传真实的名字（如「支付密码」）。 */
    label?: string
  }>(),
  { label: '密码' },
)

const visible = ref(false)

const toggleLabel = computed(() =>
  visible.value ? `隐藏${props.label}` : `显示${props.label}`,
)
</script>

<template>
  <span class="password-field">
    <input
      v-bind="$attrs"
      v-model="model"
      :type="visible ? 'text' : 'password'"
      class="password-native"
    />
    <button
      type="button"
      class="password-toggle"
      :title="toggleLabel"
      :aria-label="toggleLabel"
      :aria-pressed="visible"
      @click="visible = !visible"
    >
      <EyeOff v-if="visible" :size="16" aria-hidden="true" />
      <Eye v-else :size="16" aria-hidden="true" />
    </button>
  </span>
</template>

<style scoped>
/* 宽度是这里最容易被忽略的一条。原来这些 input 是 flex 列容器里的直接子项，被
   `align-items: stretch` 拉满整行；包一层之后它变成普通块级子元素，退回浏览器的默认宽度
   （约 195px），而绝对定位的按钮还挂在满宽的包装层右边缘 —— 于是眼睛跑到框外面去了。
   `width:100%` 两边都给，包装层负责撑满，输入框负责填满包装层。 */
.password-field{position:relative;display:block;width:100%}
.password-field .password-native{width:100%;padding-right:36px}
.password-toggle{
  position:absolute;top:50%;right:4px;transform:translateY(-50%);
  width:28px;height:28px;border-radius:4px;color:#94a197;
}
.password-toggle:hover:not(:disabled){color:#3c4a42}
.password-toggle[aria-pressed='true']{color:#176649}
</style>
