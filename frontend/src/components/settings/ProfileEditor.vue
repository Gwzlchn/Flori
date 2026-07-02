<script setup lang="ts">
import { ref, onMounted, inject } from 'vue'
import { useApi } from '../../composables/useApi'
import type { ProfileDetail } from '../../types'
import { KB_COLORS } from '../../utils/kbIcons'
import IconPicker from '../common/IconPicker.vue'
import { X, Plus, Trash2, SlidersHorizontal, Check } from 'lucide-vue-next'

const props = defineProps<{ domain: string }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const api = useApi()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast')

const loading = ref(true)
const saving = ref(false)
const profile = ref<ProfileDetail>({ domain: props.domain, role: '', domain_context: '', terminology: [], do_not: [] })
// 展示元数据(profile 上的可选字段,ProfileDetail 未声明 → 单独存,避免改 types)。
const displayName = ref('')
const icon = ref('')
const color = ref('')
const description = ref('')
const newTerm = ref('')
const newDoNot = ref('')

// 图标 / 配色候选(KB_COLORS;与 HomeView 新建弹窗共用单一来源)。图标网格抽为 IconPicker 组件。

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
    // 展示元数据从同一响应读取(后端返回但 ProfileDetail 未声明,转 any 取)。
    const meta = data as any
    displayName.value = meta.display_name ?? ''
    icon.value = meta.icon ?? ''
    color.value = meta.color ?? ''
    description.value = meta.description ?? ''
  } catch (e) {
    showToast?.('加载知识库设定失败', 'error')
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
      display_name: displayName.value.trim(),
      icon: icon.value,
      color: color.value,
      description: description.value.trim(),
      role: profile.value.role,
      domain_context: profile.value.domain_context,
      terminology: profile.value.terminology,
      do_not: profile.value.do_not,
    })
    showToast?.('知识库设定已保存', 'success')
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
  <div class="overlay show" @click.self="emit('close')">
    <div class="modal wide">
      <div class="hd">
        <SlidersHorizontal :size="16" class="lead-ic" /><b>知识库设定 · {{ domain }}</b>
        <button class="ghost" @click="emit('close')"><X :size="16" /></button>
      </div>

      <div v-if="loading" class="bd" style="color:var(--ink-500);font-size:13px;text-align:center;padding:36px 18px">
        加载中…
      </div>

      <div v-else class="bd">
        <!-- 展示名 -->
        <div class="field">
          <label>展示名（display_name）</label>
          <input v-model="displayName" class="input" placeholder="如：机器学习（留空则用标识显示）" />
          <div class="note-tip">影响知识库卡片与工作台头部显示的名字。</div>
        </div>

        <!-- 图标 -->
        <div class="field">
          <label>图标（icon）</label>
          <IconPicker v-model="icon" />
          <div class="note-tip">挑一个 lucide 图标，配色见下。</div>
        </div>

        <!-- 颜色 -->
        <div class="field">
          <label>颜色（color）</label>
          <div class="color-row">
            <button v-for="c in KB_COLORS" :key="c" class="swatch"
              :class="{ on: color === c }" :style="{ background: c }" @click="color = c" />
          </div>
        </div>

        <!-- 简介 -->
        <div class="field">
          <label>简介（description）</label>
          <textarea v-model="description" class="input"
            placeholder="这个知识库你关注什么、希望笔记怎么写…" />
        </div>

        <!-- role -->
        <div class="field">
          <label>角色（role）</label>
          <input v-model="profile.role" class="input" placeholder="如：技术文档编辑" />
        </div>

        <!-- domain_context -->
        <div class="field">
          <label>领域上下文（domain_context）</label>
          <textarea v-model="profile.domain_context" class="input"
            placeholder="如：编程/AI/系统设计相关技术讲解" />
        </div>

        <!-- terminology -->
        <div class="field">
          <label>术语表（terminology）</label>
          <div v-if="(profile.terminology ?? []).length" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px">
            <div v-for="(t, i) in profile.terminology" :key="i"
              style="display:flex;align-items:center;gap:8px">
              <span class="chip" style="flex:1;justify-content:flex-start;word-break:break-all">{{ t }}</span>
              <button class="iconbtn" @click="removeTerm(i)"><Trash2 :size="15" /></button>
            </div>
          </div>
          <form style="display:flex;gap:8px" @submit.prevent="addTerm">
            <input v-model="newTerm" class="input" placeholder="术语: 解释" />
            <button type="submit" class="btn"><Plus :size="16" /></button>
          </form>
        </div>

        <!-- do_not -->
        <div class="field" style="margin-bottom:0">
          <label>禁止事项（do_not）</label>
          <div v-if="(profile.do_not ?? []).length" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px">
            <div v-for="(t, i) in profile.do_not" :key="i"
              style="display:flex;align-items:center;gap:8px">
              <span class="chip" style="flex:1;justify-content:flex-start;word-break:break-all">{{ t }}</span>
              <button class="iconbtn" @click="removeDoNot(i)"><Trash2 :size="15" /></button>
            </div>
          </div>
          <form style="display:flex;gap:8px" @submit.prevent="addDoNot">
            <input v-model="newDoNot" class="input" placeholder="如：不要简化技术细节" />
            <button type="submit" class="btn"><Plus :size="16" /></button>
          </form>
        </div>
      </div>

      <div v-if="!loading" class="ft">
        <button class="btn" @click="emit('close')">取消</button>
        <button class="btn pri" :disabled="saving" @click="save">
          <Check :size="16" />{{ saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>
