<script setup lang="ts">
// 关于 Flori（原型 #about）：三层心智模型 + 真实能力概览。
// 「四条内容流水线」从 /api/pipelines 动态拉取（= configs/pipelines.yaml 单一事实源），
// 不再硬编码 → 永远与实际步骤链一致。
import { ref, onMounted } from 'vue'
import { useWorkerStore } from '../stores/workers'
import {
  BookOpen, Info, GitBranch, RefreshCw, FileText, Check, Send,
  Search, Layers, Cpu, Play, Newspaper, Headphones, ChevronRight,
  Sparkles, Network, Tag, Server, Rss, Library, Database, Star,
} from 'lucide-vue-next'

interface PipeStep { key: string; label: string | null; pool: string | null; needs: string[] }
interface Pipeline { name: string; steps: PipeStep[] }

const store = useWorkerStore()
const pipelines = ref<Pipeline[]>([])
onMounted(async () => {
  try { pipelines.value = await store.fetchPipelines() } catch { /* 非致命：留空，显加载态 */ }
})

// 内容类型 → 展示（中文名 / 图标 / 色 pill）；未知类型回退。
const CT: Record<string, { label: string; icon: any; pill: string }> = {
  video: { label: '视频', icon: Play, pill: 't-video' },
  paper: { label: '论文', icon: FileText, pill: 't-paper' },
  article: { label: '文章', icon: Newspaper, pill: 't-article' },
  audio: { label: '播客 / 音频', icon: Headphones, pill: 't-audio' },
}
function ct(name: string) { return CT[name] || { label: name, icon: Layers, pill: 't-video' } }
// 步骤徽章配色（复用既有 badge 语义）：评审→绿、AI 步→蓝、其余→灰。
function stepBadge(s: PipeStep): string {
  if ((s.key || '').toLowerCase().includes('review') || (s.label || '').includes('评审')) return 'b-ok'
  if (s.pool === 'ai') return 'b-info'
  return 'b-mut'
}
</script>

<template>
  <section class="page">
    <div class="h1"><BookOpen :size="18" />关于 Flori</div>
    <div class="lead">自托管的 AI 学习知识库 —— 把视频 / 论文 / 文章 / 播客自动炼成带截图与时间戳的结构化笔记，沉淀为按领域分桶、可检索、互相关联的个人知识体系。</div>
    <div style="margin-top:10px;color:var(--ink-600);font-size:13px">名字来源：Flori 取自拉丁语 <i>florilegium</i>（“采花集”）——中世纪指从群书中采撷精华、汇编成册的选集，正是“把素材摘录、沉淀为知识”的隐喻。</div>

    <!-- 这是什么 -->
    <div class="card pad" style="margin-top:18px">
      <div class="card-h"><Info :size="15" />这是什么</div>
      <p style="color:var(--ink-700)">
        投递一条链接 / 一篇 PDF / 一个网页，Flori 在后台自动下载、转写、截图、OCR，再用 AI 整理成笔记。每篇内容产出<b>两份笔记</b>：
      </p>
      <div class="grid2" style="margin-top:11px">
        <div class="metric">
          <div style="display:flex;align-items:center;gap:7px;color:var(--ink-900);font-weight:600;font-size:13.5px">
            <FileText :size="15" class="dim" />机械版
          </div>
          <div class="l" style="margin-top:5px">带标点的逐字稿 + 关键帧截图 + OCR + 弹幕，忠实还原原内容，可逐句核对。</div>
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

    <!-- 四条内容流水线 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><RefreshCw :size="15" />四条内容流水线</div>
      <p class="note-tip" style="margin-top:-4px;margin-bottom:13px">每种内容走各自的步骤链；步骤间以 JSON / MD 文件通信，输入指纹未变则跳过（幂等）。</p>
      <div class="list">
        <div v-for="p in pipelines" :key="p.name" class="row" style="cursor:default;align-items:flex-start">
          <span class="type-pill" :class="ct(p.name).pill"><component :is="ct(p.name).icon" :size="17" /></span>
          <div class="body">
            <div class="title">{{ ct(p.name).label }}<span style="font-weight:400;color:var(--ink-400);margin-left:7px;font-size:12px">{{ p.steps.length }} 步</span></div>
            <div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">
              <span v-for="s in p.steps" :key="s.key" class="badge" :class="stepBadge(s)">{{ s.label || s.key }}</span>
            </div>
          </div>
        </div>
        <div v-if="!pipelines.length" class="note-tip" style="margin:0">流水线信息加载中…</div>
      </div>
      <div class="note-tip" style="margin-top:11px">视频智能版走<b>两段式</b>：先逐帧看图产出视觉描述，再据机械稿 + 视觉描述纯文本成稿。</div>
    </div>

    <!-- 三层心智模型 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><Layers :size="15" />三层心智模型</div>
      <table class="kv">
        <tbody>
          <tr><td>领域知识库</td><td>按知识范围分桶、互相隔离 —— 你知识体系的一级容器，由内容与概念派生而成</td></tr>
          <tr><td>集合</td><td>领域内对内容的分组 —— 手动收藏，或订阅源（如某 B 站 UP 主合集，订阅是集合的一种属性）</td></tr>
          <tr><td>内容</td><td>每条投递的视频 · 论文 · 文章 · 播客，归入某个集合，产出两份笔记</td></tr>
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
        <p style="color:var(--ink-700)">手动集合策展，或订阅 B 站 UP 主自动追更 —— 订阅是集合的一种属性，无独立实体，新内容自动入库。</p>
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
          <span class="type-pill t-article"><Server :size="17" /></span>
          <div class="body">
            <div class="title">分布式 Worker</div>
            <div class="meta"><span>资源池 + 标签亲和；远程 worker 经 API 网关单条出站 HTTPS 接入，不直连中心 Redis / MinIO，可随时加一台 GPU 机器</span></div>
          </div>
        </div>
        <div class="row" style="cursor:default">
          <span class="type-pill t-paper"><Star :size="17" /></span>
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

    <!-- 规划中 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><GitBranch :size="15" />规划中</div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-500)">
        <span class="badge b-mut">规划</span>
        <span>RAG 语义检索 + 与知识库对话</span>
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span>原生 iOS / Mac 客户端</span>
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span>闪卡 / 间隔重复等学习复习回路</span>
      </div>
      <div class="note-tip" style="margin-top:9px">以上为后续里程碑，尚未构建；当前以已落地的四类摄入、概念图、订阅、FTS5 搜索与术语库为准。</div>
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
