{{intro}}

评分维度（每项打 1-5 的整数）：
{{dimensions}}
另外输出：
- key_terms: 这篇笔记**讲清楚**的关键概念 + 一句话候选定义（用于沉淀进概念库）
- missing_concepts: 笔记**遗漏**的重要概念（知识缺口，仅供选题/查漏）
- top3_improvements: 最重要的 3 条改进建议

只输出如下扁平 JSON：所有维度为顶层整数键，不要嵌套进 scores 子对象、不要加 rationale 字段、不要代码围栏、不要任何额外说明文字。
{
  {{score_example}},
  "key_terms": [{"term": "概念名", "definition": "一句话候选定义"}],
  "missing_concepts": ["遗漏的重要概念"],
  "top3_improvements": ["改进建议1", "改进建议2", "改进建议3"]
}

{{ref_block}}