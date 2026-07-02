<script setup lang="ts">
import { ref, computed, onMounted, watch, inject } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useDomainStore } from '../stores/domains'
import { fmtRelative } from '../utils/datetime'
import type { DomainOverview } from '../types'
import { resolveIcon, ICON_NAMES, KB_COLORS } from '../utils/kbIcons'
import IconPicker from '../components/common/IconPicker.vue'
import {
  BookMarked, Plus, Inbox, Folder, FileText, Lightbulb,
  Rss, X, Check,
  Cpu, Atom, Dna, Code, Database, Globe, FlaskConical, BookOpen,
  Brain, Calculator, Scale, Languages, Music, Palette, Leaf, Rocket,
} from 'lucide-vue-next'

const router = useRouter()
const route = useRoute()
const domainStore = useDomainStore()
const { domains, loading } = storeToRefs(domainStore)
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

// 加载错误态(store.fetchAll 不吞错,这里捕获展示)。
const error = ref('')

const hasDomains = computed(() => domains.value.length > 0)

// 图标名→组件解析用 utils/kbIcons 的 resolveIcon(精选集,避免 import * 拖入整库)。

// 身份图标 + 渐变色块: 按知识库名哈希出稳定的图标/配色,缺 profile 元数据时回退保证稳定。
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
function iconFor(name: string) {
  return ICONS[hash(name) % ICONS.length]
}
function gradientFor(name: string): string {
  return GRADIENTS[hash(name) % GRADIENTS.length]
}
// 卡片身份: 优先用 profile 的 icon/color/display_name,缺失才回退哈希派生。
function cardIcon(d: DomainOverview) {
  return resolveIcon(d.icon) || iconFor(d.domain)
}
function cardBg(d: DomainOverview): string {
  return d.color || gradientFor(d.domain)
}
function cardName(d: DomainOverview): string {
  return d.display_name || d.domain
}

async function loadDomains() {
  error.value = ''
  try {
    await domainStore.fetchAll()
  } catch (e: any) {
    error.value = e?.message || '加载知识库失败'
  }
}

function openDomain(d: DomainOverview) {
  router.push(`/kb/${encodeURIComponent(d.domain)}`)
}

// 活跃时间相对展示走 utils/datetime.fmtRelative(中文单位 + "活跃" 后缀;last_active_at 可能为 null)。
const activeAgo = (v: string | null) => fmtRelative(v, { style: 'cn', suffix: '活跃', fallback: '从未活跃' })

// 新建知识库内联弹窗(参考原型 #home 的 m-domain): 提交真正调 domainStore.create。
// 图标存 lucide 名字符串入 profile.icon;配色从 KB_COLORS 选,存 #hex 入 profile.color。
const showCreate = ref(false)
const draftDomain = ref('')   // 英文 slug → payload.domain(URL/过滤标识,必填)
const draftName = ref('')     // 显示名 → payload.display_name
const draftIcon = ref(ICON_NAMES[0])
const draftColor = ref(KB_COLORS[0])
const draftRole = ref('')
const draftIntro = ref('')
const submitting = ref(false)

function openCreate() {
  draftDomain.value = ''
  draftName.value = ''
  draftIcon.value = ICON_NAMES[0]
  draftColor.value = KB_COLORS[0]
  draftRole.value = ''
  draftIntro.value = ''
  showCreate.value = true
}
async function submitCreate() {
  const domain = draftDomain.value.trim()
  if (!domain) {
    showToast('请填写标识（英文 slug）', 'error')
    return
  }
  submitting.value = true
  try {
    await domainStore.create({
      domain,
      display_name: draftName.value.trim() || undefined,
      icon: draftIcon.value || undefined,
      color: draftColor.value || undefined,
      role: draftRole.value.trim() || undefined,
      description: draftIntro.value.trim() || undefined,
    })
    showToast('知识库已创建', 'success')
    showCreate.value = false
    router.push(`/kb/${encodeURIComponent(domain)}`)
  } catch (e: any) {
    if (e?.status === 409) showToast('该标识已存在', 'error')
    else if (e?.status === 400) showToast('标识非法（不能为 general 或含非法字符）', 'error')
    else showToast('创建失败', 'error')
  } finally {
    submitting.value = false
  }
}

// 侧栏「新建知识库」用 /?create=1 跳转触发(本视图弹窗为唯一实现处);触发后清掉 query,
// 避免刷新/返回重复弹。onMounted 处理直达,watch 处理"已在首页时再次点击"。
function maybeOpenFromQuery() {
  if (route.query.create !== undefined) {
    openCreate()
    router.replace({ path: '/', query: {} })
  }
}
watch(() => route.query.create, maybeOpenFromQuery)

onMounted(() => {
  loadDomains()
  maybeOpenFromQuery()
})
</script>

