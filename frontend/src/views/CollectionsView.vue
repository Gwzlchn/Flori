<script setup lang="ts">
import { ref, computed, onMounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useCollectionStore } from '../stores/collections'
import type { Collection } from '../types'
import {
  Folder, FolderPlus, Plus, RefreshCw, Rss, FileText, Cpu, X, Check,
} from 'lucide-vue-next'

// 集合列表（原型 #collections）：订阅 UP 主自动追更，或手动收藏归集。
const router = useRouter()
const store = useCollectionStore()
const { collections, loading } = storeToRefs(store)
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const error = ref('')
// 知识库筛选：空=全部。选项来自现有集合的 distinct domain。
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

// 相对时间（上次同步）。
function syncAgo(v: string | null): string {
  if (!v) return '从未同步'
  const diff = Date.now() - new Date(v).getTime()
  if (isNaN(diff)) return '从未同步'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚同步'
  if (mins < 60) return `${mins} 分钟前同步`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前同步`
  const days = Math.floor(hours / 24)
  return `${days} 天前同步`
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

// ── 新建集合 / 订阅弹窗 ──
const showCreate = ref(false)
const saving = ref(false)
const createError = ref('')
// 表单：手动 / 订阅两态，订阅需 source_id（B站 UP mid）。
const fType = ref<'manual' | 'subscription'>('manual')
const fName = ref('')
const fDomain = ref('')
const fDesc = ref('')
const fTags = ref('')
const fSourceId = ref('')

function openCreate() {
  fType.value = 'manual'
  fName.value = ''
  fDomain.value = filterDomain.value || ''
  fDesc.value = ''
  fTags.value = ''
  fSourceId.value = ''
  createError.value = ''
  showCreate.value = true
}

async function submitCreate() {
  createError.value = ''
  const domain = fDomain.value.trim()
  const isSub = fType.value === 'subscription'
  if (!domain) { createError.value = '请填写知识库'; return }
  if (isSub && !fSourceId.value.trim()) { createError.value = '订阅需填写 UP 主 mid'; return }
  const tags = fTags.value.split(',').map((s) => s.trim()).filter(Boolean)
  saving.value = true
  try {
    await store.create({
      name: fName.value.trim() || (isSub ? `UP-${fSourceId.value.trim()}` : '未命名集合'),
      domain,
      description: fDesc.value.trim() || undefined,
      tags: tags.length ? tags : undefined,
      ...(isSub ? { source_type: 'bilibili_up', source_id: fSourceId.value.trim() } : {}),
    })
    showToast('集合已创建', 'success')
    showCreate.value = false
  } catch (e: any) {
    createError.value = e?.message || '创建失败'
  } finally {
    saving.value = false
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
        <div class="lead">订阅 UP 主自动追更，或手动收藏归集——投递时自动继承集合的知识库与标签。</div>
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

    <!-- 加载态 -->
    <div v-if="loading && collections.length === 0" class="card pad" style="text-align:center;color:var(--ink-500)">
      加载中…
    </div>

    <!-- 错误态 -->
    <div v-else-if="error && collections.length === 0" class="card pad" style="text-align:center">
      <p class="muted" style="margin-bottom:12px">{{ error }}</p>
      <button class="btn" @click="load"><RefreshCw :size="14" />重试</button>
    </div>

    <!-- 空态 -->
    <div v-else-if="visible.length === 0" class="card pad" style="text-align:center;padding:40px 18px">
      <Folder :size="40" :stroke-width="1" style="color:var(--ink-300);margin-bottom:12px" />
      <p class="muted" style="margin-bottom:14px">
        {{ filterDomain ? '该知识库下暂无集合' : '还没有集合，订阅一个 UP 主或新建手动集合' }}
      </p>
      <button class="btn pri" @click="openCreate"><Plus :size="14" />新建集合</button>
    </div>

    <!-- 集合卡片网格 -->
    <div v-else class="grid3">
      <div v-for="c in visible" :key="c.id" class="card pad col-card" @click="open(c)">
        <div class="chead">
          <span class="cic" :class="c.subscription ? 'sub' : 'man'">
            <Rss v-if="c.subscription" :size="17" />
            <Folder v-else :size="17" />
          </span>
          <div class="cname-wrap"><div class="cname">{{ c.name }}</div></div>
          <span v-if="c.subscription" class="badge b-info" style="flex:none"><Rss :size="12" />订阅</span>
          <span v-else class="badge b-mut" style="flex:none">手动</span>
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
          <span class="cfoot-tag">
            <span class="dot" :class="c.subscription ? 'd-ok' : 'd-mut'"></span>
            {{ c.subscription ? syncAgo(c.subscription.last_synced_at) : '手动集合' }}
          </span>
        </div>
      </div>
    </div>

    <!-- 新建集合 / 订阅 弹窗 -->
    <div v-if="showCreate" class="overlay show" @click.self="showCreate = false">
      <div class="modal">
        <div class="hd">
          <FolderPlus :size="18" class="lead-ic" /><b>新建集合</b>
          <button class="ghost" @click="showCreate = false"><X :size="14" /></button>
        </div>
        <div class="bd">
          <div class="field">
            <label>集合类型</label>
            <div class="seg">
              <button :class="{ on: fType === 'manual' }" @click="fType = 'manual'">手动集合</button>
              <button :class="{ on: fType === 'subscription' }" @click="fType = 'subscription'">
                <Rss :size="13" />订阅 B站 UP 主
              </button>
            </div>
          </div>
          <div v-if="fType === 'subscription'" class="field">
            <label>UP 主 mid</label>
            <input v-model="fSourceId" class="input" placeholder="如 12345" />
            <div class="note-tip">订阅必填，对应 B站 UP 主空间 ID。</div>
          </div>
          <div class="field">
            <label>名称</label>
            <input v-model="fName" class="input" :placeholder="fType === 'subscription' ? '可留空，默认 UP-mid' : '如 手动收藏'" />
          </div>
          <div class="field">
            <label>知识库</label>
            <input v-model="fDomain" class="input" placeholder="如 机器学习" />
            <div class="note-tip">必填，订阅集合不能用 general。</div>
          </div>
          <div class="field">
            <label>描述</label>
            <textarea v-model="fDesc" class="input" placeholder="一句话说明这个集合收录什么内容…"></textarea>
          </div>
          <div class="field" style="margin-bottom:0">
            <label>标签</label>
            <input v-model="fTags" class="input" placeholder="逗号分隔，如 paper-reading, lecture" />
          </div>
          <p v-if="createError" class="note-tip" style="color:var(--bad)">{{ createError }}</p>
        </div>
        <div class="ft">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn pri" :disabled="saving" @click="submitCreate">
            <Check :size="14" />{{ saving ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.spin { animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
