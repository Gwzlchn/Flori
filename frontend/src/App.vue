<script setup lang="ts">
import AppShell from './components/layout/AppShell.vue'
import Toast from './components/common/Toast.vue'
import SubmitDialog from './components/job/SubmitDialog.vue'
import { useGlobalWs } from './composables/useGlobalWs'
import { ref, provide } from 'vue'

useGlobalWs()

const toast = ref<{ message: string; type: 'success' | 'error' | 'info' } | null>(null)

function showToast(message: string, type: 'success' | 'error' | 'info' = 'info') {
  toast.value = { message, type }
}

provide('showToast', showToast)
</script>

<template>
  <AppShell />
  <SubmitDialog />
  <Toast v-if="toast" :message="toast.message" :type="toast.type" @close="toast = null" />
</template>
