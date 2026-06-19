<!-- 薄封装 chip：mnemo.css 的 .chip，active -> .chip.on。对应原型筛选/标签 chip。 -->
<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    active?: boolean
  }>(),
  { active: false },
)

const classes = computed(() => (props.active ? 'chip on' : 'chip'))
</script>

<template>
  <span :class="classes">
    <slot />
  </span>
</template>
