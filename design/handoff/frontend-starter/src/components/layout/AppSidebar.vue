<!--
  侧栏：logo + 投递按钮 + 知识库三级折叠树 + 所有来源 + 底部工具。
  对应原型 <aside class="side">。
  三级折叠（nb-group → kb-sources → src-group → src-content）改用 Vue 响应式 ref 控制展开，
  替代原型的 DOM toggle（toggleKb / toggleSrc）。
-->
<script setup lang="ts">
import { reactive } from 'vue'
import { useRouter } from 'vue-router'
import {
  Plus,
  Library,
  Inbox,
  ChevronRight,
  Rss,
  Folder,
  Server,
  Settings,
  ChevronsLeft,
} from 'lucide-vue-next'

defineProps<{
  railed: boolean
}>()

const emit = defineEmits<{
  'toggle-rail': []
}>()

const router = useRouter()

// 侧栏树形示例数据 —— 知识库 ⊃ 集合 ⊃ 内容
// TODO: GET /api/knowledge-bases?expand=collections （侧栏树，含每库前若干集合）
interface NavContent {
  id: string
  title: string
  /** type-pill 配色变量，如 var(--t-video) */
  dotColor: string
}
interface NavCollection {
  id: string
  name: string
  kind: 'subscription' | 'manual'
  total: number
  contents: NavContent[]
}
interface NavKnowledgeBase {
  id: string
  name: string
  dotColor: string
  collections: NavCollection[]
}

const knowledgeBases: NavKnowledgeBase[] = [
  {
    id: 'ml',
    name: '机器学习',
    dotColor: '#4f46e5',
    collections: [
      {
        id: 'limu',
        name: '李沐读论文',
        kind: 'subscription',
        total: 18,
        contents: [
          { id: 'c1', title: 'Transformer 架构详解', dotColor: 'var(--t-video)' },
          { id: 'c2', title: 'Batch Normalization 精读', dotColor: 'var(--t-paper)' },
        ],
      },
      {
        id: '3b1b',
        name: '3Blue1Brown',
        kind: 'manual',
        total: 12,
        contents: [{ id: 'c3', title: '反向传播的直觉理解', dotColor: 'var(--t-video)' }],
      },
      {
        id: 'ml-manual',
        name: '手动收藏',
        kind: 'manual',
        total: 13,
        contents: [{ id: 'c4', title: 'ResNet 残差网络精读', dotColor: 'var(--t-paper)' }],
      },
    ],
  },
  {
    id: 'sysdesign',
    name: '系统设计',
    dotColor: '#0ea5e9',
    collections: [
      {
        id: 'sysweekly',
        name: '系统设计周刊',
        kind: 'subscription',
        total: 21,
        contents: [{ id: 'c5', title: '一致性哈希实践', dotColor: 'var(--t-article)' }],
      },
      { id: 'sys-manual', name: '手动收藏', kind: 'manual', total: 7, contents: [] },
    ],
  },
  {
    id: 'bioinfo',
    name: '生物信息学',
    dotColor: '#10b981',
    collections: [{ id: 'bio-manual', name: '手动收藏', kind: 'manual', total: 9, contents: [] }],
  },
]

// 展开状态：用 Set 记录已展开的 key（默认展开第一个知识库及其第一个集合，还原原型初始态）
const expandedKb = reactive(new Set<string>(['ml']))
const expandedCol = reactive(new Set<string>(['limu']))

function toggleKb(id: string) {
  expandedKb.has(id) ? expandedKb.delete(id) : expandedKb.add(id)
}
function toggleCol(id: string) {
  expandedCol.has(id) ? expandedCol.delete(id) : expandedCol.add(id)
}

function goKb(id: string) {
  router.push({ name: 'knowledge-base', params: { id } })
}
function goCollection(id: string) {
  router.push({ name: 'collection-detail', params: { id } })
}
function goContent(id: string) {
  router.push({ name: 'content-detail', params: { id } })
}
</script>

