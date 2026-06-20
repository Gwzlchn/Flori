<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../../stores/domains'
import { useGlobalStore } from '../../stores/global'
import { useApi } from '../../composables/useApi'
import {
  Send, Inbox, BookMarked, Lightbulb, ChevronRight,
  Rss, Folder, Server, Settings, PanelLeftClose, Plus,
} from 'lucide-vue-next'

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

onMounted(() => { if (!domainStore.domains.length) domainStore.fetchAll() })

async function loadCols(d: string) {
  if (kbCols[d]) return
  try {
    const r: any = await api.get(`/api/collections?domain=${encodeURIComponent(d)}`)
    kbCols[d] = r.collections ?? r ?? []
  } catch { kbCols[d] = [] }
}
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
const ORDER_KEY = 'mnemo.kbOrder'
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
</script>

<template>
  <aside class="side" :class="{ open: mobileOpen }">
    <div class="brand">
      <div class="logo" title="Mnemo" @click="nav('/')">M</div>
      <b>Mnemo</b>
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
            draggable="true"
            @dragstart="onDragStart(i)" @dragover.prevent="onDragOver(i)" @drop="onDrop(i)" @dragend="onDragEnd"
          >
            <span class="kb-caret" :class="{ open: expandedKb[d.domain] }" @click.stop="toggleKb(d.domain)">
              <ChevronRight :size="14" />
            </span>
            <span class="nb-dot" :style="{ background: kbColor(d.domain) }" />
            <span class="nb-name" @click="nav(`/kb/${encodeURIComponent(d.domain)}`)">{{ d.domain }}</span>
          </div>

          <div class="kb-sources" :class="{ open: expandedKb[d.domain] }">
            <div class="src-group" v-for="c in (kbCols[d.domain] || [])" :key="c.id">
              <div class="src-item">
                <span class="src-caret" :class="{ open: expandedCol[c.id] }" @click.stop="toggleCol(c.id)">
                  <ChevronRight :size="14" />
                </span>
                <component :is="c.subscription?.enabled ? Rss : Folder" :size="14" />
                <span class="nb-name" @click="nav(`/collections/${c.id}`)">{{ c.name }}</span>
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
            <div class="src-item" v-if="expandedKb[d.domain] && !(kbCols[d.domain] || []).length"
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
  </aside>
</template>

<style scoped>
.nb-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* #3 当前内容在侧栏高亮 */
.content-item.on { color: var(--brand-700); font-weight: 600; }
.content-item.on .ci-dot { box-shadow: 0 0 0 2px var(--brand-100); }
/* #5dup 拖拽中的落点提示 + 抓取手势 */
.sub-item[draggable="true"] { cursor: grab; }
.sub-item.dragover { box-shadow: inset 0 2px 0 var(--brand-500); }
</style>
