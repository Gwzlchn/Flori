<script setup lang="ts">
import { ref, computed, onMounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useCollectionStore } from '../stores/collections'
import { fmtRelative } from '../utils/datetime'
import type { Collection } from '../types'
import { sourceLabelOf, sourceBadge, sourceMeta, subState, subTip, SUB_STATE_META } from '../constants/sources'
import AddSubscriptionDialog from '../components/collection/AddSubscriptionDialog.vue'
import DeleteCollectionDialog from '../components/collection/DeleteCollectionDialog.vue'
import {
  Folder, Plus, RefreshCw, FileText, Cpu, Trash2,
} from 'lucide-vue-next'

// 集合列表(原型 #collections):多源订阅自动追更,或手动收藏归集。
const router = useRouter()
const store = useCollectionStore()
const { collections, loading } = storeToRefs(store)
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const error = ref('')
const filterDomain = ref('')

const domainOptions = computed(() => {
  const set = new Set<string>()
  collections.value.forEach((c) => { if (c.domain) set.add(c.domain) })
  return [...set].sort()
})

const visible = computed(() =>
  filterDomain.value
    ? collections.value.filter((c) => c.domain === filterDomain.value)
    : collections.value,
)

// 集合卡片图标:订阅按具体来源,手动用文件夹。
function cardIcon(c: Collection) {
  return c.subscription ? (sourceMeta(c.subscription.source_type)?.icon ?? Folder) : Folder
}
function badgeOf(c: Collection) {
  return sourceBadge(sourceLabelOf(c.subscription))
}

// 相对同步时间走 utils/datetime.fmtRelative(中文单位 + "同步" 后缀)。
const syncAgo = (v: string | null) => fmtRelative(v, { style: 'cn', suffix: '同步', fallback: '从未同步' })

// 集合底栏状态文案/tooltip(5 态与侧栏一致):订阅中显示相对同步时间,其余显示状态文案。
function colFoot(c: Collection): string {
  const sub = c.subscription
  if (!sub) return '手动集合'
  const st = subState(sub)
  return st === 'active' ? syncAgo(sub.last_synced_at) : (SUB_STATE_META[st]?.tip ?? '')
}
function colTip(c: Collection): string {
  return c.subscription ? subTip(c.subscription) : '手动集合'
}

async function load() {
  error.value = ''
  try {
    await store.fetchAll()
  } catch (e: any) {
    error.value = e?.message || '加载集合失败'
  }
}

function open(c: Collection) {
  router.push(`/collections/${c.id}`)
}

// 新建订阅
const showCreate = ref(false)
const saving = ref(false)
const createError = ref('')

function openCreate() {
  createError.value = ''
  showCreate.value = true
}
async function onCreate(payload: any) {
  createError.value = ''
  saving.value = true
  try {
    await store.create(payload)
    showToast('集合已创建', 'success')
    showCreate.value = false
  } catch (e: any) {
    createError.value = e?.message || '创建失败'
  } finally {
    saving.value = false
  }
}

