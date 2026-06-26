<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../stores/domains'
import StatusBadge from '../components/common/StatusBadge.vue'
import ProfileEditor from '../components/settings/ProfileEditor.vue'
import ConceptTimeline from '../components/ConceptTimeline.vue'
import ConceptGraph from '../components/concept/ConceptGraph.vue'
import { fmtDateTime } from '../utils/datetime'
import type { JobSummary } from '../types'
import { resolveIcon } from '../utils/kbIcons'
import { contentTypeIcon, contentTypePill } from '../utils/contentType'
import {
  SlidersHorizontal, RefreshCw, Folder, Lightbulb, BarChart3, Share2,
  Plus, Settings2, Rss, Inbox, ChevronRight, Sparkles, Bookmark,
  Star, Radar,
  Cpu, Atom, Dna, Code, Database, Globe, FlaskConical, BookOpen,
  Brain, Calculator, Scale, Languages, Music, Palette, Leaf, Rocket,
} from 'lucide-vue-next'

// 知识库工作台：数据一次性来自 useDomainStore.workspace(domain)。三 tab：内容 / 概念 / 时间线。
// 取自 route.params.domain；切换知识库时重新拉取。

interface DomainStats {
  collection_count: number
  job_count: number
  concept_count: number
  subscription_count: number
  last_active_at: string | null
  // 展示元数据(来自 profile,未设则缺省;前端回退按 domain 名派生)
  display_name?: string
  icon?: string
  color?: string
}
interface WsCollection {
  id: string
  name: string
  job_count: number
  is_subscription: boolean
  source_id: string | null
  sync_enabled: boolean
  recent?: JobSummary[]  // issue 6:后端按集合返回的最近 N 条(替代「全域最近」分组)
}
interface WsConcept {
  term: string
  definition: string
  source_count: number
  status: string
  is_topic: boolean
}
interface WsTopic {
  topic: string
  count: number
}
interface Workspace {
  domain: string
  stats: DomainStats
  collections: WsCollection[]
  recent_jobs: JobSummary[]
  top_concepts: WsConcept[]
  topics: WsTopic[]
  suggested_count: number
}

const route = useRoute()
const router = useRouter()
const store = useDomainStore()

const domain = computed(() => String(route.params.domain))
const data = ref<Workspace | null>(null)
const loading = ref(false)
const error = ref('')
const tab = ref<'content' | 'concept' | 'timeline' | 'graph'>('content')
const showProfile = ref(false)

// ── 身份图标 + 色块：优先用工作台 stats 的 icon/color/display_name，缺失才回退按名哈希（与总览页一致） ──
const ICONS = [Cpu, Atom, Dna, Code, Database, Globe, FlaskConical, BookOpen,
  Brain, Calculator, Scale, Languages, Music, Palette, Leaf, Rocket]
const GRADIENTS = [
  'linear-gradient(135deg,#6366f1,#4338ca)',
  'linear-gradient(135deg,#0ea5e9,#0369a1)',
  'linear-gradient(135deg,#10b981,#047857)',
  'linear-gradient(135deg,#f59e0b,#b45309)',
  'linear-gradient(135deg,#ec4899,#9d174d)',
  'linear-gradient(135deg,#64748b,#334155)',
]
function hash(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0
  return Math.abs(h)
}
// 图标名→组件解析改用 utils/kbIcons 的 resolveIcon（与总览页一致）。
const headIcon = computed(() => resolveIcon(data.value?.stats?.icon) || ICONS[hash(domain.value) % ICONS.length])
const headGradient = computed(() => data.value?.stats?.color || GRADIENTS[hash(domain.value) % GRADIENTS.length])
const headName = computed(() => data.value?.stats?.display_name || domain.value)

const stats = computed<DomainStats | null>(() => data.value?.stats ?? null)

// 概念按佐证强度（source_count）降序。
const sortedConcepts = computed<WsConcept[]>(() =>
  [...(data.value?.top_concepts ?? [])].sort((a, b) => b.source_count - a.source_count),
)
function strength(sourceCount: number): number {
  return Math.max(1, Math.min(5, sourceCount))
}

// 内容类型 → 图标/配色:统一走 utils/contentType(contentTypeIcon / contentTypePill)。

// issue 6:每集合最近内容改用后端按集合返回的 c.recent;此处只算「未归集合」=
// recent_jobs 中不属任何已知集合的内容(手动投递等)。
const grouped = computed(() => {
  const cols = data.value?.collections ?? []
  const jobs = data.value?.recent_jobs ?? []
  const loose = jobs.filter((j) => !(j.collection_id && cols.some((c) => c.id === j.collection_id)))
  return { loose }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = (await store.workspace(domain.value)) as Workspace
  } catch (e: any) {
    error.value = String(e?.message ?? '') || '加载失败'
    data.value = null
  } finally {
    loading.value = false
  }
}

