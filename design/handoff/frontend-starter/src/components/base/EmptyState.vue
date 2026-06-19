<!-- 空状态：mnemo.css 的 .state。对应原型搜索空态 / 列表空态。 -->
<script setup lang="ts">
import { Inbox } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    /** lucide 图标组件，默认 Inbox */
    icon?: unknown
    text?: string
  }>(),
  { text: '暂无内容' },
)
</script>

<template>
  <div class="state">
    <component :is="icon ?? Inbox" class="big" :size="44" />
    <div class="t">{{ text }}</div>
    <slot />
  </div>
</template>
