<template>
  <article class="about-panel">
    <header>
      <p class="eyebrow">
        About Flori
      </p><h1>把材料沉淀成可核对的个人知识</h1><p>Flori 是单用户、家庭存储优先的知识库。它只承诺三件事：摄入可靠，笔记易读，结论能回到原始证据。</p>
    </header>
    <section class="card">
      <h2>知识如何组织</h2><div class="flow">
        <b>领域</b><span>→</span><b>分类</b><span>→</span><b>内容</b><span>→</span><b>当前成果</b>
      </div><p>领域划定知识边界，分类组织同一领域内的投递，每次处理产生新的 Job，只有成功发布的成果进入搜索与阅读。</p>
    </section>
    <section class="card">
      <h2>论文阅读路径</h2><div class="flow">
        <b>arXiv 官方 HTML</b><span>→</span><b>ar5iv</b><span>→</span><b>PDF</b>
      </div><p>arXiv 始终保留 PDF。HTML 只有在根文档和静态资源闭包经过验证后才发布；PDF 页码、bbox 和 quote 始终是 canonical evidence。</p>
    </section>
    <section class="card">
      <h2>运行架构</h2><div class="architecture">
        <div><b>Home Core</b><small>Rust、SQLite 与 NAS 保存唯一业务和 Artifact 真相</small></div><div><b>Edge</b><small>TLS、Basic Auth、静态前端和反向代理</small></div><div><b>Runner</b><small>经出站 HTTPS 执行媒体工具、QoderCLI 或 CodexCLI</small></div>
      </div>
    </section>
    <section class="card scope">
      <h2>当前边界</h2><ul><li>PDF/arXiv 的解析、AI 笔记、evidence、发布、FTS 与阅读链已经建立。</li><li>本地视频目前只有离线黄金基线，完整视频 Pipeline 尚未交付。</li><li>MCP 和其余知识库管理面仍待后续切片；当前版本不是生产候选。</li><li>不包含 Ask、Radar、Study、audio、RSS、Redis、MinIO、Exact DR、通用 Provider 或自动 fallback。</li></ul>
    </section>
  </article>
</template>

<style scoped>
.about-panel { display: grid; gap: 13px; max-width: 980px; }
header { max-width: 760px; padding: 12px 0 4px; }
header > p:last-child, section p, li, small { color: var(--muted); line-height: 1.7; }
section { padding: 17px; }
section h2 { margin-bottom: 10px; }
.flow { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.flow b { padding: 5px 9px; border-radius: 5px; color: var(--brand-dark); background: var(--brand-soft); font-size: 12px; }
.flow span { color: #b8b6b0; }
.architecture { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.architecture div { display: grid; gap: 4px; padding: 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface-soft); }
.architecture small { font-size: 12px; }
.scope ul { display: grid; gap: 6px; margin: 0; padding-left: 20px; }
@media (max-width: 620px) { .architecture { grid-template-columns: 1fr; } }
</style>
