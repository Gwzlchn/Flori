<template>
  <article class="about-panel">
    <header>
      <p class="eyebrow">
        About Flori
      </p>
      <h1>关于 Flori</h1>
      <p>Flori 取自拉丁语 <i>florilegium</i>，意为从群书中采撷精华的选集。它是单用户、家庭存储优先的个人知识库。</p>
    </header>
    <section class="card">
      <h2>这是什么</h2>
      <p>投递 PDF、arXiv、Bilibili、YouTube 或本地视频，Flori 保留原始材料，生成易读的机械或智能笔记，并让每条结论能回到经校验的原文位置。</p>
    </section>
    <section class="card">
      <h2>核心循环</h2>
      <div class="flow">
        <b>投递内容</b><span>→</span><b>Pipeline 处理</b><span>→</span><b>阅读成果</b><span>→</span><b>回到 Evidence</b><span>→</span><b>FTS 检索</b>
      </div>
    </section>
    <section class="card">
      <h2>知识如何组织</h2>
      <div class="flow">
        <b>领域</b><span>→</span><b>分类</b><span>→</span><b>内容</b><span>→</span><b>当前成果</b>
      </div>
      <p>每次处理都创建新 Job；只有成功发布的 current 成果进入检索与阅读，上一个成功成果作为 previous 保留。</p>
    </section>
    <section class="card">
      <h2>运行架构</h2>
      <div class="architecture">
        <div><b>Home Core</b><small>Rust、SQLite 与 NAS 保存唯一业务和 Artifact 真相</small></div>
        <div><b>Edge</b><small>TLS、Basic Auth、静态前端和反向代理</small></div>
        <div><b>Runner</b><small>经出站 HTTPS 执行媒体工具、QoderCLI 或 CodexCLI</small></div>
      </div>
    </section>
    <section class="card scope">
      <h2>当前边界</h2>
      <ul>
        <li>PDF/arXiv 与视频的摄入、笔记、evidence、发布、FTS 与阅读链已建立。</li>
        <li>只使用 QoderCLI 与 CodexCLI Runner，不做通用 Provider 或自动 fallback。</li>
        <li>不包含 Ask、Radar、Study/SRS、audio、RSS、Redis、MinIO 或 Exact DR。</li>
      </ul>
    </section>
  </article>
</template>

<style scoped>
.about-panel { display: grid; gap: 13px; max-width: 980px; }.about-panel > header { max-width: 760px; padding: 12px 0 4px; }
header > p:last-child,section p,li,small { color: var(--muted); line-height: 1.7; }section { padding: 17px; }section h2 { margin-bottom: 10px; }
.flow { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }.flow b { padding: 5px 9px; border-radius: 5px; color: var(--brand-dark); background: var(--brand-soft); font-size: 12px; }.flow span { color: #b8b6b0; }
.architecture { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 10px; }.architecture div { display: grid; gap: 4px; padding: 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface-soft); }.architecture small { font-size: 12px; }
.scope ul { display: grid; gap: 6px; margin: 0; padding-left: 20px; }@media (max-width:620px) { .architecture { grid-template-columns: 1fr; } }
</style>
