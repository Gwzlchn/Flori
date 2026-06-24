<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../../stores/domains'
import { useGlobalStore } from '../../stores/global'
import { useApi } from '../../composables/useApi'
import {
  Send, Inbox, BookMarked, Lightbulb, ChevronRight, ChevronUp, ChevronDown,
  Folder, Server, Settings, PanelLeftClose, Plus, MoreHorizontal,
} from 'lucide-vue-next'
import { resolveIcon } from '../../utils/kbIcons'
import { sourceBadge, sourceLabelOf, subState, subTip } from '../../constants/sources'
import AddSubscriptionDialog from '../collection/AddSubscriptionDialog.vue'
import KbSettingsDialog from './KbSettingsDialog.vue'

defineProps<{ mobileOpen?: boolean }>()
const emit = defineEmits<{ (e: 'toggle-rail'): void; (e: 'nav'): void }>()

const route = useRoute()
const router = useRouter()
const domainStore = useDomainStore()
const global = useGlobalStore()
const api = useApi()

// 导航后通知外壳关闭移动端抽屉。桌面端 AppShell 忽略此事件。
function nav(to: string) {
  router.push(to)
  emit('nav')
}

// 4 级树的展开态与懒加载缓存(本地维护,避免 store 单数组被多知识库覆盖)
const expandedKb = reactive<Record<string, boolean>>({})
const expandedCol = reactive<Record<string, boolean>>({})
const kbCols = reactive<Record<string, any[]>>({})
const colItems = reactive<Record<string, any[]>>({})
// 未归类(无所属集合)内容,按知识库懒加载
const expandedUncat = reactive<Record<string, boolean>>({})
const uncatItems = reactive<Record<string, any[]>>({})

onMounted(() => { if (!domainStore.domains.length) domainStore.fetchAll() })

async function loadCols(d: string) {
  loadUncat(d)   // 同时拉该域未归类内容(独立、不阻塞集合)
  if (kbCols[d]) return
  try {
    const r: any = await api.get(`/api/collections?domain=${encodeURIComponent(d)}`)
    kbCols[d] = r.collections ?? r ?? []
  } catch { kbCols[d] = [] }
}
async function loadUncat(d: string) {
  if (uncatItems[d]) return
  try {
    const r: any = await api.get(`/api/jobs?domain=${encodeURIComponent(d)}&uncategorized=true&limit=50`)
    uncatItems[d] = r.items ?? r ?? []
  } catch { uncatItems[d] = [] }
}
function toggleUncat(d: string) { expandedUncat[d] = !expandedUncat[d] }
async function loadItems(id: string) {
  if (colItems[id]) return
  try {
    const r: any = await api.get(`/api/collections/${id}/jobs?limit=20`)
    colItems[id] = r.items ?? r ?? []
  } catch { colItems[id] = [] }
}
async function toggleKb(d: string) {
  expandedKb[d] = !expandedKb[d]
  if (expandedKb[d]) await loadCols(d)
}
async function toggleCol(id: string) {
  expandedCol[id] = !expandedCol[id]
  if (expandedCol[id]) await loadItems(id)
}

// #3 导航联动:进入某内容/集合/知识库时,侧栏自动展开其所在分支并高亮。
async function expandKb(d: string) { expandedKb[d] = true; await loadCols(d) }
async function expandCol(id: string) { expandedCol[id] = true; await loadItems(id) }
async function syncFromRoute() {
  const n = route.name as string
  const p = route.params as any
  if ((n === 'knowledge-base' || n === 'concept-detail' || n === 'topic') && p.domain) {
    await expandKb(String(p.domain))
  } else if (n === 'collection-detail' && p.id) {
    try { const c: any = await api.get(`/api/collections/${p.id}`); if (c?.domain) await expandKb(c.domain) } catch { /* 忽略 */ }
    await expandCol(String(p.id))
  } else if (n === 'content-detail' && p.id) {
    try {
      const j: any = await api.get(`/api/jobs/${p.id}`)
      if (j?.domain) await expandKb(j.domain)
      if (j?.collection_id) await expandCol(j.collection_id)
    } catch { /* 忽略 */ }
  }
}
watch(() => route.fullPath, syncFromRoute, { immediate: true })

// 知识库色点:按名字哈希出柔和色
function kbColor(d: string) {
  let h = 0
  for (const c of d) h = (h * 31 + c.charCodeAt(0)) % 360
  return `hsl(${h} 52% 62%)`
}

