<script setup lang="ts">
import { Cpu, MessageSquare, Pause, Play, X } from 'lucide-vue-next'
import StatusBadge from '../../common/StatusBadge.vue'
import { fmtDuration, fmtRelative } from '../../../utils/datetime'
import type { Worker } from '../../../types'

defineProps<{
  workers: Worker[]; sortedWorkers: Worker[]; loading: boolean; pendingCount: number
  driftEnabled: boolean; systemVersion: string; sameVersionCount: number; driftCount: number; rowBusy: string | null
  isOnline: (worker: Worker) => boolean; dotClass: (status: string) => string; computeDesc: (worker: Worker) => string
  drifted: (worker: Worker) => boolean; versionLabel: (version: string | null | undefined) => string
  versionBuild: (version: string | null | undefined) => string; loadText: (worker: Worker) => string; trafficText: (worker: Worker) => string
}>()
defineEmits<{ open: [workerId: string]; toggle: [worker: Worker]; remove: [worker: Worker] }>()
</script>

<template>
  <div class="seclabel workers-title"><Cpu :size="14" />Worker · {{ workers.length }}<template v-if="driftEnabled"><span class="sep">·</span><span class="version-summary">系统版本 <b class="mono">{{ versionLabel(systemVersion) }}</b></span><span v-if="sameVersionCount > 0" class="same"> · ✓{{ sameVersionCount }} 同版</span><span v-if="driftCount > 0" class="drift"> · ▲{{ driftCount }} 版本漂移</span></template></div>
  <div v-if="workers.length === 0 && pendingCount > 0" class="worker-warning"><span class="badge b-warn">{{ pendingCount }} 个任务在排队，但无可用 worker</span></div>
  <div v-else-if="workers.length === 0" class="worker-warning"><span class="badge b-mut">0 个 worker 在线 · 任务将排队等待算力</span></div>
  <slot name="enrollment" />
  <div v-if="loading && workers.length === 0" class="card pad loading">加载中…</div>
  <div v-else-if="workers.length === 0" class="card pad empty"><Cpu :size="40" :stroke-width="1" /><div>还没有接入任何 Worker</div><p class="lead">在上方「接入新 Worker」生成临时接入 token，按 Gateway HTTPS 命令在任意机器上拉起一个 worker 即可。</p></div>
  <div v-else class="list worker-list">
    <div v-for="worker in sortedWorkers" :key="worker.id" class="card pad wcard" :class="{ off: !isOnline(worker) }" @click="$emit('open', worker.id)">
      <span class="dot" :class="[dotClass(worker.status), { pulse: worker.status === 'online-busy' }]" />
      <div class="wcard-main"><div class="wcard-hd"><b class="mono wcard-id">{{ worker.id }}</b><StatusBadge :status="worker.status" /><span class="badge b-mut">{{ worker.type.toUpperCase() }}</span><span v-if="worker.status === 'online-busy' && worker.current_step" class="badge b-run">当前任务 {{ worker.current_step }}<span v-if="worker.current_job" class="mono">@ {{ worker.current_job }}</span></span><span v-if="drifted(worker)" class="badge b-warn" :title="`期望 ${systemVersion}，当前 ${worker.spec?.version}`">旧版本 {{ versionLabel(worker.spec?.version) }}<span v-if="versionBuild(worker.spec?.version)">·{{ versionBuild(worker.spec?.version) }}</span></span></div>
        <div class="wcard-stats"><span class="wstat"><b>{{ worker.tasks_completed }}</b>完成</span><span class="wstat"><b :class="{ bad: worker.tasks_failed > 0 }">{{ worker.tasks_failed }}</b>失败</span><span class="wstat"><b>{{ worker.concurrency }}</b>并发</span><span v-if="loadText(worker)" class="wload">{{ loadText(worker) }}</span></div>
        <div class="wcard-sub"><span v-if="worker.hostname">{{ worker.hostname }}</span><span v-if="worker.hostname" class="sep">·</span><span>{{ computeDesc(worker) }}</span><span class="sep">·</span><span :title="worker.spec?.version || '未上报版本(多为旧镜像 worker)'" :class="{ drift: drifted(worker) }">{{ worker.spec?.version ? versionLabel(worker.spec?.version) : '版本未报' }}</span><template v-if="worker.total_duration_sec > 0"><span class="sep">·</span><span>运行 {{ fmtDuration(worker.total_duration_sec) }}</span></template><template v-if="trafficText(worker)"><span class="sep">·</span><span title="网关中转:拉取产物 / 回传产物">中转 {{ trafficText(worker) }}</span></template><span class="sep">·</span><span>心跳 {{ fmtRelative(worker.last_heartbeat) }}</span></div>
      </div>
      <template v-if="isOnline(worker)"><button class="btn sm" :disabled="rowBusy === worker.id" @click.stop="$emit('toggle', worker)"><Play v-if="worker.status === 'paused'" :size="13" /><Pause v-else :size="13" />{{ worker.status === 'paused' ? '继续' : '暂停' }}</button><button class="btn sm" @click.stop="$emit('open', worker.id)"><MessageSquare :size="13" />备注</button></template>
      <button class="btn sm danger" :disabled="rowBusy === worker.id" @click.stop="$emit('remove', worker)"><X :size="13" />移除</button>
    </div>
  </div>
</template>

<style scoped>
.workers-title { margin-bottom: 12px; }.workers-title > .sep { margin: 0 6px; color: var(--ink-300); }.version-summary { font-weight: 500; text-transform: none; letter-spacing: 0; }.same { font-weight: 500; color: var(--ok); text-transform: none; letter-spacing: 0; }.drift { color: var(--warn); }.workers-title .drift { font-weight: 500; text-transform: none; letter-spacing: 0; }
.worker-warning { margin-bottom: 8px; }.loading { color: var(--ink-500); font-size: 13px; margin-bottom: 24px; }.empty { margin-bottom: 24px; display: flex; flex-direction: column; align-items: center; gap: 10px; text-align: center; padding: 36px 18px; }.empty svg { color: var(--ink-300); }.empty > div { font-size: 14px; color: var(--ink-700); font-weight: 600; }.empty p { max-width: 360px; }.worker-list { margin-bottom: 24px; }
</style>
