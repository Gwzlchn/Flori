<!-- 弹窗：overlay + modal，v-model 控制显隐。对应原型 <div class="overlay"><div class="modal">。 -->
<script setup lang="ts">
import { X } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    /** v-model 显隐 */
    modelValue: boolean
    title?: string
    /** 加宽 -> .modal.wide */
    wide?: boolean
  }>(),
  { wide: false },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

function close() {
  emit('update:modelValue', false)
}

// 点击遮罩空白处关闭（点 modal 内部不关）
function onOverlayClick(e: MouseEvent) {
  if (e.target === e.currentTarget) close()
}
</script>

<template>
  <div v-if="props.modelValue" class="overlay show" @click="onOverlayClick">
    <div class="modal" :class="{ wide: props.wide }">
      <div class="hd">
        <slot name="header-icon" />
        <b>{{ props.title }}</b>
        <button class="ghost" @click="close"><X :size="16" /></button>
      </div>
      <div class="bd">
        <slot />
      </div>
      <div v-if="$slots.footer" class="ft">
        <slot name="footer" :close="close" />
      </div>
    </div>
  </div>
</template>
