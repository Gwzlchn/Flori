<script setup lang="ts">
import { ref, computed, inject } from 'vue'
import { Copy, Check, KeyRound, RotateCw } from 'lucide-vue-next'
import { useWorkerStore } from '../../stores/workers'

const workerStore = useWorkerStore()
const showToast = inject<(msg: string, type: 'success' | 'error' | 'info') => void>('showToast')

const IMAGE = 'ghcr.io/gwzlchn/mnemo:latest'
const types = ['cpu', 'gpu', 'ai', 'download']
const tabs = [
  { id: 'gateway', label: '分布式 (P1)' },
  { id: 'docker', label: 'docker run' },
  { id: 'compose', label: 'compose' },
] as const

const workerType = ref('cpu')
const tagsInput = ref('')
const rejectInput = ref('')
const activeTab = ref<(typeof tabs)[number]['id']>('docker')

const token = ref('')
const minting = ref(false)
const copiedToken = ref(false)
const copiedCmd = ref(false)

// ai/gpu 才触发 vision/claude-cli 标签，需要 AI key；其余类型不下发密钥行。
const needsAiKey = computed(() => workerType.value === 'ai' || workerType.value === 'gpu')

const tagsArg = computed(() => {
  const tags = tagsInput.value.split(/[\s,]+/).filter(Boolean)
  return tags.length ? ` --tags ${tags.join(' ')}` : ''
})
const rejectArg = computed(() => {
  const tags = rejectInput.value.split(/[\s,]+/).filter(Boolean)
  return tags.length ? ` --reject-tags ${tags.join(' ')}` : ''
})
const runCmd = computed(() => `python -m worker.main --type ${workerType.value}${tagsArg.value}${rejectArg.value}`)
const tokenLine = computed(() => token.value || 'mnw-<铸取后填入>')

async function mint() {
  minting.value = true
  try {
    token.value = await workerStore.mintToken()
    showToast?.('已铸取接入 token (仅此一次完整展示)', 'success')
  } catch {
    showToast?.('铸取失败', 'error')
  } finally {
    minting.value = false
  }
}

const command = computed(() => {
  if (activeTab.value === 'gateway') {
    // 出站接入：注册/心跳走 gateway；认领/产物 P1 仍直连 minio。WORKER_ID_FILE 持久化身份,重启复用同一 id。
    const aiLine = needsAiKey.value
      ? '  -e ANTHROPIC_API_KEY=<KEY> -e DEEPSEEK_API_KEY=<KEY> \\\n'
      : ''
    return `docker run -d --restart unless-stopped \\
  -e GATEWAY_URL=https://<MNEMO_HOST> \\
  -e WORKER_REGISTRATION_TOKEN=${tokenLine.value} \\
  -e WORKER_ID_FILE=/data/.worker_id \\
  -e DATA_DIR=/data -e CONFIG_DIR=/app/configs -e WORK_DIR=/tmp/mnemo-work \\
  -e MINIO_URL=<HOST>:9000 -e MINIO_ACCESS_KEY=<KEY> -e MINIO_SECRET_KEY=<SECRET> -e MINIO_BUCKET=mnemo \\
${aiLine}  -v mnemo-data:/data \\
  ${IMAGE} \\
  ${runCmd.value}`
  }

  if (activeTab.value === 'compose') {
    const aiLines = needsAiKey.value
      ? '      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}\n      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}\n'
      : ''
    return `# 追加到 docker-compose.yml services:
  worker-${workerType.value}-extra:
    image: ${IMAGE}
    restart: unless-stopped
    command: ${runCmd.value}
    volumes: [ "\${MNEMO_DATA_DIR:-mnemo-data}:/data" ]
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATA_DIR=/data
      - CONFIG_DIR=/app/configs
      - WORK_DIR=\${WORK_DIR:-/tmp/mnemo-work}
      - MINIO_URL=\${MINIO_URL:-}
      - MINIO_BUCKET=\${MINIO_BUCKET:-mnemo}
${aiLines}      - HTTPS_PROXY=\${HTTPS_PROXY:-}
    security_opt: [ "no-new-privileges:true" ]
    depends_on: [ redis ]`
  }

  // docker run：单机直连 redis/minio。
  const aiLine = needsAiKey.value
    ? '  -e ANTHROPIC_API_KEY=<KEY> -e DEEPSEEK_API_KEY=<KEY> \\\n'
    : ''
  return `docker run -d --restart unless-stopped \\
  -e REDIS_URL=redis://<HOST>:6379/0 \\
  -e MINIO_URL=<HOST>:9000 -e MINIO_ACCESS_KEY=<KEY> -e MINIO_SECRET_KEY=<SECRET> -e MINIO_BUCKET=mnemo \\
  -e DATA_DIR=/data -e CONFIG_DIR=/app/configs -e WORK_DIR=/tmp/mnemo-work \\
${aiLine}  -v mnemo-data:/data \\
  ${IMAGE} \\
  ${runCmd.value}`
})

