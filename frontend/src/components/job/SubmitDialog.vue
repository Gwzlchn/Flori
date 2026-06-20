<script setup lang="ts">
import { watch } from 'vue'
import { useRoute } from 'vue-router'
import { Send, X } from 'lucide-vue-next'
import { useGlobalStore } from '../../stores/global'
import JobSubmitForm from './JobSubmitForm.vue'

// 全局投递内容弹窗:由侧栏/底栏「投递内容」经 global.openSubmit() 打开。
// 投递成功 JobSubmitForm 会 router.push 到内容详情 + emit done → 关闭;路由变动也兜底关闭。
const global = useGlobalStore()
const route = useRoute()
watch(() => route.fullPath, () => { if (global.submitOpen) global.closeSubmit() })
</script>

<template>
  <div v-if="global.submitOpen" class="overlay show" @click.self="global.closeSubmit()">
    <div class="modal">
      <div class="hd">
        <Send :size="16" class="lead-ic" /><b>投递内容</b>
        <button class="ghost" @click="global.closeSubmit()"><X :size="16" /></button>
      </div>
      <div class="bd">
        <JobSubmitForm bare @done="global.closeSubmit()" />
      </div>
    </div>
  </div>
</template>
