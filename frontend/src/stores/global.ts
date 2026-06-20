import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { ProfileSummary } from '../types'

export const useGlobalStore = defineStore('global', () => {
  const api = useApi()
  const profiles = ref<ProfileSummary[]>([])
  const styleTags = ref<string[]>([])

  // 面包屑覆盖:详情页加载到真实数据后(如内容标题/所属领域)发布给 TopBar,
  // 替代 TopBar 仅按路由名派生的通用文案。视图离开时务必置 null(onBeforeUnmount)避免残留。
  const crumbOverride = ref<{ t: string; to?: string }[] | null>(null)
  function setCrumbs(segs: { t: string; to?: string }[] | null) {
    crumbOverride.value = segs
  }

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

  return { profiles, styleTags, crumbOverride, setCrumbs, fetchProfiles, fetchStyleTags }
})
