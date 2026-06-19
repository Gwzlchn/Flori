// 路由表 —— 对齐 IA：知识库 ⊃ 集合 ⊃ 内容；概念为知识层。
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'knowledge-bases',
    // 知识库总览（原型 id="home"）
    component: () => import('@/views/KnowledgeBasesView.vue'),
  },
  {
    path: '/kb/:id',
    name: 'knowledge-base',
    // 知识库工作台（原型 id="domain"）
    component: () => import('@/views/KnowledgeBaseView.vue'),
    props: true,
  },
  {
    path: '/content',
    name: 'content-list',
    // 所有来源（原型 id="content"）
    component: () => import('@/views/ContentListView.vue'),
  },
  {
    path: '/content/:id',
    name: 'content-detail',
    // 内容详情（原型 id="detail"）
    component: () => import('@/views/ContentDetailView.vue'),
    props: true,
  },
  {
    path: '/collections',
    name: 'collections',
    // 集合列表（原型 id="collections"）
    component: () => import('@/views/CollectionsView.vue'),
  },
  {
    path: '/collections/:id',
    name: 'collection-detail',
    // 集合详情（原型 id="collection"）
    component: () => import('@/views/CollectionDetailView.vue'),
    props: true,
  },
  {
    path: '/search',
    name: 'search',
    // 全文搜索（原型 id="search"）
    component: () => import('@/views/SearchView.vue'),
  },
  {
    path: '/glossary',
    name: 'glossary',
    // 概念库（原型 id="glossary"）
    component: () => import('@/views/GlossaryView.vue'),
  },
  {
    path: '/system',
    name: 'system',
    // 系统与 Worker 监控（原型 id="system"）
    component: () => import('@/views/SystemView.vue'),
  },
  {
    path: '/settings',
    name: 'settings',
    // 设置（原型 id="settings"）
    component: () => import('@/views/SettingsView.vue'),
  },
  {
    path: '/about',
    name: 'about',
    // 关于 Mnemo（原型 id="about"）
    component: () => import('@/views/AboutView.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  },
})

export default router
