你要把一组已通过证据门的章节知识卡综合成供最终论文笔记写作的主题学习图。不要继续压缩成摘要，也不要逐条拼接卡片。

任务：
1. 找出本主题的核心问题、概念骨架、方法链、结果链和限制链。
2. 显式连接跨包知识：背景怎样引出问题，定义怎样支撑方法，方法怎样产生实验，实验怎样支持或限制结论。
3. 每条综合陈述列出 knowledge_refs，只能使用输入 ID。
4. coverage_refs 必须精确包含 EXPECTED_KNOWLEDGE_REFS 的每一个 ID，表示每项都已审阅并决定归属，不等于每项都必须进入最终正文。
5. figure_refs 只能使用 FIGURE_CATALOG。保留真正有教学价值的图并写清读图顺序、支持的论点和不能推出的内容。
6. 模型综合分析必须明确依据和不确定性，不输出隐藏思维链。

输出严格符合 OUTPUT_SCHEMA 的 JSON，不要 Markdown fence 或额外说明。
输出前检查 JSON 字符串转义：换行写成 `\n`，字面反斜杠写成 `\\`，不得输出未转义换行或 `\ ` 等非法 escape。

OUTPUT_SCHEMA:
{{OUTPUT_SCHEMA}}

THEME:
{{THEME}}

PAPER_MAP:
{{PAPER_MAP}}

EXPECTED_KNOWLEDGE_REFS:
{{EXPECTED_KNOWLEDGE_REFS}}

FIGURE_CATALOG:
{{FIGURE_CATALOG}}

CHAPTER_CARDS:
{{CHAPTER_CARDS}}