const isKbActive = (d: string) =>
  route.path === `/kb/${d}` || route.path.startsWith(`/kb/${encodeURIComponent(d)}`)
const isContentActive = (jid: string) =>
  route.name === 'content-detail' && String(route.params.id) === String(jid)

// #5dup 知识库拖拽排序:领域是派生视图、后端无顺序,故顺序存浏览器 localStorage。
const ORDER_KEY = 'flori.kbOrder'
const kbOrder = ref<string[]>(loadOrder())
function loadOrder(): string[] {
  try { return JSON.parse(localStorage.getItem(ORDER_KEY) || '[]') } catch { return [] }
}
const orderedDomains = computed(() => {
  const rank = (d: string) => { const i = kbOrder.value.indexOf(d); return i === -1 ? 1e9 : i }
  return [...domainStore.domains].sort((a, b) => rank(a.domain) - rank(b.domain))
})
const dragFrom = ref(-1)
const dragOver = ref(-1)
function onDragStart(i: number) { dragFrom.value = i }
function onDragOver(i: number) { dragOver.value = i }
function onDrop(i: number) {
  const from = dragFrom.value
  dragFrom.value = -1; dragOver.value = -1
  if (from === -1 || from === i) return
  const arr = orderedDomains.value.map(d => d.domain)
  const [moved] = arr.splice(from, 1)
  arr.splice(i, 0, moved)
  kbOrder.value = arr
  localStorage.setItem(ORDER_KEY, JSON.stringify(arr))
}
function onDragEnd() { dragFrom.value = -1; dragOver.value = -1 }
// 上移/下移:触屏没有 HTML5 拖拽,用按钮换序(同样写 localStorage)。
function moveKb(i: number, dir: number) {
  const arr = orderedDomains.value.map(d => d.domain)
  const j = i + dir
  if (j < 0 || j >= arr.length) return
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  kbOrder.value = arr
  localStorage.setItem(ORDER_KEY, JSON.stringify(arr))
}

// #3 知识库图标:有 d.icon 用之;未设按名关键词 smart 默认;颜色用 d.color 否则名字哈希(替代纯色点)。
const ICON_KW: [RegExp, string][] = [
  [/financ|金融|财经|invest|trad|market|stock|经济/i, 'landmark'],
  [/deep.?learn|深度学习|machine.?learn|neural|\bml\b|\bai\b|人工智能|算法/i, 'brain'],
  [/program|coding|编程|代码|software|\bdev\b|开发/i, 'code'],
]
function smartIcon(d: any): string {
  const hay = `${d.domain} ${d.display_name || ''}`
  for (const [re, name] of ICON_KW) if (re.test(hay)) return name
  return 'book-marked'
}
function kbIconComp(d: any) { return resolveIcon(d.icon) || resolveIcon(smartIcon(d)) }
function kbHue(d: any) { return d.color || kbColor(d.domain) }
function kbLabel(d: any) { return d.display_name || d.domain }

// #4 集合来源图标 + 订阅状态点(5 态由共享 subState/subTip 推,数据来自 c.subscription)
function srcIcon(c: any) { return c.subscription ? sourceBadge(sourceLabelOf(c.subscription)).icon : Folder }

// #1/#2 KB 设置弹窗(重命名/图标/配色)+ 新增集合/订阅弹窗
const settingsFor = ref<any | null>(null)
const addFor = ref('')
const addSaving = ref(false)
const addError = ref('')
function openSettings(d: any) { settingsFor.value = d }
async function saveSettings(patch: { display_name: string; icon: string; color: string }) {
  const d = settingsFor.value
  if (!d) return
  try { await domainStore.updateMeta(d.domain, patch) } finally { settingsFor.value = null }
}
// 改英文 domain key(二期):确认 → 事务迁移 → 关弹窗 + 跳到新 key 的工作台。失败(如重名 409)弹错。
async function renameSettings(newDomain: string) {
  const d = settingsFor.value
  if (!d) return
  if (!confirm(`把知识库英文标识从 “${d.domain}” 改为 “${newDomain}”?\n会迁移该领域下所有内容 / 集合 / 术语,URL 也会变。`)) return
  try {
    const nk = await domainStore.renameKey(d.domain, newDomain)
    settingsFor.value = null
    router.push(`/kb/${encodeURIComponent(nk)}`)
  } catch (e: any) {
    alert(e?.message || '改标识失败(可能与现有领域重名)')
  }
}
function openAdd(d: string) { addError.value = ''; addFor.value = d }
async function onCreateCollection(payload: any) {
  addSaving.value = true; addError.value = ''
  try {
    await api.post('/api/collections', payload)
    const dom = payload.domain
    delete kbCols[dom]; await loadCols(dom); expandedKb[dom] = true
    addFor.value = ''
  } catch (e: any) { addError.value = e?.message || '创建失败' }
  finally { addSaving.value = false }
}
</script>

