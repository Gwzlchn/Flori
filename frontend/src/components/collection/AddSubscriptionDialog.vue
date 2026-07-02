<script setup lang="ts">
import { ref, computed } from 'vue'
import { FolderPlus, X, Check, Folder } from 'lucide-vue-next'
import { SOURCE_TYPES, sourceMeta } from '../../constants/sources'

// 新建集合 / 订阅弹窗。手动集合 or 多源订阅。
// name 不让用户填:订阅集合首次同步自动取来源真实名;手动集合用「名称」字段(下方手动态才显示)。
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'create', payload: {
    name?: string; domain: string; description?: string; tags?: string[]
    source_type?: string; source_id?: string; sync_now?: boolean
  }): void
}>()
const props = defineProps<{ saving?: boolean; error?: string; defaultDomain?: string }>()

// mode: manual | <source_type>
const mode = ref<'manual' | string>('manual')
const isSub = computed(() => mode.value !== 'manual')
const meta = computed(() => (isSub.value ? sourceMeta(mode.value) : undefined))

const fName = ref('')
const fDomain = ref(props.defaultDomain || '')
const fDesc = ref('')
const fTags = ref('')
const fSourceId = ref('')
const fSyncNow = ref(true)
const localErr = ref('')

function submit() {
  localErr.value = ''
  const domain = fDomain.value.trim()
  if (!domain) { localErr.value = '请填写知识库'; return }
  if (isSub.value && domain === 'general') { localErr.value = '订阅集合不能用 general 知识库'; return }
  if (isSub.value && !fSourceId.value.trim()) { localErr.value = `请填写${meta.value?.idLabel || '来源'}`; return }
  if (!isSub.value && !fName.value.trim()) { localErr.value = '手动集合需填写名称'; return }
  const tags = fTags.value.split(',').map((s) => s.trim()).filter(Boolean)
  emit('create', {
    domain,
    description: fDesc.value.trim() || undefined,
    tags: tags.length ? tags : undefined,
    ...(isSub.value
      ? { source_type: mode.value, source_id: fSourceId.value.trim(), sync_now: fSyncNow.value }
      : { name: fName.value.trim() }),
  })
}
</script>

<template>
  <!-- 侧栏 .side 是 position:sticky(建立层叠上下文)→ 弹窗留在侧栏 DOM 内会被主内容盖住,Teleport 到 body 逃出。 -->
  <Teleport to="body">
  <div class="overlay show" @click.self="emit('close')">
    <div class="modal">
      <div class="hd">
        <FolderPlus :size="18" class="lead-ic" /><b>新建集合 / 订阅</b>
        <button class="ghost" @click="emit('close')"><X :size="14" /></button>
      </div>
      <div class="bd">
        <!-- 来源类型选择 -->
        <div class="field">
          <label>来源类型</label>
          <div class="src-grid">
            <button
              class="src-opt" :class="{ on: mode === 'manual' }" type="button"
              @click="mode = 'manual'"
            >
              <Folder :size="16" /><span>手动集合</span>
            </button>
            <button
              v-for="s in SOURCE_TYPES" :key="s.type"
              class="src-opt" :class="{ on: mode === s.type }" type="button"
              @click="mode = s.type"
            >
              <component :is="s.icon" :size="16" /><span>{{ s.label }}</span>
            </button>
          </div>
        </div>

        <!-- 订阅:来源 ID 输入 -->
        <div v-if="isSub" class="field">
          <label>{{ meta?.idLabel }}</label>
          <input v-model="fSourceId" class="input" :placeholder="meta?.placeholder" />
          <div class="note-tip">{{ meta?.hint }}</div>
        </div>

        <!-- 手动:名称(订阅不填,名字自动取来源真实名) -->
        <div v-if="!isSub" class="field">
          <label>名称</label>
          <input v-model="fName" class="input" placeholder="如 手动收藏" />
        </div>

        <div class="field">
          <label>知识库</label>
          <input v-model="fDomain" class="input" placeholder="如 机器学习" />
          <div class="note-tip">{{ isSub ? '必填，订阅集合不能用 general。' : '内容归属的知识库。' }}</div>
        </div>

        <div class="field">
          <label>描述<span class="opt">（可选）</span></label>
          <textarea v-model="fDesc" class="input" placeholder="一句话说明这个集合收录什么内容…"></textarea>
        </div>

        <div class="field" :style="{ marginBottom: isSub ? '14px' : '0' }">
          <label>标签<span class="opt">（可选）</span></label>
          <input v-model="fTags" class="input" placeholder="逗号分隔，如 paper-reading, lecture" />
        </div>

        <!-- 订阅:创建后立即同步 -->
        <label v-if="isSub" class="sync-now">
          <input type="checkbox" v-model="fSyncNow" />
          <span>创建后立即同步一次（拉取来源现有内容）</span>
        </label>

        <p v-if="error || localErr" class="note-tip" style="color:var(--bad)">{{ error || localErr }}</p>
      </div>
      <div class="ft">
        <button class="btn" @click="emit('close')">取消</button>
        <button class="btn pri" :disabled="saving" @click="submit">
          <Check :size="14" />{{ saving ? '创建中…' : '创建' }}
        </button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
.src-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.src-opt {
  display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--raised);
  font-size: 13px; color: var(--ink-700); cursor: pointer; transition: all .12s; text-align: left;
}
.src-opt:hover { border-color: var(--brand-300); background: var(--brand-50); }
.src-opt.on { border-color: var(--brand-500); background: var(--brand-50); color: var(--brand-700); font-weight: 600; }
.src-opt :deep(svg) { flex: none; }
.opt { color: var(--ink-400); font-weight: 400; font-size: 11.5px; }
.sync-now { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink-600); cursor: pointer; }
.sync-now input { width: 14px; height: 14px; }
</style>
