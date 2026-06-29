<script setup lang="ts">
// Prompt 白盒 Phase 2:编辑某 AI 步的 prompt 覆盖(全局 / 按领域)。
// UX(1.1.4 重做):打开即【一个可编辑 textarea,预填当前生效 prompt】——有覆盖填覆盖,否则填默认模板内容;
// 直接在上面改。保存时 trim 后 == 默认 → 删除该 scope 覆盖(视为无覆盖);否则存覆盖。「恢复默认」把
// textarea 重置回默认(保存后即清除覆盖)。变体多模板步(08_punctuate/11_smart)预填用主模板,其余变体
// 在下方只读列出(不混进可编辑框)。覆盖存 DB,下个 job 派发时注入。
// 复用 ProfileEditor 的 modal 范式(.overlay/.modal/.field/.btn 全局类)。
import { ref, onMounted, inject, computed } from 'vue'
import { useApi } from '../../composables/useApi'
import { X, Check, RotateCcw } from 'lucide-vue-next'

const props = defineProps<{ pipeline: string; step: string; label?: string }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved'): void }>()

const api = useApi()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

// 提示文案里的字面占位符;放 script 常量,避免模板内 `{{ '{{..}}' }}` 嵌套大括号被 Vue 解析器报错。
const refBlockHint = '{{ref_block}}'

const scope = ref<'global' | 'domain'>('global')
const domain = ref('')
const content = ref('')
const defaultTemplate = ref<string | null>(null)
const defaultTemplates = ref<{ name: string; content: string }[]>([])
const defaultSystem = ref<string | null>(null)
const loading = ref(true)
const saving = ref(false)

interface PromptDetail {
  default_template: string | null
  default_templates?: { name: string; content: string }[]
  default_system?: string | null
  override: { scope: string; domain: string; content: string; updated_at: string } | null
}

// 主模板内容(预填默认用):后端 default_template 已取「主模板({step}.md)否则首个变体」。
const defaultContent = computed(() => defaultTemplate.value ?? '')

// 主模板的 name(用于把"其余变体"从全变体列表里剔出来只读展示)。
const mainName = computed(() => {
  const tpls = defaultTemplates.value
  if (!tpls.length) return props.step
  const exact = tpls.find((t) => t.name === props.step)
  return exact ? exact.name : tpls[0].name
})
// 其余变体(只读参考,不进可编辑框):如 11_smart.vision、08_punctuate 的另一态。
const otherVariants = computed(() => defaultTemplates.value.filter((t) => t.name !== mainName.value))

// 当前 textarea 是否 == 默认(trim 后):决定保存是"删覆盖"还是"存覆盖"。
const isDefault = computed(() => content.value.trim() === defaultContent.value.trim())

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
    defaultTemplates.value = d.default_templates ?? []
    defaultSystem.value = d.default_system ?? null
    // domain scope 但未填领域时:后端归一会回 global 覆盖,不能据此预填 → 视为无覆盖,预填默认。
    const ov = scope.value === 'domain' && !domain.value.trim() ? null : (d.override?.content ?? null)
    // 预填【当前生效 prompt】:有覆盖填覆盖,否则填默认模板内容。
    content.value = ov ?? defaultContent.value
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
    if (isDefault.value) {
      // 内容 == 默认 → 视为无覆盖:删除该 scope 覆盖(无则后端 no-op)。
      await api.del(`/api/prompts/${props.pipeline}/${props.step}${_query()}`)
      showToast('已是默认(无覆盖)', 'success')
    } else {
      await api.put(`/api/prompts/${props.pipeline}/${props.step}`, {
        scope: scope.value,
        domain: scope.value === 'domain' ? domain.value.trim() : undefined,
        content: content.value,
      })
      showToast('已保存覆盖', 'success')
    }
    emit('saved')
  } catch (e: any) {
    showToast('保存失败:' + (e?.message || e), 'error')
  } finally {
    saving.value = false
  }
}

// 恢复默认:把 textarea 重置回默认模板内容(本地);保存后因 == 默认即清除覆盖。
function restoreDefault() {
  content.value = defaultContent.value
  showToast('已填回默认,保存后将清除覆盖', 'success')
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

        <!-- prompt 编辑(预填当前生效 prompt;直接改) -->
        <div class="field" style="margin-bottom:6px">
          <label style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span>Prompt(直接编辑)</span>
            <span class="state-tag" :class="isDefault ? 's-default' : 's-override'">
              {{ isDefault ? '当前为默认(无覆盖)' : '已修改 · 保存为覆盖' }}
            </span>
            <span style="flex:1"></span>
            <span class="char-count">{{ content.length }} 字</span>
          </label>
          <textarea v-model="content" class="input" rows="16"
            placeholder="该步的 prompt;直接修改即可,内容与默认一致则视为无覆盖" />
          <div class="note-tip">
            预填当前生效内容(有覆盖填覆盖,否则填默认)。评审等步含 <code>{{ refBlockHint }}</code> 等占位符,
            由运行期按本步实参注入,请保留。
          </div>
        </div>

        <!-- 其余变体(只读参考):多模板步(如 11_smart.vision、08_punctuate 另一态)不进可编辑框 -->
        <div v-if="otherVariants.length || defaultSystem" class="field" style="margin-bottom:0">
          <label>其他模板(只读,仅供参考)</label>
          <div v-for="t in otherVariants" :key="t.name" style="margin-bottom:8px">
            <div class="tpl-name">{{ t.name }}</div>
            <pre class="default-tpl">{{ t.content }}</pre>
          </div>
          <template v-if="defaultSystem">
            <div class="tpl-name">system(默认)</div>
            <pre class="default-tpl">{{ defaultSystem }}</pre>
          </template>
        </div>
      </div>

      <div v-if="!loading" class="ft">
        <button class="btn" :disabled="saving || isDefault" @click="restoreDefault">
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
.state-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
}
.s-default {
  color: var(--ink-500, #6b7280);
  background: var(--mut-bg, #f1f5f9);
}
.s-override {
  color: var(--info-700, #1d4ed8);
  background: var(--info-bg, #eff6ff);
}
.char-count {
  font-size: 11px;
  color: var(--ink-500, #9ca3af);
  font-family: ui-monospace, monospace;
}
.tpl-name {
  font-size: 11px;
  font-weight: 600;
  color: var(--ink-500, #6b7280);
  margin: 2px 0 3px;
  font-family: ui-monospace, monospace;
}
.default-tpl {
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
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
