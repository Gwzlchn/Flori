"""知识库读服务(单一来源)。

纯函数,吃 Database / StorageBackend,返回普通 dict —— 供 MCP 工具(api.mcp_server)
和(后续可迁移的)FastAPI 路由共用,避免同一份读逻辑两处实现而漂移。

检索后端通过 SearchBackend 协议可插拔:v1 = FtsSearch(包现有 notes_fts5);
v2 可换 sqlite-vec 语义检索,而 MCP 工具签名不变(零返工)。
"""

from __future__ import annotations

from typing import Protocol

from shared.db import Database
from shared.notes_versions import latest_smart
from shared.storage import StorageBackend


def list_knowledge_bases(db: Database) -> list[dict]:
    """所有知识库(domain)及其 集合/内容/概念/订阅 计数 + 最近活跃。"""
    return db.list_domains()


def search(
    db: Database, query: str, domain: str | None = None, limit: int = 10
) -> list[dict]:
    """全文检索笔记(FTS5 trigram,中文子串友好),归一为 agent 友好结构。

    返回 [{title, snippet, job_id, domain, kind}];snippet 内 <mark> 包裹命中。
    注意:trigram 至少需 3 个字符才会命中,更短查询返回空。
    """
    _total, items = db.search_notes(query, domain=domain, limit=limit)
    return [
        {
            "title": it["title"] or "(无标题)",
            "snippet": it["snippet"],
            "job_id": it["job_id"],
            "domain": it["domain"],
            "kind": it["note_type"],
        }
        for it in items
    ]


async def get_note(db: Database, storage: StorageBackend, job_id: str) -> dict:
    """取一篇笔记的完整智能笔记 Markdown + 元信息。

    markdown 取最新版本智能笔记(output/versions/notes_smart_*.md);
    为 None 表示该内容的智能笔记尚未生成。job 不存在抛 KeyError。
    """
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    files = await storage.list_files(job_id)
    rel = latest_smart(files)
    markdown: str | None = None
    if rel:
        data = await storage.read_file(job_id, rel)
        markdown = data.decode("utf-8") if data else None
    return {
        "job_id": job.id,
        "title": job.title,
        "domain": job.domain,
        "collection_id": job.collection_id,
        "content_type": job.content_type,
        "status": job.status.value,
        "note_file": rel,
        "markdown": markdown,
    }


def list_collections(db: Database, domain: str | None = None) -> list[dict]:
    """集合(内容分组)清单,可按 domain 过滤;归一为 agent 友好 compact dict。
    订阅集合额外带 source_type/source_id/last_synced_at/last_sync_status。"""
    out: list[dict] = []
    for c in db.list_collections(domain):
        d = {"id": c.id, "name": c.name, "domain": c.domain, "job_count": c.job_count}
        if c.source_type and c.source_id:
            d["source_type"] = c.source_type
            d["source_id"] = c.source_id
            d["last_synced_at"] = (
                c.last_synced_at.isoformat() if c.last_synced_at else None
            )
            d["last_sync_status"] = c.last_sync_status
        out.append(d)
    return out


def get_glossary(
    db: Database, domain: str, status: str | None = None
) -> list[dict]:
    """某库概念/术语表(compact:term/definition/status/is_topic/occurrence_count)。
    status 可选(如 accepted/review)。单条详情用 get_term。"""
    return [
        {
            "term": t["term"],
            "definition": t["definition"],
            "status": t["status"],
            "is_topic": t["is_topic"],
            "occurrence_count": len(t.get("occurrences") or []),
        }
        for t in db.list_glossary(domain, status)
    ]


def get_term(db: Database, domain: str, term: str) -> dict | None:
    """单条术语/概念详情(定义/出处 occurrences/相关 related/状态)。未命中返回 None。
    去掉 datetime 等非 JSON 友好字段,保 agent 可直接消费。"""
    t = db.get_glossary_term(domain, term)
    if t is None:
        return None
    return {
        "domain": t["domain"],
        "term": t["term"],
        "definition": t["definition"],
        "status": t["status"],
        "is_topic": t["is_topic"],
        "occurrences": t.get("occurrences") or [],
        "related": t.get("related") or [],
    }


def concept_timeline(db: Database, domain: str, granularity: str = "month") -> dict:
    """某库概念时间线:概念按其源内容发布时间分桶计数。granularity=day|week|month。"""
    return db.concept_timeline(domain, granularity)


