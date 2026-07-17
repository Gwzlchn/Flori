<script setup lang="ts">
// 关于 Flori(原型 #about):三层心智模型 + 真实能力概览。
// 内容流水线从 /api/pipelines 动态拉取,服务端 metadata 是名称与适用范围的单一事实源.
import { ref, onMounted } from 'vue'
import { useWorkerStore } from '../stores/workers'
import { contentTypeIcon, contentTypePill, documentKindLabel } from '../utils/contentType'
import { ensureSourceCatalog, SOURCE_PROFILE_LABELS } from '../constants/sources'
import {
  BookOpen, Info, GitBranch, RefreshCw, FileText, Check, Send,
  Search, Layers, Cpu, ChevronRight,
  Sparkles, Network, Tag, Server, Rss, Library, Database, Star,
} from 'lucide-vue-next'

interface PipeStep { key: string; label: string | null; pool: string | null; needs: string[] }
interface Pipeline {
  name: string
  key: string
  label: string
  content_types: string[]
  document_kinds: string[]
  source_profiles: string[]
  steps: PipeStep[]
}

const store = useWorkerStore()
const pipelines = ref<Pipeline[]>([])
onMounted(async () => {
  try {
    await ensureSourceCatalog()
    pipelines.value = await store.fetchPipelines() as Pipeline[]
  } catch { /* 非致命:留空,显加载态 */ }
})

function pipelineContentType(pipeline: Pipeline): string {
  return pipeline.content_types[0] || pipeline.key
}
function sourceProfileLabel(profile: string): string {
  return SOURCE_PROFILE_LABELS[profile] || profile
}
// 步骤徽章配色(复用既有 badge 语义):评审→绿、AI 步→蓝、其余→灰。
function stepBadge(s: PipeStep): string {
  if ((s.key || '').toLowerCase().includes('review') || (s.label || '').includes('评审')) return 'b-ok'
  if (s.pool === 'ai') return 'b-info'
  return 'b-mut'
}
</script>

