<script setup lang="ts">
import AppShell from './components/layout/AppShell.vue'
import Toast from './components/common/Toast.vue'
import SubmitDialog from './components/job/SubmitDialog.vue'
import { useGlobalWs } from './composables/useGlobalWs'
import { ref, provide } from 'vue'
import { ensureSourceCatalog } from './constants/sources'

useGlobalWs()
void ensureSourceCatalog()

// id 自增并作 Toast 的 :key:同一文案连续提示也强制重建组件,确保每次都重新弹出/计时。
const toast = ref<{ id: number; message: string; type: 'success' | 'error' | 'info' } | null>(null)
let toastSeq = 0

function showToast(message: string, type: 'success' | 'error' | 'info' = 'info') {
  toast.value = { id: ++toastSeq, message, type }
}

provide('showToast', showToast)
</script>

<template>
  <AppShell />
  <SubmitDialog />
  <Toast v-if="toast" :key="toast.id" :message="toast.message" :type="toast.type" @close="toast = null" />
</template>
