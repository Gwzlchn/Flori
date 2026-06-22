# ADR-0012: 案例取证 / 权威来源（AI fetch 权威外部源，带引用喂笔记与评审）

> 改写 [ADR-0010](0010-review-feedback-loop.md) 的 Loop 0 红线与评审 traceability 检查。
> 本 ADR 只记**域无关的通用模式**；首个落地实例（具体领域 Profile / 来源站点 / 锚点字段 / 示例数据）属实现细节，见 `.local` 实现总账，不入本 ADR。

## 背景

案例类内容（对某一具体事件、案件、人物的复盘）的笔记价值在**精确事实**：金额、当事人、编号、日期、比率等。

对一批已生成案例笔记的现状核查发现：

1. **机制解释其实到位**——多数案例笔记真在讲「某手法/概念怎么运作、为何如此」，"只列名词不解释" 对现状基本不成立。
2. **真正普遍的硬伤是 traceability**：案例笔记普遍"超出转写的精确数据无出处"。但 provenance 核查（机械稿 vs 纯口播比对）证明：这些精确数据**绝大多数是真实的、来自视频片中出示的权威文书截图（被 OCR 进机械稿）或口播**，不是幻觉——只是没标来源，与"模型凭记忆补的"无法区分。

结论：缺陷本质是**缺 provenance**，不是造假。既然视频只放了文书的*片段截图*，让 AI 去 fetch **权威全文 + 报道**来夯实并标源，是高价值功能而非要防的风险。这同时改写 ADR-0010：评审里 traceability 被一刀切当幻觉、Loop 0 红线"禁补素材外事实"把"取真实权威源"也误禁了。

## 问题分解：provenance 三层

| 层 | 来源 | 现状 | 处置 |
|----|------|------|------|
| 1 视频原述 / 片中 OCR | 在素材内（口播或屏幕文书 OCR） | 现状大部分精确数据属此 | 标源「据视频 / 据片中文书」 |
| 2 AI fetch 的权威外部源 | 权威源文书全文（裁定 / 处罚 / 公告等）/ 报道 | **本 ADR 新增** | 带引用 `[E#]` |
| 3 模型凭训练记忆补 | 无源 | 真幻觉风险 | 消灭或标 `〔待核实〕` |

现状把三层混成一锅、都不标源 = 核心缺陷。本 ADR 把 provenance 变成「精确声明的**强制属性**」。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 维持现状（精确数据不标源） | 零成本 | 真假不分；评审无从核；伪权威无人守 |
| 仅"标注已在素材内的数据"（轻，无 fetch） | 纯 prompt+评审，立刻能做，解释 ~90% 的 traceability 缺陷 | 视频没展示的部分仍缺，无法补全/核对 OCR 残缺 |
| **取证：fetch 权威全文 + 带引用 + 评审逐条核（决定）** | 把 #1 缺陷变功能；精确数据可核可点；案例笔记升级为证据链档案；对味 M2.5 | 跨 数据/步骤/API/前端/评审 的新能力；要 case-match 门 + 网络兜底 |

（"轻"选项不被取代，而是作为本能力的 prompt/评审底座一并落地。）

## 决定

新增「案例取证」能力，遵循"文件即接口"：

### 数据模型
- 新增 `output/evidence.json`（每源 `{id, type, title, url, publisher, ref(编号), retrieved_at, match_confidence, excerpt, key_facts:[{figure, quote}], file}`）+ `case_match` 块（subject / anchors / confidence）。
- **全文落 `output/evidence/evidence-NN.md`**：NN 零填充 2 位、1-based、稳定索引（对齐 `frame-NNNN.jpg` 先例）；`id:"E1"` ↔ `file:"evidence/evidence-01.md"`；元数据只在 evidence.json，文件名不编码标题（避免非法字符/漂移）。

### 取证步
- 新 pipeline 步「取证(evidence)」，`depends_on=[09_mechanical]`（顺序由 depends_on 决定，非数字前缀；插入位置/是否 renumber 后续步留实现，注意 renumber 触及 `_REVIEW_STEPS`/契约/测试）。
- **仅案例类条件触发**（`rules`: style_tags 含 case-study 或 域为案例域），心法/通用类不跑。
- **绑 claude-cli worker**（有 WebFetch/WebSearch；gateway provider 多无工具）；走 claude worker 网络，出网熔断兜底。

### 决定 1 — case-match 硬门
用机械稿 OCR 锚点（主体标识 / 当事人 / 编号 / 日期）比对抓回文书；**对不上 → match_confidence=low → smart 不引该源、前端 tab 标灰**。抓错案子代价最大，宁缺毋滥。

