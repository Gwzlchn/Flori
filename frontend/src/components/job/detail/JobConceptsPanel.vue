<script setup lang="ts">
import { Bookmark, ChevronRight, Lightbulb, RotateCcw } from 'lucide-vue-next'
import StatusBadge from '../../common/StatusBadge.vue'
import type { JobConcept } from '../../../types'

defineProps<{
  concepts: JobConcept[]
  loading: boolean
  error: string
  occurrenceText: (concept: JobConcept) => string
}>()

defineEmits<{ retry: []; select: [concept: JobConcept] }>()
</script>

<template>
  <div class="card pad">
    <div class="card-h"><Lightbulb :size="15" />本内容涉及的概念<template v-if="concepts.length"> · {{ concepts.length }}</template></div>
    <p class="lead" style="margin:-6px 0 12px">这条内容里命中的概念。点进去可反查它在整个知识库里——还有哪些内容也讲过它。</p>
    <div v-if="loading" class="state"><span class="spinner" />加载概念…</div>
    <div v-else-if="error" class="state">
      <Lightbulb class="big" /><div class="t">{{ error }}</div>
      <button class="btn" @click="$emit('retry')"><RotateCcw :size="14" />重试</button>
    </div>
    <div v-else-if="concepts.length === 0" class="state"><Lightbulb class="big" /><div class="t">这条内容暂未关联任何概念</div></div>
    <div v-else>
      <div v-for="concept in concepts" :key="concept.term" class="concept" @click="$emit('select', concept)">
        <Bookmark v-if="concept.is_topic" class="pin" /><span v-else style="width:14px;flex:none" />
        <div style="flex:1;min-width:0">
          <div class="t">{{ concept.term }}<span v-if="concept.is_topic" class="badge b-brand" style="margin-left:4px">主题概念</span></div>
          <div v-if="concept.definition" class="d" style="white-space:normal">{{ concept.definition }}</div>
          <div class="d"><template v-if="occurrenceText(concept)">本内容 {{ occurrenceText(concept) }} · </template>全库 {{ concept.occurrences?.length ?? 0 }} 条内容讲过</div>
        </div>
        <StatusBadge :status="concept.status" /><ChevronRight :size="15" class="dim" style="flex:none" />
      </div>
    </div>
  </div>
</template>
