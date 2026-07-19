import { createRouter, createWebHistory } from 'vue-router'

// IA:知识库(锚) ⊃ 集合 ⊃ 内容;概念 = 知识层。
// 后端路由(/api/domains、/api/jobs)不变,仅前端路径与文案用知识库词汇。
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'knowledge-bases', component: () => import('../views/HomeView.vue') },
    { path: '/kb/:domain', name: 'knowledge-base', component: () => import('../views/DomainWorkspaceView.vue') },
    { path: '/kb/:domain/radar', name: 'knowledge-radar', component: () => import('../views/RadarView.vue') },
    { path: '/kb/:domain/concepts/:term', name: 'concept-detail', component: () => import('../views/TermDetailView.vue') },
    { path: '/kb/:domain/topics/:topic', name: 'topic', component: () => import('../views/TopicView.vue') },

    { path: '/content', name: 'content', component: () => import('../views/JobListView.vue') },
    { path: '/content/:id', name: 'content-detail', component: () => import('../views/JobDetailView.vue') },

    { path: '/collections', name: 'collections', component: () => import('../views/CollectionsView.vue') },
    { path: '/collections/:id', name: 'collection-detail', component: () => import('../views/CollectionDetailView.vue') },

    { path: '/search', name: 'search', component: () => import('../views/SearchView.vue') },
    { path: '/ask', name: 'ask', component: () => import('../views/AskView.vue') },
    { path: '/study', name: 'study', component: () => import('../views/StudyView.vue') },
    { path: '/glossary', name: 'glossary', component: () => import('../views/GlossaryView.vue') },

    { path: '/system', name: 'system', component: () => import('../views/SystemView.vue') },
    { path: '/system/events', name: 'system-events', component: () => import('../views/EventsView.vue') },
    { path: '/system/queue', name: 'system-queue', component: () => import('../views/QueueView.vue') },
    { path: '/system/workers/:id', name: 'worker-detail', component: () => import('../views/WorkerDetailView.vue') },

    { path: '/settings', name: 'settings', component: () => import('../views/SettingsView.vue') },
    { path: '/settings/prompts', name: 'settings-prompts', component: () => import('../views/PromptsView.vue') },
    { path: '/settings/recovery', name: 'settings-recovery', component: () => import('../views/RecoverySettingsView.vue') },
    { path: '/about', name: 'about', component: () => import('../views/AboutView.vue') },

    // 旧路径兼容(防止过渡期遗留跳转 404,重建完成后清理)
    { path: '/domains/:domain', redirect: (to) => `/kb/${to.params.domain}` },
    { path: '/domains/:domain/terms/:term', redirect: (to) => `/kb/${to.params.domain}/concepts/${to.params.term}` },
    { path: '/domains/:domain/topics/:topic', redirect: (to) => `/kb/${to.params.domain}/topics/${to.params.topic}` },
    { path: '/jobs', redirect: '/content' },
    { path: '/jobs/:id', redirect: (to) => `/content/${to.params.id}` },
    { path: '/jobs/:id/notes/:type', redirect: (to) => `/content/${to.params.id}` },
    { path: '/workers', redirect: '/system' },
  ],
})

export default router
