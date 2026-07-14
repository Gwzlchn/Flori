<script setup lang="ts">
import { Cpu, Database, HardDrive, Server } from 'lucide-vue-next'
import { fmtBytes } from '../../../utils/format'
import type { DiskInfo, JobCounts, Throughput } from '../../../types'

defineProps<{
  versionTitle: string; systemVersion: string; frontendVersion: string; deployMode: string
  disk: DiskInfo | null; diskBarColor: string; traffic: { pull_bytes: number; push_bytes: number } | null
  onlineCount: number; workerCount: number; busyCount: number; pendingCount: number; doneCount: number
  jobs: JobCounts | null; throughput: Throughput | null; versionLabel: (version: string) => string
}>()
</script>

<template>
  <div class="seclabel" style="margin-bottom:8px"><Server :size="14" />系统</div>
  <div class="card pad statgrid" style="margin-bottom:16px">
    <div class="st-cell st-cell-version"><div class="st-lbl">版本</div><div class="st-val" :title="versionTitle">系统 {{ versionLabel(systemVersion) }} / 前端 {{ versionLabel(frontendVersion) }}</div></div>
    <div class="st-cell"><div class="st-lbl">部署</div><div class="st-val">{{ deployMode }}</div></div>
    <div class="st-cell"><div class="st-lbl"><HardDrive :size="11" />磁盘</div><template v-if="disk && disk.total_gb >= 0"><div class="st-val">{{ disk.used_gb }}/{{ disk.total_gb }}GB <b :style="{ color: disk.used_pct > 90 ? 'var(--bad)' : 'var(--ink-900)' }">{{ disk.used_pct }}%</b></div><div class="st-subline">剩余 {{ disk.available_gb }}GB</div><span class="track disk-track"><span :style="{ width: `${Math.min(100, disk.used_pct)}%`, background: diskBarColor }" /></span></template><div v-else class="st-val dim">不可用</div></div>
    <div v-if="traffic && (traffic.pull_bytes > 0 || traffic.push_bytes > 0)" class="st-cell"><div class="st-lbl" title="网关产物代理:出库=worker 拉取(NAS→worker) / 入库=回传(worker→NAS)">网关中转</div><div class="st-val">出库 {{ fmtBytes(traffic.pull_bytes) }} · 入库 {{ fmtBytes(traffic.push_bytes) }}</div></div>
  </div>
  <div class="seclabel" style="margin-bottom:8px"><Cpu :size="14" />Worker · 作业</div>
  <div class="card pad statgrid sg-worker" style="margin-bottom:18px">
    <div class="st-cell"><div class="st-lbl">Worker 在线 / 共</div><div class="st-val"><b>{{ onlineCount }} / {{ workerCount }}</b></div></div>
    <div class="st-cell"><div class="st-lbl">忙碌 · 处理中</div><div class="st-val"><b>{{ busyCount }}</b></div></div>
    <div class="st-cell"><div class="st-lbl">待处理 · 队列</div><div class="st-val"><b>{{ pendingCount }}</b></div></div>
    <div class="st-cell"><div class="st-lbl">累计完成 · 吞吐</div><div class="st-val"><b>{{ doneCount }}</b></div></div>
    <div class="st-cell"><div class="st-lbl"><Database :size="11" />内容(作业)</div><div v-if="jobs" class="st-val">共 {{ jobs.total }} · 处理中 {{ jobs.processing }} · 失败 <b :style="{ color: jobs.failed > 0 ? 'var(--bad)' : 'var(--ink-900)' }">{{ jobs.failed }}</b></div></div>
    <div class="st-cell"><div class="st-lbl">近 1h</div><div class="st-val">完成 {{ throughput?.done ?? 0 }} · 失败 {{ throughput?.failed ?? 0 }}</div></div>
  </div>
</template>

<style scoped>
.statgrid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px 24px; align-items: start; }
.st-cell { min-width: 0; }.st-lbl { display: flex; align-items: center; gap: 4px; font-size: 10.5px; color: var(--ink-400); letter-spacing: .03em; margin-bottom: 3px; }
.st-val { font-size: 13px; color: var(--ink-800); font-variant-numeric: tabular-nums; line-height: 1.35; word-break: break-word; }.st-subline { margin-top: 2px; font-size: 12px; color: var(--ink-400); font-variant-numeric: tabular-nums; }
.disk-track { margin-top: 5px; max-width: 240px; }
@media (max-width: 900px) { .statgrid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 560px) { .statgrid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
