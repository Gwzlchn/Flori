"""api 侧 LiteLLM 价表缓存.

启动时优先从 MinIO 载入缓存,之后每天刷新一次并写回 MinIO。计费在 api 侧完成:
纯网关 worker 不直连 MinIO/Redis,record_ai_usage 依据本表补 cost。claude-cli
CLI 路径使用 CLI total_cost_usd,未命中或空表时回退 worker 上报值。对象落 MinIO
bucket 内 _pricing/litellm.json.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import structlog

from shared.pricing import LITELLM_PRICING_URL, cost_from_table, fetch_litellm_pricing

_log = structlog.get_logger(component="pricing")

_PRICING_JOB = "_pricing"        # 伪 job_id,经 storage.write_file 落 MinIO/_pricing/.
_PRICING_FILE = "litellm.json"
_PRICING_META = "litellm.meta.json"   # sidecar: {"fetched_at": ISO};价表本体不带时间戳.
_REFRESH_SEC = 86400             # 每天拉一次


class PricingStore:
    def __init__(self) -> None:
        self._table: dict = {}
        self._fetched_at: datetime | None = None   # 末次 refresh 成功取得新表的时间.

    @property
    def ready(self) -> bool:
        return bool(self._table)

    @property
    def model_count(self) -> int:
        return len(self._table)

    @property
    def source_url(self) -> str:
        return LITELLM_PRICING_URL

    def status(self) -> dict:
        """价表状态(供 GET /api/pricing + 手动更新后回显)。fetched_at 为 ISO 串或 None。"""
        return {
            "ready": self.ready,
            "model_count": self.model_count,
            "fetched_at": self._fetched_at.isoformat() if self._fetched_at else None,
            "source_url": self.source_url,
        }

    def raw(self) -> dict:
        """原始价表 dict(供 GET /api/pricing/raw 全量查看)。空表返回 {}。"""
        return self._table

    def cost(self, provider: str, model: str, input_tokens: int, output_tokens: int,
             cache_creation_tokens: int = 0, cache_read_tokens: int = 0) -> float | None:
        """据当前 LiteLLM 表算成本;空表/未命中返回 None(调用方回退)。"""
        if not self._table:
            return None
        return cost_from_table(self._table, provider, model, input_tokens, output_tokens,
                               cache_creation_tokens, cache_read_tokens)

    async def load_from_storage(self, storage) -> bool:
        """启动快速载入(无网络):读 MinIO 缓存(价表本体 + sidecar 的 fetched_at)。成功返回 True。"""
        try:
            raw = await storage.read_file(_PRICING_JOB, _PRICING_FILE)
            if raw:
                table = json.loads(raw)
                if isinstance(table, dict) and table:
                    self._table = table
                    self._fetched_at = await self._load_meta_fetched_at(storage)
                    _log.info("pricing_loaded", models=len(table), source="minio")
                    return True
        except Exception as e:
            _log.warning("pricing_load_failed", error=str(e)[:200])
        return False

    async def _load_meta_fetched_at(self, storage) -> datetime | None:
        """读 sidecar litellm.meta.json 的 fetched_at(ISO). 读不到或解析失败返回 None."""
        try:
            raw = await storage.read_file(_PRICING_JOB, _PRICING_META)
            if raw:
                meta = json.loads(raw)
                ts = (meta or {}).get("fetched_at") if isinstance(meta, dict) else None
                if isinstance(ts, str) and ts:
                    return datetime.fromisoformat(ts)
        except Exception as e:
            _log.warning("pricing_meta_load_failed", error=str(e)[:200])
        return None

    async def refresh(self, storage) -> bool:
        """拉 LiteLLM 最新并写回 MinIO. 失败保留旧表,避免 cost 归零."""
        try:
            table = await fetch_litellm_pricing()
        except Exception as e:
            _log.warning("pricing_fetch_failed", error=str(e)[:200])
            return False
        if not isinstance(table, dict) or not table:
            return False
        self._table = table
        now = datetime.now(timezone.utc)
        self._fetched_at = now
        try:
            await storage.write_file(
                _PRICING_JOB, _PRICING_FILE,
                json.dumps(table, ensure_ascii=False).encode("utf-8"),
            )
            # sidecar 同写更新时间,载入时据此回填 _fetched_at.
            await storage.write_file(
                _PRICING_JOB, _PRICING_META,
                json.dumps({"fetched_at": now.isoformat()}).encode("utf-8"),
            )
        except Exception as e:
            _log.warning("pricing_persist_failed", error=str(e)[:200])
        _log.info("pricing_refreshed", models=len(table))
        return True

    async def daily_loop(self, storage) -> None:
        """启动先从 MinIO 载入(快,warm start),再拉一次最新;此后每 24h 刷新。"""
        await self.load_from_storage(storage)
        while True:
            try:
                await self.refresh(storage)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("pricing_loop_error")
            await asyncio.sleep(_REFRESH_SEC)
