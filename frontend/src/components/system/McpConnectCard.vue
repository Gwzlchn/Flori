<script setup lang="ts">
// 系统页「接入 MCP」卡片:把知识库作为 MCP 提供给 agent。
// 统一走 HTTP(streamable-http + Bearer token):本地连 127.0.0.1:8090/mcp、公网连 <origin>/mcp,仅 URL 不同。
// 工具清单 + token(默认遮掩,点击显示/复制)。
// 信息来自 GET /api/mcp/info(工具实时派生);token 明文经 GET /api/mcp/token 按需取。
import { ref, computed, onMounted, inject } from 'vue'
import { Boxes, Copy, Check, Key, Eye, EyeOff } from 'lucide-vue-next'
import { useApi } from '../../composables/useApi'

interface McpTool { name: string; description: string }
interface McpStats { total: number; by_tool: Record<string, number> }
interface McpInfo {
  enabled: boolean
  http_path: string
  local_url: string
  token_configured: boolean
  tools: McpTool[]
  stats?: McpStats
}

const api = useApi()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

const info = ref<McpInfo | null>(null)
const loading = ref(true)
const activeTab = ref<'local' | 'http'>('local')
const revealed = ref<string | null>(null) // 显示后的 token 明文(null=遮掩)

onMounted(async () => {
  try {
    info.value = await api.get<McpInfo>('/api/mcp/info')
  } catch {
    /* 非致命:卡片只读,失败则不渲染内容 */
  } finally {
    loading.value = false
  }
})

const endpoint = computed(() => {
  const origin = typeof window !== 'undefined' ? window.location?.origin : ''
  const base = origin && origin.startsWith('http') ? origin : 'https://<FLORI_HOST>'
  return base + (info.value?.http_path || '/mcp')
})
// 本地端点(同机直连 mcp-http);当前 tab 决定用本地还是公网 URL。
const localEndpoint = computed(() => info.value?.local_url || 'http://127.0.0.1:8090/mcp')
const curEndpoint = computed(() => (activeTab.value === 'local' ? localEndpoint.value : endpoint.value))
const tokenShown = computed(() => revealed.value || '<TOKEN>')

// 调用统计:总调用 + 按工具(取调用过的、按次数降序的前几条;subtle)。
const statsTotal = computed(() => info.value?.stats?.total ?? 0)
const statsByTool = computed(() =>
  Object.entries(info.value?.stats?.by_tool || {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1]),
)

const httpAddCmd = computed(
  () => `claude mcp add --transport http flori ${curEndpoint.value} --header "Authorization: Bearer ${tokenShown.value}"`,
)
const curlCmd = computed(
  () => `curl -X POST ${curEndpoint.value} \\
  -H "Authorization: Bearer ${tokenShown.value}" \\
  -H "Accept: application/json, text/event-stream" \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"c","version":"1"}}}'`,
)

const copied = ref('')
async function copy(text: string, which: string) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = which
    setTimeout(() => (copied.value = ''), 1600)
  } catch {
    showToast('复制失败', 'error')
  }
}

async function toggleReveal() {
  if (revealed.value) {
    revealed.value = null // 再点=隐藏
    return
  }
  try {
    const r = await api.get<{ token: string | null }>('/api/mcp/token')
    if (!r.token) {
      showToast('未配置 token', 'error')
      return
    }
    revealed.value = r.token
    await navigator.clipboard.writeText(r.token).catch(() => {})
    showToast('token 已显示并复制(敏感,勿外传)', 'success')
  } catch {
    showToast('获取 token 失败', 'error')
  }
}
</script>

<template>
  <details class="card pad" style="margin-bottom:18px">
    <summary class="card-h" style="margin-bottom:0;cursor:pointer;list-style:none">
      <Boxes :size="15" />接入 MCP
      <span class="dim" style="font-weight:400;font-size:12px;margin-left:6px">把知识库作为 MCP 提供给 agent</span>
    </summary>

    <p v-if="loading" class="note-tip" style="margin:12px 0 0">加载中…</p>
    <template v-else-if="info">
      <div class="seg" style="margin:12px 0">
        <button :class="{ on: activeTab === 'local' }" @click="activeTab = 'local'">本地</button>
        <button :class="{ on: activeTab === 'http' }" @click="activeTab = 'http'">公网</button>
      </div>

      <p class="note-tip" style="margin:0 0 8px">
        <template v-if="activeTab === 'local'">同机 agent(如本机 Claude Code)直连本机 MCP 服务。</template>
        <template v-else>外网 agent 经公网域名接入(Caddy 反代 + Bearer)。</template>
        端点 <code class="mono">{{ curEndpoint }}</code>(streamable-http,需 Bearer token)。把 &lt;TOKEN&gt; 换成下方真实 token(或先点「显示/复制」自动带入)。
      </p>
      <pre class="mcp-snip">{{ httpAddCmd }}</pre>
      <button class="btn sm" style="margin-top:10px" @click="copy(httpAddCmd, 'add')">
        <component :is="copied === 'add' ? Check : Copy" :size="13" />{{ copied === 'add' ? '已复制' : '复制' }}
      </button>
      <p class="note-tip" style="margin:12px 0 8px">原始 curl(initialize 握手):</p>
      <pre class="mcp-snip">{{ curlCmd }}</pre>
      <button class="btn sm" style="margin-top:10px" @click="copy(curlCmd, 'curl')">
        <component :is="copied === 'curl' ? Check : Copy" :size="13" />{{ copied === 'curl' ? '已复制' : '复制' }}
      </button>

      <div style="margin-top:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <Key :size="14" /><span style="font-size:13px">Bearer token</span>
        <template v-if="info.token_configured">
          <code class="mono" style="font-size:12px">{{ revealed || '••••••••••••' }}</code>
          <button class="btn sm" @click="toggleReveal">
            <component :is="revealed ? EyeOff : Eye" :size="13" />{{ revealed ? '隐藏' : '显示/复制' }}
          </button>
          <span class="dim" style="font-size:11px">敏感,勿外传(LAN :8080 无鉴权)</span>
        </template>
        <span v-else class="dim" style="font-size:12px">未配置:在 NAS .env 设 FLORI_MCP_TOKEN</span>
      </div>

      <div style="margin-top:14px">
        <div class="dim" style="font-size:12px;margin-bottom:6px">工具({{ info.tools.length }})</div>
        <div v-for="t in info.tools" :key="t.name" style="font-size:12.5px;margin-bottom:4px;line-height:1.5">
          <code class="mono">{{ t.name }}</code>
          <span style="color:var(--ink-600)"> — {{ t.description }}</span>
        </div>
      </div>

      <div class="dim" style="margin-top:12px;font-size:12px;line-height:1.6">
        调用统计:总调用 {{ statsTotal }}
        <span v-if="statsByTool.length">
          ·
          <span v-for="([name, n], i) in statsByTool" :key="name">
            <code class="mono">{{ name }}</code> {{ n }}<span v-if="i < statsByTool.length - 1"> · </span>
          </span>
        </span>
      </div>
    </template>
  </details>
</template>

<style scoped>
.mcp-snip {
  background: var(--ink-900);
  color: #cbd5e1;
  font-family: var(--mono);
  font-size: 12px;
  padding: 12px;
  border-radius: var(--r-sm);
  overflow: auto;
  line-height: 1.7;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
