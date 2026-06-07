<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useWorkerStore } from '../stores/workers'
import WorkerCard from '../components/worker/WorkerCard.vue'
import WorkerJoinGuide from '../components/worker/WorkerJoinGuide.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { RefreshCw, HardDrive } from 'lucide-vue-next'

const workerStore = useWorkerStore()

onMounted(() => workerStore.fetchAll())

const sortedWorkers = computed(() => {
  const order: Record<string, number> = {
    'online-busy': 0, 'online-idle': 1, draining: 2, offline: 3, stale: 4,
  }
  return [...workerStore.workers].sort((a, b) => (order[a.status] ?? 5) - (order[b.status] ?? 5))
})

// 在线数一律以后端 status 为准(online-* 即在线);不再用时间戳自算(时区会算错→在线判离线)。
const onlineCount = computed(() => workerStore.workers.filter(w => w.status.startsWith('online')).length)

const busyCount = computed(() => workerStore.workers.filter(w => w.status === 'online-busy').length)

const todayCompleted = computed(() =>
  workerStore.workers.reduce((sum, w) => sum + w.tasks_completed, 0)
)
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-xl font-bold flex items-center gap-2">
        <HardDrive :size="22" />
        Worker 管理
      </h2>
      <button @click="workerStore.fetchAll()" class="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors">
        <RefreshCw :size="16" :class="workerStore.loading ? 'animate-spin' : ''" />
      </button>
    </div>

    <!-- Summary -->
    <div class="flex gap-4 text-sm text-gray-600">
      <span>在线 <strong>{{ onlineCount }}</strong> / {{ workerStore.workers.length }}</span>
      <span>忙碌 <strong>{{ busyCount }}</strong></span>
      <span>累计完成 <strong>{{ todayCompleted }}</strong></span>
    </div>

    <!-- Worker list -->
    <div v-if="workerStore.loading && workerStore.workers.length === 0" class="text-sm text-gray-400 py-8 text-center">加载中...</div>
    <div v-else-if="workerStore.workers.length === 0">
      <EmptyState message="暂无 Worker" />
    </div>
    <div v-else class="space-y-3">
      <WorkerCard v-for="w in sortedWorkers" :key="w.id" :worker="w" />
    </div>

    <WorkerJoinGuide />
  </div>
</template>
