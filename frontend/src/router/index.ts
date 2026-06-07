import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('../views/HomeView.vue'),
    },
    {
      path: '/jobs',
      name: 'jobs',
      component: () => import('../views/JobListView.vue'),
    },
    {
      path: '/jobs/:id',
      name: 'job-detail',
      component: () => import('../views/JobDetailView.vue'),
    },
    {
      path: '/notes/:jobId',
      name: 'notes',
      component: () => import('../views/NotesView.vue'),
    },
    {
      path: '/notes/:jobId/mechanical',
      name: 'notes-mechanical',
      component: () => import('../views/NotesView.vue'),
    },
    {
      path: '/collections',
      name: 'collections',
      component: () => import('../views/CollectionsView.vue'),
    },
    {
      path: '/collections/:id',
      name: 'collection-detail',
      component: () => import('../views/CollectionDetailView.vue'),
    },
    {
      path: '/search',
      name: 'search',
      component: () => import('../views/SearchView.vue'),
    },
    {
      path: '/glossary',
      name: 'glossary',
      component: () => import('../views/GlossaryView.vue'),
    },
    {
      path: '/workers',
      name: 'workers',
      component: () => import('../views/WorkersView.vue'),
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue'),
    },
  ],
})

export default router
