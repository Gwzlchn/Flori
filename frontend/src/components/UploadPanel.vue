<script setup lang="ts">
defineProps<{
  busy: boolean;
  collectionName: string | undefined;
  domainName: string;
  file: File | undefined;
  ready: boolean;
}>();
defineEmits<{ choose: [event: Event]; submit: [] }>();
</script>

<template>
  <details
    id="upload"
    class="upload-card card"
    open
  >
    <summary>
      <span><b>上传并解析 PDF</b><small>投递到 {{ domainName }} / {{ collectionName ?? "未归分类" }}</small></span>
      <span class="summary-action">选择文件</span>
    </summary>
    <div class="upload-body">
      <label
        class="file-drop"
        for="pdf-file"
      >
        <span class="file-icon">PDF</span>
        <span><b>{{ file?.name ?? "选择数字版 PDF" }}</b><small>浏览器先计算 SHA-256，再安全上传</small></span>
      </label>
      <input
        id="pdf-file"
        class="visually-hidden"
        type="file"
        accept="application/pdf,.pdf"
        :disabled="busy"
        @change="$emit('choose', $event)"
      >
      <button
        type="button"
        class="btn primary"
        :disabled="!file || !ready || busy"
        @click="$emit('submit')"
      >
        {{ busy ? "处理中…" : "开始解析" }}
      </button>
    </div>
  </details>
</template>
