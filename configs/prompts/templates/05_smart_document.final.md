你要依据所有已验证的主题学习图，为认真学习这篇论文的人撰写完整中文智能笔记。

写作目标：
- 忠实呈现论文的问题、方法、评估设计、主要发现、限制和适用边界，并用教学性语言解释为什么和怎样读懂。
- 结构跟随论文的论证逻辑，不机械复刻目录，也不把知识卡逐条拼接。
- 不设统一字数、段落数或章节数限制；使用清晰标题、自然段、列表和局部总结，避免一整页只有一个巨型段落。
- 区分作者主张与模型综合。后者必须有依据、有限定、可反驳，不得冒充作者原话。
- Markdown 正文不得自行写“模型综合”小节；把跨章判断写进末尾的 synthesis 块，并列出最小知识依据集，服务端会确定性渲染分析、依据和不确定性。
- 相关性、规模门槛、代理指标或跨模型差异不得改写成因果结论；输入不能区分的解释必须明确写“无法归因”。
- 完整调用审计、哈希和覆盖清单不进入 Markdown 正文，它们由系统单独折叠展示。

内容要求：
- 正文先展开论证，不要用一句摘要替代背景、问题和方法。
- 覆盖后半部分、失败条件、附录限定和来源内部未决矛盾。
- 数字、公式和因果关系只能沿用知识卡能够支持的强度；冲突值必须中性并列。
- 图像内容与 caption 冲突时不得静默选边或复用其它图注，只写两者共同支持的内容并披露冲突。

图片规则：
- 只使用 FIGURE_CATALOG 中的 figure_ref。
- 在合适位置单独写 `{{FIGURE:<figure_ref>}}`，不要手写路径或 Markdown 图片。
- 正文、图解和模型综合都不得写 Markdown/HTML 图片或 source marker。
- 服务端会按主题学习图中已验证的 figure_guides 渲染读图顺序和边界；你只决定正文中放置哪些 figure_ref 以及放置顺序，不要重复输出图解 metadata。
- 图应服务理解；涉及论文主线的关键框架图、方法图或结果图不得无故省略。

证据规则：
- 重要段落末尾用 `[证据: p001-k001]`，只列足以支持该段的最小集合，形成最小联合来源组。证据组必须与它支持的正文处于同一物理行，不得单独占一行。每个可见行最多一个证据组；组内知识 ID 可解析到 1..32 个来源片段，超过上限必须拆成多个各自可证的自然段。
- 只能使用 EXPECTED_KNOWLEDGE_REFS；服务端会从正文、已验证图解和模型综合确定性计算 used_knowledge_refs。
- 服务端会从冻结输入确定标题和完整主题覆盖，不要输出 title、subtitle、theme_coverage_refs、figure_placements、audit_summary 或其它 metadata。

输出只能是以下纯文本 wire，不要 JSON、Markdown fence、前言或额外说明。每个保留标记必须独占一行且只出现一次：
1. 先输出 `---FLORI-FINAL-MARKDOWN-BEGIN---`。
2. 输出原始 Markdown 正文。正文不得包含任何 `FLORI-FINAL-` 保留标记。
3. 正文末尾输出 `---FLORI-FINAL-SYNTHESIS-BEGIN---`，随后输出跨主题综合分析的原始文本。
4. 输出 `---FLORI-FINAL-SYNTHESIS-BASIS---`，随后输出依据说明的原始文本。
5. 输出 `---FLORI-FINAL-SYNTHESIS-UNCERTAINTY---`，随后输出不确定性说明的原始文本。
6. 输出 `---FLORI-FINAL-SYNTHESIS-KNOWLEDGE-REFS---`，下一行只输出逗号分隔的 knowledge ID，不要括号、JSON 数组或其它文字。
7. 输出 `---FLORI-FINAL-SYNTHESIS-END---`，再输出 `---FLORI-FINAL-MARKDOWN-END---`；其后不得有其它内容。

PAPER_MAP:
{{PAPER_MAP}}

EXPECTED_THEME_REFS:
{{EXPECTED_THEME_REFS}}

EXPECTED_KNOWLEDGE_REFS:
{{EXPECTED_KNOWLEDGE_REFS}}

KNOWLEDGE_SOURCE_MAP:
{{KNOWLEDGE_SOURCE_MAP}}

FIGURE_CATALOG:
{{FIGURE_CATALOG}}

THEME_SYNTHESES:
{{THEME_SYNTHESES}}
