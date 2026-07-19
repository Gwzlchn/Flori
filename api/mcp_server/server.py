"""Flori MCP server。

借鉴 Notion:单 server 管整库 + 工具少而精(search/fetch 式)+ Markdown 输出省 token。
只读工具薄包 api.services.kb(单一来源);domain 作为作用域参数,不做一库一 server。
检索后端可插拔,默认 FtsSearch;换 sqlite-vec 语义检索时工具签名不变。

按库作用域来自 /mcp/{domain} 端点或环境变量 FLORI_MCP_DEFAULT_DOMAIN,仍是同一个 server:
生效 domain 由请求级 contextvar current_domain 与环境变量解析,见 scope_domain。
设了作用域后工具自动锁定该库,无法越库:search 忽略入参 domain,get_note 校验归属,
其余只读工具默认/覆盖 domain;未设作用域(全局 /mcp)行为不变。
"""

from __future__ import annotations

import os
from contextvars import ContextVar

import structlog
from mcp.server.fastmcp import FastMCP

from api.services import kb
from api.services import concepts as concept_service
from api.services.evidence import attach_canonical_evidence
from api.schemas import ConceptTermDetailResponse
from shared.content_maintenance import acquire_service_lease
from shared.db import Database
from shared.storage import StorageBackend

log = structlog.get_logger()

# 当前请求的作用域 domain。HTTP 端点 /mcp/{domain} 经中间件 set;或读环境变量 FLORI_MCP_DEFAULT_DOMAIN。
# 默认 None = 全局(无作用域),工具行为不变。
current_domain: ContextVar[str | None] = ContextVar("flori_mcp_domain", default=None)

# 工具调用计数(best-effort 可观测)
# 用同步 redis 客户端:MCP 工具多为同步函数,FastMCP 在线程池跑;async 工具里此 incr 极快可忽略。
# REDIS_URL 未设(如本地/测试)时懒构造返回 None,静默 no-op。绝不因 redis 缺失/出错破坏工具。
MCP_CALLS_TOTAL_KEY = "mcp:calls:total"


def _mcp_calls_tool_key(name: str) -> str:
    return f"mcp:calls:tool:{name}"


_stats_redis = None  # 进程级懒缓存:None=未尝试 / False=不可用(REDIS_URL 未设或构造失败)


def _get_stats_redis():
    """懒构造同步 redis 客户端,供工具计数;REDIS_URL 未设或不可用返回 None,静默 no-op。"""
    global _stats_redis
    if _stats_redis is None:
        url = os.environ.get("REDIS_URL")
        if not url:
            _stats_redis = False  # 无 REDIS_URL(本地/测试)→ 永不再试
            return None
        try:
            import redis

            # 短超时:计数永远不该拖慢工具响应。decode_responses 不影响 INCR。
            _stats_redis = redis.from_url(
                url, socket_connect_timeout=0.5, socket_timeout=0.5
            )
        except Exception as e:  # noqa: BLE001 — 构造失败即放弃计数,不影响工具
            log.warning("mcp.stats.redis_init_failed", err=str(e))
            _stats_redis = False
            return None
    return _stats_redis or None


def _record_call(name: str) -> None:
    """记一次工具调用到 redis(总计 + 按工具);fire-and-forget,任何异常都吞掉。"""
    r = _get_stats_redis()
    if r is None:
        return
    try:
        pipe = r.pipeline()
        pipe.incr(MCP_CALLS_TOTAL_KEY)
        pipe.incr(_mcp_calls_tool_key(name))
        pipe.execute()
    except Exception:  # noqa: BLE001 — best-effort,绝不因计数破坏工具
        pass


def scope_domain() -> str | None:
    """解析当前生效的作用域 domain:请求级 contextvar 优先,其次环境变量 FLORI_MCP_DEFAULT_DOMAIN,否则 None。

    None = 无作用域(全局 /mcp 未限定),工具按传入参数走。
    非 None = 工具锁定该 domain:search 忽略入参 domain、list_knowledge_bases 只回该库、
    get_note 校验归属、其余只读工具 domain 默认/覆盖为该作用域。
    """
    return current_domain.get(None) or os.environ.get("FLORI_MCP_DEFAULT_DOMAIN") or None


