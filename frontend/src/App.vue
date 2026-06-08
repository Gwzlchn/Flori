<script setup lang="ts">
import AppLayout from './components/layout/AppLayout.vue'
import Toast from './components/common/Toast.vue'
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
  <AppLayout>
    <router-view />
  </AppLayout>
  <Toast v-if="toast" :message="toast.message" :type="toast.type" @close="toast = null" />
</template>
