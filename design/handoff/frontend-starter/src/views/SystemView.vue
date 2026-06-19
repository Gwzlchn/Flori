<!-- 系统与 Worker 监控（原型 id="system"）：指标 + Worker 列表 + 接入新 Worker。占位实现。 -->
<script setup lang="ts">
import { useRouter } from 'vue-router'
import { Server, RefreshCw, Cpu, Loader, MessageSquare, X } from 'lucide-vue-next'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'

const router = useRouter()
void router

// 指标示例数据
// TODO: GET /api/system/stats
const metrics = [
  { v: '4 / 6', l: 'Worker 在线 / 共' },
  { v: '2', l: '忙碌 · 处理中' },
  { v: '47', l: '今日完成 · 吞吐' },
]

// Worker 列表示例数据
// TODO: GET /api/workers
type WorkerState = 'idle' | 'busy' | 'offline'
interface Worker {
  id: string
  state: WorkerState
  kind: string
  host: string
  spec: string
  done: number
  failed: number
  uptime?: string
  heartbeat: string
  current?: string
}
const workers: Worker[] = [
  { id: 'ai-a1b2', state: 'idle', kind: 'AI', host: 'office-pc', spec: 'Claude Max', done: 142, failed: 3, uptime: '7h12m', heartbeat: '心跳 5s 前' },
  { id: 'ai-c3d4', state: 'busy', kind: 'AI', host: 'office-pc', spec: 'Claude Max', done: 89, failed: 1, uptime: '3h05m', heartbeat: '心跳 2s 前', current: '当前 10_smart' },
  { id: 'gpu-e5f6', state: 'idle', kind: 'GPU', host: 'gpu-server', spec: 'RTX 4090 24GB', done: 88, failed: 1, uptime: '5h40m', heartbeat: '心跳 4s 前' },
  { id: 'cpu-i9j0', state: 'offline', kind: 'CPU', host: 'old-laptop', spec: '4 核 / 8GB', done: 23, failed: 5, heartbeat: '心跳 2h 前' },
]
function dotClass(s: WorkerState) {
  return s === 'idle' ? 'd-ok' : s === 'busy' ? 'd-info pulse' : 'd-mut'
}
function stateBadge(s: WorkerState): { variant: 'ok' | 'info' | 'mut'; label: string } {
  if (s === 'idle') return { variant: 'ok', label: '在线空闲' }
  if (s === 'busy') return { variant: 'info', label: '在线忙碌' }
  return { variant: 'mut', label: '离线' }
}
</script>

<template>
  <section class="page">
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 18px">
      <div class="h1"><Server :size="18" />系统与 Worker</div>
      <BaseButton size="sm" style="margin-left: auto"><RefreshCw :size="13" />刷新</BaseButton>
    </div>

    <div class="grid3" style="margin-bottom: 24px">
      <div v-for="m in metrics" :key="m.l" class="metric">
        <div class="v">{{ m.v }}</div>
        <div class="l">{{ m.l }}</div>
      </div>
    </div>

    <div class="seclabel" style="margin-bottom: 12px"><Cpu :size="14" />Worker · {{ workers.length }}</div>
    <div class="list" style="margin-bottom: 24px">
      <div
        v-for="w in workers"
        :key="w.id"
        class="card pad wcard"
        :class="{ off: w.state === 'offline' }"
      >
        <span class="dot" :class="dotClass(w.state)"></span>
        <div class="wcard-main">
          <div class="wcard-hd">
            <b class="mono wcard-id">{{ w.id }}</b>
            <BaseBadge :variant="stateBadge(w.state).variant">{{ stateBadge(w.state).label }}</BaseBadge>
            <BaseBadge variant="mut">{{ w.kind }}</BaseBadge>
            <BaseBadge v-if="w.current" variant="run">{{ w.current }}</BaseBadge>
          </div>
          <div class="meta">
            <span>{{ w.host }}</span><span class="sep">·</span><span>{{ w.spec }}</span>
            <span class="sep">·</span><span>完成 {{ w.done }}</span>
            <span class="sep">·</span><span>失败 {{ w.failed }}</span>
            <template v-if="w.uptime"><span class="sep">·</span><span>运行 {{ w.uptime }}</span></template>
            <span class="sep">·</span><span>{{ w.heartbeat }}</span>
          </div>
        </div>
        <template v-if="w.state !== 'offline'">
          <!-- TODO: POST /api/workers/{id}/drain -->
          <BaseButton size="sm" @click.stop><Loader :size="13" />排空</BaseButton>
          <BaseButton size="sm" @click.stop><MessageSquare :size="13" />备注</BaseButton>
        </template>
        <!-- TODO: DELETE /api/workers/{id} -->
        <BaseButton v-else variant="danger" size="sm" @click.stop><X :size="13" />移除</BaseButton>
      </div>
    </div>

    <div class="card pad">
      <div class="card-h"><Cpu :size="15" />接入新 Worker</div>
      <div class="note-tip">
        （接入向导占位 —— 选类型 / 标签，生成 JOIN token，给出 docker run / compose 命令。）
      </div>
    </div>
  </section>
</template>