// 支持深链 ?tab=graph(如概念库「查看图谱」入口);仅接受已知 tab 值,否则默认内容。
const _TABS = ['content', 'concept', 'timeline', 'graph'] as const
function initTabFromQuery() {
  const q = String(route.query.tab ?? '')
  if ((_TABS as readonly string[]).includes(q)) tab.value = q as typeof tab.value
}

onMounted(() => { initTabFromQuery(); load() })
watch(domain, load)
watch(() => route.query.tab, initTabFromQuery)

function goJob(id: string) {
  router.push(`/content/${id}`)
}
function goCollection(id: string) {
  router.push(`/collections/${id}`)
}
function goTopic(topic: string) {
  router.push(`/kb/${encodeURIComponent(domain.value)}/topics/${encodeURIComponent(topic)}`)
}
function goTerm(term: string) {
  router.push(`/kb/${encodeURIComponent(domain.value)}/concepts/${encodeURIComponent(term)}`)
}

function onProfileSaved() {
  // 设定可能影响知识库聚合（角色/术语）→ 重新拉取。
  load()
}
</script>

<template>
  <section class="page">
    <!-- 头部：身份图标 + 名字 + 统计行 + 知识库设定 / 刷新 -->
    <div style="display:flex;align-items:center;gap:13px;margin-bottom:6px">
      <span class="dcard ic" :style="{ width: '42px', height: '42px', background: headGradient, padding: 0 }">
        <component :is="headIcon" :size="20" />
      </span>
      <div style="min-width:0">
        <div class="h1">{{ headName }}</div>
        <div v-if="stats" class="lead">
          {{ stats.collection_count }} 集合 · {{ stats.job_count }} 内容 · {{ stats.concept_count }} 概念
          <template v-if="stats.last_active_at"> · {{ fmtDateTime(stats.last_active_at) }} 活跃</template>
        </div>
      </div>
      <button class="btn sm" style="margin-left:auto"
        @click="router.push(`/kb/${encodeURIComponent(domain)}/radar`)">
        <Radar :size="13" />雷达/周报
      </button>
      <button class="btn sm" @click="showProfile = true">
        <SlidersHorizontal :size="13" />知识库设定
      </button>
      <button class="btn sm" @click="load">
        <RefreshCw :size="13" :class="{ spin: loading }" />刷新
      </button>
    </div>

    <!-- 加载态 -->
    <div v-if="loading && !data" class="card pad" style="color:var(--ink-500);font-size:13px;margin-top:18px">
      加载中…
    </div>

    <!-- 错误态 -->
    <div v-else-if="error && !data" class="card pad"
      style="display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:36px 18px;margin-top:18px">
      <div style="font-size:13.5px;color:var(--ink-700)">{{ error }}</div>
      <button class="btn" @click="load">重试</button>
    </div>

    <!-- 内容主体 -->
    <template v-else-if="data">
      <!-- tab 头 -->
      <div class="tabs" style="margin-top:18px">
        <button :class="{ on: tab === 'content' }" @click="tab = 'content'">
          <Folder :size="15" />内容
        </button>
        <button :class="{ on: tab === 'concept' }" @click="tab = 'concept'">
          <Lightbulb :size="15" />概念
        </button>
        <button :class="{ on: tab === 'timeline' }" @click="tab = 'timeline'">
          <BarChart3 :size="15" />时间线
        </button>
        <button :class="{ on: tab === 'graph' }" @click="tab = 'graph'">
          <Share2 :size="15" />图谱
        </button>
      </div>

      <!-- TAB 内容 -->
      <div v-show="tab === 'content'">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center">
          <button class="chip" @click="router.push('/collections')">
            <Plus :size="13" />新建集合 / 订阅
          </button>
          <button class="ghost" style="font-size:12px" @click="router.push('/collections')">
            <Settings2 :size="14" />管理集合
          </button>
          <template v-if="data.topics.length">
            <span class="divider-v" />
            <span class="dim" style="font-size:12px">主题</span>
            <span v-for="t in data.topics" :key="t.topic" class="chip" @click="goTopic(t.topic)">
              #{{ t.topic }}<span class="n">{{ t.count }}</span>
            </span>
          </template>
        </div>

        <!-- 空：无集合且无内容 -->
        <div v-if="data.collections.length === 0 && data.recent_jobs.length === 0" class="card pad"
          style="display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:40px 18px">
          <Inbox :size="40" :stroke-width="1" style="color:var(--ink-300)" />
          <div style="font-size:13.5px;color:var(--ink-700)">这个知识库还没有内容</div>
          <button class="btn" @click="router.push('/collections')"><Plus :size="14" />新建集合 / 订阅</button>
        </div>

        <template v-else>
          <!-- 各集合分组 -->
          <div v-for="c in data.collections" :key="c.id" class="col-group">
            <div class="col-gh" @click="goCollection(c.id)">
              <Rss v-if="c.is_subscription" :size="15" style="color:var(--brand-500)" />
              <Folder v-else :size="15" />
              <b>{{ c.name }}</b>
              <span v-if="c.is_subscription" class="badge b-info">订阅</span>
              <span v-else class="badge b-mut">手动</span>
              <span class="dim" style="font-size:12px">{{ c.job_count }} 条</span>
              <ChevronRight :size="14" class="dim" style="margin-left:auto" />
            </div>
            <div class="list">
              <template v-if="c.recent?.length">
                <div v-for="j in c.recent" :key="j.job_id" class="row" @click="goJob(j.job_id)">
                  <span class="type-pill" :class="contentTypePill(j.content_type)">
                    <component :is="contentTypeIcon(j.content_type)" :size="16" />
                  </span>
                  <div class="body">
                    <div class="title">{{ j.title || '未命名内容' }}</div>
                    <div class="meta">
                      <StatusBadge :status="j.status" />
                      <span v-if="j.source">{{ j.source }}</span>
                    </div>
                  </div>
                  <ChevronRight :size="16" class="dim" />
                </div>
              </template>
              <div v-else class="dim" style="font-size:12px;padding:6px 2px">该集合暂无最近内容</div>
            </div>
          </div>

          <!-- 未归集合 -->
          <div v-if="grouped.loose.length" class="col-group">
            <div class="col-gh" style="cursor:default">
              <Inbox :size="15" /><b>未归集合</b>
              <span class="dim" style="font-size:12px">{{ grouped.loose.length }} 条</span>
            </div>
            <div class="list">
              <div v-for="j in grouped.loose" :key="j.job_id" class="row" @click="goJob(j.job_id)">
                <span class="type-pill" :class="contentTypePill(j.content_type)">
                  <component :is="contentTypeIcon(j.content_type)" :size="16" />
                </span>
                <div class="body">
                  <div class="title">{{ j.title || '未命名内容' }}</div>
                  <div class="meta">
                    <StatusBadge :status="j.status" />
                    <span v-if="j.source">{{ j.source }}</span>
                  </div>
                </div>
                <ChevronRight :size="16" class="dim" />
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- TAB 概念 -->
      <div v-show="tab === 'concept'">
        <div v-if="data.suggested_count > 0" class="callout warn" style="margin-bottom:14px">
          <Sparkles :size="16" />有 {{ data.suggested_count }} 个 AI 提取的待确认概念。
          <button class="btn sm" style="margin-left:auto" @click="router.push('/glossary')">去审阅</button>
        </div>

        <div class="card pad">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <div class="seclabel"><Lightbulb :size="14" />概念 · 按佐证强度</div>
            <button class="ghost" style="font-size:12px" @click="router.push('/glossary')">
              <BookOpen :size="14" />概念库
            </button>
          </div>

          <div v-if="sortedConcepts.length === 0" class="dim"
            style="font-size:13px;padding:18px 2px;text-align:center">
            暂无概念
          </div>
          <template v-else>
            <div v-for="c in sortedConcepts" :key="c.term" class="concept" @click="goTerm(c.term)">
              <Bookmark v-if="c.is_topic" class="pin" :size="14" />
              <span v-else style="width:14px" />
              <div style="flex:1;min-width:0">
                <div class="t">
                  {{ c.term }}
                  <span v-if="c.is_topic" class="badge b-brand" style="margin-left:4px">主题概念</span>
                  <StatusBadge v-if="c.status === 'suggested'" :status="c.status" />
                </div>
                <div v-if="c.definition" class="d">{{ c.definition }}</div>
              </div>
              <div class="stars" :title="`佐证强度 ${strength(c.source_count)}/5`">
                <Star
                  v-for="i in 5" :key="i" :size="13"
                  :style="i <= strength(c.source_count)
                    ? { color: 'var(--amber)', fill: 'var(--amber)' }
                    : { color: 'var(--ink-300)', fill: 'none' }"
                />
              </div>
              <span class="dim" style="font-size:12px;width:36px;text-align:right">{{ c.source_count }} 源</span>
            </div>
          </template>
        </div>
      </div>

      <!-- TAB 时间线（组件由集成方提供，仅 import + 使用） -->
      <div v-show="tab === 'timeline'">
        <ConceptTimeline :domain="domain" />
      </div>

      <!-- TAB 图谱（概念共现力导向网络） -->
      <div v-show="tab === 'graph'">
        <ConceptGraph :domain="domain" />
      </div>
    </template>

    <!-- 知识库设定弹窗（内嵌 ProfileEditor，自带 overlay） -->
    <ProfileEditor
      v-if="showProfile"
      :domain="domain"
      @close="showProfile = false"
      @saved="onProfileSaved"
    />
  </section>
</template>

<style scoped>
/* 刷新图标旋转 */
.spin { animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
