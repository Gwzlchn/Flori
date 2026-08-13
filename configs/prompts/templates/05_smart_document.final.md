你要依据所有已验证的主题学习图，为认真学习这篇论文的人撰写完整中文智能笔记。

写作目标：
- 忠实呈现论文的问题、方法、评估设计、主要发现、限制和适用边界，并用教学性语言解释为什么和怎样读懂。
- 结构跟随论文的论证逻辑，不机械复刻目录，也不把知识卡逐条拼接。
- 不设统一字数、段落数或章节数限制；使用清晰标题、自然段、列表和局部总结，避免一整页只有一个巨型段落。
- 区分作者主张与模型综合。后者必须有依据、有限定、可反驳，不得冒充作者原话。
- note_markdown 不得自行写“模型综合”小节；把跨章判断写进结构化 synthesis，并用 knowledge_refs 列出最小依据集，服务端会确定性渲染分析、依据和不确定性。
- 相关性、规模门槛、代理指标或跨模型差异不得改写成因果结论；输入不能区分的解释必须明确写“无法归因”。
- 完整调用审计、哈希和覆盖清单不进入 note_markdown，它们由系统单独折叠展示。

内容要求：
- 正文先展开论证，不要用一句摘要替代背景、问题和方法。
- 覆盖后半部分、失败条件、附录限定和来源内部未决矛盾。
- 数字、公式和因果关系只能沿用知识卡能够支持的强度；冲突值必须中性并列。
- 图像内容与 caption 冲突时不得静默选边或复用其它图注，只写两者共同支持的内容并披露冲突。

图片规则：
- 只使用 FIGURE_CATALOG 中的 figure_ref。
- 在合适位置单独写 `{{FIGURE:<figure_ref>}}`，不要手写路径或 Markdown 图片。
- 标题必须是单行纯文本；正文、标题、图解和模型综合都不得写 Markdown/HTML 图片或 source marker。
- 每个选中图必须出现在 figure_placements，并说明读图顺序、支持的论点和不能推出的内容。
- 图应服务理解；涉及论文主线的关键框架图、方法图或结果图不得无故省略。

证据规则：
- 重要段落末尾用 `[证据: p001-k001]`，只列足以支持该段的最小集合。同一证据段的所有知识 ID 必须在 KNOWLEDGE_SOURCE_MAP 中解析到同一 source；跨 source 比较必须拆成多个各自可证的自然段，不得把联合支持伪写成单条证明。
- 只能使用 EXPECTED_KNOWLEDGE_REFS；used_knowledge_refs 只列正文实际使用的 ID。
- theme_coverage_refs 必须精确包含全部 EXPECTED_THEME_REFS。

输出严格符合 OUTPUT_SCHEMA 的 JSON，不要 Markdown fence 或额外说明。

OUTPUT_SCHEMA:
{{OUTPUT_SCHEMA}}

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
