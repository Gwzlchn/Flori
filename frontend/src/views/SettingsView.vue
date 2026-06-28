<script setup lang="ts">
// 设置（原型 #settings）：平台认证（B站扫码 + YouTube cookies）+ 运维/关于入口。
// 知识库设定 / Profile 不在此页（已移到工作台）。auth/status 给出各平台是否已配置。
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import BiliLogin from '../components/settings/BiliLogin.vue'
import CookieUpload from '../components/auth/CookieUpload.vue'
import McpConnectCard from '../components/system/McpConnectCard.vue'
import StatusBadge from '../components/common/StatusBadge.vue'
import type { AuthStatus } from '../types'
import { Settings, QrCode, Server, Activity, Info, BookOpen, ChevronRight, Youtube, FileCode2 } from 'lucide-vue-next'

const api = useApi()

const authStatus = ref<AuthStatus | null>(null)
const loading = ref(true)
const error = ref('')

async function loadAuth() {
  loading.value = true
  error.value = ''
  try {
    authStatus.value = await api.get<AuthStatus>('/api/auth/status')
  } catch (e: any) {
    error.value = e?.message || '读取认证状态失败'
  } finally {
    loading.value = false
  }
}

// CookieUpload 上传成功后刷新 youtube 配置状态。
function refreshAuth() {
  api.get<AuthStatus>('/api/auth/status').then(s => { authStatus.value = s }).catch(() => {})
}

onMounted(loadAuth)
</script>

<template>
  <section class="page">
    <div class="h1" style="margin-bottom:20px"><Settings :size="18" />设置</div>

    <!-- 平台认证 -->
    <div class="card pad" style="margin-bottom:18px">
      <div class="card-h"><QrCode :size="15" />平台认证</div>

      <!-- 加载态 -->
      <div v-if="loading" style="color:var(--ink-500);font-size:13px">读取认证状态…</div>

      <!-- 错误态 -->
      <div v-else-if="error"
        style="display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:16px">
        <div style="font-size:13px;color:var(--ink-700)">{{ error }}</div>
        <button class="btn sm" @click="loadAuth">重试</button>
      </div>

      <template v-else>
        <!-- Bilibili：扫码登录走 /api/bili/* 契约，组件自管状态 -->
        <div style="margin-bottom:6px">
          <div class="seclabel" style="margin-bottom:8px"><Activity :size="14" />Bilibili</div>
          <BiliLogin />
        </div>

        <!-- YouTube：上传 cookies.txt -->
        <div style="border-top:1px solid var(--line-soft);margin-top:14px;padding-top:14px">
          <div class="row" style="cursor:default">
            <span class="type-pill" style="background:#fef2f2;color:#dc2626"><Youtube :size="17" /></span>
            <div class="body">
              <div class="title">YouTube</div>
              <div class="meta">
                <StatusBadge :status="authStatus?.youtube.has_cookies ? 'done' : 'pending'" />
                <span class="sep">·</span>
                <span>{{ authStatus?.youtube.has_cookies ? '已配置 cookies' : '需提供登录 cookies 才能下载会员/限制内容' }}</span>
              </div>
            </div>
            <CookieUpload platform="youtube" @success="refreshAuth" />
          </div>
        </div>
      </template>
    </div>

    <!-- 接入 MCP(把知识库作为 MCP 提供给 agent;用户集成,非运维)-->
    <McpConnectCard />

    <!-- Prompt 白盒(查看流水线 + 编辑每步 prompt 覆盖)-->
    <div class="card pad" style="margin-bottom:18px">
      <div class="card-h"><FileCode2 :size="15" />Prompt(白盒)</div>
      <div class="row" style="cursor:pointer" @click="$router.push('/settings/prompts')">
        <span class="type-pill" style="background:var(--brand-50);color:var(--brand-600)"><FileCode2 :size="17" /></span>
        <div class="body">
          <div class="title">流水线 &amp; Prompt</div>
          <div class="meta"><span>查看四条流水线全部步骤,编辑每个 AI 步的 prompt 覆盖(全局/按领域)</span></div>
        </div>
        <ChevronRight :size="16" class="dim" />
      </div>
    </div>

    <!-- 运维 -->
    <div class="card pad" style="margin-bottom:18px">
      <div class="card-h"><Server :size="15" />运维</div>
      <div class="row" style="cursor:pointer" @click="$router.push('/system')">
        <span class="type-pill" style="background:var(--mut-bg);color:var(--ink-600)"><Activity :size="17" /></span>
        <div class="body">
          <div class="title">系统与 Worker</div>
          <div class="meta"><span>查看系统状态、资源池与 Worker</span></div>
        </div>
        <ChevronRight :size="16" class="dim" />
      </div>
    </div>

    <!-- 关于 -->
    <div class="card pad">
      <div class="card-h"><Info :size="15" />关于</div>
      <div class="row" style="cursor:pointer" @click="$router.push('/about')">
        <span class="type-pill" style="background:var(--brand-50);color:var(--brand-600)"><BookOpen :size="17" /></span>
        <div class="body">
          <div class="title">关于 Flori</div>
          <div class="meta"><span>这个项目在做什么、如何使用</span></div>
        </div>
        <ChevronRight :size="16" class="dim" />
      </div>
    </div>
  </section>
</template>
