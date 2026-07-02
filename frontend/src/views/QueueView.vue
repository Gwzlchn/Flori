<script setup lang="ts">
// 任务队列(/system/queue):各资源池里运行中 + 排队中的 task,与独立 worker 页风格接近、
// 与 worker 任务历史共用 TaskRow。从 /system 资源池区「查看队列」或池卡队列徽章进入(可预选某池)。
// 队列是动态快照:定时轮询刷新;另起 1s ticker 让 TaskRow 的「已等/已运行」时长走字。
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkerStore } from '../stores/workers'
import { useGlobalStore } from '../stores/global'
import TaskRow from '../components/system/TaskRow.vue'
import type { QueueStatus } from '../types'
import { ListChecks, RefreshCw, ArrowLeft, Play, Clock } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const workerStore = useWorkerStore()
const global = useGlobalStore()

const data = ref<QueueStatus | null>(null)
const loading = ref(false)
const now = ref(Date.now())
const poolFilter = ref<string>(typeof route.query.pool === 'string' ? route.query.pool : '')

let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null

async function load() {
  loading.value = true
  try {
    data.value = await workerStore.fetchQueue()
  } catch { /* 非致命:留旧值 */ } finally {
    loading.value = false
  }
}

// 池筛选:全部 / 单池。池名取自返回数据(后端单一事实源,不前端硬编码)。
const poolNames = computed(() => (data.value?.pools ?? []).map(p => p.name))
const pools = computed(() => {
  const all = data.value?.pools ?? []
  return poolFilter.value ? all.filter(p => p.name === poolFilter.value) : all
})

const totalRunning = computed(() => (data.value?.pools ?? []).reduce((n, p) => n + p.running.length, 0))
const totalQueued = computed(() => (data.value?.pools ?? []).reduce((n, p) => n + p.queued_count, 0))

function selectPool(name: string) {
  poolFilter.value = name
  router.replace({ query: name ? { pool: name } : {} })
}

onMounted(() => {
  global.setCrumbs([{ t: '系统', to: '/system' }, { t: '队列' }])
  load()
  pollTimer = setInterval(load, 5000)
  tickTimer = setInterval(() => { now.value = Date.now() }, 1000)
})
onBeforeUnmount(() => {
  global.setCrumbs(null)
  if (pollTimer) clearInterval(pollTimer)
  if (tickTimer) clearInterval(tickTimer)
})
</script>

<template>
  <section class="page">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap">
      <button class="btn sm" @click="router.push('/system')"><ArrowLeft :size="14" />系统</button>
      <div class="h1"><ListChecks :size="18" />任务队列</div>
      <span class="dim" style="font-size:12.5px">运行中 {{ totalRunning }} · 排队中 {{ totalQueued }}</span>
      <button class="btn sm" style="margin-left:auto" :disabled="loading" @click="load">
        <RefreshCw :size="13" :class="loading ? 'spin' : ''" />刷新
      </button>
    </div>

    <!-- 池筛选 chip -->
    <div class="card pad" style="margin-bottom:16px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <button class="chip" :class="{ on: poolFilter === '' }" @click="selectPool('')">全部</button>
      <button v-for="p in poolNames" :key="p" class="chip" :class="{ on: poolFilter === p }" @click="selectPool(p)">{{ p }}</button>
    </div>

    <div v-if="loading && !data" class="card pad" style="color:var(--ink-500);font-size:13px">加载中…</div>
    <div v-else-if="pools.length === 0" class="card pad" style="color:var(--ink-500);font-size:13px;text-align:center;padding:28px">
      暂无资源池数据
    </div>

    <template v-else>
      <div v-for="pool in pools" :key="pool.name" class="card pad pool-card">
        <div class="card-h" style="display:flex;align-items:center;gap:10px">
          <span class="pool-name">{{ pool.name }}</span>
          <span class="dim" style="font-size:12px;font-weight:400">运行 {{ pool.running.length }} · 排队 {{ pool.queued_count }}</span>
        </div>

        <!-- 运行中 -->
        <div class="sub">
          <div class="sublabel"><Play :size="13" />运行中 · {{ pool.running.length }}</div>
          <div v-if="pool.running.length === 0" class="empty">当前无运行中 task</div>
          <div v-else class="list">
            <TaskRow
              v-for="t in pool.running"
              :key="`r-${t.job_id}-${t.step}`"
              state="running"
              :job-id="t.job_id" :step="t.step" :pool="t.pool"
              :title="t.title" :content-type="t.content_type" :pipeline="t.pipeline"
              :started-at="t.started_at" :worker="t.worker_hostname || t.worker_id"
              :now="now"
              @deleted="load"
            />
          </div>
        </div>

        <!-- 排队中 -->
        <div class="sub">
          <div class="sublabel"><Clock :size="13" />排队中 · {{ pool.queued_count }}</div>
          <div v-if="pool.queued.length === 0" class="empty">当前无排队 task</div>
          <div v-else class="list">
            <TaskRow
              v-for="t in pool.queued"
              :key="`q-${t.job_id}-${t.step}`"
              state="queued"
              :job-id="t.job_id" :step="t.step" :pool="t.pool"
              :title="t.title" :content-type="t.content_type" :pipeline="t.pipeline"
              :priority="t.priority" :enqueued-at="t.enqueued_at"
              :now="now"
              @deleted="load"
            />
          </div>
          <div v-if="pool.queued_count > pool.queued_shown" class="trunc">
            共 {{ pool.queued_count }} 条,已列前 {{ pool.queued_shown }} 条
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.chip {
  border: 1px solid var(--line); background: var(--card); color: var(--ink-600);
  border-radius: 999px; padding: 4px 13px; font-size: 12.5px; cursor: pointer;
}
.chip.on { background: var(--brand-500); border-color: var(--brand-500); color: #fff; }
.pool-card { margin-bottom: 14px; }
.pool-name { font-weight: 600; font-size: 14px; text-transform: uppercase; letter-spacing: .3px; }
.sub { margin-top: 12px; }
.sublabel {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--ink-500); margin-bottom: 6px;
}
.empty { color: var(--ink-400); font-size: 12.5px; padding: 6px 0; }
.trunc { color: var(--ink-500); font-size: 12px; margin-top: 8px; text-align: center; }
</style>
