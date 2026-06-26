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
