"""跨源综合问答(Cross-Source Synthesis Q&A)服务。

用户提问 → 跨语料检索相关笔记 → LLM 综合出**带引用**的答案,标注共识/分歧。

检索是最大风险:现有唯一检索是 FTS5 trigram,db.search_notes 把【整条查询】当一个
带引号的字面短语匹配(_fts_match_query),自然语言问句几乎不可能整句命中。本模块通过
`derive_queries` 把问句拆成有意义的小词/术语,分别检索再并集去重,缓解该字面短语限制。

门面纯函数(吃 Database),便于单测:不在此持有连接/全局状态。
LLM 综合本身现作为独立 AI task 投给 ai-worker(见 api/routes/ask.py + shared.models.AITask),本模块只产 retrieve/build_prompt。
"""

from __future__ import annotations

import re

from shared.db import Database

# 每段正文喂给 LLM 的字符预算:太长会撑爆上下文 + 涨成本,4000 中文足够承载要点。
_BODY_CHAR_BUDGET = 4000
# derive_queries 产出的查询条数上限:每条都要打一次 FTS,过多无谓放大检索成本。
_MAX_QUERIES = 6
# trigram 至少 3 字符才命中;<2 的 token(单字/单字母)留着也只会拖低信噪,丢弃。
_MIN_TOKEN_LEN = 2

# 极常见中文/英文停用词:它们组成的短语 trigram 噪声大、区分度低,从派生查询里剔除。
_STOPWORDS = {
    # 中文
    "的", "了", "和", "是", "在", "我", "有", "也", "就", "不", "人", "都",
    "一个", "什么", "怎么", "如何", "为什么", "哪些", "这个", "那个", "可以",
    "他们", "我们", "你们", "以及", "或者", "还是", "因为", "所以", "但是",
    "关于", "对于", "通过", "进行", "区别", "差异", "比较", "之间",
    # 英文
    "the", "a", "an", "of", "to", "in", "is", "are", "and", "or", "for",
    "what", "how", "why", "which", "this", "that", "with", "about", "between",
    "do", "does", "can", "vs", "versus", "compare", "difference",
}

# 把问句切成「连续 CJK 串」或「连续 ascii 词(字母+数字)」的 token。
# 标点/空白天然成为分隔符;CJK 串后续再做滑窗细切以提高短词命中率。
_TOKEN_RE = re.compile(r"[一-鿿]+|[A-Za-z0-9]+")
# 连续 CJK 串内部再切出的子串长度(2~4 字滑窗),覆盖「神经网络/注意力机制」等复合词。
_CJK_WINDOWS = (4, 3, 2)


def _cjk_subgrams(run: str) -> list[str]:
    """对一段连续 CJK 串做 2~4 字滑窗,产出候选子词(长串优先,保序去重)。
    例:"注意力机制" → ["注意力机", "意力机制", "注意力", ...]。配合 trigram 子串检索,
    比把整串当一个 token 更易命中分散在不同笔记里的概念。"""
    out: list[str] = []
    n = len(run)
    if n <= max(_CJK_WINDOWS):
        out.append(run)  # 短串本身就是一个合理查询词
    for w in _CJK_WINDOWS:
        if w >= n:
            continue
        for i in range(n - w + 1):
            out.append(run[i : i + w])
    return out


def _dedup(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(s for s in seq if s))


def derive_queries(question: str, db: Database, domain: str | None = None) -> list[str]:
    """把自然语言问句拆成一组 FTS 友好的检索词,缓解「整句字面短语」无法命中的问题。

    策略:
    1. 切出 token(连续 CJK 串 / ascii 词),丢停用词与 <2 字噪声;CJK 长串再滑窗细切。
    2. 叠加任何「术语表里的词」且其 term 串恰出现在问句中的 —— 领域概念是最高信噪检索词。
    3. 去重,按信息量(术语 > 长词 > 短词)排序,截断到 _MAX_QUERIES 条。
    """
    q = question or ""
    glossary_terms: list[str] = []
    try:
        for t in db.list_glossary(domain):
            term = (t.get("term") or "").strip()
            if term and term in q:
                glossary_terms.append(term)
    except Exception:
        # 术语表读失败不应让整个问答挂掉;退化为纯 token 派生。
        glossary_terms = []

    tokens: list[str] = []
    for m in _TOKEN_RE.findall(q):
        if re.fullmatch(r"[A-Za-z0-9]+", m):
            if len(m) >= _MIN_TOKEN_LEN and m.lower() not in _STOPWORDS:
                tokens.append(m)
        else:  # CJK 串
            for sub in _cjk_subgrams(m):
                if len(sub) >= _MIN_TOKEN_LEN and sub not in _STOPWORDS:
                    tokens.append(sub)

    # 术语优先(信噪最高),其后按长度降序(长词更具体)再原序兜底。
    ranked_tokens = sorted(_dedup(tokens), key=lambda s: -len(s))
    candidates = _dedup([*glossary_terms, *ranked_tokens])
    return candidates[:_MAX_QUERIES]


