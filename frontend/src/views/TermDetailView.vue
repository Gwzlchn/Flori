<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../stores/domains'
import { useApi } from '../composables/useApi'
import { contentTypeIcon, contentTypePill, contentTypeLabel } from '../utils/contentType'
import type { TermOccurrence, GlossaryTerm } from '../types'
import {
  Lightbulb, Bookmark, Check, FileText, Link, MapPin, ChevronRight, Star,
} from 'lucide-vue-next'

// 概念详情页:定义 / 关联概念 / 出现处反查。
// 返回形状见 api/schemas.py GlossaryTermResponse,以后端为准。
const route = useRoute()
const router = useRouter()
const store = useDomainStore()
const api = useApi()

const domain = computed(() => String(route.params.domain))
const term = computed(() => String(route.params.term))

const data = ref<GlossaryTerm | null>(null)
const loading = ref(false)
const notFound = ref(false)
const error = ref('')
const toggling = ref(false)

import type { RelatedEdge } from '../types'
const related = computed<RelatedEdge[]>(() => (Array.isArray(data.value?.related) ? data.value!.related : []))
// 边类型中文标签(related 不标,免噪声)。
const relLabel: Record<string, string> = { prerequisite: '先修', is_a: '是一种', part_of: '组成' }
const aliases = computed<string[]>(() => (Array.isArray(data.value?.aliases) ? data.value!.aliases : []))
const occurrences = computed<TermOccurrence[]>(() => (Array.isArray(data.value?.occurrences) ? data.value!.occurrences : []))
const isTopic = computed<boolean>(() => data.value?.is_topic === true)

async function load() {
  loading.value = true
  notFound.value = false
  error.value = ''
  data.value = null
  try {
    data.value = await store.term(domain.value, term.value)
  } catch (e: any) {
    const msg = String(e?.message ?? '')
    if (msg.includes('404')) notFound.value = true
    else error.value = msg || '加载失败'
  } finally {
    loading.value = false
  }
}

// 标为主题 / 取消主题:POST /api/glossary/{domain}/{term}/topic,用返回的概念刷新本页。
async function toggleTopic() {
  if (!data.value || toggling.value) return
  toggling.value = true
  try {
    data.value = await api.post<GlossaryTerm>(
      `/api/glossary/${encodeURIComponent(domain.value)}/${encodeURIComponent(term.value)}/topic`,
      { is_topic: !isTopic.value },
    )
  } catch {
    // 失败保持原状态,按钮可重试。
  } finally {
    toggling.value = false
  }
}

// 关注/取关:watched 概念在雷达页「我关注的概念」区跟踪近窗动静。
const watching = ref(false)
async function toggleWatch() {
  if (!data.value || watching.value) return
  watching.value = true
  try {
    data.value = await api.post<GlossaryTerm>(
      `/api/glossary/${encodeURIComponent(domain.value)}/${encodeURIComponent(term.value)}/watch`,
      { watched: !data.value.watched },
    )
  } catch {
    // 失败保持原状态,按钮可重试。
  } finally {
    watching.value = false
  }
}

function goDomain() {
  router.push(`/kb/${encodeURIComponent(domain.value)}`)
}
function goRelated(name: string) {
  router.push(`/kb/${encodeURIComponent(domain.value)}/concepts/${encodeURIComponent(name)}`)
}
function goJob(jobId: string) {
  router.push(`/content/${encodeURIComponent(jobId)}`)
}
onMounted(load)
watch(() => [route.params.domain, route.params.term], load)
</script>

