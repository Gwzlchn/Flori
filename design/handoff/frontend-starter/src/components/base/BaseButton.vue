<!-- 薄封装按钮：emit mnemo.css 的 .btn 系列类。对应原型 <button class="btn ..."> -->
<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 视觉变体 -> mnemo.css：pri / 默认 / danger / ghost */
    variant?: 'pri' | 'default' | 'danger' | 'ghost'
    /** 小尺寸 -> .btn.sm */
    size?: 'sm'
  }>(),
  { variant: 'default' },
)

const classes = computed(() => {
  // ghost 是独立类（.ghost），不与 .btn 叠加
  if (props.variant === 'ghost') return 'ghost'
  const parts = ['btn']
  if (props.variant === 'pri') parts.push('pri')
  if (props.variant === 'danger') parts.push('danger')
  if (props.size === 'sm') parts.push('sm')
  return parts.join(' ')
})
</script>

<template>
  <button :class="classes">
    <slot />
  </button>
</template>
