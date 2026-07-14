<script setup lang="ts">
import { AlertTriangle, Trash2 } from 'lucide-vue-next'
defineEmits<{ cancel: []; confirm: [] }>()
</script>

<template>
  <div class="overlay show confirm" @click.self="$emit('cancel')">
    <div class="modal">
      <div class="hd"><span class="danger-ic"><AlertTriangle :size="18" /></span><b>删除内容</b></div>
      <div class="bd" style="font-size:13.5px;color:var(--ink-700)">确定删除此内容及所有产物？此操作不可恢复。</div>
      <div class="ft"><button class="btn" @click="$emit('cancel')">取消</button><button class="btn danger" @click="$emit('confirm')"><Trash2 :size="14" />删除</button></div>
    </div>
  </div>
</template>