<template>
  <section class="page">
    <div class="h1"><BookOpen :size="18" />关于 Flori</div>
    <div class="lead">自托管的 AI 学习知识库 —— 把视频、文档和音频自动炼成结构化笔记，沉淀为按领域分桶、可检索、互相关联的个人知识体系。</div>
    <div style="margin-top:10px;color:var(--ink-600);font-size:13px">名字来源：Flori 取自拉丁语 <i>florilegium</i>（“采花集”）——中世纪指从群书中采撷精华、汇编成册的选集，正是“把素材摘录、沉淀为知识”的隐喻。</div>

    <!-- 这是什么 -->
    <div class="card pad" style="margin-top:18px">
      <div class="card-h"><Info :size="15" />这是什么</div>
      <p style="color:var(--ink-700)">
        投递一条链接或一个文件，Flori 会按内容类型选择下载、解析、转写、截图或 OCR，再用 AI 整理成笔记。不同类型的原始材料不同，但都保留可核对的来源并生成结构化智能笔记：
      </p>
      <div class="grid2" style="margin-top:11px">
        <div class="metric">
          <div style="display:flex;align-items:center;gap:7px;color:var(--ink-900);font-weight:600;font-size:13.5px">
            <FileText :size="15" class="dim" />原始 / 机械材料
          </div>
          <div class="l" style="margin-top:5px">视频保留逐字稿、关键帧、OCR 与弹幕；文档保留原生 HTML 或 PDF、结构与定位；音频保留转写，均可回到来源核对。</div>
        </div>
        <div class="metric">
          <div style="display:flex;align-items:center;gap:7px;color:var(--ink-900);font-weight:600;font-size:13.5px">
            <Sparkles :size="15" class="dim" />智能版
          </div>
          <div class="l" style="margin-top:5px">AI 按主题重组的结构化讲解，含术语解释与要点回顾，便于阅读理解。</div>
        </div>
      </div>
      <p style="color:var(--ink-700);margin-top:11px">
        这些笔记按知识范围归入各个<b>领域知识库</b>，并通过评审里抽出的<b>概念</b>互相关联，逐渐织成一张属于你自己的知识网。
      </p>
    </div>

    <!-- 核心循环 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><GitBranch :size="15" />核心循环</div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="badge b-brand"><Send :size="12" />投递 URL/文件</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-info"><RefreshCw :size="12" />流水线自动处理</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-mut"><FileText :size="12" />读智能笔记</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-ok"><Check :size="12" />采纳概念</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-brand"><Network :size="12" />连成概念图</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-mut"><Search :size="12" />全文搜索回溯</span>
      </div>
      <div class="note-tip" style="margin-top:11px">采纳的概念会回流到该领域的 Prompt Profile，让后续 AI 笔记的措辞逐步统一。</div>
    </div>

    <!-- 内容处理流水线 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><RefreshCw :size="15" />内容处理流水线</div>
      <p class="note-tip" style="margin-top:-4px;margin-bottom:13px">Video、Document、Audio 共用统一执行约定；步骤间以文件通信，输入指纹未变则跳过（幂等）。</p>
      <div class="list">
        <div v-for="p in pipelines" :key="p.key" class="row" style="cursor:default;align-items:flex-start">
          <span class="type-pill" :class="contentTypePill(pipelineContentType(p))"><component :is="contentTypeIcon(pipelineContentType(p))" :size="17" /></span>
          <div class="body">
            <div class="title">{{ p.label }}<span style="font-weight:400;color:var(--ink-400);margin-left:7px;font-size:12px">{{ p.steps.length }} 步</span></div>
            <div v-if="p.key === 'document'" class="pipeline-scope">
              <span>{{ p.document_kinds.map(documentKindLabel).join('、') }}</span>
              <span>{{ p.source_profiles.map(sourceProfileLabel).join('、') }}</span>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">
              <span v-for="s in p.steps" :key="s.key" class="badge" :class="stepBadge(s)">{{ s.label || s.key }}</span>
            </div>
          </div>
        </div>
        <div v-if="!pipelines.length" class="note-tip" style="margin:0">流水线信息加载中…</div>
      </div>
      <div class="note-tip" style="margin-top:11px">Document 中论文、文章、白皮书等体裁决定展示与 Prompt profile；HTML、数字 PDF、扫描 PDF 等来源能力决定 adapter。</div>
    </div>

    <!-- 三层心智模型 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><Layers :size="15" />三层心智模型</div>
      <table class="kv">
        <tbody>
          <tr><td>领域知识库</td><td>按知识范围分桶、互相隔离 —— 你知识体系的一级容器，由内容与概念派生而成</td></tr>
          <tr><td>集合</td><td>领域内对内容的分组 —— 手动收藏，或连接一个受支持的订阅源；订阅是集合的一种属性</td></tr>
          <tr><td>内容</td><td>每条投递的视频、文档或音频；文档再按论文、文章、白皮书等体裁组织，可归入集合并产出原始材料与智能笔记</td></tr>
        </tbody>
      </table>
      <div class="note-tip" style="margin-top:9px">在这三层之上，<b>概念图</b>横向贯通所有内容：术语、主题与时间线跨来源互相引用。</div>
    </div>

    <!-- 领域中心概念图 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><Network :size="15" />领域中心的概念图</div>
      <p style="color:var(--ink-700)">
        知识不只是一篇篇笔记。每个领域会聚成一组并行的<b>概念图</b>，把分散在各来源里的知识点收拢到一处：
      </p>
      <div class="grid3" style="margin-top:13px">
        <div class="metric">
          <div style="display:flex;align-items:center;gap:7px;color:var(--ink-900);font-weight:600;font-size:13px">
            <Tag :size="14" class="dim" />术语页
          </div>
          <div class="l" style="margin-top:5px">跨来源综合定义 + 类型化的出现处（在哪篇笔记、以什么身份出现）</div>
        </div>
        <div class="metric">
          <div style="display:flex;align-items:center;gap:7px;color:var(--ink-900);font-weight:600;font-size:13px">
            <Library :size="14" class="dim" />主题页
          </div>
          <div class="l" style="margin-top:5px">域内跨集合的内容聚合，加上概念时间线视图</div>
        </div>
        <div class="metric">
          <div style="display:flex;align-items:center;gap:7px;color:var(--ink-900);font-weight:600;font-size:13px">
            <Star :size="14" class="dim" />术语库
          </div>
          <div class="l" style="margin-top:5px">候选 → 采纳 → 回流 Profile，可手动 CRUD、标主题</div>
        </div>
      </div>
      <div class="note-tip" style="margin-top:11px">质量评审产出的 <span class="mono">key_terms</span> 自动成为候选，采纳后回流领域 Profile，统一后续 AI 笔记的措辞。</div>
    </div>

    <!-- 检索与组织 -->
    <div class="grid2" style="margin-top:16px">
      <div class="card pad">
        <div class="card-h"><Search :size="15" />全文搜索</div>
        <p style="color:var(--ink-700)">SQLite FTS5（trigram 中文子串匹配），跨领域、跨集合检索所有笔记，带分面与高亮，搜索一般 &lt; 1 秒。</p>
      </div>
      <div class="card pad">
        <div class="card-h"><Rss :size="15" />集合与订阅</div>
        <p style="color:var(--ink-700)">手动集合策展，或连接频道、RSS / Atom、本地目录与在线书等来源。来源目录由后端 registry 动态下发，订阅是集合属性。</p>
      </div>
    </div>

    <!-- 引擎能力 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><Cpu :size="15" />引擎</div>
      <div class="list">
        <div class="row" style="cursor:default">
          <span class="type-pill t-video"><Sparkles :size="17" /></span>
          <div class="body">
            <div class="title">多 Provider AI 网关</div>
            <div class="meta"><span>Anthropic · DeepSeek · Kimi · OpenAI · 本地 Ollama · Claude CLI，带成本追踪与 DRY_RUN 空跑</span></div>
          </div>
        </div>
        <div class="row" style="cursor:default">
          <span class="type-pill t-document"><Server :size="17" /></span>
          <div class="body">
            <div class="title">分布式 Worker</div>
            <div class="meta"><span>资源池 + 标签亲和；远程 worker 经 API 网关单条出站 HTTPS 接入，不直连中心 Redis / MinIO，可随时加一台 GPU 机器</span></div>
          </div>
        </div>
        <div class="row" style="cursor:default">
          <span class="type-pill t-document"><Star :size="17" /></span>
          <div class="body">
            <div class="title">质量评审</div>
            <div class="meta"><span>每篇智能笔记按多维度打分，并析出可采纳的关键概念</span></div>
          </div>
        </div>
        <div class="row" style="cursor:default">
          <span class="type-pill t-audio"><Database :size="17" /></span>
          <div class="body">
            <div class="title">可靠执行</div>
            <div class="meta"><span>文件即接口 · 幂等（输入指纹未变跳过）· 故障隔离（单任务失败不影响其他）</span></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 能力成熟度 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><GitBranch :size="15" />能力成熟度</div>
      <div class="list">
        <div class="row" style="cursor:default">
          <span class="badge b-ok">完整</span>
          <div class="body"><div class="meta"><span>来源 registry、OpenAPI 枚举、API 入队前 fail-closed 与前端来源目录同源</span></div></div>
        </div>
        <div class="row" style="cursor:default">
          <span class="badge b-info">first-pass</span>
          <div class="body"><div class="meta"><span>三类内容族摄入、FTS5 Search / Ask / MCP、订阅、概念图、评审、手工建卡 SRS、知识雷达与远程 Worker 网关</span></div></div>
        </div>
        <div class="row" style="cursor:default">
          <span class="badge b-mut">未开始</span>
          <div class="body"><div class="meta"><span>原生客户端、通知 / PWA、自动分类、知识缺口与矛盾检测、证据型自动卡片</span></div></div>
        </div>
      </div>
      <div class="note-tip" style="margin-top:9px"><b>first-pass</b> 表示可用但真实集成、可靠性或质量门尚未闭环。向量检索仅在黄金集证明 FTS5 未达阈值时启动，不作为预定完成项。</div>
    </div>

    <!-- 技术栈 -->
    <div class="card pad" style="margin-top:16px">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-500)">
        <span class="badge b-mut"><Cpu :size="12" />技术栈</span>
        <span>Python 3.11 · FastAPI · Redis · SQLite · Vue 3 · Docker</span>
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span>全 Docker 自托管，数据完全自有</span>
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span>MIT 开源</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.pipeline-scope { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; color: var(--ink-500); font-size: 12px; }
</style>
