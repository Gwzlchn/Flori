<!-- 薄封装卡片：mnemo.css 的 .card，可选 .pad 内边距。对应原型 <div class="card pad"> -->
<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 是否带内边距 -> .card.pad */
    pad?: boolean
  }>(),
  { pad: true },
)

const classes = computed(() => (props.pad ? 'card pad' : 'card'))
</script>

<template>
  <div :class="classes">
    <slot />
  </div>
</template>