<template>
  <section class="page">
    <!-- 页头 -->
    <div style="display:flex;align-items:flex-end;gap:12px;margin-bottom:20px">
      <div>
        <div class="h1"><BookMarked :size="18" />我的知识库</div>
        <div class="lead">投递的每条内容都会自动归入对应知识库，逐步沉淀成体系。</div>
      </div>
      <button class="btn pri" style="margin-left:auto" @click="openCreate">
        <Plus :size="16" />新建知识库
      </button>
      <button class="btn" @click="router.push('/content')">
        <Inbox :size="16" />所有来源
      </button>
    </div>

    <!-- 加载态 -->
    <div v-if="loading && !hasDomains" class="card pad" style="color:var(--ink-500);font-size:13px">
      加载中…
    </div>

    <!-- 错误态 -->
    <div v-else-if="error && !hasDomains" class="card pad"
      style="display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:36px 18px">
      <div style="font-size:13.5px;color:var(--ink-700)">{{ error }}</div>
      <button class="btn" @click="loadDomains">重试</button>
    </div>

    <!-- 空态 -->
    <div v-else-if="!hasDomains" class="card pad"
      style="display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:48px 18px">
      <Inbox :size="44" :stroke-width="1" style="color:var(--ink-300)" />
      <div style="font-size:14px;color:var(--ink-700);font-weight:600">还没有知识库</div>
      <div class="lead" style="max-width:380px">
        从一条视频 / 论文 / 文章开始，系统会自动把内容归入对应知识库，逐步沉淀成你的知识体系。
      </div>
      <button class="btn pri" style="margin-top:4px" @click="openCreate">
        <Plus :size="16" />新建知识库
      </button>
    </div>

    <!-- 知识库卡片网格 -->
    <div v-else class="grid3">
      <a v-for="d in domains" :key="d.domain" class="dcard" @click="openDomain(d)">
        <div class="top">
          <span class="ic" :style="{ background: cardBg(d) }">
            <component :is="cardIcon(d)" :size="18" />
          </span>
          <h3>{{ cardName(d) }}</h3>
          <span v-if="d.subscription_count > 0" class="badge b-info"
            :title="`${d.subscription_count} 个订阅集合在自动追更`">
            <Rss :size="12" />{{ d.subscription_count }}
          </span>
        </div>
        <div class="stats">
          <span><Folder :size="13" />{{ d.collection_count }} 集合</span>
          <span><FileText :size="13" />{{ d.job_count }} 内容</span>
          <span><Lightbulb :size="13" />{{ d.concept_count }} 概念</span>
        </div>
        <div class="foot">
          <span class="dot" :class="d.last_active_at ? 'd-ok' : 'd-mut'" />
          {{ activeAgo(d.last_active_at) }}
        </div>
      </a>
    </div>

    <!-- 新建知识库弹窗(提交真正调 domainStore.create) -->
    <div v-if="showCreate" class="overlay show" @click.self="showCreate = false">
      <div class="modal">
        <div class="hd">
          <Plus :size="16" class="lead-ic" /><b>新建知识库</b>
          <button class="ghost" @click="showCreate = false"><X :size="16" /></button>
        </div>
        <div class="bd">
          <div class="field">
            <label>标识（英文 slug）</label>
            <input v-model="draftDomain" class="input" placeholder="如：rl、cryptography、macro-econ" />
            <div class="note-tip">用于 URL 与归类的唯一标识，建议小写英文 / 连字符；不可为 general。</div>
          </div>
          <div class="field">
            <label>名称</label>
            <input v-model="draftName" class="input" placeholder="如：强化学习、密码学、宏观经济…" />
            <div class="note-tip">展示用名称（可中文）。留空则用上面的标识显示。</div>
          </div>
          <div class="field">
            <label>图标</label>
            <IconPicker v-model="draftIcon" />
            <div class="note-tip">从图标库挑一个，配色见下。</div>
          </div>
          <div class="field">
            <label>颜色</label>
            <div class="color-row">
              <button v-for="c in KB_COLORS" :key="c" class="swatch"
                :class="{ on: draftColor === c }" :style="{ background: c }"
                @click="draftColor = c" />
            </div>
          </div>
          <div class="field">
            <label>角色（可选）</label>
            <input v-model="draftRole" class="input" placeholder="如：研究者 / 学习者 / 架构师" />
          </div>
          <div class="field" style="margin-bottom:0">
            <label>简介（可选）</label>
            <textarea v-model="draftIntro" class="input"
              placeholder="这个知识库你关注什么、希望笔记怎么写…（影响该知识库的笔记生成）" />
          </div>
        </div>
        <div class="ft">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn pri" :disabled="submitting" @click="submitCreate">
            <Check :size="16" />{{ submitting ? '创建中…' : '创建知识库' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
