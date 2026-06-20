<script setup lang="ts">
import { ref, computed, onMounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import StatusBadge from '../components/common/StatusBadge.vue'
import type { GlossaryTerm } from '../types'
import {
  Lightbulb, Plus, Sparkles, CheckCircle2, Check, X, Pencil, Trash2, Bookmark,
} from 'lucide-vue-next'

// 概念库（原型 #glossary）：AI 提取候选概念，采纳后沉淀为可检索知识节点。
const router = useRouter()
const api = useApi()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const loading = ref(true)
const error = ref('')
const terms = ref<GlossaryTerm[]>([])

// 筛选：知识库（domain）+ 状态（suggested/accepted）。
const filterDomain = ref('')
const filterStatus = ref<'' | 'suggested' | 'accepted'>('')

const domainOptions = computed(() => {
  const set = new Set<string>()
  terms.value.forEach((t) => { if (t.domain) set.add(t.domain) })
  return [...set].sort()
})

const suggested = computed(() =>
  filterStatus.value === 'accepted' ? [] : terms.value.filter((t) => t.status === 'suggested'),
)
const accepted = computed(() =>
  filterStatus.value === 'suggested' ? [] : terms.value.filter((t) => t.status === 'accepted'),
)
const isEmpty = computed(() => suggested.value.length === 0 && accepted.value.length === 0)

async function loadTerms() {
  loading.value = true
  error.value = ''
  try {
    const params: string[] = []
    if (filterDomain.value) params.push(`domain=${encodeURIComponent(filterDomain.value)}`)
    if (filterStatus.value) params.push(`status=${filterStatus.value}`)
    const q = params.length ? `?${params.join('&')}` : ''
    terms.value = await api.get<GlossaryTerm[]>(`/api/glossary${q}`)
  } catch (e: any) {
    error.value = e?.message || '加载概念失败'
  } finally {
    loading.value = false
  }
}

function goTerm(t: GlossaryTerm) {
  router.push(`/kb/${encodeURIComponent(t.domain)}/concepts/${encodeURIComponent(t.term)}`)
}

// ── 采纳 / 删除 / 主题切换 ──
async function acceptTerm(t: GlossaryTerm) {
  try {
    await api.post(`/api/glossary/${encodeURIComponent(t.domain)}/${encodeURIComponent(t.term)}/accept`)
    showToast('已采纳', 'success')
    await loadTerms()
  } catch (e: any) {
    showToast(e?.message || '采纳失败', 'error')
  }
}

async function removeTerm(t: GlossaryTerm) {
  try {
    await api.del(`/api/glossary/${encodeURIComponent(t.domain)}/${encodeURIComponent(t.term)}`)
    showToast('已删除', 'success')
    await loadTerms()
  } catch (e: any) {
    showToast(e?.message || '删除失败', 'error')
  }
}

async function toggleTopic(t: GlossaryTerm) {
  try {
    await api.post(`/api/glossary/${encodeURIComponent(t.domain)}/${encodeURIComponent(t.term)}/topic`, {
      is_topic: !t.is_topic,
    })
    showToast(t.is_topic ? '已取消主题' : '已标为主题', 'success')
    await loadTerms()
  } catch (e: any) {
    showToast(e?.message || '操作失败', 'error')
  }
}

// ── 新增概念弹窗 ──
const showAdd = ref(false)
const addDomain = ref('')
const addTerm = ref('')
const addDefinition = ref('')
const addRelated = ref('')
const saving = ref(false)
const addError = ref('')

function openAdd() {
  addDomain.value = filterDomain.value || domainOptions.value[0] || ''
  addTerm.value = ''
  addDefinition.value = ''
  addRelated.value = ''
  addError.value = ''
  showAdd.value = true
}

async function submitAdd() {
  const domain = addDomain.value.trim()
  const term = addTerm.value.trim()
  addError.value = ''
  if (!domain || !term) { addError.value = '知识库与概念名不能为空'; return }
  const related = addRelated.value.split(',').map((s) => s.trim()).filter(Boolean)
  saving.value = true
  try {
    await api.post(`/api/glossary?domain=${encodeURIComponent(domain)}`, {
      term,
      definition: addDefinition.value.trim() || null,
      related,
    })
    showToast('概念已添加', 'success')
    showAdd.value = false
    if (filterDomain.value && filterDomain.value !== domain) filterDomain.value = domain
    await loadTerms()
  } catch (e: any) {
    addError.value = e?.message || '添加失败'
  } finally {
    saving.value = false
  }
}

// ── 编辑定义弹窗 ──
const editing = ref<GlossaryTerm | null>(null)
const editDefinition = ref('')
const editRelated = ref('')
const editError = ref('')

function openEdit(t: GlossaryTerm) {
  editing.value = t
  editDefinition.value = t.definition
  editRelated.value = t.related.join(', ')
  editError.value = ''
}

async function submitEdit() {
  const t = editing.value
  if (!t) return
  editError.value = ''
  const related = editRelated.value.split(',').map((s) => s.trim()).filter(Boolean)
  saving.value = true
  try {
    await api.put(`/api/glossary/${encodeURIComponent(t.domain)}/${encodeURIComponent(t.term)}`, {
      term: t.term,
      definition: editDefinition.value.trim() || null,
      related,
    })
    showToast('已保存', 'success')
    editing.value = null
    await loadTerms()
  } catch (e: any) {
    editError.value = e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(loadTerms)
</script>

<template>
  <section class="page">
    <!-- 头部 -->
    <div style="display:flex;align-items:flex-end;gap:12px;margin-bottom:20px">
      <div>
        <div class="h1"><Lightbulb :size="18" />概念库</div>
        <div class="lead">AI 从内容中提取候选概念，采纳后沉淀为可检索的知识节点与正文概念链接。</div>
      </div>
    </div>

    <!-- 筛选 + 新增 -->
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:22px;flex-wrap:wrap">
      <select v-model="filterDomain" class="input" style="max-width:160px" @change="loadTerms">
        <option value="">全部知识库</option>
        <option v-for="d in domainOptions" :key="d" :value="d">{{ d }}</option>
      </select>
      <select v-model="filterStatus" class="input" style="max-width:140px" @change="loadTerms">
        <option value="">全部状态</option>
        <option value="suggested">候选</option>
        <option value="accepted">已采纳</option>
      </select>
      <button class="btn pri" style="margin-left:auto" @click="openAdd"><Plus :size="14" />新增概念</button>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="card pad" style="text-align:center;color:var(--ink-500)">加载中…</div>

    <!-- 错误态 -->
    <div v-else-if="error" class="card pad" style="text-align:center">
      <p class="muted" style="margin-bottom:12px">{{ error }}</p>
      <button class="btn" @click="loadTerms">重试</button>
    </div>

    <!-- 空态 -->
    <div v-else-if="isEmpty" class="card pad" style="text-align:center;padding:40px 18px">
      <Lightbulb :size="40" :stroke-width="1" style="color:var(--ink-300);margin-bottom:12px" />
      <p class="muted" style="margin-bottom:14px">
        {{ filterDomain || filterStatus ? '当前筛选下暂无概念' : '还没有概念，内容解析后会自动抽取候选概念' }}
      </p>
      <button class="btn pri" @click="openAdd"><Plus :size="14" />新增概念</button>
    </div>

    <template v-else>
      <!-- 待审建议（候选） -->
      <template v-if="suggested.length">
        <div class="seclabel" style="margin-bottom:12px"><Sparkles :size="14" />待审建议 · {{ suggested.length }}</div>
        <div class="card pad" style="margin-bottom:26px;border-color:var(--warn-bd)">
          <div v-for="t in suggested" :key="`${t.domain}/${t.term}`" class="occ" style="cursor:default">
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px">
                <span style="font-weight:600;color:var(--ink-900)">{{ t.term }}</span>
                <StatusBadge :status="t.status" />
              </div>
              <div class="dim" style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px;color:var(--ink-500)">
                {{ t.definition || '暂无定义' }} · 出现于 {{ t.occurrences.length }} 条
              </div>
            </div>
            <button class="btn sm" style="color:var(--ok);border-color:var(--ok-bd)" @click="acceptTerm(t)"><Check :size="13" />采纳</button>
            <button class="iconbtn" title="删除" @click="removeTerm(t)"><X :size="14" /></button>
          </div>
        </div>
      </template>

      <!-- 已采纳 -->
      <template v-if="accepted.length">
        <div class="seclabel" style="margin-bottom:12px"><CheckCircle2 :size="14" />已采纳 · {{ accepted.length }}</div>
        <div class="card pad">
          <div v-for="t in accepted" :key="`${t.domain}/${t.term}`" class="occ" @click="goTerm(t)">
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px">
                <span class="occ-t" style="font-weight:600;color:var(--ink-900)">{{ t.term }}</span>
                <span v-if="t.is_topic" class="badge b-brand"><Bookmark :size="12" />主题概念</span>
              </div>
              <div class="dim" style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px;color:var(--ink-500)">
                {{ t.definition || '暂无定义' }} · 出现 {{ t.occurrences.length }} · 关联 {{ t.related.length }}
              </div>
            </div>
            <button
              class="iconbtn"
              :title="t.is_topic ? '取消主题' : '标为主题'"
              :style="t.is_topic ? 'color:var(--brand-600)' : ''"
              @click.stop="toggleTopic(t)"
            ><Bookmark :size="15" /></button>
            <button class="iconbtn" title="编辑定义" @click.stop="openEdit(t)"><Pencil :size="15" /></button>
            <button class="iconbtn" title="删除" @click.stop="removeTerm(t)"><Trash2 :size="15" /></button>
          </div>
        </div>
      </template>
    </template>

    <!-- 新增概念弹窗 -->
    <div v-if="showAdd" class="overlay show" @click.self="showAdd = false">
      <div class="modal">
        <div class="hd">
          <Lightbulb :size="18" class="lead-ic" /><b>新增概念</b>
          <button class="ghost" @click="showAdd = false"><X :size="14" /></button>
        </div>
        <div class="bd">
          <div class="field">
            <label>知识库</label>
            <input v-model="addDomain" class="input" list="glossary-domains" placeholder="如 机器学习" />
            <datalist id="glossary-domains">
              <option v-for="d in domainOptions" :key="d" :value="d" />
            </datalist>
          </div>
          <div class="field">
            <label>概念名</label>
            <input v-model="addTerm" class="input" placeholder="如 注意力机制" />
          </div>
          <div class="field">
            <label>定义</label>
            <textarea v-model="addDefinition" class="input" placeholder="用一两句话说明该概念的核心含义…"></textarea>
          </div>
          <div class="field" style="margin-bottom:0">
            <label>关联概念</label>
            <input v-model="addRelated" class="input" placeholder="逗号分隔，如 自注意力, 多头注意力, 位置编码" />
            <div class="note-tip">采纳后正文中出现的该概念会自动转为<span class="term-link">蓝色虚线链接</span>。</div>
          </div>
          <p v-if="addError" class="note-tip" style="color:var(--bad)">{{ addError }}</p>
        </div>
        <div class="ft">
          <button class="btn" @click="showAdd = false">取消</button>
          <button class="btn pri" :disabled="saving" @click="submitAdd"><Plus :size="14" />{{ saving ? '添加中…' : '添加' }}</button>
        </div>
      </div>
    </div>

    <!-- 编辑定义弹窗 -->
    <div v-if="editing" class="overlay show" @click.self="editing = null">
      <div class="modal">
        <div class="hd">
          <Pencil :size="18" class="lead-ic" /><b>编辑概念 · {{ editing.term }}</b>
          <button class="ghost" @click="editing = null"><X :size="14" /></button>
        </div>
        <div class="bd">
          <div class="field">
            <label>定义</label>
            <textarea v-model="editDefinition" class="input" placeholder="用一两句话说明该概念的核心含义…"></textarea>
          </div>
          <div class="field" style="margin-bottom:0">
            <label>关联概念</label>
            <input v-model="editRelated" class="input" placeholder="逗号分隔，如 自注意力, 多头注意力" />
          </div>
          <p v-if="editError" class="note-tip" style="color:var(--bad)">{{ editError }}</p>
        </div>
        <div class="ft">
          <button class="btn" @click="editing = null">取消</button>
          <button class="btn pri" :disabled="saving" @click="submitEdit"><Check :size="14" />{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </section>
</template>