<template>
  <aside class="side" :class="{ open: mobileOpen }">
    <div class="brand">
      <div class="logo" title="Flori" @click="nav('/')">F</div>
      <b>Flori</b>
    </div>

    <div class="top-row">
      <button class="btn-submit" data-tip="投递内容" title="投递内容" @click="global.openSubmit()"><Send :size="16" /><span>投递内容</span></button>
      <button class="top-tool" :class="{ on: route.name === 'content' }" data-tip="所有来源" title="所有来源" @click="nav('/content')">
        <Inbox :size="18" />
      </button>
    </div>

    <nav class="nav">
      <a :class="{ on: route.path === '/' }" data-tip="知识库" title="知识库" @click="nav('/')"><BookMarked :size="16" /><span>知识库</span></a>

      <div class="sub-list">
        <div class="nb-group" v-for="(d, i) in orderedDomains" :key="d.domain">
          <div
            class="sub-item" :class="{ on: isKbActive(d.domain), dragover: dragOver === i }"
            draggable="true" @click="nav(`/kb/${encodeURIComponent(d.domain)}`)"
            @dragstart="onDragStart(i)" @dragover.prevent="onDragOver(i)" @drop="onDrop(i)" @dragend="onDragEnd"
          >
            <span class="kb-caret" :class="{ open: expandedKb[d.domain] }" title="展开/收起内容" @click.stop="toggleKb(d.domain)">
              <ChevronRight :size="14" />
            </span>
            <span class="kb-ic" :style="{ color: kbHue(d) }"><component :is="kbIconComp(d)" :size="15" /></span>
            <span class="nb-name">{{ kbLabel(d) }}</span>
            <span class="kb-actions">
              <button class="kb-mv" title="上移" :disabled="i === 0" @click.stop="moveKb(i, -1)"><ChevronUp :size="13" /></button>
              <button class="kb-mv" title="下移" :disabled="i === orderedDomains.length - 1" @click.stop="moveKb(i, 1)"><ChevronDown :size="13" /></button>
              <button class="kb-mv" title="设置:重命名 / 图标 / 配色" @click.stop="openSettings(d)"><MoreHorizontal :size="14" /></button>
              <button class="kb-mv" title="新增集合 / 订阅来源" @click.stop="openAdd(d.domain)"><Plus :size="14" /></button>
            </span>
          </div>

          <div class="kb-sources" :class="{ open: expandedKb[d.domain] }">
            <div class="src-group" v-for="c in (kbCols[d.domain] || [])" :key="c.id">
              <div class="src-item">
                <span class="src-caret" :class="{ open: expandedCol[c.id] }" @click.stop="toggleCol(c.id)">
                  <ChevronRight :size="14" />
                </span>
                <component :is="srcIcon(c)" :size="14" :title="c.subscription ? sourceLabelOf(c.subscription) : '手动集合'" />
                <span class="nb-name" @click="nav(`/collections/${c.id}`)">{{ c.name }}</span>
                <span v-if="c.subscription" class="sub-dot" :class="subState(c.subscription)" :title="subTip(c.subscription)" />
              </div>
              <div class="src-content" :class="{ open: expandedCol[c.id] }">
                <div class="content-item" v-for="j in (colItems[c.id] || [])" :key="j.job_id"
                     :class="{ on: isContentActive(j.job_id) }"
                     @click="nav(`/content/${j.job_id}`)">
                  <span class="ci-dot" :style="{ background: kbColor(d.domain) }" />
                  <span>{{ j.title || j.job_id }}</span>
                </div>
                <div class="content-item more" v-if="expandedCol[c.id] && !(colItems[c.id] || []).length">空</div>
              </div>
            </div>
            <!-- 未归类:该知识库下无所属集合的内容(YouTube 等手动投递) -->
            <div class="src-group" v-if="(uncatItems[d.domain] || []).length">
              <div class="src-item">
                <span class="src-caret" :class="{ open: expandedUncat[d.domain] }" @click.stop="toggleUncat(d.domain)">
                  <ChevronRight :size="14" />
                </span>
                <Inbox :size="14" />
                <span class="nb-name" @click.stop="toggleUncat(d.domain)">未归类 · {{ (uncatItems[d.domain] || []).length }}</span>
              </div>
              <div class="src-content" :class="{ open: expandedUncat[d.domain] }">
                <div class="content-item" v-for="j in (uncatItems[d.domain] || [])" :key="j.job_id"
                     :class="{ on: isContentActive(j.job_id) }"
                     @click="nav(`/content/${j.job_id}`)">
                  <span class="ci-dot" :style="{ background: kbColor(d.domain) }" />
                  <span>{{ j.title || j.job_id }}</span>
                </div>
              </div>
            </div>

            <div class="src-item" v-if="expandedKb[d.domain] && !(kbCols[d.domain] || []).length && !(uncatItems[d.domain] || []).length"
                 style="color:var(--ink-400);padding-left:24px">暂无集合</div>
          </div>
        </div>

        <a class="sub-item new" @click="nav('/?create=1')">
          <Plus :size="15" /><span>新建知识库</span>
        </a>
      </div>

      <a :class="{ on: route.name === 'glossary' }" data-tip="概念库" title="概念库" @click="nav('/glossary')"><Lightbulb :size="16" /><span>概念库</span></a>
    </nav>

    <div class="side-tools">
      <button class="tool" :class="{ on: route.path.startsWith('/system') }" data-tip="系统" title="系统" @click="nav('/system')"><Server :size="17" /></button>
      <button class="tool" :class="{ on: route.name === 'settings' }" data-tip="设置" title="设置" @click="nav('/settings')"><Settings :size="17" /></button>
      <button class="tool collapse" data-tip="折叠侧栏" title="折叠侧栏" @click="$emit('toggle-rail')"><PanelLeftClose :size="17" /></button>
    </div>

    <KbSettingsDialog
      v-if="settingsFor" :domain="settingsFor.domain"
      :name="settingsFor.display_name || ''" :icon="settingsFor.icon || ''" :color="settingsFor.color || ''"
      @close="settingsFor = null" @save="saveSettings" @rename="renameSettings"
    />
    <AddSubscriptionDialog
      v-if="addFor" :default-domain="addFor" :saving="addSaving" :error="addError"
      @close="addFor = ''" @create="onCreateCollection"
    />
  </aside>
