# ADR-0010: AI 评审闭环（笔记质量反馈回路）

## 背景

每条内容流水线（video/paper/article/audio）末尾都有一个质量评审步（`11_review` / `06_review` / `05_review`，pool=ai，便宜模型 Haiku/DeepSeek-Flash）。它对比机械版与智能版笔记，产出 `review.json`：6 维整数分（completeness/accuracy/structure/terminology/visual_integration/readability）+ `overall` + `key_terms` + `missing_concepts` + `top3_improvements`，并版本化落 `output/versions/review_*.json` 与笔记版本 1:1 配对（`note_file`）。

`docs/00-vision.md` 把「笔记质量 ≥ 4/5（AI 评审）」立为非功能目标。但**门立了、没装**：评审是流水线终点，低分不触发任何动作；状态机（`docs/02-domain-model.md` §2.2）只有 failed→retry 的错误重试，没有「需返工 / 需重生成」态；DB 无质量字段。三类评审产出里只有 `key_terms` 有回路（人工 accept → `Profile.terminology` → 改善**未来**笔记）；`missing_concepts`、`top3_improvements` 仅在评审面板展示，**死胡同**——既不改本篇也不改未来。

触发本 ADR 的是一次真实评审反馈（金融操纵案例笔记）：缺失概念只列名词不解释机制（对倒/虚假申报/龙虎榜/退市标准/商誉减值）、内部口径矛盾（表格「7 折」vs 正文「6.5 折」，实算 5.89/8.30≈7.1 折）、精确数据无出处（罚金 1.7 亿 / 资金占用 3.87 亿等超出转写素材）。这些建议系统目前**不会消化**。需要把评审从「旁路打分」升级为**闭环**。

## 问题分解（核心洞察）

评审产出不是一坨「改进建议」，而是**三种迭代路径完全不同**的东西，混为一谈是最大的陷阱：

| 类别 | 例子 | 性质 | 能否从本篇修 |
|------|------|------|--------------|
| **B 自洽性缺陷** | 7 折 / 6.5 折 / 7.1 折 互相矛盾 | 自包含、可校验、不需外部知识 | ✅ 最安全，该自动修 |
| **A-a 素材内漏讲** | 视频讲了机制但笔记没收进来 | 笔记缺陷 | ✅ 可从转写补回 |
| **A-b 素材外缺口** | 视频默认你懂「对倒交易」原理 | 知识缺口 / **选题信号** | ❌ 无源，硬补即幻觉 |
| **C 可追溯性** | 罚金 1.7 亿等「超出转写」的精确数据 | **疑似幻觉 / 无源警告** | ❌ 只能标记或删，不能补出处 |

关键：C 类是评审在替你问「这数从哪来的」；若把它当普通建议丢给重生成「补一个出处」，模型会**编一个出处**——反馈反而催生新幻觉。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 维持现状（只打分，人工看面板） | 简单、零幻觉风险 | 门没装，质量目标不可执行；反馈靠人肉、不 scale |
| 单一「低分→自动重生成」回路 | 概念简单、直接 | 把三类反馈混为一谈；C 类自动重生成催生幻觉；每低分笔记重跑贵模型，成本高；可能反复震荡 |
| **分类三回路 + 修订红线 + 分期（决定）** | 各类反馈各走对的路；红线挡幻觉；按风险分期上线 | 实现面更大，需结构化 issues / 新状态字段 / 质量聚合 |

## 决定

评审升级为闭环。先把 `review.json` 产出**显式三分类并各自路由**，再接三条回路：

### 三条回路

- **Loop 0 · 本篇修订**（新增，真正的「本篇闭环」）
  评审除打分外，输出结构化 `issues` 与 `needs_revision` 标志。调度按门限（`overall < 阈值` **或** 含自洽矛盾/无源旗标）再跑一次 smart 的**「修订模式」**：输入 = 原笔记 + 转写素材 + issues，产出新版本（沿用 `output/versions/` 1:1 配对）。**每篇最多 1 轮**修订（幂等 + 成本上限）；修订模型 ≥ 生成模型（不能用评审那个便宜模型）。

