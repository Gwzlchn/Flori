请从以下内容提取【核心概念】并写一句话摘要,严格输出 JSON。
要求:
- key_terms:文中讲清楚的关键概念(术语),每个给一句简洁中文定义;英文专有名词原样保留、不翻译。
- zh_name:该术语的【标准中文译名】(不是解释,是短译名,如 Kelly criterion→凯利准则);term 本身是中文或无通行译名时为 null。
- related:该概念与本次 key_terms 里【其它概念】的关系边(可空数组)。rel 只允许 prerequisite(先修)/is_a(是一种)/part_of(是其组成)/related(相关);term 必须逐字引用 key_terms 中的其它概念,文中没讲清楚的关系不要编。
- summary:用一句话(≤60 字)概括全文要点。
- 输出格式:{"summary": "...", "key_terms": [{"term": "...", "definition": "...", "zh_name": "...|null", "related": [{"term": "...", "rel": "..."}]}]}
- 只输出 JSON,不要额外解释或代码块标记。
