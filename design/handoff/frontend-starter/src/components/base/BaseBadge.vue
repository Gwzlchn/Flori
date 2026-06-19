<!-- 薄封装徽章：variant 映射 mnemo.css 的 b-ok/b-info/b-run/b-warn/b-bad/b-mut/b-brand。 -->
<script setup lang="ts">
import { computed } from 'vue'
import type { BadgeVariant } from '@/types'

const props = withDefaults(
  defineProps<{
    variant?: BadgeVariant
  }>(),
  { variant: 'mut' },
)

const VARIANT_CLASS: Record<BadgeVariant, string> = {
  ok: 'b-ok',
  info: 'b-info',
  run: 'b-run',
  warn: 'b-warn',
  bad: 'b-bad',
  mut: 'b-mut',
  brand: 'b-brand',
}

const classes = computed(() => `badge ${VARIANT_CLASS[props.variant]}`)
</script>

<template>
  <span :class="classes">
    <slot />
  </span>
</template>
