<script setup lang="ts">
import { onMounted, ref, inject } from 'vue'
import { useCollectionStore } from '../stores/collections'
import CollectionCard from '../components/collection/CollectionCard.vue'
import CollectionEditDialog from '../components/collection/CollectionEditDialog.vue'
import ConfirmDialog from '../components/common/ConfirmDialog.vue'
import EmptyState from '../components/common/EmptyState.vue'
import type { Collection } from '../types'
import { Library, Plus, RefreshCw } from 'lucide-vue-next'

const store = useCollectionStore()
const showToast = inject<(msg: string, type: 'success' | 'error' | 'info') => void>('showToast')!

// 对话框状态：editing=新建/编辑表单，removing=待删确认目标。
const showEdit = ref(false)
const editing = ref<Collection | null>(null)
const removing = ref<Collection | null>(null)
const submitting = ref(false)
const submitError = ref('')

onMounted(() => store.fetchAll())

function openCreate() {
  editing.value = null
  submitError.value = ''
  showEdit.value = true
}

function openEdit(c: Collection) {
  editing.value = c
  submitError.value = ''
  showEdit.value = true
}

function closeEdit() {
  showEdit.value = false
  editing.value = null
  submitError.value = ''
}

async function onSubmit(payload: {
  name: string
  domain: string
  description: string
  tags: string[]
  source_type?: string
  source_id?: string
}) {
  submitError.value = ''
  submitting.value = true
  try {
    if (editing.value) {
      await store.update(editing.value.id, {
        name: payload.name,
        description: payload.description,
        tags: payload.tags,
      })
      showToast('集合已更新', 'success')
    } else {
      await store.create(payload)
      showToast('集合已创建', 'success')
    }
    // 成功才关闭，失败保留对话框内的输入。
    showEdit.value = false
    editing.value = null
  } catch (e: any) {
    submitError.value = e.message || '保存失败'
  } finally {
    submitting.value = false
  }
}

async function onConfirmRemove() {
  if (!removing.value) return
  try {
    await store.remove(removing.value.id)
    showToast('集合已删除', 'success')
    removing.value = null
  } catch (e: any) {
    showToast(e.message || '删除失败', 'error')
  }
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-xl font-bold flex items-center gap-2">
        <Library :size="22" />
        集合
      </h2>
      <div class="flex items-center gap-2">
        <button @click="store.fetchAll()" class="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors">
          <RefreshCw :size="16" :class="store.loading ? 'animate-spin' : ''" />
        </button>
        <button
          @click="openCreate"
          class="flex items-center gap-1 px-3 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors"
        >
          <Plus :size="16" />
          新建
        </button>
      </div>
    </div>

    <div v-if="store.loading && store.collections.length === 0" class="text-sm text-gray-400 py-8 text-center">
      加载中...
    </div>
    <div v-else-if="store.collections.length === 0">
      <EmptyState message="暂无集合，点击右上角新建" />
    </div>
    <div v-else class="space-y-3">
      <CollectionCard
        v-for="c in store.collections"
        :key="c.id"
        :collection="c"
        @edit="openEdit"
        @remove="removing = $event"
      />
    </div>

    <CollectionEditDialog
      v-if="showEdit"
      :collection="editing"
      :submitting="submitting"
      :error="submitError"
      @submit="onSubmit"
      @cancel="closeEdit"
    />

    <ConfirmDialog
      v-if="removing"
      title="删除集合"
      :message="`删除「${removing.name}」后，其下 job 将解绑但保留（不会删除）。`"
      confirm-text="删除"
      :danger="true"
      @confirm="onConfirmRemove"
      @cancel="removing = null"
    />
  </div>
</template>
