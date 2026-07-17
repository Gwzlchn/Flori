"""跨源综合问答(Cross-Source Synthesis Q&A)服务。

对用户提问跨语料检索相关笔记,由 LLM 综合出带引用的答案,标注共识/分歧。

检索是最大风险:整句字面短语常无法命中。服务端查 note_chunks_fts5,
只把带 evidence 的证据块交给 LLM,避免无来源的大段正文进入答案。
本模块通过 `derive_queries` 把问句拆成有意义的小词/术语,分别检索再并集去重。

门面纯函数(吃 Database),便于单测:不在此持有连接/全局状态。
LLM 综合作为独立 AI task 投给 ai-worker(见 api/routes/ask.py + shared.models.AITask),本模块只产 retrieve/build_prompt。
"""

from __future__ import annotations

import re

from shared.db import Database

# 每段证据喂给 LLM 的字符预算:太长会撑爆上下文 + 涨成本。
_BODY_CHAR_BUDGET = 4000
# derive_queries 产出的查询条数上限:每条都要打一次 FTS,过多无谓放大检索成本。
_MAX_QUERIES = 6
# trigram 至少 3 字符才命中;<2 的 token(单字/单字母)留着也只会拖低信噪,丢弃。
_MIN_TOKEN_LEN = 2
# Reciprocal Rank Fusion 的平滑常量;固定值保证离线评测可复算。
_RRF_K = 60
# 同一 job 的不同笔记在 RRF 完全同分时优先使用综合笔记。机械笔记仍可在
# 词法得分更高时胜出，这里只消除同分时按字符串把 mechanical 排在 smart 前的偏差。
_NOTE_TYPE_TIE_PRIORITY = {
    "smart": 0,
    "translated": 1,
    "original": 2,
    "mechanical": 3,
    "transcript": 4,
}

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

# 把问句切成连续 CJK 串或连续 ascii 词(字母+数字)的 token。
# 标点/空白天然成为分隔符;CJK 串后续再做滑窗细切以提高短词命中率。
_TOKEN_RE = re.compile(r"[一-鿿]+|[A-Za-z0-9]+")
# 连续 CJK 串内部再切出的子串长度(2~4 字滑窗),覆盖 "神经网络/注意力机制" 等复合词。
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


def _is_plain_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def derive_queries(question: str, db: Database, domain: str | None = None) -> list[str]:
    """把自然语言问句拆成一组 FTS 友好的检索词,缓解整句字面短语无法命中的问题。

    策略:
    1. 切出 token(连续 CJK 串 / ascii 词),丢停用词与 <2 字噪声;CJK 长串再滑窗细切。
    2. 叠加术语表里 term 串恰出现在问句中的词 —— 领域概念是最高信噪检索词。
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

    # 术语优先,信噪最高;其后按长度降序(长词更具体),再原序兜底。
    ranked_tokens = sorted(_dedup(tokens), key=lambda s: -len(s))
    candidates = _dedup([*glossary_terms, *ranked_tokens])
    return candidates[:_MAX_QUERIES]


def retrieve(
    db: Database, question: str, domain: str | None = None, k: int = 8
) -> list[dict]:
    """用确定性 RRF 融合全部派生查询,每个 job 最多返回一个证据块。"""
    if k <= 0:
        return []
    queries = derive_queries(question, db, domain)
    if not queries:
        # 派生不出任何词(纯停用词/纯标点):退化为整句直检,聊胜于无。
        queries = [question] if (question or "").strip() else []

    candidates: dict[tuple[str, ...], dict] = {}
    candidate_limit = min(100, max(k, k * 4))
    for qi in queries:
        try:
            _total, items = db.search_note_chunks(
                qi, domain=domain, limit=candidate_limit,
            )
        except Exception:
            continue
        seen_in_query: set[tuple[str, ...]] = set()
        for rank, it in enumerate(items, start=1):
            chunk_id = it.get("chunk_id") or (
                f"{it.get('job_id')}:{it.get('note_type') or ''}:{rank}"
            )
            evidence = it.get("evidence") or {}
            artifact_sha = evidence.get("artifact_sha256")
            body_sha = evidence.get("body_sha256")
            if (
                it.get("job_id")
                and _is_plain_sha256(artifact_sha)
                and _is_plain_sha256(body_sha)
            ):
                candidate_key = (it["job_id"], artifact_sha, body_sha)
            else:
                candidate_key = (chunk_id,)
            candidate = candidates.get(candidate_key)
            if candidate is None:
                candidate = {
                    "item": it,
                    "chunk_id": chunk_id,
                    "score": 0.0,
                }
                candidates[candidate_key] = candidate
            elif (
                _NOTE_TYPE_TIE_PRIORITY.get(it.get("note_type") or "", 99),
                it.get("note_type") or "",
                chunk_id,
            ) < (
                _NOTE_TYPE_TIE_PRIORITY.get(
                    candidate["item"].get("note_type") or "", 99,
                ),
                candidate["item"].get("note_type") or "",
                candidate["chunk_id"],
            ):
                # 完全相同的 artifact/body 在不同笔记类型中只算一份证据，
                # 但保留质量更高的代表，避免索引插入顺序决定最终 note_type。
                candidate["item"] = it
                candidate["chunk_id"] = chunk_id
            if candidate_key in seen_in_query:
                continue
            seen_in_query.add(candidate_key)
            candidate["score"] += 1.0 / (_RRF_K + rank)

    ranked = sorted(
        candidates.values(),
        key=lambda candidate: (
            -candidate["score"],
            candidate["item"].get("job_id") or "",
            _NOTE_TYPE_TIE_PRIORITY.get(
                candidate["item"].get("note_type") or "", 99,
            ),
            candidate["item"].get("note_type") or "",
            candidate["chunk_id"],
        ),
    )
    passages: list[dict] = []
    seen_jobs: set[str] = set()
    for candidate in ranked:
        it = candidate["item"]
        job_id = it.get("job_id") or ""
        if not job_id or job_id in seen_jobs:
            continue
        chunk_id = candidate["chunk_id"]
        note_type = it.get("note_type") or ""
        ev = dict(it.get("evidence") or {})
        ev.setdefault("chunk_id", chunk_id)
        ev.setdefault("note_type", note_type)
        ev.setdefault("section", it.get("section") or "")
        ev["snippet"] = it.get("snippet") or ""
        if (
            not note_type
            or not _is_plain_sha256(ev.get("artifact_sha256"))
            or not _is_plain_sha256(ev.get("body_sha256"))
        ):
            continue
        seen_jobs.add(job_id)
        passages.append({
            "job_id": job_id,
            "note_type": note_type,
            "title": it.get("title") or "(无标题)",
            "domain": it.get("domain") or "",
            "content_type": it.get("content_type") or "",
            "document_kind": it.get("document_kind") or "",
            "body": (it.get("body") or "")[:_BODY_CHAR_BUDGET],
            "evidence": ev,
        })
        if len(passages) >= k:
            break
    return passages


def build_prompt(question: str, passages: list[dict]) -> tuple[str, str]:
    """构造 (system, user) prompt:要求 LLM 跨段综合、内联引用 [来源N]、补 "共识 / 分歧" 段。"""
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
        if p.get("document_kind"):
            tags.append(f"文档类别={p['document_kind']}")
        ev = p["evidence"]
        if ev.get("section"):
            tags.append(f"段落={ev['section']}")
        if ev.get("timestamp_sec") is not None:
            tags.append(f"时间={ev['timestamp_sec']}秒")
        if ev.get("page") is not None:
            tags.append(f"页码={ev['page']}")
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