</template>

<style scoped>
.nb-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* #3 KB 图标(替代纯色点):色取 d.color / 名字哈希 */
.kb-ic { display: inline-flex; align-items: center; justify-content: center; width: 18px; flex: none; }
.kb-ic svg { width: 15px; height: 15px; }
/* #3 当前内容在侧栏高亮 */
.content-item.on { color: var(--brand-700); font-weight: 600; }
.content-item.on .ci-dot { box-shadow: 0 0 0 2px var(--brand-100); }
/* #4 订阅状态点(5 态):绿=订阅中 / 灰=已暂停 / 琥珀=尚未同步 / 红=同步出错 / 蓝脉冲=同步中 */
.sub-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; margin-left: auto; }
.sub-dot.active { background: #10b981; }
.sub-dot.paused { background: var(--ink-300); }
.sub-dot.never { background: #f59e0b; }
.sub-dot.error { background: #ef4444; }
.sub-dot.syncing { background: #3b82f6; animation: subpulse 1.1s ease-in-out infinite; }
@keyframes subpulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: .45; transform: scale(.78); } }
/* #2 Notion 式:整行点击=进工作台;箭头 hover 抬起(底色+阴影)提示"点这里才展开" */
.sub-item:hover .kb-caret { background: var(--surface); box-shadow: 0 1px 3px rgba(15, 23, 42, .18); color: var(--ink-700); }
/* #5dup 拖拽落点提示;行可点(进工作台)故 pointer */
.sub-item[draggable="true"] { cursor: pointer; }
.sub-item.dragover { box-shadow: inset 0 2px 0 var(--brand-500); }
/* 行尾操作(上移/下移/设置…/新增＋):默认淡出,hover 行显现;移动端常驻 */
.kb-actions { display: inline-flex; flex: none; gap: 0; opacity: 0; transition: opacity .12s; }
.sub-item:hover .kb-actions { opacity: 1; }
.kb-mv { display: inline-flex; align-items: center; padding: 1px; color: var(--ink-400); border-radius: 3px; }
.kb-mv:hover:not(:disabled) { color: var(--ink-700); background: var(--raised); }
.kb-mv:disabled { opacity: .35; cursor: default; }
@media (max-width: 768px) { .kb-actions { opacity: 1; } }
</style>
