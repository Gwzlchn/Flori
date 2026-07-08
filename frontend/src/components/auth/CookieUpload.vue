<script setup lang="ts">
import { ref, inject } from 'vue'
import { useApi } from '../../composables/useApi'
import { Upload } from 'lucide-vue-next'

const props = defineProps<{ platform: string }>()
const emit = defineEmits<{ success: [] }>()

const api = useApi()
const showToast = inject<(msg: string, type: 'success' | 'error' | 'info') => void>('showToast')
const uploading = ref(false)

async function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  uploading.value = true
  try {
    const form = new FormData()
    form.append('file', file)
    await api.upload(`/api/auth/${props.platform}/cookies`, form)
    showToast?.(`${props.platform} cookies 已上传`, 'success')
    emit('success')
  } catch (e: any) {
    showToast?.(e.message || '上传失败', 'error')
  } finally {
    uploading.value = false
    input.value = ''
  }
}
</script>

<template>
  <label class="btn sm cookie-upload" :class="{ disabled: uploading }">
    <Upload :size="14" />
    <span>{{ uploading ? '上传中…' : '上传 cookies.txt' }}</span>
    <input type="file" accept=".txt" class="hidden" @change="onFileChange" :disabled="uploading" />
  </label>
</template>

<style scoped>
.cookie-upload { white-space: nowrap; }
.cookie-upload.disabled { opacity: .6; cursor: wait; }
</style>
