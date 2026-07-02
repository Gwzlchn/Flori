<script setup lang="ts">
import { ref, computed } from 'vue'
import { Trash2, X, AlertTriangle, Archive } from 'lucide-vue-next'
import type { Collection } from '../../types'

// 删除集合弹窗。二选一:仅删集合保留内容 / 连内容一起删(需二次确认)。
const props = defineProps<{ collection: Collection; deleting?: boolean }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'confirm', purge: boolean): void
}>()

const mode = ref<'detach' | 'purge'>('detach')
// 全删需勾选二次确认。
const confirmed = ref(false)
const n = computed(() => props.collection.job_count)
const blocked = computed(() => mode.value === 'purge' && !confirmed.value)

function confirm() {
  if (blocked.value) return
  emit('confirm', mode.value === 'purge')
}
</script>

<template>
  <div class="overlay show" @click.self="emit('close')">
    <div class="modal" style="max-width:440px">
      <div class="hd">
        <Trash2 :size="18" class="lead-ic" style="color:var(--bad)" /><b>删除集合</b>
        <button class="ghost" @click="emit('close')"><X :size="14" /></button>
      </div>
      <div class="bd">
        <p style="font-size:13px;color:var(--ink-700);margin-bottom:14px">
          删除「<b>{{ collection.name }}</b>」（{{ n }} 条内容）。请选择如何处理其中的内容：
        </p>

        <label class="opt-row" :class="{ on: mode === 'detach' }">
          <input type="radio" value="detach" v-model="mode" />
          <Archive :size="16" class="opt-ic" />
          <span class="opt-text">
            <b>保留内容</b>
            <span class="opt-sub">仅删除集合，名下 {{ n }} 条内容及笔记保留（移出该集合）。</span>
          </span>
        </label>

        <label class="opt-row danger" :class="{ on: mode === 'purge' }">
          <input type="radio" value="purge" v-model="mode" />
          <AlertTriangle :size="16" class="opt-ic" />
          <span class="opt-text">
            <b>全部删除</b>
            <span class="opt-sub">连同 {{ n }} 条内容、笔记、产物一并删除，<b>不可恢复</b>。</span>
          </span>
        </label>

        <label v-if="mode === 'purge'" class="confirm-row">
          <input type="checkbox" v-model="confirmed" />
          <span>我确认要永久删除这 {{ n }} 条内容及其全部产物。</span>
        </label>
      </div>
      <div class="ft">
        <button class="btn" @click="emit('close')">取消</button>
        <button
          class="btn"
          :class="mode === 'purge' ? 'danger' : 'pri'"
          :disabled="deleting || blocked"
          @click="confirm"
        >
          <Trash2 :size="14" />{{ deleting ? '删除中…' : (mode === 'purge' ? '永久删除' : '删除集合') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.opt-row {
  display: flex; align-items: flex-start; gap: 10px; padding: 11px 12px; margin-bottom: 9px;
  border: 1px solid var(--line); border-radius: var(--r-md); background: var(--raised);
  cursor: pointer; transition: all .12s;
}
.opt-row:hover { border-color: var(--brand-300); }
.opt-row.on { border-color: var(--brand-500); background: var(--brand-50); }
.opt-row.danger.on { border-color: var(--bad); background: var(--bad-bg); }
.opt-row input { margin-top: 2px; flex: none; }
.opt-ic { flex: none; margin-top: 1px; color: var(--ink-500); }
.opt-row.danger .opt-ic { color: var(--bad); }
.opt-text { display: flex; flex-direction: column; gap: 3px; font-size: 13px; color: var(--ink-800); }
.opt-sub { font-size: 12px; color: var(--ink-500); font-weight: 400; line-height: 1.5; }
.confirm-row {
  display: flex; align-items: center; gap: 8px; margin-top: 4px; padding: 9px 11px;
  background: var(--bad-bg); border: 1px solid var(--bad-bd); border-radius: var(--r-sm);
  font-size: 12.5px; color: var(--bad); cursor: pointer;
}
.confirm-row input { flex: none; }
.btn.danger { background: var(--bad); color: #fff; border-color: var(--bad); }
.btn.danger:hover:not(:disabled) { filter: brightness(.94); }
.btn.danger:disabled { opacity: .5; }
</style>