<template>
  <aside class="side">
    <!-- 品牌 logo：深色方块 + 白色 M（路径来自原型内联 svg），点击回总览 -->
    <div class="brand">
      <span class="logo" data-tip="知识库" @click="router.push('/')">
        <svg
          viewBox="0 0 24 24"
          width="16"
          height="16"
          fill="none"
          stroke="#fff"
          stroke-width="2.4"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M4 19V6l8 7 8-7v13" />
        </svg>
      </span>
      <b>Mnemo</b>
    </div>

    <!-- 投递内容 -->
    <button class="btn-submit" data-tip="投递内容">
      <!-- TODO: 打开投递弹窗（Modal m-submit） -->
      <Plus :size="16" /><span>投递内容</span>
    </button>

    <nav class="nav">
      <!-- 知识库（顶层入口） -->
      <RouterLink to="/" custom v-slot="{ isActive }">
        <a :class="{ on: isActive }" data-tip="知识库" @click="router.push('/')">
          <Library :size="17" /><span>知识库</span>
        </a>
      </RouterLink>

      <!-- 三级折叠树：知识库 → 集合 → 内容 -->
      <div class="sub-list">
        <div v-for="kb in knowledgeBases" :key="kb.id" class="nb-group">
          <a class="sub-item" @click="goKb(kb.id)">
            <span
              class="kb-caret"
              :class="{ open: expandedKb.has(kb.id) }"
              @click.stop="toggleKb(kb.id)"
            >
              <ChevronRight :size="12" />
            </span>
            <span class="nb-dot" :style="{ background: kb.dotColor }"></span>
            <span>{{ kb.name }}</span>
          </a>

          <!-- 第 2 级：集合 -->
          <div class="kb-sources" :class="{ open: expandedKb.has(kb.id) }">
            <div v-for="col in kb.collections" :key="col.id" class="src-group">
              <a class="src-item" @click="goCollection(col.id)">
                <span
                  class="src-caret"
                  :class="{ open: expandedCol.has(col.id) }"
                  @click.stop="toggleCol(col.id)"
                >
                  <ChevronRight :size="11" />
                </span>
                <Rss v-if="col.kind === 'subscription'" :size="11" />
                <Folder v-else :size="11" />
                {{ col.name }}
              </a>

              <!-- 第 3 级：内容 -->
              <div class="src-content" :class="{ open: expandedCol.has(col.id) }">
                <a
                  v-for="content in col.contents"
                  :key="content.id"
                  class="content-item"
                  @click="goContent(content.id)"
                >
                  <span class="ci-dot" :style="{ background: content.dotColor }"></span>
                  <span>{{ content.title }}</span>
                </a>
                <a class="content-item more" @click="goCollection(col.id)">
                  查看全部 {{ col.total }} 条 →
                </a>
              </div>
            </div>
          </div>
        </div>

        <!-- 新建知识库 -->
        <a class="sub-item new">
          <!-- TODO: 打开新建知识库弹窗（Modal m-domain） -->
          <Plus :size="15" /><span>新建知识库</span>
        </a>
      </div>

      <!-- 所有来源 -->
      <RouterLink to="/content" custom v-slot="{ isActive }">
        <a :class="{ on: isActive }" data-tip="所有来源" @click="router.push('/content')">
          <Inbox :size="17" /><span>所有来源</span>
        </a>
      </RouterLink>
    </nav>

    <!-- 底部工具：系统 / 设置 / 折叠 -->
    <div class="side-tools">
      <button class="tool" data-tip="系统状态" @click="router.push('/system')">
        <Server :size="17" /><span class="dot d-ok"></span>
      </button>
      <button class="tool" data-tip="设置" @click="router.push('/settings')">
        <Settings :size="17" />
      </button>
      <button class="tool collapse" data-tip="收起 / 展开" @click="emit('toggle-rail')">
        <ChevronsLeft :size="17" />
      </button>
    </div>
  </aside>
</template>