- **Loop 1 · 概念库 + 选题**（把死胡同接上，forward 回路）
  `missing_concepts` 按**素材内/外**分流：A-a 交 Loop 0 补；A-b 缺口 → glossary 存 `status=gap` + 进选题池（「找一个讲对倒交易的源」），不再丢弃。复用现成 `key_terms → glossary` 管道，只是换生命周期。

- **Loop 2 · Prompt / 系统演进**（最被低估的慢回路，forward）
  评审分按 domain 聚合看趋势；高频 `top3_improvements` 与低分维度 → 改 base prompt / domain Profile。例：「金融案例类笔记，列出的每个操纵手法必须补一句『怎么操作、为何获利』」——**这类「只列名词」是 prompt 系统缺陷，不是单篇缺陷**，源头改一次让以后所有同类笔记自带机制说明，胜过事后补 N 篇。需新增质量维度聚合（`docs/10-observability.md` 现仅运维计数）。

Loop 0 是本篇真闭环；Loop 1/2 是 forward 回路。三者合起来才让评审从「死胡同打分器」变成闭环系统。

### 修订红线（决定的一部分，最关键）

修订**只能使用转写素材内已有的事实**：

- **B 自洽矛盾**：可自由改写求一致（无需外部知识）。
- **A-b 素材外缺口 / C 无源精确数据**：**禁止补充新事实、禁止编造出处**；动作是标 `〔待核实〕` 或删除，**不是「补全」**。

红线把「幻觉风险」与「质量提升」解耦：能安全自动化的先上，危险的永不自动。

### 分期落地（按 安全 × 杠杆 排序）

| 阶段 | 内容 | 风险 | 杠杆 |
|------|------|------|------|
| **P1** | Loop 2 prompt 修一刀：「命名的术语/手法必须补一句机制说明」写进 base/Profile | 零 | 最高（源头修一次 > 补 N 篇） |
| **P2** | Loop 1：`missing_concepts` 素材内/外分流 + glossary `gap` + 选题池 | 低 | 中（复用现有管道） |
| **P3** | Loop 0：**首版只自动修 B 类自洽矛盾**；C 类只标记；A-a 暂不动。幻觉可控后再放宽到机制补全 | 高 | 高 |

## 理由

1. 三类反馈迭代路径本就不同——混为一谈要么催生幻觉（C 自动补），要么做无用功（A-b 无源硬补）。
2. 红线把幻觉风险与质量提升解耦：自洽 / prompt 这类安全的先自动化，无源补全永不自动。
3. 分期让「最高杠杆 × 最低风险」先落地（prompt 源头修），「最贵且最险」（Loop 0 重生成）最后且最保守。
4. 复用现有 `versions/` 1:1 配对与 `key_terms → glossary → Profile` 管道，不另起炉灶。
5. 让 vision 的「≥4/5」从一把**标尺**变成可执行的**门**。

## 评审的目标态：从打分旁路 → 闭环的「路由器」

评审不再是终点打分，而是闭环的分诊路由——给每个缺陷**定类型**，由**服务端**据此决定动作，再分发到三条回路：

- **结构化 `issues[]`（模型产出，扁平短字段）**：`type ∈ consistency | missing_in_source | missing_external | traceability`、`severity`、短定位、一句话。替代 prose `top3_improvements` 死胡同；A-a/A-b 必须由模型在 `type` 上标（调度器无法推断）。
- **两个服务端派生布尔（权威，不信模型；刻意不引入 verdict 枚举/多重状态字段，避免重叠）**：
  - `review_reliable`：解析失败 / 仅 salvage 救回分 / `coverage_truncated` 任一 → false。**替代静默中性分**——不可信评审永不算「干净通过」、永不触发自动重写（至多自动重评）。
  - `needs_revision`：v1 保守 =「存在 consistency issue 且 revision_round<1」。仅素材内可安全自洽修的才自动触发 Loop 0。