async function copy(text: string, which: 'token' | 'cmd') {
  try {
    await navigator.clipboard.writeText(text)
    if (which === 'token') {
      copiedToken.value = true
      setTimeout(() => { copiedToken.value = false }, 2000)
    } else {
      copiedCmd.value = true
      setTimeout(() => { copiedCmd.value = false }, 2000)
    }
    showToast?.('已复制', 'success')
  } catch {
    showToast?.('复制失败', 'error')
  }
}
</script>

<template>
  <div class="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
    <h4 class="text-sm font-semibold text-gray-700">接入新 Worker</h4>

    <!-- 表单 -->
    <div class="flex flex-wrap items-end gap-3">
      <label class="text-xs text-gray-600 flex flex-col gap-1">
        类型
        <select v-model="workerType" class="px-2 py-1 border border-gray-300 rounded text-sm bg-white">
          <option v-for="t in types" :key="t" :value="t">{{ t.toUpperCase() }}</option>
        </select>
      </label>
      <label class="text-xs text-gray-600 flex flex-col gap-1 flex-1 min-w-[8rem]">
        标签 (可选, 空=自动探测)
        <input v-model="tagsInput" class="px-2 py-1 border border-gray-300 rounded text-sm" placeholder="vision claude-cli" />
      </label>
      <label class="text-xs text-gray-600 flex flex-col gap-1 flex-1 min-w-[8rem]">
        拒绝标签 (可选)
        <input v-model="rejectInput" class="px-2 py-1 border border-gray-300 rounded text-sm" placeholder="private" />
      </label>
    </div>

    <!-- token -->
    <div class="flex items-center gap-2">
      <button
        @click="mint"
        :disabled="minting"
        class="flex items-center gap-1 px-3 py-1.5 bg-indigo-600 text-white text-xs rounded-lg hover:bg-indigo-700 transition-colors disabled:opacity-50"
      >
        <component :is="token ? RotateCw : KeyRound" :size="14" />
        {{ token ? '重铸 token' : '铸取接入 token' }}
      </button>
      <div v-if="token" class="flex-1 flex items-center gap-2 min-w-0">
        <code class="flex-1 px-2 py-1 bg-gray-100 rounded text-xs font-mono truncate">{{ token }}</code>
        <button @click="copy(token, 'token')" class="p-1 text-gray-500 hover:text-gray-700">
          <component :is="copiedToken ? Check : Copy" :size="14" />
        </button>
      </div>
      <span v-else class="text-xs text-gray-400">铸取后仅此一次完整展示，妥善保存</span>
    </div>

    <!-- Tabs -->
    <div class="flex gap-1 border-b border-gray-200">
      <button
        v-for="t in tabs"
        :key="t.id"
        @click="activeTab = t.id"
        class="px-3 py-1.5 text-xs -mb-px border-b-2 transition-colors"
        :class="activeTab === t.id ? 'border-blue-600 text-blue-600 font-medium' : 'border-transparent text-gray-500 hover:text-gray-700'"
      >{{ t.label }}</button>
    </div>

    <p v-if="activeTab === 'gateway'" class="text-xs text-amber-600">
      出站接入模式：注册 / 心跳已走 gateway (worker 不暴露入站端口)；认领与产物 P1 仍直连 minio。
    </p>

    <div class="bg-gray-900 text-green-400 rounded-lg p-3 text-xs font-mono whitespace-pre-wrap break-all">{{ command }}</div>

    <div class="flex justify-end">
      <button @click="copy(command, 'cmd')" class="flex items-center gap-1 px-3 py-1.5 bg-blue-600 text-white text-xs rounded-lg hover:bg-blue-700 transition-colors">
        <component :is="copiedCmd ? Check : Copy" :size="14" />
        {{ copiedCmd ? '已复制' : '复制命令' }}
      </button>
    </div>
  </div>
</template>