### 决定 2 — 评审逐条忠实性核对
评审参照系扩为 **机械稿 + evidence.json vs 智能稿**：
- 不再给"超出转写但已引源"扣分（修掉 0010 的一刀切）。
- 新增「引用完整性」：每条精确外部事实须有有效 `[E#]`；裸着没标的精确数字 → flag。
- **逐条 citation faithfulness**：`[E#]` 声称的数字，来源 excerpt/全文是否真支撑？拿真源给它没说的数字背书（伪权威）比无源更坏——一上来就核（不延后）。这正是「存全文」的用途；为防自审失效，须**服务端确定性核对**（被引数字串是否出现在来源），不只信模型自述。

### 决定 3 — fetch 全文 + 统一命名
fetch 全文落 `output/evidence/evidence-NN.md`（命名见上）；excerpt 进 evidence.json 供卡片速览，全文供评审忠实性核对 + 离线读。

### smart 引用
- 笔记用 `[E#]` 角标（复用 `![](img:N)` 占位符那套回填/可点机制）；`_build_user_prompt` 新增 evidence 块，指令强制"精确外部事实必须标 `[E#]`，不得引用来源列表外的精确数据"。
- 三层 provenance 落笔：fetch→`[E#]`、视频/OCR→「据视频 / 据片中文书」、查不到→`〔待核实〕`。
- smart 文本 pass 仍 `--tools ""` 纯净成稿（fetch 在前置取证步完成，不在成稿轮联网）。

### API / 前端
- `GET /api/jobs/{id}/evidence`（裸透传，同 `get_review`）。
- 新增「权威来源」tab：N 张来源卡（类型徽标 / 标题 / publisher / 编号 / **原始链接** / excerpt 可展开看全文 / 置信度）；笔记里 `[E#]` chip 可点跳对应卡片。

### 1M 上下文（关键约束）
当前所有 AI provider 均 **1M 上下文** → **全文 fetch 直接喂 smart 与 review 可行，无需 RAG/分块**；评审逐条忠实性可读全文核对。顺带：`REVIEW_NOTE_LIMIT` 的裁剪在 1M 下过紧，可大幅放宽——0010 的 `coverage_truncated` 盲点部分即此过紧裁剪所致。

### 幂等 / 来源范围
- 取证步指纹 = case 锚点（锚点不变不重抓，省网络/省钱）；`evidence.json` hash 折进 smart + review 的 `input_hashes`（取证更新 → 自动重生成+重评审）。
- 来源范围 v1：权威发布方的官方文书 + 权威报道（稳）；受限的官方库（按域而定，部分近年抓取受限）后置/尽力。

## 红线

- 永不编造来源/出处；`[E#]` 必须对应真实 fetch 到的源。
- 抓错案子（case-match low）→ 不引、标灰，宁缺毋滥。
- faithfulness：`[E#]` 必须真支撑被引事实，否则评审 flag（伪权威）。

## 影响 / 改写 ADR-0010

- **Loop 0 红线改写**：从「绝不引入素材外事实」→「只允许引用 fetch 来的带源事实，永不编」。fetch 让修订既安全又更有用。
- **评审 traceability 升级**为「引用完整性 + 逐条忠实性」（决定 2），替代 0010 里把 out-of-transcript 一刀切当风险的口径。
- **依赖**：评审须先修 parse_failure/salvage 盲点（0010 §影响，已坐实一例：评审因定义内未转义双引号 → JSON 崩 → key_terms 全丢、分数蒙混）；否则忠实性核对的产出同样可能被 salvage 丢。
- 新增 step → `pipelines.yaml` + 02 领域模型（evidence 制品）；smart/review `input_hashes` 加 evidence hash；前端新 tab。
- 契约：`output/evidence.json` + `/api/jobs/{id}/evidence` → `docs/03-contracts.md`（commit 用 `contract:` 前缀）。
- 成本：fetch 走 claude worker/外网，仅案例类条件触发 + 锚点幂等不重抓。

## 与其它 ADR / 文档的关系

- 改写 [ADR-0010](0010-review-feedback-loop.md)（Loop 0 红线 + 评审 traceability）；构建在 [ADR-0004](0004-llm-multi-provider.md)（claude-cli 工具能力/多 provider）之上。
- 对味 ROADMAP **M2.5 AI-native（RAG/agentic）**；落地横跨 `docs/05-content-adapters.md`（案例 enrichment）、`docs/04-module-design/`（新 step + 前端 tab）、`docs/02-domain-model.md`（evidence 制品）、`docs/03-contracts.md`。
- **首个落地实例**（领域 Profile / 来源站点 / 锚点字段 / 示例数据）属实现细节，记于 `.local` 实现总账，不入本 ADR——保持本 ADR 域无关。
