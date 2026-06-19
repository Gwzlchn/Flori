<!-- 设置（原型 id="settings"）：平台认证 + 运维 + 关于入口。占位实现。 -->
<script setup lang="ts">
import { useRouter } from 'vue-router'
import {
  Settings,
  QrCode,
  Play,
  Youtube,
  Check,
  LogOut,
  Upload,
  Server,
  Activity,
  ChevronRight,
  Info,
  BookOpen,
} from 'lucide-vue-next'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'

const router = useRouter()
</script>

<template>
  <section class="page">
    <div class="h1" style="margin-bottom: 20px"><Settings :size="18" />设置</div>

    <!-- 平台认证 -->
    <div class="card pad" style="margin-bottom: 18px">
      <div class="card-h"><QrCode :size="15" />平台认证</div>
      <div class="list">
        <div class="row" style="cursor: default">
          <span class="type-pill" style="background: #fb7299; color: #fff"><Play :size="17" /></span>
          <div class="body">
            <div class="title">Bilibili</div>
            <div class="meta">
              <BaseBadge variant="ok"><Check :size="12" />已登录</BaseBadge><span>·</span><span>李同学</span>
            </div>
          </div>
          <!-- TODO: POST /api/auth/bilibili/logout -->
          <BaseButton size="sm" variant="danger"><LogOut :size="13" />注销</BaseButton>
        </div>
        <div class="row" style="cursor: default">
          <span class="type-pill" style="background: #fef2f2; color: #dc2626"><Youtube :size="17" /></span>
          <div class="body">
            <div class="title">YouTube</div>
            <div class="meta">
              <BaseBadge variant="warn">未配置</BaseBadge><span>·</span>
              <span>需提供登录 cookies 才能下载会员/限制内容</span>
            </div>
          </div>
          <!-- TODO: POST /api/auth/youtube/cookies -->
          <BaseButton size="sm"><Upload :size="13" />上传 cookies.txt</BaseButton>
        </div>
      </div>
    </div>

    <!-- 运维 -->
    <div class="card pad" style="margin-bottom: 18px">
      <div class="card-h"><Server :size="15" />运维</div>
      <div class="row" @click="router.push('/system')">
        <span class="type-pill" style="background: var(--mut-bg); color: var(--ink-600)"><Activity :size="17" /></span>
        <div class="body">
          <div class="title">Worker 监控</div>
          <div class="meta"><span>查看系统与 Worker 状态</span></div>
        </div>
        <ChevronRight :size="16" class="dim" />
      </div>
    </div>

    <!-- 关于 -->
    <div class="card pad">
      <div class="card-h"><Info :size="15" />关于</div>
      <div class="row" @click="router.push('/about')">
        <span class="type-pill" style="background: var(--brand-50); color: var(--brand-600)"><BookOpen :size="17" /></span>
        <div class="body">
          <div class="title">关于 Mnemo</div>
          <div class="meta"><span>这个项目在做什么、如何使用</span></div>
        </div>
        <ChevronRight :size="16" class="dim" />
      </div>
    </div>
  </section>
</template>