// 删除
const delTarget = ref<Collection | null>(null)
const deleting = ref(false)
async function onDelete(purge: boolean) {
  if (!delTarget.value) return
  deleting.value = true
  try {
    await store.remove(delTarget.value.id, purge)
    showToast(purge ? '集合及内容已删除' : '集合已删除（内容保留）', 'success')
    delTarget.value = null
  } catch (e: any) {
    showToast(e?.message || '删除失败', 'error')
  } finally {
    deleting.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="page">
    <!-- 头部 -->
    <div style="display:flex;align-items:flex-end;gap:12px;margin-bottom:20px">
      <div>
        <div class="h1"><Folder :size="18" />集合</div>
        <div class="lead">订阅 B站/YouTube/RSS/本地目录 自动追更，或手动收藏归集——投递时自动继承集合的知识库与标签。</div>
      </div>
      <button class="btn sm" style="margin-left:auto" :disabled="loading" @click="load">
        <RefreshCw :size="13" :class="{ spin: loading }" />刷新
      </button>
      <button class="btn pri" @click="openCreate"><Plus :size="14" />新建</button>
    </div>

    <!-- 知识库筛选 -->
    <div v-if="domainOptions.length" style="display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap">
      <select v-model="filterDomain" class="input" style="max-width:180px">
        <option value="">全部知识库</option>
        <option v-for="d in domainOptions" :key="d" :value="d">{{ d }}</option>
      </select>
    </div>

    <!-- 加载/错误/空 态 -->
    <div v-if="loading && collections.length === 0" class="card pad" style="text-align:center;color:var(--ink-500)">加载中…</div>
    <div v-else-if="error && collections.length === 0" class="card pad" style="text-align:center">
      <p class="muted" style="margin-bottom:12px">{{ error }}</p>
      <button class="btn" @click="load"><RefreshCw :size="14" />重试</button>
    </div>
    <div v-else-if="visible.length === 0" class="card pad" style="text-align:center;padding:40px 18px">
      <Folder :size="40" :stroke-width="1" style="color:var(--ink-300);margin-bottom:12px" />
      <p class="muted" style="margin-bottom:14px">
        {{ filterDomain ? '该知识库下暂无集合' : '还没有集合，订阅一个来源或新建手动集合' }}
      </p>
      <button class="btn pri" @click="openCreate"><Plus :size="14" />新建集合</button>
    </div>

    <!-- 集合卡片网格 -->
    <div v-else class="grid3">
      <div v-for="c in visible" :key="c.id" class="card pad col-card" @click="open(c)">
        <div class="chead">
          <span class="cic" :class="c.subscription ? 'sub' : 'man'">
            <component :is="cardIcon(c)" :size="17" />
          </span>
          <div class="cname-wrap"><div class="cname">{{ c.name }}</div></div>
          <!-- 来源徽标(派生 source_label) / 手动 -->
          <span v-if="c.subscription" class="badge" :class="badgeOf(c).cls" style="flex:none">
            <component :is="badgeOf(c).icon" :size="12" />{{ badgeOf(c).text }}
          </span>
          <span v-else class="badge b-mut" style="flex:none">手动</span>
          <button class="card-del" title="删除集合" @click.stop="delTarget = c"><Trash2 :size="14" /></button>
        </div>

        <div class="stats">
          <span><FileText :size="13" style="color:var(--ink-400)" />{{ c.job_count }} 条</span>
          <span v-if="c.domain"><Cpu :size="13" style="color:var(--ink-400)" />{{ c.domain }}</span>
        </div>

        <div v-if="c.tags.length" class="taglist" style="margin-bottom:10px">
          <span v-for="t in c.tags" :key="t" class="tag">{{ t }}</span>
        </div>

        <p v-if="c.description" class="cdesc">{{ c.description }}</p>

        <div class="cfoot">
          <span class="cfoot-tag" :title="colTip(c)">
            <span v-if="c.subscription" class="sub-dot" :class="subState(c.subscription)"></span>
            <span v-else class="dot d-mut"></span>
            {{ colFoot(c) }}
          </span>
        </div>
      </div>
    </div>

    <!-- 弹窗 -->
    <AddSubscriptionDialog
      v-if="showCreate"
      :saving="saving" :error="createError" :default-domain="filterDomain"
      @close="showCreate = false" @create="onCreate"
    />
    <DeleteCollectionDialog
      v-if="delTarget"
      :collection="delTarget" :deleting="deleting"
      @close="delTarget = null" @confirm="onDelete"
    />
  </section>
</template>

<style scoped>
.spin { animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
/* 卡片右上角删除按钮:默认淡出,hover 卡片时显现 */
.card-del { flex: none; opacity: 0; color: var(--ink-400); padding: 3px; border-radius: 5px; transition: all .12s; }
.col-card:hover .card-del { opacity: 1; }
.card-del:hover { color: var(--bad); background: var(--bad-bg); }
/* 订阅状态点(5 态,与侧栏 .sub-dot 配色一致) */
.sub-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex: none; }
.sub-dot.active { background: #10b981; }
.sub-dot.paused { background: var(--ink-300); }
.sub-dot.never { background: #f59e0b; }
.sub-dot.error { background: #ef4444; }
.sub-dot.syncing { background: #3b82f6; animation: subpulse 1.1s ease-in-out infinite; }
@keyframes subpulse { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
</style>
