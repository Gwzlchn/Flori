"""Flori MCP server(v1)。

借鉴 Notion:单 server 管整库 + 工具少而精(search/fetch 式)+ Markdown 输出省 token。
3 个只读工具薄包 api.services.kb(单一来源);domain 作为作用域参数(非一库一 server)。
检索后端可插拔(默认 FtsSearch;未来换 sqlite-vec 语义,工具签名不变)。
"""

from __future__ import annotations

import os

import structlog
from mcp.server.fastmcp import FastMCP

from api.services import kb
from shared.db import Database
from shared.storage import StorageBackend

log = structlog.get_logger()


def build_server(
    db: Database,
    storage: StorageBackend,
    search_backend: kb.SearchBackend | None = None,
    *,
    stateless_http: bool = False,
) -> FastMCP:
    """构造 FastMCP server(可注入 db/storage/检索后端,便于测试与未来替换)。

    stateless_http=True:streamable-http 无状态模式(每请求独立),适合放在反代后面。
    """
    mcp = FastMCP("flori", stateless_http=stateless_http)
    backend: kb.SearchBackend = search_backend or kb.FtsSearch(db)

    @mcp.tool()
    def list_knowledge_bases() -> list[dict]:
        """列出所有知识库(domain)及其 集合/内容/概念/订阅 计数。

        agent 探索的起点:先用它知道有哪些知识库,再用 search 在某个库里检索。
        """
        res = kb.list_knowledge_bases(db)
        log.info("mcp.list_knowledge_bases", n=len(res))
        return res

    @mcp.tool()
    def search(query: str, domain: str | None = None, limit: int = 10) -> list[dict]:
        """在知识库里全文检索内容/笔记,返回候选列表。

        - domain 可选:限定某个知识库(来自 list_knowledge_bases)。
        - 返回 [{title, snippet, job_id, domain, kind}];snippet 内 <mark> 包裹命中片段。
        - 典型用法:先用本工具按关键词找到候选,再用 get_note(job_id) 取整篇 Markdown。
        - 注意:中文 trigram 检索,查询词至少 3 个字符才会命中。
        """
        try:
            res = backend.search(query, domain, limit)
        except Exception as e:  # noqa: BLE001 — 工具边界,记录后回抛给 client
            log.warning("mcp.search.error", query=query, domain=domain, err=str(e))
            raise
        log.info("mcp.search", query=query, domain=domain, n=len(res))
        return res

    @mcp.tool()
    async def get_note(job_id: str) -> dict:
        """按 job_id 取一篇笔记的完整智能笔记 Markdown + 元信息。

        - job_id 来自 search 的结果。
        - 返回 {job_id, title, domain, collection_id, content_type, status, note_file, markdown}。
        - markdown 为 null 表示该内容的智能笔记尚未生成(如 job 未完成)。
        """
        try:
            res = await kb.get_note(db, storage, job_id)
        except KeyError:
            log.warning("mcp.get_note.not_found", job_id=job_id)
            raise
        log.info("mcp.get_note", job_id=job_id, has_md=bool(res.get("markdown")))
        return res

    @mcp.tool()
    def list_collections(domain: str | None = None) -> list[dict]:
        """列出集合(内容分组/订阅来源);domain 可选限定某知识库。

        返回 [{id, name, domain, job_count, 及订阅集合的 source_type/source_id/last_synced_at/last_sync_status}]。
        """
        res = kb.list_collections(db, domain)
        log.info("mcp.list_collections", domain=domain, n=len(res))
        return res

    @mcp.tool()
    def get_glossary(domain: str, status: str | None = None) -> list[dict]:
        """列出某知识库的概念/术语表。

        - domain 必填(来自 list_knowledge_bases);status 可选(如 accepted / review)。
        - 返回 [{term, definition, status, is_topic, occurrence_count}]。要单条详情(出处/相关)用 get_term。
        """
        res = kb.get_glossary(db, domain, status)
        log.info("mcp.get_glossary", domain=domain, n=len(res))
        return res

    @mcp.tool()
    def get_term(domain: str, term: str) -> dict | None:
        """取某库单条术语/概念详情(定义 + 出处 occurrences + 相关 related + 状态)。

        - domain + term 必填(term 来自 get_glossary / search)。未命中返回 null。
        """
        res = kb.get_term(db, domain, term)
        log.info("mcp.get_term", domain=domain, term=term, found=res is not None)
        return res

    @mcp.tool()
    def concept_timeline(domain: str, granularity: str = "month") -> dict:
        """某库概念时间线:概念按其源内容发布时间分桶计数,看「概念何时出现/演化」。

        - domain 必填;granularity = day | week | month(默认 month)。
        """
        res = kb.concept_timeline(db, domain, granularity)
        log.info("mcp.concept_timeline", domain=domain, granularity=granularity)
        return res

    return mcp


def build_default_server(*, stateless_http: bool = False) -> FastMCP:
    """从环境(CONFIG_DIR/DATA_DIR,默认与容器一致)构造生产用 server(只读)。

    storage 用 create_storage:设了 MINIO_URL 即对象存储,否则本地 —— 与 api 服务一致,
    保证 get_note 读到的是同一份笔记产物。
    """
    from shared.config import load_config
    from shared.storage import create_storage

    cfg = load_config(
        config_dir=os.environ.get("CONFIG_DIR", "/data/configs"),
        data_dir=os.environ.get("DATA_DIR", "/data"),
    )
    db = Database(cfg.db_path)
    db.init_schema()  # 幂等:表已存在则 no-op
    storage = create_storage(cfg.jobs_dir)
    return build_server(db, storage, stateless_http=stateless_http)