def _short_definition(text: str | None, limit: int = 120) -> str:
    """概念定义的简短形态(图谱节点/侧栏摘要):取首句,过长再按字符截断。

    首句以中文/英文句末标点(。！？.!?)切分;无标点则整体按 limit 截断(超长补省略号)。
    """
    s = (text or "").strip()
    if not s:
        return ""
    head = s
    for i, ch in enumerate(s):
        if ch in "。！？!?":
            head = s[: i + 1]
            break
        if ch == "." and i + 1 < len(s) and s[i + 1] in " \t\n":
            head = s[: i + 1]
            break
    head = head.strip()
    if len(head) > limit:
        head = head[:limit].rstrip() + "…"
    return head


def concept_graph(db: Database, domain: str) -> dict:
    """某库「概念图谱」:节点=概念,边=共现(两概念的 occurrences 引用同一 job_id 即相连)。

    单一来源:供 FastAPI 路由 / MCP 工具共用,避免共现推导逻辑两处分叉。

    - 节点:{id, term, definition(短), status, is_topic, occurrence_count}。id=term(域内唯一)。
    - 边(无向,去重):
        * 共现边——按 job_id 倒排:同一 job 下出现的每对概念连一条,weight=两者共享的 job 数。
        * 手动 related 叠加——把 related 里的术语名当额外边(实践中多为空)。同一对已有共现边则取权重较大者。
      边按 (source, target) 字典序规范化方向,自连(同名)忽略,related 指向不存在的概念忽略。
    - stats:{node_count, edge_count, isolated_count(度为 0 的节点数)}。
    全程按 domain 作用域;孤立概念(无 occurrences/无共现)仍作为节点保留(度 0)。
    """
    terms = db.list_glossary(domain)

    nodes: list[dict] = []
    node_terms: set[str] = set()
    # job_id -> 在该 job 中出现的概念名集合(共现倒排)。
    by_job: dict[str, set[str]] = {}
    for t in terms:
        term = t["term"]
        if term in node_terms:
            continue
        node_terms.add(term)
        occs = t.get("occurrences") or []
        occ_list = occs if isinstance(occs, list) else []
        nodes.append({
            "id": term,
            "term": term,
            "definition": _short_definition(t.get("definition")),
            "status": t.get("status"),
            "is_topic": bool(t.get("is_topic")),
            "occurrence_count": len(occ_list),
        })
        for o in occ_list:
            jid = (o or {}).get("job_id") if isinstance(o, dict) else None
            if jid:
                by_job.setdefault(jid, set()).add(term)

    # 共现边:每个 job 下两两配对累加权重(= 共享 job 数)。键已字典序规范化方向。
    weights: dict[tuple[str, str], int] = {}
    for members in by_job.values():
        ms = sorted(members)
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                weights[(ms[i], ms[j])] = weights.get((ms[i], ms[j]), 0) + 1

    # 手动 related 叠加:related 术语名当额外边(权重 1),已有共现边则保留较大权重。
    for t in terms:
        src = t["term"]
        for rel in (t.get("related") or []):
            if not isinstance(rel, str) or rel == src or rel not in node_terms:
                continue
            key = (src, rel) if src < rel else (rel, src)
            weights[key] = max(weights.get(key, 0), 1)

    edges = [
        {"source": s, "target": tgt, "weight": w}
        for (s, tgt), w in weights.items()
    ]
    edges.sort(key=lambda e: (-e["weight"], e["source"], e["target"]))

    degree: dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    isolated_count = sum(1 for n in nodes if degree.get(n["id"], 0) == 0)

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "isolated_count": isolated_count,
        },
    }


# ── 检索后端:可插拔(FtsSearch → 未来 VecSearch/HybridSearch)──


class SearchBackend(Protocol):
    """检索后端协议。换实现(如 sqlite-vec 语义)不动 MCP 工具签名。"""

    def search(
        self, query: str, domain: str | None = None, limit: int = 10
    ) -> list[dict]: ...


class FtsSearch:
    """v1 检索后端:包现有 FTS5(db.search_notes)。"""

    def __init__(self, db: Database):
        self._db = db

    def search(
        self, query: str, domain: str | None = None, limit: int = 10
    ) -> list[dict]:
        return search(self._db, query, domain, limit)