def retrieve(
    db: Database, question: str, domain: str | None = None, k: int = 8
) -> list[dict]:
    """跨语料检索:对每个派生查询跑 FTS,并集去重(保留最佳 rank),取前 k 篇,批量拉正文。

    返回 [{job_id, title, domain, content_type, body}];body 截断到 _BODY_CHAR_BUDGET。
    去重以 job_id 为粒度(同一篇内容只进一次);rank 以「首个命中它的派生查询的次序 +
    该查询内部的命中序」近似——越靠前的查询越具体,命中即视为更相关。
    """
    queries = derive_queries(question, db, domain)
    if not queries:
        # 派生不出任何词(纯停用词/纯标点):退化为整句直检,聊胜于无。
        queries = [question] if (question or "").strip() else []

    # job_id -> 首次命中的全局序(越小越相关),同时记录命中元信息(title/domain/content_type)。
    best_rank: dict[str, int] = {}
    meta: dict[str, dict] = {}
    order = 0
    for qi in queries:
        try:
            _total, items = db.search_notes(qi, domain=domain, limit=k)
        except Exception:
            continue
        for it in items:
            jid = it["job_id"]
            if jid not in best_rank:
                best_rank[jid] = order
                meta[jid] = {
                    "job_id": jid,
                    "title": it.get("title") or "(无标题)",
                    "domain": it.get("domain") or "",
                    "content_type": it.get("content_type") or "",
                }
            order += 1

    if not best_rank:
        return []

    top_ids = sorted(best_rank, key=lambda j: best_rank[j])[:k]
    bodies = db.note_bodies(top_ids)

    passages: list[dict] = []
    for jid in top_ids:
        body = (bodies.get(jid) or "").strip()
        if not body:
            continue  # 无正文(仅 snippet 索引缺失)→ 喂给 LLM 无意义,跳过
        passages.append(
            {
                **meta[jid],
                "body": body[:_BODY_CHAR_BUDGET],
            }
        )
    return passages


def build_prompt(question: str, passages: list[dict]) -> tuple[str, str]:
    """构造 (system, user) prompt:要求 LLM 跨段综合、内联引用 [来源N]、补「共识 / 分歧」段。"""
    system = (
        "你是一个严谨的知识综合助手。你将收到一个问题和若干来自不同笔记的资料段落。\n"
        "请遵守以下要求:\n"
        "1. 综合【所有】段落作答,不要只复述单一来源;若段落与问题无关可忽略。\n"
        "2. 凡是用到某段资料,必须在该句末尾内联标注来源,格式为 [来源N](N 为段落编号),\n"
        "   可在一句末同时标多个,如 [来源1][来源3]。不要编造段落里没有的内容。\n"
        "3. 在答案末尾追加一个二级标题章节 `## 共识 / 分歧`,分别说明各来源在哪些点上\n"
        "   达成一致(共识)、在哪些点上存在差异或冲突(分歧);若无明显分歧,如实说明。\n"
        "4. 全程用中文,markdown 格式,简洁清晰。"
    )
    blocks: list[str] = []
    for i, p in enumerate(passages, start=1):
        head = f"[来源{i}] 《{p.get('title') or '(无标题)'}》"
        tags = []
        if p.get("domain"):
            tags.append(f"领域={p['domain']}")
        if p.get("content_type"):
            tags.append(f"类型={p['content_type']}")
        if tags:
            head += f"({', '.join(tags)})"
        blocks.append(f"{head}\n{p.get('body', '')}")
    sources_block = "\n\n".join(blocks)
    user = (
        f"问题:{question}\n\n"
        f"以下是检索到的相关资料段落(共 {len(passages)} 段):\n\n"
        f"{sources_block}\n\n"
        "请根据上述资料综合作答,并按要求内联引用 [来源N] 且补充「共识 / 分歧」章节。"
    )
    return system, user
