<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useCollectionStore } from '../stores/collections'
import CollectionCard from '../components/collection/CollectionCard.vue'
import CollectionEditDialog from '../components/collection/CollectionEditDialog.vue'
import ConfirmDialog from '../components/common/ConfirmDialog.vue'
import EmptyState from '../components/common/EmptyState.vue'
import type { Collection } from '../types'
import { Library, Plus, RefreshCw } from 'lucide-vue-next'

const store = useCollectionStore()

// 对话框状态：editing=新建/编辑表单，removing=待删确认目标。
const showEdit = ref(false)
const editing = ref<Collection | null>(null)
const removing = ref<Collection | null>(null)

onMounted(() => store.fetchAll())

function openCreate() {
  editing.value = null
  showEdit.value = true
}

function openEdit(c: Collection) {
  editing.value = c
  showEdit.value = true
}

async function onSubmit(payload: {
  name: string
  domain: string
  description: string
  tags: string[]
}) {
  if (editing.value) {
    await store.update(editing.value.id, {
      name: payload.name,
      description: payload.description,
      tags: payload.tags,
    })
  } else {
    await store.create(payload)
  }
  showEdit.value = false
  editing.value = null
}

async function onConfirmRemove() {
  if (!removing.value) return
  await store.remove(removing.value.id)
  removing.value = null
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
      @submit="onSubmit"
      @cancel="showEdit = false; editing = null"
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
