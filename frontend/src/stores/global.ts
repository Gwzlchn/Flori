import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { ProfileSummary, BreadcrumbSeg } from '../types'

export const useGlobalStore = defineStore('global', () => {
  const api = useApi()
  const profiles = ref<ProfileSummary[]>([])
  const styleTags = ref<string[]>([])

  // 面包屑覆盖:详情页加载到真实数据后(如内容标题/所属领域)发布给 TopBar,
  // 替代 TopBar 仅按路由名派生的通用文案。视图离开时务必置 null(onBeforeUnmount)避免残留。
  const crumbOverride = ref<BreadcrumbSeg[] | null>(null)
  function setCrumbs(segs: BreadcrumbSeg[] | null) {
    crumbOverride.value = segs
  }

  // 全局投递内容弹窗:侧栏/底栏「投递内容」按钮打开;投递表单 JobSubmitForm 只挂在该弹窗里。
  const submitOpen = ref(false)
  function openSubmit() { submitOpen.value = true }
  function closeSubmit() { submitOpen.value = false }

  async function fetchProfiles() {
    profiles.value = await api.get<ProfileSummary[]>('/api/profiles')
  }

  async function fetchStyleTags() {
    try {
      styleTags.value = await api.get<string[]>('/api/config/styles')
    } catch {
      styleTags.value = ['animated', 'lecture', 'code-tutorial', 'talk', 'case-study', 'math-visual']
    }
  }

  return {
    profiles, styleTags, crumbOverride, setCrumbs,
    submitOpen, openSubmit, closeSubmit,
    fetchProfiles, fetchStyleTags,
  }
})
