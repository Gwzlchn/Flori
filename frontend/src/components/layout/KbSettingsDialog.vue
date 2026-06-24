<script setup lang="ts">
// 知识库设置弹窗(重命名 / 图标 / 配色)。从侧栏 KB 行的「…」打开;
// 一期只改 profile 展示元数据(display_name/icon/color),不改英文标识 domain key(真正改 key 见后续迁移工作项)。
import { ref } from 'vue'
import { Settings2, X, Check } from 'lucide-vue-next'
import IconPicker from '../common/IconPicker.vue'
import { KB_COLORS } from '../../utils/kbIcons'

const props = defineProps<{ domain: string; name?: string; icon?: string; color?: string }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'save', patch: { display_name: string; icon: string; color: string }): void
  (e: 'rename', newDomain: string): void
}>()

const name = ref(props.name || '')
const icon = ref(props.icon || '')
const color = ref(props.color || '')
const saving = ref(false)
function save() {
  saving.value = true
  emit('save', { display_name: name.value.trim(), icon: icon.value, color: color.value })
}

// 高级:改英文标识 domain key(会迁移该领域下所有内容)。父组件负责确认/调用/跳转。
const newKey = ref('')
function onRename() {
  const nk = newKey.value.trim()
  if (!nk || nk === props.domain) return
  emit('rename', nk)
}
</script>

<template>
  <div class="overlay show confirm" @click.self="emit('close')">
    <div class="modal">
      <div class="hd">
        <Settings2 :size="18" class="lead-ic" /><b>知识库设置 · {{ domain }}</b>
        <button class="ghost" @click="emit('close')"><X :size="14" /></button>
      </div>
      <div class="bd">
        <div class="field">
          <label>显示名</label>
          <input v-model="name" class="kf-in" :placeholder="domain" />
          <p class="kf-hint">仅改展示名,不动英文标识 domain。改英文标识见下方「高级」。</p>
        </div>
        <div class="field">
          <label>图标</label>
          <IconPicker v-model="icon" />
        </div>
        <div class="field">
          <label>配色</label>
          <div class="color-row">
            <button v-for="c in KB_COLORS" :key="c" class="swatch" :class="{ on: color === c }"
                    :style="{ background: c }" type="button" @click="color = c" />
            <button class="swatch sw-default" :class="{ on: !color }" type="button"
                    title="默认(按名自动生成)" @click="color = ''">A</button>
          </div>
        </div>
        <details class="kf-adv">
          <summary>高级:修改英文标识(domain key)</summary>
          <input v-model="newKey" class="kf-in" :placeholder="domain" style="margin-top:8px" />
          <p class="kf-hint">改的是 URL/过滤用的英文 key,会<b>迁移该领域下所有内容 / 集合 / 术语</b>;不可与现有领域重名。</p>
          <button class="btn sm danger" type="button"
                  :disabled="!newKey.trim() || newKey.trim() === domain" @click="onRename">
            改标识为 “{{ newKey.trim() || '…' }}”
          </button>
        </details>
      </div>
      <div class="ft">
        <button class="btn" @click="emit('close')">取消</button>
        <button class="btn pri" :disabled="saving" @click="save"><Check :size="14" />保存</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.kf-in{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:var(--r-sm);font-size:14px;background:var(--surface);color:var(--ink-900)}
.kf-in:focus{outline:none;border-color:var(--brand-500)}
.kf-hint{margin:6px 0 0;font-size:11.5px;color:var(--ink-400)}
.sw-default{background:var(--line-soft);color:var(--ink-500);display:grid;place-items:center;font-size:12px;font-weight:600}
.kf-adv{margin-top:16px;border-top:1px solid var(--line);padding-top:12px}
.kf-adv summary{font-size:12.5px;color:var(--ink-500);cursor:pointer;user-select:none}
.kf-adv summary:hover{color:var(--ink-700)}
.kf-adv .btn.danger{margin-top:8px}
</style>