async def get_note_for_scope(
    db: Database, storage: StorageBackend, job_id: str
) -> dict:
    """取笔记并施加当前作用域:有作用域且 job 不属该库 → KeyError(不泄露其它库)。

    抽成模块级函数,既供 get_note 工具复用,也便于直接对作用域校验做单测。
    """
    res = await kb.get_note(db, storage, job_id)
    sc = scope_domain()
    if sc is not None and res.get("domain") != sc:
        raise KeyError(f"job not found: {job_id}")
    return res


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
        作用域端点(/mcp/{domain})下只返回该库一条。
        """
        res = kb.list_knowledge_bases(db)
        sc = scope_domain()
        if sc is not None:
            res = [r for r in res if r.get("domain") == sc]
        _record_call("list_knowledge_bases")
        log.info("mcp.list_knowledge_bases", n=len(res), scope=sc)
        return res

    @mcp.tool()
    async def search(query: str, domain: str | None = None, limit: int = 10) -> list[dict]:
        """在知识库里全文检索内容/笔记,返回候选列表。

        - domain 可选:限定某个知识库(来自 list_knowledge_bases)。
        - 返回 [{title, snippet, job_id, domain, kind}];snippet 内 <mark> 包裹命中片段。
        - 典型用法:先用本工具按关键词找到候选,再用 get_note(job_id) 取整篇 Markdown。
        - 注意:中文 trigram 检索,查询词至少 3 个字符才会命中。
        """
        sc = scope_domain()
        if sc is not None:
            domain = sc  # 作用域端点:强制锁定该库,忽略入参 domain(防越库检索)
        try:
            res = backend.search(query, domain, limit)
        except Exception as e:  # noqa: BLE001 — 工具边界,记录后回抛给 client
            log.warning("mcp.search.error", query=query, domain=domain, err=str(e))
            raise
        refs = [
            (str(item.get("job_id") or ""), str(item.get("kind") or ""))
            for item in res
        ]
        ids_by_note = db.canonical_evidence_ids_for_notes(refs)
        for item, ref in zip(res, refs):
            item["canonical_evidence_ids"] = ids_by_note.get(ref, [])
        await attach_canonical_evidence(db, storage, res)
        _record_call("search")
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
            res = await get_note_for_scope(db, storage, job_id)
        except KeyError:
            log.warning("mcp.get_note.not_found", job_id=job_id, scope=scope_domain())
            raise
        res["canonical_evidence_ids"] = db.canonical_evidence_ids_for_job(job_id)
        await attach_canonical_evidence(db, storage, [res])
        _record_call("get_note")
        log.info("mcp.get_note", job_id=job_id, has_md=bool(res.get("markdown")),
                 scope=scope_domain())
        return res

    @mcp.tool()
    def list_collections(domain: str | None = None) -> list[dict]:
        """列出集合(内容分组/订阅来源);domain 可选限定某知识库。

        返回 [{id, name, domain, job_count, 及订阅集合的 source_type/source_id/last_synced_at/last_sync_status}]。
        """
        sc = scope_domain()
        if sc is not None:
            domain = sc  # 作用域端点:锁定该库
        res = kb.list_collections(db, domain)
        _record_call("list_collections")
        log.info("mcp.list_collections", domain=domain, n=len(res))
        return res

    @mcp.tool()
    def get_glossary(domain: str, status: str | None = None) -> list[dict]:
        """列出某知识库的概念/术语表。

        - domain 必填(来自 list_knowledge_bases);status 可选(如 accepted / review)。
        - 返回 [{term, definition, status, is_topic, occurrence_count}]。要单条详情(出处/相关)用 get_term。
        """
        sc = scope_domain()
        if sc is not None:
            domain = sc  # 作用域端点:锁定该库,忽略入参 domain
        res = kb.get_glossary(db, domain, status)
        _record_call("get_glossary")
        log.info("mcp.get_glossary", domain=domain, n=len(res))
        return res

    @mcp.tool()
    async def get_term(domain: str, term: str) -> ConceptTermDetailResponse | None:
        """取某库单条概念详情，含版本历史与重验后佐证。

        - domain + term 必填(term 来自 get_glossary / search)。未命中返回 null。
        """
        sc = scope_domain()
        if sc is not None:
            domain = sc  # 作用域端点:锁定该库,忽略入参 domain
        projection = await concept_service.project_concept_detail(
            db, storage, domain, term,
        )
        res = (
            ConceptTermDetailResponse.model_validate(projection)
            if projection is not None else None
        )
        _record_call("get_term")
        log.info("mcp.get_term", domain=domain, term=term, found=res is not None)
        return res

    @mcp.tool()
    def concept_timeline(domain: str, granularity: str = "month") -> dict:
        """某库概念时间线:概念按其源内容发布时间分桶计数,看概念何时出现/演化。

        - domain 必填;granularity = day | week | month(默认 month)。
        """
        sc = scope_domain()
        if sc is not None:
            domain = sc  # 作用域端点:锁定该库,忽略入参 domain
        res = kb.concept_timeline(db, domain, granularity)
        _record_call("concept_timeline")
        log.info("mcp.concept_timeline", domain=domain, granularity=granularity)
        return res

    @mcp.tool()
    def concept_graph(domain: str) -> dict:
        """某库概念图谱:节点=概念,边=共现(两概念引用同一 job_id),权重=共享 job 数,叠加手动 related。

        - domain 必填。返回 {nodes:[{id,term,definition,status,is_topic,occurrence_count}],
          edges:[{source,target,weight}], stats:{node_count,edge_count,isolated_count}}。
        - 图谱不逐节点重算佐证;需要版本、佐证或定位时调 get_term。
        """
        sc = scope_domain()
        if sc is not None:
            domain = sc  # 作用域端点:锁定该库,忽略入参 domain
        res = kb.concept_graph(db, domain)
        _record_call("concept_graph")
        log.info("mcp.concept_graph", domain=domain,
                 nodes=res["stats"]["node_count"], edges=res["stats"]["edge_count"])
        return res

    return mcp


def build_default_server(*, stateless_http: bool = False) -> FastMCP:
    """从环境变量 CONFIG_DIR/DATA_DIR(默认与容器一致)构造生产用只读 server。

    storage 用 create_storage:设了 MINIO_URL 即对象存储,否则本地 —— 与 api 服务一致,
    保证 get_note 读到的是同一份笔记产物。
    """
    from shared.config import load_config
    from shared.storage import create_storage

    cfg = load_config(
        config_dir=os.environ.get("CONFIG_DIR", "/data/configs"),
        data_dir=os.environ.get("DATA_DIR", "/data"),
    )
    maintenance_lease = acquire_service_lease(
        db_path=cfg.db_path,
        jobs_dir=cfg.jobs_dir,
        object_bucket=os.environ.get("MINIO_BUCKET"),
        config_root=cfg.prompts_dir,
        owner="mcp-http",
    )
    db: Database | None = None
    try:
        db = Database(cfg.db_path)
        db.init_schema()  # 幂等:表已存在则 no-op
        storage = create_storage(cfg.jobs_dir)
        mcp = build_server(db, storage, stateless_http=stateless_http)
    except BaseException:
        try:
            if db is not None:
                db.close()
        finally:
            maintenance_lease.close()
        raise
    # HTTP ASGI生命周期负责先关DB再释放租约。属性只在默认生产构造路径存在,
    # 注入db/storage的纯工具测试仍保持无全局资源副作用。
    mcp._flori_maintenance_lease = maintenance_lease
    mcp._flori_database = db
    return mcp
