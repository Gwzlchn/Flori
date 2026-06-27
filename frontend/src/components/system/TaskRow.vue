<script setup lang="ts">
// 统一 task 行：排队中 / 运行中 / 已完成 三态共用一种行样式(用户「任务池与 worker 历史同样的显示方式」)。
// 主显作业标题(无则退 类型名 → 流水线 → job_id 兜底);job_id 退为 tooltip。整行可点 → 跳内容详情。
// 右侧按状态呈现:排队=优先级 + 「投递…·已等…」;运行=运行徽章 + 「开始…·已运行…」;完成=状态 + 「耗时…·结束…」。
import { computed, inject, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChevronRight, Layers, Trash2 } from 'lucide-vue-next'
import StatusBadge from '../common/StatusBadge.vue'
import { useJobStore } from '../../stores/jobs'
import { contentTypeIcon, contentTypeLabel } from '../../utils/contentType'
import { fmtClock, fmtDuration } from '../../utils/datetime'

const props = withDefaults(defineProps<{
  state: 'queued' | 'running' | 'completed'
  jobId: string
  step: string
  title?: string | null
  contentType?: string | null
  pipeline?: string | null
  pool?: string | null
  // 时间(按 state 取其一):enqueuedAt=epoch 秒;startedAt/finishedAt=ISO
  enqueuedAt?: number | null
  startedAt?: string | null
  finishedAt?: string | null
  durationSec?: number | null
  // 其它态字段
  priority?: number | null
  status?: string | null        // 完成态原始 status(done/failed/…)→ StatusBadge
  worker?: string | null        // 运行态:执行 worker(主机名/id)
  now?: number                  // 父级注入的当前时刻(epoch ms),驱动"已等/已运行"刷新
  clickable?: boolean
  deletable?: boolean           // 显示"删除作业"快捷按钮(worker 历史/队列页用,删后 emit deleted 让父刷新)
}>(), {
  clickable: true,
  deletable: true,
})

const emit = defineEmits<{ (e: 'deleted', jobId: string): void }>()

const router = useRouter()
const jobStore = useJobStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast')
const deleting = ref(false)

// 删除对应作业(级联,P1):确认 → deleteJob → emit deleted(父级 reload)。@click.stop 不触发整行跳转。
async function onDelete() {
  if (deleting.value || !props.jobId) return
  if (!confirm('删除该作业?将级联清除其队列任务、产物、用量、DB,不可恢复。')) return
  deleting.value = true
  try {
    await jobStore.deleteJob(props.jobId)
    showToast?.('已删除作业', 'success')
    emit('deleted', props.jobId)
  } catch {
    showToast?.('删除失败', 'error')
  } finally {
    deleting.value = false
  }
}

const icon = computed(() => contentTypeIcon(props.contentType))

// 主显标题:作业标题 → 类型名(有 content_type 才用)→ 流水线 → job_id(最后兜底)。
const mainTitle = computed(() =>
  props.title?.trim()
  || (props.contentType ? contentTypeLabel(props.contentType) : '')
  || props.pipeline
  || props.jobId,
)

const nowMs = computed(() => props.now ?? Date.now())

// 右侧时间文案(§5.1:别用「10h前」,按状态给"点 + 时长")。
const timeText = computed(() => {
  if (props.state === 'queued') {
    if (props.enqueuedAt) {
      const waited = nowMs.value / 1000 - props.enqueuedAt
      return `投递 ${fmtClock(props.enqueuedAt * 1000)} · 已等 ${fmtDuration(waited)}`
    }
    return '等待认领'
  }
  if (props.state === 'running') {
    if (props.startedAt) {
      const waited = (nowMs.value - new Date(props.startedAt).getTime()) / 1000
      return `开始 ${fmtClock(props.startedAt)} · 已运行 ${fmtDuration(waited)}`
    }
    return '运行中'
  }
  // 已完成
  const parts: string[] = []
  if (props.durationSec != null) parts.push(`耗时 ${fmtDuration(props.durationSec)}`)
  if (props.finishedAt) parts.push(`结束 ${fmtClock(props.finishedAt)}`)
  return parts.join(' · ') || '—'
})

function onClick() {
  if (props.clickable && props.jobId) router.push(`/content/${encodeURIComponent(props.jobId)}`)
}
</script>

<template>
  <div class="row task-row" :class="{ clickable }" @click="onClick">
    <span class="type-pill" style="background:var(--mut-bg);color:var(--ink-600)">
      <component :is="icon" :size="17" />
    </span>

    <div class="body">
      <div class="title" :title="jobId" style="font-size:13.5px">{{ mainTitle }}</div>
      <div class="meta">
        <span class="mono">{{ step }}</span>
        <template v-if="pool">
          <span class="sep">·</span>
          <span class="badge b-brand"><Layers :size="11" />{{ pool }}</span>
        </template>
        <template v-if="state === 'running' && worker">
          <span class="sep">·</span>
          <span class="dim mono">{{ worker }}</span>
        </template>
      </div>
    </div>

    <div class="task-right">
      <div class="task-badge">
        <StatusBadge v-if="state === 'completed' && status" :status="status" />
        <span v-else-if="state === 'running'" class="badge b-run">运行中</span>
        <span v-else class="badge b-mut">优先级 {{ priority ?? '—' }}</span>
      </div>
      <div class="task-time dim">{{ timeText }}</div>
    </div>

    <button
      v-if="deletable"
      class="task-del"
      :disabled="deleting"
      title="删除该作业(级联清除队列/产物/用量/DB)"
      @click.stop="onDelete"
    ><Trash2 :size="14" /></button>

    <ChevronRight v-if="clickable" :size="16" class="dim" />
  </div>
</template>

<style scoped>
.task-row.clickable { cursor: pointer; }
.task-right {
  display: flex; flex-direction: column; align-items: flex-end; gap: 3px;
  margin-left: auto; text-align: right; white-space: nowrap;
}
.task-badge { display: flex; align-items: center; gap: 6px; }
.task-time { font-size: 11.5px; }
.task-del {
  display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; color: var(--ink-300);
  cursor: pointer; padding: 4px; border-radius: 6px; flex: none;
}
.task-del:hover:not(:disabled) { color: var(--bad); background: var(--bad-bg, #fde8e8); }
.task-del:disabled { opacity: .4; cursor: default; }
@media (max-width: 560px) {
  .task-time { font-size: 11px; }
}
</style>
