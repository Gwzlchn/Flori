<script setup lang="ts">
// Prompt 白盒 Phase 2:编辑某 AI 步的 system prompt 覆盖(全局 / 按领域)。
// 默认 prompt(外置模板 templates/{step}.md)只读展示供参考;覆盖存 DB,下个 job 派发时注入。
// 复用 ProfileEditor 的 modal 范式(.overlay/.modal/.field/.btn 全局类)。
import { ref, onMounted, inject } from 'vue'
import { useApi } from '../../composables/useApi'
import { X, Check, RotateCcw } from 'lucide-vue-next'

const props = defineProps<{ pipeline: string; step: string; label?: string }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved'): void }>()

const api = useApi()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const scope = ref<'global' | 'domain'>('global')
const domain = ref('')
const content = ref('')
const defaultTemplate = ref<string | null>(null)
const showDefault = ref(false)
const loading = ref(true)
const saving = ref(false)

interface PromptDetail {
  default_template: string | null
  override: { scope: string; domain: string; content: string; updated_at: string } | null
}

function _query(): string {
  if (scope.value === 'domain' && domain.value.trim()) {
    return `?scope=domain&domain=${encodeURIComponent(domain.value.trim())}`
  }
  return '?scope=global'
}

async function load() {
  loading.value = true
  try {
    const d = await api.get<PromptDetail>(`/api/prompts/${props.pipeline}/${props.step}${_query()}`)
    defaultTemplate.value = d.default_template ?? null
    // domain scope 但未填领域时:不套用 global 的内容(后端归一会回 global),显式清空。
    content.value = scope.value === 'domain' && !domain.value.trim() ? '' : (d.override?.content ?? '')
  } catch (e: any) {
    showToast('读取失败:' + (e?.message || e), 'error')
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function save() {
  if (scope.value === 'domain' && !domain.value.trim()) {
    showToast('请先填写领域', 'error')
    return
  }
  saving.value = true
  try {
    await api.put(`/api/prompts/${props.pipeline}/${props.step}`, {
      scope: scope.value,
      domain: scope.value === 'domain' ? domain.value.trim() : undefined,
      content: content.value,
    })
    showToast(content.value.trim() ? '已保存' : '已恢复默认', 'success')
    emit('saved')
  } catch (e: any) {
    showToast('保存失败:' + (e?.message || e), 'error')
  } finally {
    saving.value = false
  }
}

async function restoreDefault() {
  saving.value = true
  try {
    await api.del(`/api/prompts/${props.pipeline}/${props.step}${_query()}`)
    content.value = ''
    showToast('已恢复默认', 'success')
    emit('saved')
  } catch (e: any) {
    showToast('恢复失败:' + (e?.message || e), 'error')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="overlay show" @click.self="emit('close')">
    <div class="modal wide">
      <div class="hd">
        <b>编辑 Prompt · {{ pipeline }} · {{ label || step }}</b>
        <button class="ghost" @click="emit('close')"><X :size="16" /></button>
      </div>

      <div v-if="loading" class="bd" style="color:var(--ink-500);font-size:13px;text-align:center;padding:36px 18px">
        加载中…
      </div>

      <div v-else class="bd">
        <!-- 作用域 -->
        <div class="field">
          <label>作用域</label>
          <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
            <label style="display:flex;gap:6px;align-items:center;cursor:pointer">
              <input type="radio" value="global" v-model="scope" @change="load" /> 全局
            </label>
            <label style="display:flex;gap:6px;align-items:center;cursor:pointer">
              <input type="radio" value="domain" v-model="scope" @change="load" /> 领域
            </label>
            <input v-if="scope === 'domain'" v-model="domain" class="input" style="max-width:200px"
              placeholder="领域标识,如 finance" @change="load" />
          </div>
          <div class="note-tip">覆盖存 DB,下个 job 派发时注入该步;领域覆盖优先于全局。</div>
        </div>

        <!-- 默认 prompt(只读) -->
        <div class="field">
          <label style="display:flex;align-items:center;gap:8px">
            默认 prompt(只读,来自模板)
            <button class="btn sm" type="button" @click="showDefault = !showDefault">
              {{ showDefault ? '收起' : '展开' }}
            </button>
          </label>
          <pre v-if="showDefault" class="default-tpl">{{ defaultTemplate || '(无外置模板;该步 prompt 内联在代码默认)' }}</pre>
        </div>

        <!-- system 覆盖 -->
        <div class="field" style="margin-bottom:0">
          <label>System 覆盖(空 = 用默认)</label>
          <textarea v-model="content" class="input" rows="12"
            placeholder="填写后,该步将用这段作为 system prompt(替代默认)" />
        </div>
      </div>

      <div v-if="!loading" class="ft">
        <button class="btn" :disabled="saving" @click="restoreDefault">
          <RotateCcw :size="15" />恢复默认
        </button>
        <span style="flex:1"></span>
        <button class="btn" @click="emit('close')">取消</button>
        <button class="btn pri" :disabled="saving" @click="save">
          <Check :size="16" />{{ saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.default-tpl {
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow: auto;
  background: var(--mut-bg, #f6f7f9);
  border: 1px solid var(--line-soft, #e5e7eb);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.5;
  margin: 0;
}
</style>