- **三路 gating（刻意不合一）**：① consistency → 自动修订（Loop 0，1 轮）；② `overall<4`（vision 门）→ 只记录供 Loop 2 聚合 + UI 提示，**不单独触发重写**（低分若因不可安全修的原因，自动重写＝赌幻觉）；③ 其余（traceability / 外部缺口 / 引用问题）→ **只标记**，转 Loop 1 / 取证，永不自动改。
- **accuracy 口径反转**：已带源（素材内 OCR 标注 或 fetch `[S#]`）的「超出转写」数据**不再扣分**；扣分只落「无源裸数字」或「引用与来源对不上」。
- **key_terms 采集受可信度护栏**：不可信评审只 append、绝不删/改概念（否则一次解析失败会清空该 job 概念）。

## 概念（glossary 条目）的目标态

概念库从「词 + 一条粘死候选定义 + 扁平 job 列表」升级为**机制优先、带来源层级、可升级、按最新版本对账**：

- **定义机制优先且可升级**：不再 fill-if-empty 粘死。授权层高者覆盖（`authoritative`(权威源) > `in_note`(笔记讲清) > `inferred`）；`accepted` / `definition_locked` 永不自动覆盖。
- **provenance 层 + occurrence 定位**：概念定义记来源层（视频/OCR vs 权威源）；occurrence 落 `location`（时间戳/章节），KB 才能从概念跳回出处。
- **采集＝对账到最新版，非跨版本累加**：一个 job 贡献的概念 = 它最新评审的 `key_terms`；新版本不再讲的词，摘掉该 job 的 occurrence（零 occurrence 且 `suggested` 则删，`accepted` 保留）。重跑/换 provider 下保持幂等、不灌重复/陈旧概念。
- **状态生命周期**：`gap`(缺口/想学) → `suggested`(某篇讲清) → `accepted`(人工采纳→回流 Profile)；gap 被讲清则升级。

## 影响

- **接口**：`review.json` 新增结构化 `issues`（`type: consistency | missing_in_source | missing_external | traceability`、`severity`、定位）与 `needs_revision`。改对外接口 → 同提交更新 `docs/03-contracts.md`（`contract:` 前缀）。
- **领域模型**：需表达「需返工」语义——优先用「既有 `versions/` + `needs_revision` 标志 + 1 轮上限」表达，**避免**引入与错误重试混淆的重 retry 机制；`docs/02-domain-model.md` §2.2 状态机相应补注。
- **知识库**：glossary `status` 增 `gap`（现 `suggested/accepted`）；`docs/04-module-design/knowledge-store.md` 同步。
- **可观测**：新增评审分按 domain 的质量趋势聚合（现仅 `flori_jobs` 运维计数）。
- **成本**：Loop 0 用贵模型且有门限——只有触发门限的笔记重跑，非全量；1 轮上限封顶。
- **既有盲点一并收口**：评审 `parse_failed` 现默认中性 3.0 分（静默），应改为触发标志而非伪装通过；长笔记 `coverage_truncated` 截断后评审不覆盖全文，需在 issues 里显式标注「未覆盖」。

## 与其它 ADR / 文档的关系

- 不取代任何 ADR；构建在 [ADR-0004](0004-llm-multi-provider.md)（多 Provider 网关——决定修订模型选择与回退）与既有概念沉淀（`key_terms → Profile`）之上。
- **被 [ADR-0012](0012-case-evidence-authoritative-sources.md) 改写**：Loop 0 红线从「绝不引入素材外事实」→「只允许引用 fetch 来的带源事实，永不编」；评审 traceability 检查升级为「引用完整性 + 逐条忠实性」（不再给已引源的 out-of-transcript 数据扣分）。0010 的 `parse_failed`/`coverage_truncated` 盲点已在 10 篇金融笔记核查中坐实（温州帮 review 因定义内未转义双引号致 JSON 崩、key_terms 全丢、overall 4.5 蒙混），是 0012 忠实性核对的前置修复项。
- 落地横跨 `docs/06-prompt-engineering.md`（P1）、`docs/04-module-design/knowledge-store.md`（Loop 1）、`docs/02-domain-model.md`（返工语义）、`docs/03-contracts.md`（`review.json` schema）、`docs/10-observability.md`（质量聚合）。
- ROADMAP：Loop 0/1 落在评审步扩展；质量趋势聚合可挂 M2.5（AI-native）/ M4（Agent 自主行为）附近。
