<script setup lang="ts">
// Prompt 白盒编辑 + 版本管理(类 Grafana save):编辑某 AI 步的 prompt 覆盖(全局 / 按领域)。
// - 顶部「版本」下拉 = `默认(无覆盖)` + 各历史版本 v{n},标出当前激活。选默认载入默认模板内容;
//   选某历史版本调 GET versions/{n} 把该版本全文载入 textarea,可基于它改。
// - 保存有两个动作:「覆盖当前版本」mode=overwrite,改激活版本内容,版本号不变;
//   「另存为新版本」mode=new,version=max+1 并激活,可填一行 note。首次保存恒为 v1。
// - 「设为当前激活」把下拉所选设为激活指针(POST .../activate)。选默认发 version:null,
//   停用回内置默认,历史保留;选 vN 发 version:vN,把该历史版本设为派发所用。激活后原地刷新。
// - 「回到内置默认」= activate{version:null},停用覆盖且保留全部历史版本,非破坏。
//   彻底删除入口不在此面板,如需可走 DELETE。
// 变体多模板步(08_punctuate/11_smart)预填用主模板,其余变体在下方只读列出。覆盖存 DB,下个 job 派发时注入。
// 复用 ProfileEditor 的 modal 范式(.overlay/.modal/.field/.btn 全局类)。
import { ref, onMounted, inject, computed } from 'vue'
import { useApi } from '../../composables/useApi'
import type { PromptDetailWire } from '../../types/wire'
import { X, Check, RotateCcw, GitBranch, Power } from 'lucide-vue-next'

const props = defineProps<{
  pipeline: string
  step: string
  label?: string
  documentKind?: string | null
}>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved'): void; (e: 'changed'): void }>()

const api = useApi()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

// 提示文案里的字面占位符;放 script 常量,避免模板内 `{{ '{{..}}' }}` 嵌套大括号被 Vue 解析器报错。
const refBlockHint = '{{ref_block}}'

const scope = ref<'global' | 'domain'>('global')
const domain = ref('')
const content = ref('')
const note = ref('')                                  // 「另存为新版本」的一行备注
const defaultTemplate = ref<string | null>(null)
const defaultTemplates = ref<{ name: string; content: string }[]>([])
const defaultSystem = ref<string | null>(null)
// 版本管理:激活版本号(无覆盖 null)+ 全部历史版本元信息 + 当前下拉选中项('default' 或版本号)。
const activeVersion = ref<string | null>(null)
const versions = ref<{ version: string; note: string | null; created_at: string }[]>([])
const selectedVersion = ref<'default' | string>('default')
// 协议 prompt(如语义核验):模板只读展示,作用域/版本/保存控件全部隐藏,覆盖被后端 403。
const locked = ref(false)
const loading = ref(true)
const saving = ref(false)

// 主模板内容(预填默认用):后端 default_template 取主模板 {step}.md,没有则取首个变体。
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

// 有无激活覆盖(决定「回到内置默认」是否可点 + 「覆盖当前版本」按钮文案带版本号)。
const hasOverride = computed(() => activeVersion.value != null)

// 下拉所选是否已是当前激活态,决定「设为当前激活」是否禁用:
// 选默认时,当且仅当当前无激活(activeVersion==null);选 vN 时,当且仅当 vN 已是激活版本。
const selectedIsActive = computed(() =>
  selectedVersion.value === 'default'
    ? activeVersion.value == null
    : selectedVersion.value === activeVersion.value,
)

function _query(): string {
  const params = new URLSearchParams()
  if (scope.value === 'domain' && domain.value.trim()) {
    params.set('scope', 'domain')
    params.set('domain', domain.value.trim())
  } else {
    params.set('scope', 'global')
  }
  if (props.documentKind) params.set('document_kind', props.documentKind)
  return `?${params.toString()}`
}

