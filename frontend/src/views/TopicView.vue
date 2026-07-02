<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../stores/domains'
import StatusBadge from '../components/common/StatusBadge.vue'
import { fmtDateTime } from '../utils/datetime'
import { contentTypeIcon, contentTypePill, contentTypeLabel } from '../utils/contentType'
import type { JobSummary } from '../types'
import { Hash, Lightbulb, LayoutList, ChevronRight } from 'lucide-vue-next'

// 主题页:把某个主题(is_topic 概念)下跨集合/跨来源的内容聚到一处。
// 返回形状见 api/routes/domains.py topic_page,以后端为准。
const route = useRoute()
const router = useRouter()
const store = useDomainStore()

const domain = computed(() => String(route.params.domain))
const topic = computed(() => String(route.params.topic))

const jobs = ref<JobSummary[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

// 防御性归一:后端 job 子集字段与 JobSummary 对齐,缺字段给安全默认。
function normalizeJob(raw: any): JobSummary {
  return {
    job_id: String(raw?.job_id ?? ''),
    content_type: raw?.content_type ?? 'article',
    status: String(raw?.status ?? 'pending'),
    created_at: String(raw?.created_at ?? ''),
    title: raw?.title ?? null,
    progress_pct: Number(raw?.progress_pct ?? 0),
    source: raw?.source ?? null,
    domain: String(raw?.domain ?? domain.value),
    collection_id: raw?.collection_id ?? null,
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await store.topic(domain.value, topic.value)
    const list: any[] = Array.isArray(res?.jobs) ? res.jobs : []
    jobs.value = list.map(normalizeJob)
    total.value = typeof res?.total === 'number' ? res.total : jobs.value.length
  } catch (e: any) {
    error.value = e?.message || '加载主题内容失败'
  } finally {
    loading.value = false
  }
}

function goDomain() {
  router.push(`/kb/${encodeURIComponent(domain.value)}`)
}
function goConcept() {
  router.push(`/kb/${encodeURIComponent(domain.value)}/concepts/${encodeURIComponent(topic.value)}`)
}
function openJob(j: JobSummary) {
  router.push(`/content/${j.job_id}`)
}

onMounted(load)
watch(() => [route.params.domain, route.params.topic], load)
</script>

<template>
  <section class="page">
    <!-- 头部:主题名 + 面包屑 + 查看概念 -->
    <div style="display:flex;align-items:center;gap:13px;margin-bottom:6px">
      <div style="min-width:0">
        <div class="h1"><Hash :size="18" />{{ topic }}</div>
        <div class="lead">主题 · <a class="term-link" @click="goDomain">{{ domain }}</a> · 共 {{ total }} 条内容</div>
      </div>
      <button class="btn sm" style="margin-left:auto" @click="goConcept"><Lightbulb :size="13" />查看概念</button>
    </div>

    <!-- 错误态 -->
    <div v-if="error" class="card pad" style="text-align:center;margin-top:20px">
      <p class="muted" style="margin-bottom:12px">{{ error }}</p>
      <button class="btn" @click="load">重试</button>
    </div>

    <!-- 加载态 -->
    <div v-else-if="loading && jobs.length === 0" class="card pad" style="text-align:center;color:var(--ink-500);margin-top:20px">
      加载中…
    </div>

    <!-- 空态:主题靠抽取自动聚合 -->
    <div v-else-if="jobs.length === 0" class="card pad" style="text-align:center;padding:40px 18px;margin-top:20px">
      <Hash :size="40" :stroke-width="1" style="color:var(--ink-300);margin-bottom:12px" />
      <p class="muted">这个主题还没有内容（内容被解析、抽到该概念时自动聚合）</p>
    </div>

    <!-- 关联内容列表 -->
    <template v-else>
      <div class="seclabel" style="margin:22px 0 12px"><LayoutList :size="14" />关联内容 · {{ total }}</div>
      <div class="list">
        <div v-for="j in jobs" :key="j.job_id" class="row" @click="openJob(j)">
          <span class="type-pill" :class="contentTypePill(j.content_type)">
            <component :is="contentTypeIcon(j.content_type)" :size="17" />
          </span>
          <div class="body">
            <div class="title">{{ j.title || j.job_id }}</div>
            <div class="meta">
              <StatusBadge :status="j.status" />
              <span>{{ contentTypeLabel(j.content_type) }}</span>
              <template v-if="j.source"><span class="sep">·</span><span>{{ j.source }}</span></template>
              <span class="sep">·</span>
              <span class="dim">{{ fmtDateTime(j.created_at) }}</span>
            </div>
          </div>
          <ChevronRight :size="16" class="dim" />
        </div>
      </div>
    </template>
  </section>
</template>
