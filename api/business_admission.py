"""Job 业务入口在读取大请求体前执行认证、限流和 Worker 门禁。"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from api.deps import authenticate_api_request
from shared.job_admission import pipeline_requirements, workers_cover_pipeline
from shared.source_detect import detect_source
from shared.source_registry import (
    CONTENT_TYPE_NAMES,
    SourceRegistryError,
    default_content_type,
    pipeline_for_content_type,
    validate_job_route,
)


class BusinessAdmissionError(HTTPException):
    """携带受信任机器码的业务入口拒绝。"""

    def __init__(self, status_code: int, error_code: str, detail: str, headers=None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code


def job_admission_guard(kind: str):
    """给受保护 endpoint 写显式门禁标记,避免函数重命名静默绕过。"""
    if kind not in {"create", "upload"}:
        raise ValueError("invalid job admission kind")

    def decorate(endpoint: Callable) -> Callable:
        setattr(endpoint, "__flori_job_admission__", kind)
        return endpoint

    return decorate


def _rate_config() -> tuple[int, int]:
    return (
        max(1, int(os.environ.get("FLORI_JOBS_CREATE_RATE_LIMIT", "30"))),
        max(1, int(os.environ.get("FLORI_JOBS_CREATE_RATE_WINDOW_SEC", "60"))),
    )


async def _workers(redis: Any) -> list[dict]:
    worker_ids = await redis.list_worker_ids()
    values = [await redis.get_worker_info(worker_id) for worker_id in worker_ids]
    return [value for value in values if isinstance(value, dict)]


async def ensure_job_workers(
    *,
    redis: Any,
    config: Any,
    content_type: str,
    source: str,
    url: str | None,
    domain: str = "general",
    style_tags: list[str] | None = None,
    smart_note: bool | None = None,
) -> None:
    pipeline = pipeline_for_content_type(content_type)
    try:
        pipelines = config.pipelines
        if not pipeline or not isinstance(pipelines, dict) or pipeline not in pipelines:
            raise ValueError("pipeline admission configuration unavailable")
        resolved_smart = smart_note if smart_note is not None else content_type != "article"
        requirements = pipeline_requirements(
            config, pipeline, source=source, url=url, domain=domain,
            style_tags=style_tags or [], flags={"smart_note": bool(resolved_smart)},
        )
        available = await _workers(redis)
        covered = workers_cover_pipeline(available, requirements, config)
    except Exception as exc:
        raise BusinessAdmissionError(
            503, "unavailable", "worker availability check unavailable",
        ) from exc
    if not covered:
        raise BusinessAdmissionError(
            503, "no_workers", "no workers can execute the requested pipeline",
        )


def _content_type_from_create(
    payload: dict,
) -> tuple[str | None, str, str | None] | None:
    url = payload.get("url")
    content_type = payload.get("content_type")
    if url is not None and not isinstance(url, str):
        return None
    if content_type is not None and not isinstance(content_type, str):
        return None
    if not isinstance(payload.get("domain", "general"), str):
        return None
    style_tags = payload.get("style_tags", [])
    if not isinstance(style_tags, list) or not all(isinstance(tag, str) for tag in style_tags):
        return None
    smart_note = payload.get("smart_note")
    if smart_note is not None and not isinstance(smart_note, bool):
        return None
    source = detect_source(url or "")
    content_type = content_type or default_content_type(source)
    return content_type, source, url


class JobAdmissionRoute(APIRoute):
    """只包 create/upload,并保证 multipart 拒绝路径不触发 receive。"""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()
        admission_kind = getattr(self.endpoint, "__flori_job_admission__", None)
        if admission_kind not in {"create", "upload"}:
            return original

        async def handler(request: Request):
            principal = authenticate_api_request(request)
            request.state.api_principal = principal
            try:
                limit, window = _rate_config()
                allowed, _count, retry_after = await request.app.state.redis.consume_rate_limit(
                    "jobs:create", principal, limit, window,
                )
            except Exception as exc:
                if isinstance(exc, BusinessAdmissionError):
                    raise
                raise BusinessAdmissionError(
                    503, "unavailable", "rate limiter unavailable",
                ) from exc
            if not allowed:
                raise BusinessAdmissionError(
                    429, "rate_limited", "job creation rate limit exceeded",
                    headers={"Retry-After": str(max(1, int(retry_after)))},
                )

            if admission_kind == "upload":
                content_type = request.query_params.get("content_type")
                if content_type not in CONTENT_TYPE_NAMES:
                    raise BusinessAdmissionError(
                        422, "invalid_request", "content_type query is required",
                    )
                try:
                    validate_job_route("upload", content_type)
                except SourceRegistryError as exc:
                    raise BusinessAdmissionError(
                        422, "invalid_request", f"unsupported_source: {exc}",
                    ) from exc
                await ensure_job_workers(
                    redis=request.app.state.redis, config=request.app.state.config,
                    content_type=content_type, source="upload", url=None,
                )
            else:
                try:
                    payload = await request.json()
                except RecursionError as exc:
                    raise BusinessAdmissionError(
                        422, "invalid_request", "request JSON is too deeply nested",
                    ) from exc
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return await original(request)
                if isinstance(payload, dict):
                    projection = _content_type_from_create(payload)
                    if projection is not None:
                        content_type, source, url = projection
                    else:
                        content_type = None
                    if projection is not None and content_type in CONTENT_TYPE_NAMES:
                        try:
                            validate_job_route(source, content_type)
                        except SourceRegistryError:
                            return await original(request)
                        await ensure_job_workers(
                            redis=request.app.state.redis, config=request.app.state.config,
                            content_type=content_type, source=source, url=url,
                            domain=str(payload.get("domain") or "general"),
                            style_tags=payload.get("style_tags") if isinstance(payload.get("style_tags"), list) else [],
                            smart_note=payload.get("smart_note") if isinstance(payload.get("smart_note"), bool) else None,
                        )
            return await original(request)

        return handler