async function load() {
  loading.value = true
  try {
    const d = await api.get<PromptDetailWire>(`/api/prompts/${props.pipeline}/${props.step}${_query()}`)
    locked.value = !!d.locked
    defaultTemplate.value = d.default_template ?? null
    defaultTemplates.value = d.default_templates ?? []
    defaultSystem.value = d.default_system ?? null
    versions.value = d.versions ?? []
    // domain scope 但未填领域时:后端归一会回 global 覆盖,不能据此预填 → 视为无覆盖,预填默认。
    const noDomain = scope.value === 'domain' && !domain.value.trim()
    activeVersion.value = noDomain ? null : (d.active_version ?? null)
    const ov = noDomain ? null : (d.override?.content ?? null)
    // 预填当前生效 prompt:有覆盖填覆盖(激活版本),否则填默认模板内容;下拉相应选激活版本 / 默认。
    content.value = ov ?? defaultContent.value
    selectedVersion.value = activeVersion.value ?? 'default'
    note.value = ''
  } catch (e: any) {
    showToast('读取失败:' + (e?.message || e), 'error')
  } finally {
    loading.value = false
  }
}
onMounted(load)

// 切换版本下拉:默认 → 载入默认模板内容;历史版本 → 拉该版本全文载入 textarea(可基于它改)。
async function onSelectVersion() {
  if (selectedVersion.value === 'default') {
    content.value = defaultContent.value
    return
  }
  try {
    const v = await api.get<{ content: string }>(
      `/api/prompts/${props.pipeline}/${props.step}/versions/${selectedVersion.value}${_query()}`,
    )
    content.value = v.content ?? ''
  } catch (e: any) {
    showToast('读取版本失败:' + (e?.message || e), 'error')
  }
}

// 两种保存:overwrite=覆盖当前激活版本;new=另存为新版本(带 note)。统一走 PUT(空内容由后端当删除)。
async function save(mode: 'overwrite' | 'new') {
  if (scope.value === 'domain' && !domain.value.trim()) {
    showToast('请先填写领域', 'error')
    return
  }
  saving.value = true
  try {
    await api.put(`/api/prompts/${props.pipeline}/${props.step}`, {
      scope: scope.value,
      domain: scope.value === 'domain' ? domain.value.trim() : undefined,
      ...(props.documentKind ? { document_kind: props.documentKind } : {}),
      content: content.value,
      mode,
      note: mode === 'new' ? (note.value.trim() || undefined) : undefined,
    })
    showToast(mode === 'new' ? '已另存为新版本' : '已覆盖当前版本', 'success')
    emit('saved')
  } catch (e: any) {
    showToast('保存失败:' + (e?.message || e), 'error')
  } finally {
    saving.value = false
  }
}

// activate 指针操作的公共体:version=null 停用回内置默认、version=vN 设激活。成功后原地刷新
// (active 标记移到所选)+ 通知父组件刷新圆点角标(不关闭面板)。
async function _activate(version: string | null, okMsg: string) {
  if (scope.value === 'domain' && !domain.value.trim()) {
    showToast('请先填写领域', 'error')
    return
  }
  saving.value = true
  try {
    await api.post(`/api/prompts/${props.pipeline}/${props.step}/activate`, {
      scope: scope.value,
      domain: scope.value === 'domain' ? domain.value.trim() : undefined,
      ...(props.documentKind ? { document_kind: props.documentKind } : {}),
      version,
    })
    showToast(okMsg, 'success')
    await load() // 原地刷新:active 标记移到新激活态
    emit('changed') // 父组件刷新圆点角标(不关闭面板)
  } catch (e: any) {
    showToast('操作失败:' + (e?.message || e), 'error')
  } finally {
    saving.value = false
  }
}

// 设为当前激活:把下拉所选设为激活指针。选默认→停用回内置默认;选 vN→该历史版本设为激活。
async function setActive() {
  if (selectedIsActive.value) return
  if (selectedVersion.value === 'default') {
    await _activate(null, '已回到内置默认(保留历史版本)')
  } else {
    await _activate(selectedVersion.value, `已激活 v${selectedVersion.value}`)
  }
}

// 回到内置默认 = 停用覆盖(activate version:null),保留全部历史版本,非破坏。无激活则禁用。
async function restoreDefault() {
  if (!hasOverride.value) return
  await _activate(null, '已回到内置默认(保留历史版本)')
}
</script>

