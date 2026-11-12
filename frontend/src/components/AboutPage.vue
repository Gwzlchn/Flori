<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import { api, bytes, failure, type Schemas } from "../client";

// The three levels and the not-doing list restate docs/scope.md and the vault layout of
// contract 2; those two stay the authority, this page only shows them where the user is.
const levels = [
  { name: "领域", what: "一级分类,投递时必选", folder: "<vault>/<领域>/" },
  { name: "集合", what: "领域内的专题或一个订阅来源,可以不选", folder: "<领域>/<集合>/,没选时是 unsorted/" },
  { name: "笔记", what: "一次成功的任务发布出的自包含文件夹", folder: "<集合>/<日期>-<bili|yt|arxiv>-<来源 ID>/" },
];
const excluded = [
  ["PDF、网页文章、音频、本地视频", "每种来源都是一条要长期维护的抓取与转换链"],
  ["站内搜索", "Obsidian 搜索已覆盖,网页再做一份是两处真相"],
  ["标签、阅读状态、评分的网页管理", "这些属于笔记本身,在 Obsidian 里改"],
  ["术语与概念库、知识图谱、主题页", "需要人工审阅队列,笔记少时跨篇关联很稀"],
  ["站内问答、周报", "外部 agent 直接读 vault 目录就能做"],
  ["SRS 与复习排期", "调度交给 Obsidian 的复习插件"],
  ["MCP", "vault 是普通目录,agent 不需要专门协议"],
  ["AI 评审打分", "分数不驱动任何动作"],
  ["可编辑的设置页、提示词在线编辑", "配置只走环境变量,提示词随代码走"],
  ["多 worker、定时订阅、声明式流水线", "两条固定流水线、单机单人用不上"],
  ["导出命令、静态站生成", "vault 目录已经是导出物"],
  ["vault 备份与快照", "用户改动丢失可接受,机器生成的部分可由任务目录重建"],
];

const info = ref<Schemas["SystemInfo"]>();
const message = ref("");

type Row = { label: string; text: string; pill?: string };

function configured(label: string, value: boolean): Row {
  return { label, text: value ? "已配置" : "未配置", pill: value ? "state-succeeded" : "" };
}

const cards = computed((): { heading: string; rows: Row[] }[] => {
  const system = info.value;
  if (!system) return [];
  const { disk_free_bytes: free, disk_total_bytes: total } = system;
  const disk = free == null || total == null || total <= 0
    ? "unknown"
    : `剩余 ${bytes(free)},共 ${bytes(total)}(已用 ${Math.round((1 - free / total) * 100)}%)`;
  return [
    { heading: "版本与 AI", rows: [
      { label: "版本", text: `v${system.version}` },
      { label: "构建提交", text: system.commit },
      { label: "AI 工具", text: system.ai.tool },
      { label: "笔记模型", text: system.ai.model_note },
      { label: "翻译模型", text: system.ai.model_translate },
      { label: "CLI 版本", text: system.ai.cli_version },
    ] },
    { heading: "来源与工具", rows: [
      configured("B站登录态", system.bili_login),
      configured("代理", system.proxy),
      { label: "yt-dlp 版本", text: system.ytdlp_version },
      { label: "whisper 模型", text: system.whisper_model },
    ] },
    { heading: "存储与内容", rows: [
      { label: "数据目录", text: system.data_dir },
      { label: "vault 名", text: system.vault_name },
      { label: "磁盘", text: disk },
      { label: "领域", text: `${system.domains} 个` },
      { label: "集合", text: `${system.collections} 个` },
      { label: "已发布笔记", text: `${system.notes} 篇` },
    ] },
  ];
});

onMounted(async () => {
  const { data, error, response } = await api.GET("/api/system");
  if (data) info.value = data;
  else message.value = failure(error, response);
});
</script>

<template>
  <div class="stack">
    <section class="card about-text">
      <h1>关于 Flori</h1>
      <p>Flori 是单人自用的学习笔记生成器:把想学的 B站、YouTube 视频和 arXiv 论文变成归自己所有的 Markdown 笔记。它只保证三件事:摄入可靠,笔记易读,文件归你。</p>
      <h2>领域、集合、笔记</h2>
      <p class="meta">分类只有两级,每一级就是 vault 里的一层文件夹。名字建好后不能改;发布后的笔记归你,服务端不再改也不删。</p>
      <div class="scroll">
        <table class="grid">
          <thead><tr><th>层</th><th>是什么</th><th>vault 里的文件夹</th></tr></thead>
          <tbody>
            <tr v-for="level in levels" :key="level.name">
              <th scope="row">{{ level.name }}</th><td>{{ level.what }}</td><td><code>{{ level.folder }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="meta">网页负责投递、订阅、按领域和集合列出笔记、预览与处理过程;阅读、编辑、搜索、标签和复习在 Obsidian 里做。</p>
      <h2>不做什么</h2>
      <ul class="excluded">
        <li v-for="[what, why] in excluded" :key="what"><b>{{ what }}</b><span class="meta">{{ why }}</span></li>
      </ul>
      <p class="meta">每一项重新考虑的条件写在代码仓库的 docs/scope.md,那里是范围的唯一权威。</p>
    </section>

    <section class="card">
      <h2>系统</h2>
      <p v-if="message" class="error">{{ message }}</p>
      <p v-else-if="!info" class="empty">加载中…</p>
      <div v-else class="system">
        <div v-for="card in cards" :key="card.heading">
          <h3>{{ card.heading }}</h3>
          <table class="kv">
            <tbody>
              <tr v-for="row in card.rows" :key="row.label">
                <th scope="row">{{ row.label }}</th>
                <td>
                  <span v-if="row.pill !== undefined" class="pill" :class="row.pill">{{ row.text }}</span>
                  <template v-else>{{ row.text }}</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <h3>近四周投递</h3>
          <table class="kv">
            <tbody>
              <tr v-for="(week, index) in info.weeks" :key="week.start">
                <th scope="row">{{ index === 0 ? "本周" : `${week.start} 起` }}</th>
                <td>
                  <div class="row">
                    <span class="grow">{{ week.submitted }} 次</span>
                    <span class="pill" :class="week.met ? 'state-succeeded' : index === 0 ? 'state-queued' : ''">
                      {{ week.met ? "达标" : index === 0 ? `还差 ${info.weekly_goal - week.submitted} 次` : "未达标" }}
                    </span>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="meta">每周至少投递 {{ info.weekly_goal }} 次算达标,按创建的任务计,不论结果。</p>
        </div>
      </div>
    </section>
  </div>
</template>
