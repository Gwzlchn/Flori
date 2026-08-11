{{intro}}

这是最终知识发布前的统一评审。必须同时检查智能笔记、原始 Document、解析质量报告和概念清单:
- 智能笔记是否覆盖核心问题、方法链、关键证据、限制与失败条件,并正确解释重要 Figure/Table。
- 概念清单是否都是正文真正讲清楚的概念,定义与智能笔记及原始来源一致,没有遗漏主干概念。
- 解析质量为 degraded 时,必须把受影响的公式、图表、页码或章节列为问题,不得用流畅措辞掩盖来源缺口。
- 任何来源不支持的数字、比较、因果、范围或结论都必须形成 issue;不能因为表述看似合理而放行。

评分维度（每项打 1-5 的整数）：
{{dimensions}}
另外输出：
- key_terms：笔记讲清楚的关键概念及一句话候选定义。
- missing_concepts：来源存在但笔记遗漏的重要概念。
- top3_improvements：最重要的 3 条改进建议。
- issues：结构化问题列表。type 只能是 consistency / missing_in_source / missing_external / traceability；severity 只能是 info / warning / error。dimension 必须来自评分维度，claim 是待核验主张。有证据时 evidence_status=supported 且 locator.source 必须是下方来源标签，locator.quote 必须逐字来自该来源；证据不足时 evidence_status=insufficient 且给 reason。

只输出扁平 JSON，不要 Markdown fence 或额外说明：
{
  {{score_example}},
  "key_terms": [{"term": "概念名", "definition": "一句话候选定义"}],
  "missing_concepts": ["遗漏概念"],
  "top3_improvements": ["建议1", "建议2", "建议3"],
  "issues": [{"type":"traceability","severity":"warning","dimension":"accuracy","claim":"待核验主张","message":"问题","evidence_status":"supported","locator":{"source":"document","quote":"来源逐字片段"}}]
}

{{ref_block}}
