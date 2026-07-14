<script setup lang="ts">
import { Check, ChevronRight, Copy, Key, Plus } from 'lucide-vue-next'

type Option = { id: string; label: string }
defineProps<{
  open: boolean; workerTypes: string[]; outputModes: readonly Option[]; aiAccessMethods: readonly Option[]
  nameBase: string; workerName: string; registrationCode: string; tokenTtlText: string; minting: boolean
  copiedRegistrationCode: boolean; copiedCommand: boolean; command: string; commandTitle: string; commandCopyLabel: string; gatewayUrl: string
}>()
const selectedPools = defineModel<string[]>('selectedPools', { required: true })
const workerNameDraft = defineModel<string>('workerNameDraft', { required: true })
const nameTouched = defineModel<boolean>('nameTouched', { required: true })
const newTags = defineModel<string>('newTags', { required: true })
const workerStateDir = defineModel<string>('workerStateDir', { required: true })
const stateDirTouched = defineModel<boolean>('stateDirTouched', { required: true })
const outputMode = defineModel<string>('outputMode', { required: true })
const watchtowerEnabled = defineModel<boolean>('watchtowerEnabled', { required: true })
const watchtowerInterval = defineModel<number>('watchtowerInterval', { required: true })
const aiAccessMethod = defineModel<string>('aiAccessMethod', { required: true })
defineEmits<{ toggle: [event: Event]; mint: []; copy: [text: string, target: 'token' | 'cmd'] }>()
</script>

<template>
  <details class="card pad worker-enroll" :open="open" @toggle="$emit('toggle', $event)">
    <summary class="card-h enroll-summary"><span><Plus :size="15" />接入新 Worker</span></summary>
    <div class="enroll-panel"><div class="enroll-flow">
      <section class="enroll-step-card step-capabilities">
        <details class="step-advanced"><summary class="step-head"><div class="step-head-main"><span class="step-title"><span class="step-dot">1</span>选择能力</span><span class="enroll-hint">{{ selectedPools.length ? [...selectedPools].sort().join(' / ') : '至少选一个' }}</span></div><span class="advanced-toggle">高级选项<ChevronRight class="summary-chevron" :size="14" /></span></summary>
          <div class="advanced-grid">
            <div class="field"><label>Worker 名称</label><input v-model="workerNameDraft" class="input" :placeholder="`${nameBase}-1`" @input="nameTouched = true" /></div>
            <div class="field"><label>标签</label><input v-model="newTags" class="input" placeholder="home-desktop vision" /></div>
            <div class="field"><label>状态目录</label><input v-model="workerStateDir" class="input" :placeholder="`./flori-worker-state/${workerName}`" @input="stateDirTouched = true" /></div>
            <div class="field"><label>部署形式</label><div class="seg advanced-seg"><button v-for="mode in outputModes" :key="mode.id" :class="{ on: outputMode === mode.id }" @click="outputMode = mode.id">{{ mode.label }}</button></div></div>
            <div class="field"><label>自动更新</label><div class="seg advanced-seg watchtower-mode"><input v-model="watchtowerEnabled" data-testid="watchtower-enabled" type="checkbox" /><button :class="{ on: watchtowerEnabled }" @click="watchtowerEnabled = true">开 Watchtower</button><button :class="{ on: !watchtowerEnabled }" @click="watchtowerEnabled = false">关 Watchtower</button></div></div>
            <div class="field"><label>更新间隔</label><div class="inline-number"><input v-model.number="watchtowerInterval" data-testid="watchtower-interval" type="number" min="1" class="input" :disabled="!watchtowerEnabled" /><span>秒</span></div></div>
          </div>
          <p class="note-tip">Watchtower 会挂载 Docker socket。自签证书部署时可加 <code>GATEWAY_TLS_INSECURE=1</code> 或 <code>GATEWAY_CA_BUNDLE</code>。</p>
        </details>
        <div class="pool-picker"><label v-for="type in workerTypes" :key="type" :class="{ on: selectedPools.includes(type) }"><input v-model="selectedPools" type="checkbox" :value="type" /><span>{{ type }}</span></label></div>
        <div v-if="selectedPools.includes('ai')" class="inline-field"><span>AI 接入方式</span><select v-model="aiAccessMethod" class="input" data-testid="ai-access-method"><option v-for="method in aiAccessMethods" :key="method.id" :value="method.id">{{ method.label }}</option></select></div>
        <p v-if="selectedPools.includes('ai')" class="note-tip"><template v-if="aiAccessMethod === 'claude-cli'">状态目录内使用 .claude。</template><template v-else-if="aiAccessMethod === 'codex-cli'">状态目录内使用 .codex。</template><template v-else>KIMI_API_KEY 从环境变量注入。</template></p>
      </section>
      <section class="enroll-step-card token-step"><div class="token-strip"><div class="step-head-main"><span class="step-title"><span class="step-dot">2</span>生成 token</span><span class="enroll-hint">{{ registrationCode ? `有效期 ${tokenTtlText || '已生成'}` : '首次注册用' }}</span></div><div v-if="registrationCode" class="token-row"><code class="mono">{{ registrationCode }}</code><button class="iconbtn" @click="$emit('copy', registrationCode, 'token')"><component :is="copiedRegistrationCode ? Check : Copy" :size="15" /></button></div><button class="btn pri enroll-main-action" :class="{ compact: registrationCode }" :disabled="minting" @click="$emit('mint')"><Key :size="14" />{{ registrationCode ? '重生成' : '生成 token' }}</button></div></section>
      <section class="enroll-step-card deploy-box"><details class="deploy-details"><summary class="step-head deploy-head"><div class="step-head-main"><span class="step-title"><span class="step-dot">3</span>复制部署文件</span><span class="enroll-hint">{{ selectedPools.length ? `能力 ${[...selectedPools].sort().join(' + ')}` : '未选择能力' }} · {{ commandTitle }} · Gateway {{ gatewayUrl }}</span></div><div class="deploy-actions"><button class="btn sm" @click.stop.prevent="$emit('copy', command, 'cmd')"><component :is="copiedCommand ? Check : Copy" :size="13" />{{ copiedCommand ? '已复制' : commandCopyLabel }}</button><ChevronRight class="summary-chevron" :size="16" /></div></summary><pre>{{ command }}</pre></details></section>
    </div></div>
  </details>