<template>
  <section class="page">
    <!-- 404 -->
    <div v-if="notFound" class="card pad" style="text-align:center;padding:40px 18px">
      <p class="muted" style="margin-bottom:14px">概念不存在或已删除</p>
      <button class="btn" @click="goDomain">返回知识库</button>
    </div>

    <!-- 错误态 -->
    <div v-else-if="error && !data" class="card pad" style="text-align:center">
      <p class="muted" style="margin-bottom:12px">{{ error }}</p>
      <button class="btn" @click="load">重试</button>
    </div>

    <!-- 加载态 -->
    <div v-else-if="loading && !data" class="card pad" style="text-align:center;color:var(--ink-500)">加载中…</div>

    <template v-else-if="data">
      <!-- 头部卡片 -->
      <div class="card pad" style="margin-bottom:16px">
        <div style="display:flex;align-items:flex-start;gap:14px">
          <span
            class="ic"
            style="width:44px;height:44px;border-radius:11px;display:grid;place-items:center;flex:none;color:#fff;background:linear-gradient(135deg,var(--brand-500),var(--brand-700))"
          ><Lightbulb :size="18" /></span>
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
              <div class="h1">{{ data.term }}</div>
              <span v-if="data.zh_name && data.zh_name !== data.term" class="dim" style="font-size:15px">{{ data.zh_name }}</span>
              <span v-if="isTopic" class="badge b-brand"><Bookmark :size="12" />主题概念</span>
              <span v-if="data.status" class="badge" :class="data.status === 'accepted' ? 'b-ok' : 'b-warn'">
                <Check v-if="data.status === 'accepted'" :size="12" />{{ data.status === 'accepted' ? '已采纳' : '候选' }}
              </span>
            </div>
            <div class="lead">
              <a class="term-link" @click="goDomain">{{ data.domain }}</a>
              · {{ occurrences.length }} 处出现 · {{ related.length }} 个关联
              <template v-if="aliases.length"> · 别名：{{ aliases.join('、') }}</template>
            </div>
          </div>
          <span style="margin-left:auto;display:flex;gap:8px;flex:none">
            <button
              class="btn sm" :disabled="watching"
              :style="data.watched ? 'color:var(--warn,#cb7b1f);border-color:var(--warn-bd,#e8c78a)' : ''"
              @click="toggleWatch"
            >
              <Star :size="13" :fill="data.watched ? 'currentColor' : 'none'" />{{ data.watched ? '已关注' : '关注' }}
            </button>
            <button class="btn sm" :disabled="toggling" @click="toggleTopic">
              <Bookmark :size="13" />{{ isTopic ? '取消主题' : '标为主题' }}
            </button>
          </span>
        </div>
      </div>

      <!-- 定义 -->
      <div class="card pad" style="margin-bottom:16px">
        <div class="card-h"><FileText :size="15" />定义</div>
        <p v-if="data.definition" style="color:var(--ink-700);line-height:1.6;white-space:pre-wrap">{{ data.definition }}</p>
        <p v-else class="muted">暂无定义</p>
      </div>

      <!-- 关联概念 -->
      <div class="card pad" style="margin-bottom:16px">
        <div class="card-h"><Link :size="15" />关联概念</div>
        <div v-if="related.length" style="display:flex;gap:8px;flex-wrap:wrap">
          <span v-for="r in related" :key="r.term" class="chip" @click="goRelated(r.term)">
            {{ r.term }}<span v-if="relLabel[r.rel]" class="dim" style="font-size:11px;margin-left:4px">{{ relLabel[r.rel] }}</span>
          </span>
        </div>
        <p v-else class="muted">暂无关联概念</p>
      </div>

      <!-- 出现处反查 -->
      <div class="card pad">
        <div class="card-h"><MapPin :size="15" />出现处 · {{ occurrences.length }}</div>
        <template v-if="occurrences.length">
          <div v-for="o in occurrences" :key="o.job_id + (o.location || '')" class="occ" @click="goJob(o.job_id)">
            <span class="type-pill" :class="contentTypePill(o.content_type)" style="width:28px;height:28px">
              <component :is="contentTypeIcon(o.content_type)" :size="13" />
            </span>
            <span class="occ-t" style="flex:1;min-width:0;font-weight:600;color:var(--ink-900);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ o.title || o.job_id }}</span>
            <span class="badge b-mut">{{ contentTypeLabel(o.content_type) }}</span>
            <span v-if="o.location" class="dim" style="font-size:12px">{{ o.location }}</span>
            <ChevronRight :size="15" class="dim" />
          </div>
        </template>
        <p v-else class="muted">还没有内容提到这个概念</p>
      </div>
    </template>
  </section>
</template>