<template>
  <div class="overlay show" @click.self="emit('close')">
    <div class="modal wide">
      <div class="hd">
        <b>
          {{ locked ? '查看' : '编辑' }} Prompt · {{ pipeline }} · {{ documentKind || 'common' }} · {{ label || step }}
        </b>
        <button class="ghost" @click="emit('close')"><X :size="16" /></button>
      </div>

      <div v-if="loading" class="bd" style="color:var(--ink-500);font-size:13px;text-align:center;padding:36px 18px">
        加载中…
      </div>

      <div v-else class="bd">
        <!-- 协议 prompt:只读展示,无作用域/版本/保存控件 -->
        <template v-if="locked">
          <div class="field" style="margin-bottom:0">
            <label style="display:flex;align-items:center;gap:8px">
              <span>Prompt(协议,只读)</span>
              <span class="state-tag s-locked" data-test="locked-tag">锁定</span>
            </label>
            <pre class="default-tpl" style="max-height:340px" data-test="locked-content">{{ defaultContent }}</pre>
            <div class="note-tip">
              该步为锁定的协议 prompt:响应结构与服务端校验逻辑成对,不支持前端修改或领域覆盖。
              如需调整,修改仓库 configs/prompts/templates/ 下的对应模板并随代码评审发布。
            </div>
          </div>
        </template>

        <!-- 作用域 -->
        <div v-if="!locked" class="field">
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

        <!-- 版本下拉:默认 + 各历史版本(标当前激活) -->
        <div v-if="!locked" class="field" style="margin-bottom:10px">
          <label style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span>版本</span>
            <span v-if="hasOverride" class="state-tag s-override">当前激活 v{{ activeVersion }}</span>
            <span v-else class="state-tag s-default">当前为默认(无覆盖)</span>
          </label>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <select class="input" v-model="selectedVersion" @change="onSelectVersion" data-test="version-select"
              style="max-width:360px;flex:1">
              <option value="default">默认(无覆盖)</option>
              <option v-for="v in versions" :key="v.version" :value="v.version">
                v{{ v.version }}{{ v.version === activeVersion ? ' · 当前激活' : '' }}{{ v.note ? ' — ' + v.note : '' }}
              </option>
            </select>
            <button class="btn" :disabled="saving || selectedIsActive" @click="setActive" data-test="set-active"
              :title="selectedVersion === 'default' ? '停用覆盖,派发回内置默认(保留历史)' : '把该历史版本设为当前激活,下个 job 派发用它'">
              <Power :size="15" />设为当前激活
            </button>
          </div>
          <div class="note-tip">
            选历史版本可查看其内容并基于它改;「设为当前激活」把所选(默认 = 停用回内置默认,保留历史)设为派发所用。
          </div>
        </div>

        <!-- prompt 编辑(预填当前生效 prompt;直接改) -->
        <div v-if="!locked" class="field" style="margin-bottom:6px">
          <label style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span>Prompt(直接编辑)</span>
            <span style="flex:1"></span>
            <span class="char-count">{{ content.length }} 字</span>
          </label>
          <textarea v-model="content" class="input" rows="15"
            placeholder="该步的 prompt;直接修改即可。「覆盖当前版本」改激活版本,「另存为新版本」加一版" />
          <div class="note-tip">
            评审等步含 <code>{{ refBlockHint }}</code> 等占位符,由运行期按本步实参注入,请保留。
          </div>
        </div>

        <!-- 新版本备注(另存为新版本时记录) -->
        <div v-if="!locked" class="field" style="margin-bottom:6px">
          <label>版本备注(另存为新版本时记录,可空)</label>
          <input v-model="note" class="input" placeholder="如:加了配图要求 / 收紧字数" data-test="version-note" />
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
        <button v-if="!locked" class="btn" :disabled="saving || !hasOverride" @click="restoreDefault"
          data-test="restore-default" title="停用覆盖,派发回内置默认;保留全部历史版本(不删除)">
          <RotateCcw :size="15" />回到内置默认
        </button>
        <span style="flex:1"></span>
        <button class="btn" @click="emit('close')">{{ locked ? '关闭' : '取消' }}</button>
        <template v-if="!locked">
          <button class="btn" :disabled="saving" @click="save('overwrite')">
            <Check :size="16" />{{ hasOverride ? `覆盖当前版本 v${activeVersion}` : '保存为覆盖' }}
          </button>
          <button class="btn pri" :disabled="saving" @click="save('new')">
            <GitBranch :size="15" />另存为新版本
          </button>
        </template>
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
.s-locked {
  color: var(--warn-700, #b45309);
  background: var(--warn-bg, #fffbeb);
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
