<script setup lang="ts">
import { ref, onMounted, inject } from 'vue'
import { useApi } from '../../composables/useApi'
import type { ProfileDetail } from '../../types'
import { X, Plus, Trash2 } from 'lucide-vue-next'

const props = defineProps<{ domain: string }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const api = useApi()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast')

const loading = ref(true)
const saving = ref(false)
const profile = ref<ProfileDetail>({ domain: props.domain, role: '', domain_context: '', terminology: [], do_not: [] })
const newTerm = ref('')
const newDoNot = ref('')

onMounted(async () => {
  try {
    const data = await api.get<ProfileDetail>(`/api/profiles/${encodeURIComponent(props.domain)}`)
    profile.value = {
      domain: data.domain ?? props.domain,
      role: data.role ?? '',
      domain_context: data.domain_context ?? '',
      output_style: data.output_style,
      terminology: data.terminology ?? [],
      do_not: data.do_not ?? [],
    }
  } catch (e) {
    showToast?.('加载 Profile 失败', 'error')
  } finally {
    loading.value = false
  }
})

function addTerm() {
  const t = newTerm.value.trim()
  if (!t) return
  profile.value.terminology = [...(profile.value.terminology ?? []), t]
  newTerm.value = ''
}

function removeTerm(i: number) {
  profile.value.terminology = (profile.value.terminology ?? []).filter((_, idx) => idx !== i)
}

function addDoNot() {
  const t = newDoNot.value.trim()
  if (!t) return
  profile.value.do_not = [...(profile.value.do_not ?? []), t]
  newDoNot.value = ''
}

function removeDoNot(i: number) {
  profile.value.do_not = (profile.value.do_not ?? []).filter((_, idx) => idx !== i)
}

async function save() {
  saving.value = true
  try {
    await api.put(`/api/profiles/${encodeURIComponent(props.domain)}`, {
      role: profile.value.role,
      domain_context: profile.value.domain_context,
      terminology: profile.value.terminology,
      do_not: profile.value.do_not,
    })
    showToast?.('Profile 已保存', 'success')
    emit('saved')
    emit('close')
  } catch (e) {
    showToast?.('保存失败', 'error')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-4" @click.self="emit('close')">
    <div class="bg-white rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
      <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 sticky top-0 bg-white">
        <h2 class="text-base font-bold">编辑 Profile · {{ domain }}</h2>
        <button @click="emit('close')" class="p-1 text-gray-400 hover:text-gray-600">
          <X :size="18" />
        </button>
      </div>

      <div v-if="loading" class="px-5 py-10 text-center text-sm text-gray-400">加载中...</div>

      <div v-else class="px-5 py-4 space-y-5">
        <!-- role -->
        <div>
          <label class="block text-xs font-medium text-gray-500 mb-1">角色（role）</label>
          <input v-model="profile.role" type="text"
            class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
            placeholder="如：技术文档编辑" />
        </div>

        <!-- domain_context -->
        <div>
          <label class="block text-xs font-medium text-gray-500 mb-1">领域上下文（domain_context）</label>
          <textarea v-model="profile.domain_context" rows="2"
            class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
            placeholder="如：编程/AI/系统设计相关技术讲解" />
        </div>

        <!-- terminology -->
        <div>
          <label class="block text-xs font-medium text-gray-500 mb-2">术语表（terminology）</label>
          <div class="space-y-1.5">
            <div v-for="(t, i) in profile.terminology" :key="i" class="flex items-center gap-2">
              <span class="flex-1 text-sm bg-gray-50 rounded px-2 py-1.5 break-all">{{ t }}</span>
              <button @click="removeTerm(i)" class="p-1 text-gray-400 hover:text-red-500">
                <Trash2 :size="15" />
              </button>
            </div>
          </div>
          <form @submit.prevent="addTerm" class="flex gap-2 mt-2">
            <input v-model="newTerm" type="text"
              class="flex-1 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
              placeholder="术语: 解释" />
            <button type="submit" class="px-2.5 py-1.5 bg-blue-50 text-blue-600 rounded-lg hover:bg-blue-100">
              <Plus :size="16" />
            </button>
          </form>
        </div>

        <!-- do_not -->
        <div>
          <label class="block text-xs font-medium text-gray-500 mb-2">禁止事项（do_not）</label>
          <div class="space-y-1.5">
            <div v-for="(t, i) in profile.do_not" :key="i" class="flex items-center gap-2">
              <span class="flex-1 text-sm bg-gray-50 rounded px-2 py-1.5 break-all">{{ t }}</span>
              <button @click="removeDoNot(i)" class="p-1 text-gray-400 hover:text-red-500">
                <Trash2 :size="15" />
              </button>
            </div>
          </div>
          <form @submit.prevent="addDoNot" class="flex gap-2 mt-2">
            <input v-model="newDoNot" type="text"
              class="flex-1 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
              placeholder="如：不要简化技术细节" />
            <button type="submit" class="px-2.5 py-1.5 bg-blue-50 text-blue-600 rounded-lg hover:bg-blue-100">
              <Plus :size="16" />
            </button>
          </form>
        </div>
      </div>

      <div v-if="!loading" class="px-5 py-4 border-t border-gray-100 flex justify-end gap-2 sticky bottom-0 bg-white">
        <button @click="emit('close')" class="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg">取消</button>
        <button @click="save" :disabled="saving"
          class="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">
          {{ saving ? '保存中...' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>
