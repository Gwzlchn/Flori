"""Prompt 白盒(Phase 2):列出可编辑 AI 步 + 读/写/删每步 prompt 覆盖。

覆盖按 (scope,domain,pipeline,step) 存 DB prompt_overrides;job 创建时由 api 解析注入
job.json.prompt_overrides(见 shared.db.resolve_prompt_overrides + api/routes/jobs.py),
worker step_base._load_system_prompt 优先用(pure worker 无 DB,只能靠 job 带过去)。
与 29-externalize 的默认模板(configs/prompts/templates/{step}.md)正交:模板是默认骨架,
DB 覆盖是上层 system prompt 覆盖;编辑器把模板当「默认 prompt(只读)」展示供参考。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from shared.config import AppConfig
from shared.db import Database
from api.deps import get_config, get_db, validate_path_segment, verify_token
from api.schemas import PromptOverrideRequest

router = APIRouter(prefix="/api/prompts", tags=["prompts"], dependencies=[Depends(verify_token)])


def _ai_steps(config: AppConfig) -> list[tuple[str, str, str | None, str | None]]:
    """枚举四条 pipeline 的 AI 步(pool=='ai')→ [(pipeline, step_key, label, pool)]。
    模板/'.'前缀/default 不算 pipeline(与 GET /api/pipelines 同口径)。"""
    out: list[tuple[str, str, str | None, str | None]] = []
    for name, pc in (config.pipelines or {}).items():
        if name.startswith(".") or name == "default":
            continue
        steps = (pc or {}).get("steps")
        if not isinstance(steps, list):
            continue
        for s in steps:
            if s.get("pool") == "ai":
                out.append((name, s.get("name"), s.get("label"), s.get("pool")))
    return out


def _find_step(config: AppConfig, pipeline: str, step: str) -> dict | None:
    pc = (config.pipelines or {}).get(pipeline)
    if not isinstance(pc, dict):
        return None
    for s in pc.get("steps", []):
        if s.get("name") == step:
            return s
    return None


def _default_template(config: AppConfig, step: str) -> str | None:
    """该步外置默认 user-prompt 模板(29-externalize:templates/{step}.md);无则 None(内联默认)。"""
    p = config.prompts_dir / "templates" / f"{step}.md"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


@router.get("")
async def list_prompts(
    config: AppConfig = Depends(get_config), db: Database = Depends(get_db)
):
    """列各 pipeline 的可编辑 AI 步 + 已有哪些覆盖(供设置页画 DAG/列表 + 标 ●)。"""
    overrides = await asyncio.to_thread(db.list_prompt_overrides)
    by_step: dict[tuple[str, str], list[dict]] = {}
    for o in overrides:
        by_step.setdefault((o["pipeline"], o["step"]), []).append(
            {"scope": o["scope"], "domain": o["domain"]}
        )
    steps = [
        {
            "pipeline": pipeline, "step": key, "label": label, "pool": pool,
            "is_ai": True,
            "has_template": _default_template(config, key) is not None,
            "overrides": by_step.get((pipeline, key), []),
        }
        for pipeline, key, label, pool in _ai_steps(config)
    ]
    return {"steps": steps}


@router.get("/{pipeline}/{step}")
async def get_prompt(
    pipeline: str,
    step: str,
    scope: str = "global",
    domain: str | None = None,
    config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    """单步详情:默认模板(只读)+ 该 (scope,domain) 当前覆盖(无则 null)。"""
    validate_path_segment(pipeline, "pipeline")
    validate_path_segment(step, "step")
    s = _find_step(config, pipeline, step)
    if s is None:
        raise HTTPException(404, f"step '{step}' not found in pipeline '{pipeline}'")
    ov = await asyncio.to_thread(db.get_prompt_override, scope, domain, pipeline, step)
    return {
        "pipeline": pipeline,
        "step": step,
        "label": s.get("label"),
        "pool": s.get("pool"),
        "is_ai": s.get("pool") == "ai",
        # 默认 prompt 来源:外置 user-prompt 模板(29-externalize);system 默认无(各步内联在 user)。
        "default_template": _default_template(config, step),
        "default_system": None,
        "override": ov,
    }


@router.put("/{pipeline}/{step}")
async def put_prompt(
    pipeline: str,
    step: str,
    req: PromptOverrideRequest,
    config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    """存该步 system prompt 覆盖。content 为空(纯空白)= 删除覆盖(恢复默认)。"""
    validate_path_segment(pipeline, "pipeline")
    validate_path_segment(step, "step")
    if req.domain:
        validate_path_segment(req.domain, "domain")
    s = _find_step(config, pipeline, step)
    if s is None:
        raise HTTPException(404, f"step '{step}' not found in pipeline '{pipeline}'")
    if s.get("pool") != "ai":
        raise HTTPException(400, f"step '{step}' is not an AI step")
    if req.scope == "domain" and not (req.domain or "").strip():
        raise HTTPException(400, "domain scope requires a non-empty domain")
    content = req.content or ""
    if not content.strip():
        await asyncio.to_thread(
            db.delete_prompt_override, req.scope, req.domain, pipeline, step
        )
        return {"status": "deleted", "pipeline": pipeline, "step": step}
    await asyncio.to_thread(
        db.set_prompt_override, req.scope, req.domain, pipeline, step, content
    )
    return {
        "status": "saved", "pipeline": pipeline, "step": step,
        "scope": req.scope, "domain": (req.domain or "") if req.scope == "domain" else "",
    }


@router.delete("/{pipeline}/{step}")
async def delete_prompt(
    pipeline: str,
    step: str,
    scope: str = "global",
    domain: str | None = None,
    db: Database = Depends(get_db),
):
    """删该步 (scope,domain) 覆盖(恢复默认)。无则 no-op。"""
    validate_path_segment(pipeline, "pipeline")
    validate_path_segment(step, "step")
    if domain:
        validate_path_segment(domain, "domain")
    await asyncio.to_thread(db.delete_prompt_override, scope, domain, pipeline, step)
    return {"status": "deleted", "pipeline": pipeline, "step": step}