</template>

<style scoped>
summary::-webkit-details-marker { display: none; }.seg button:disabled { opacity: .45; cursor: not-allowed; }
.worker-enroll { margin-bottom: 18px; scroll-margin-top: 72px; }.enroll-summary { margin-bottom: 0; cursor: pointer; list-style: none; justify-content: flex-start; gap: 12px; }.enroll-summary > span { display: inline-flex; align-items: center; gap: 7px; }
.enroll-panel { margin-top: 12px; }.enroll-flow { display: flex; flex-direction: column; gap: 10px; }.enroll-step-card { min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--surface); }
.step-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; color: var(--ink-800); cursor: pointer; list-style: none; }.step-head-main { display: flex; align-items: center; gap: 10px; min-width: 0; }.step-title { display: inline-flex; align-items: center; gap: 7px; min-width: 0; font-size: 13px; font-weight: 700; }.step-dot { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; flex: none; border-radius: 50%; background: var(--brand-50); color: var(--brand-700); font-size: 11px; font-weight: 700; }.enroll-hint { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11.5px; font-weight: 500; color: var(--ink-500); }
.pool-picker { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }.pool-picker label { position: relative; display: flex; align-items: center; justify-content: center; min-height: 34px; border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--ink-600); font-size: 12.5px; font-weight: 700; cursor: pointer; user-select: none; }.pool-picker label.on { border-color: var(--brand-300); background: var(--brand-50); color: var(--brand-700); }.pool-picker input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.inline-field { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 10px; margin-top: 12px; }.inline-field > span { font-size: 12px; color: var(--ink-500); white-space: nowrap; }.inline-field .input { padding: 6px 9px; font-size: 12px; }
.token-step { padding: 11px 14px; }.token-strip { display: grid; grid-template-columns: minmax(180px, auto) minmax(0, 1fr) auto; align-items: center; gap: 12px; }.enroll-main-action { justify-content: center; min-width: 180px; min-height: 36px; }.enroll-main-action.compact { width: 112px; min-width: 112px; }.token-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; align-items: center; min-width: 0; }.token-row code { min-width: 0; padding: 7px 9px; border-radius: var(--r-sm); background: var(--line-soft); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step-advanced > summary { margin-bottom: 12px; }.advanced-toggle { display: inline-flex; align-items: center; gap: 5px; flex: none; padding: 5px 8px; border: 1px solid var(--line-soft); border-radius: var(--r-sm); color: var(--ink-600); font-size: 12px; font-weight: 700; }.summary-chevron { flex: none; transition: transform .16s ease; }details[open] > summary .summary-chevron { transform: rotate(90deg); }
.advanced-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 2px 0 12px; align-items: end; }.advanced-grid .field { display: flex; flex-direction: column; justify-content: flex-end; gap: 6px; margin: 0; }.advanced-grid .field > label { margin: 0 !important; min-height: 16px; }.advanced-grid .input { min-height: 34px; padding: 7px 9px; font-size: 12px; }.advanced-grid .advanced-seg { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); width: 100%; min-height: 34px; }.advanced-grid .advanced-seg button { display: inline-flex; align-items: center; justify-content: center; min-height: 28px; padding: 5px 10px; white-space: nowrap; }.watchtower-mode { position: relative; }.watchtower-mode input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }.inline-number { display: flex; align-items: center; gap: 8px; }.inline-number .input { flex: 1; min-width: 0; width: auto; }.inline-number span { font-size: 12px; color: var(--ink-500); }.step-advanced .note-tip { margin: -2px 0 12px; line-height: 1.6; }
.deploy-box { min-width: 0; overflow: visible; }.deploy-details:not([open]) > .deploy-head { margin-bottom: 0; }.deploy-head > div:first-child { min-width: 0; }.deploy-actions { display: inline-flex; align-items: center; gap: 8px; flex: none; }.deploy-box pre { margin: 0; max-height: 360px; padding: 12px; overflow: auto; border-radius: var(--r-sm); background: var(--ink-900); color: #cbd5e1; font-family: var(--mono); font-size: 12px; line-height: 1.65; white-space: pre; word-break: normal; }
@media (max-width: 900px) { .advanced-grid { grid-template-columns: 1fr; }.pool-picker { grid-template-columns: repeat(2, minmax(0, 1fr)); }.token-strip { grid-template-columns: 1fr; }.enroll-main-action { width: 100%; min-width: 0; }.deploy-box pre { white-space: pre-wrap; word-break: break-word; }.deploy-head { align-items: stretch; flex-direction: column; }.deploy-head .step-head-main { align-items: flex-start; flex-direction: column; gap: 6px; }.deploy-actions { justify-content: space-between; } }
@media (max-width: 560px) { .enroll-summary,.step-head { align-items: stretch; flex-direction: column; }.step-head-main { align-items: flex-start; flex-direction: column; gap: 6px; }.advanced-toggle { align-self: flex-end; }.deploy-head span { white-space: normal; }.deploy-actions { align-items: stretch; flex-direction: row; } }
</style>
