# ADR-0015: 论文、文章和白皮书统一为 Document 内容族

## 状态

已采纳。

## 背景

论文、文章、白皮书、报告和书籍章节的业务体裁不同，但都需要同一组能力：保留原始 HTML/PDF、抽取结构化正文、建立稳定锚点、翻译、生成笔记、评审并进入检索。旧 `paper` 与 `article` 顶层 pipeline 重复实现这些能力，还把业务体裁、媒介格式和解析方式混在一个枚举中。PDF 逆向 Markdown 无法可靠保留公式、表格和版式，也不适合作为原文真相源。

## 决定

- 顶层 `content_type` 和 pipeline 只保留 `video / document / audio`。
- `document_kind` 表达业务体裁，包括 `research_paper / article / whitepaper / report / book_chapter / documentation / standard / thesis / unknown`。
- `source_profile` 和 `capabilities` 表达实际媒介能力。当前 profile 为 `scholarly_html / generic_html / digital_pdf / scanned_pdf`。
- 所有 Document 共用 `01_download → 02_parse → 03_structure → 04_translate → 05_smart → 06_semantic_attestation → 07_concepts → 08_review`。条件步骤由 job flag 和实际产物门控，不能复制 pipeline。
- `intermediate/document.json` 是结构化真相源；不可变 `input/source.html|source.pdf` 是原文真相源。系统不生成或兼容 `output/original.md`。
- HTML 原文在安全副本中保持 DOM 结构；PDF 用 PDF.js 保持原始版式。两者都通过稳定 segment locator 与译文、概念、Figure/Table 和证据深链互跳。
- 翻译发布 block 对齐的 `output/translation.json` 和可再生 `output/translated.html`，不把模型输出当原文替代品。
- Figure 与 Table 分别进入稳定注册表和分组导航；无法完整抽取时保留来源 locator、crop 和降级原因，不能静默丢失。
- `configs/document_kinds.yaml` 是体裁与 source profile 的单一目录；`configs/sources.yaml` 只负责来源到 Document 默认分类的映射。

## 数据迁移

schema v7 在一个事务内把旧 `paper` 映射为 `document/research_paper`，把旧 `article` 映射为 `document/article`，并同步 jobs、全文索引、证据块、术语 occurrence 和 Prompt namespace。迁移失败必须连同数据、索引、migration ledger 和 `user_version` 一起回滚。新 schema 拒绝旧顶层枚举、Document 空 kind 和非 Document 非空 kind。

## 影响

- Search、Ask、MCP 和 radar 以 `content_type=document` 聚合，并可用 `document_kind` 继续区分论文、文章和白皮书。
- Prompt override 的身份扩为 `(scope, domain, pipeline, document_kind, step)`；Document 可先使用共同覆盖，再叠加体裁覆盖。
- 读取端点按原始媒介分流，但前端仍展示统一的原文、译文、原文 PDF、Figure 和 Table 导航。
- 新增 Document 体裁不再复制后端 pipeline、前端页面或数据库顶层枚举。

## 被否决的选项

- 保留 `paper` 与 `article` 两条 pipeline：继续制造步骤、Prompt、测试和 UI 漂移。
- 把所有文档只标成 `document` 而不保留体裁：会丢失业务筛选、Prompt profile 和评审语义。
- 继续以 PDF→Markdown 作为论文原文：公式、表格、图和定位不可可靠复算。

## 关联文档

- `docs/02-domain-model.md`
- `docs/03-contracts.md`
- `docs/04-module-design/steps-document.md`
- `docs/05-content-adapters.md`
