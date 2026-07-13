"""Prompt 白盒:列出可编辑 AI 步 + 读/写/删每步 prompt 覆盖。

覆盖按 (scope,domain,pipeline,step) 存 DB prompt_overrides。job 创建时由 api 解析注入
job.json.prompt_overrides,见 shared.db.resolve_prompt_overrides 与 api/routes/jobs.py;
worker step_base 派发时优先用(pure worker 无 DB,只能靠 job 带过去)。
所见即所改:覆盖替换的就是编辑器展示的那段默认 user-prompt 模板,即 templates/{step}.md
及 .zh/.translate/.vision 等变体。worker 回退序 = DB 覆盖 > 模板文件 > 内联默认,
见 step_base._load_prompt_template。无模板的步(评审等 prompt 内联)覆盖回落为 system
prompt,见 step_base._load_system_prompt。模板读取双保险:运行时 prompts_dir/templates 优先,
缺失回退镜像烤入的 config_dir/prompts/templates,api 容器即使没挂 templates 也能看到默认。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from shared.config import AppConfig
from shared.db import (
    Database,
    PROMPT_VERSION_EXCLUSIVE_MAX,
    PROMPT_VERSION_MIN,
    PromptVersionExhaustedError,
)
from api.deps import get_config, get_db, validate_path_segment, verify_token
from api.schemas import PromptActivateRequest, PromptOverrideRequest

router = APIRouter(prefix="/api/prompts", tags=["prompts"], dependencies=[Depends(verify_token)])


def _ai_steps(config: AppConfig) -> list[tuple[str, str, str | None, str | None]]:
    """枚举各 pipeline 的 AI 步(pool=='ai'),返回 (pipeline, step_key, label, pool)。
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


def _template_dirs(config: AppConfig) -> list:
    """默认 prompt 模板搜索目录(双保险,按优先级):
    1. prompts_dir/templates(/data/prompts/templates,运行时挂载,改文件即生效);
    2. config_dir/prompts/templates(/app/configs/prompts/templates,镜像烤入,永不被 /data 命名卷 shadow)。
    api 容器即使没挂运行时目录,仍能从烤入目录读到默认模板。"""
    return [config.prompts_dir / "templates", config.config_dir / "prompts" / "templates"]


def _read_first(paths: list) -> str | None:
    """按序返回首个存在文件的内容;都不存在/读失败则 None。"""
    for p in paths:
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def _step_templates(config: AppConfig, step: str) -> list[dict]:
    """该步全部外置默认 user-prompt 模板:主模板 {step}.md 加变体 {step}.<变体>.md,
    如 08_punctuate.zh、11_smart.vision。按 name 去重(prompts_dir 优先于烤入),主模板排在变体前。
    供白盒展示全变体。"""
    by_name: dict[str, str] = {}
    for d in _template_dirs(config):
        if not d.is_dir():
            continue
        for p in sorted(d.glob(f"{step}*.md")):
            name = p.stem
            # 仅认 {step} 或 {step}.<变体>(防 11_smart 误收 11_smarter 这类前缀邻居)。
            if name != step and not name.startswith(step + "."):
                continue
            if name in by_name:
                continue
            try:
                by_name[name] = p.read_text(encoding="utf-8")
            except OSError:
                continue
    ordered = sorted(by_name, key=lambda n: (n != step, n))  # 主模板优先,其余字典序
    return [{"name": n, "content": by_name[n]} for n in ordered]


def _default_template(config: AppConfig, step: str) -> str | None:
    """该步主默认 user-prompt 模板内容(向后兼容字段):{step}.md;无主则取首个变体;全无则 None。"""
    tpls = _step_templates(config, step)
    if not tpls:
        return None
    for t in tpls:
        if t["name"] == step:
            return t["content"]
    return tpls[0]["content"]


def _default_system(config: AppConfig, step: str) -> str | None:
    """该步外置 system prompt 钩子内容(prompts_dir/{step}.md,镜像烤入 config_dir/prompts/{step}.md);
    无文件则 None(各步 system 默认内联/为 None,真正起作用的默认在 user-prompt 模板)。"""
    return _read_first([
        config.prompts_dir / f"{step}.md",
        config.config_dir / "prompts" / f"{step}.md",
    ])


@router.get("")
async def list_prompts(
    config: AppConfig = Depends(get_config), db: Database = Depends(get_db)
):
    """列各 pipeline 的可编辑 AI 步 + 已有哪些覆盖(供设置页画 DAG/列表并标圆点角标)。"""
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
            "has_template": bool(_step_templates(config, key)),
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
    """单步详情:默认模板(只读,全变体)+ system 默认钩子 + 该 (scope,domain) 当前覆盖 + 版本历史。
    system 默认有钩子文件才非空。active_version 为当前激活版本号,versions 为全部历史版本元信息;
    无覆盖时 None/空。"""
    validate_path_segment(pipeline, "pipeline")
    validate_path_segment(step, "step")
    s = _find_step(config, pipeline, step)
    if s is None:
        raise HTTPException(404, f"step '{step}' not found in pipeline '{pipeline}'")
    ov = await asyncio.to_thread(db.get_prompt_override, scope, domain, pipeline, step)
    versions = await asyncio.to_thread(
        db.list_prompt_override_versions, scope, domain, pipeline, step
    )
    templates = _step_templates(config, step)
    return {
        "pipeline": pipeline,
        "step": step,
        "label": s.get("label"),
        "pool": s.get("pool"),
        "is_ai": s.get("pool") == "ai",
        # 默认 prompt = 外置 user-prompt 模板(覆盖即替换它,所见即所改)。default_template 为主模板
        # (向后兼容);default_templates 列全变体 [{name,content}]。default_system = 外置 system 钩子(多数步 null)。
        "default_template": _default_template(config, step),
        "default_templates": templates,
        "default_system": _default_system(config, step),
        "override": ov,
        # 版本管理:active_version=主表指向的激活版本号(无覆盖 None);versions=该 (scope,domain) 全部
        # 历史版本元信息 [{version,note,created_at}](version 升序,不含 content,内容经 versions/{version} 取)。
        "active_version": (ov.get("version") if ov else None),
        "versions": versions,
    }


@router.get("/{pipeline}/{step}/versions/{version}")
async def get_prompt_version(
    pipeline: str,
    step: str,
    version: Annotated[
        int,
        Path(ge=PROMPT_VERSION_MIN, lt=PROMPT_VERSION_EXCLUSIVE_MAX),
    ],
    scope: str = "global",
    domain: str | None = None,
    db: Database = Depends(get_db),
):
    """查看某历史版本的完整内容(供编辑器选历史版本载入 textarea 后基于它改)。未命中 404。"""
    validate_path_segment(pipeline, "pipeline")
    validate_path_segment(step, "step")
    if domain:
        validate_path_segment(domain, "domain")
    row = await asyncio.to_thread(
        db.get_prompt_override_version, scope, domain, pipeline, step, version
    )
    if row is None:
        raise HTTPException(404, f"version {version} not found for {pipeline}/{step}")
    return {
        "version": row["version"], "content": row["content"],
        "note": row["note"], "created_at": row["created_at"],
    }


@router.put("/{pipeline}/{step}")
async def put_prompt(
    pipeline: str,
    step: str,
    req: PromptOverrideRequest,
    config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    """存该步 prompt 覆盖(替换展示的默认 user-prompt 模板;无模板步则作 system),带版本管理。
    content 为空或纯空白即删除覆盖,恢复默认并清全部版本。否则按 mode:
    'overwrite'(默认)改当前激活版本内容;'new'=另存为新版本(version=max+1 并激活)。返回 active_version。"""
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
    mode = req.mode if req.mode in ("overwrite", "new") else "overwrite"
    try:
        version = await asyncio.to_thread(
            db.set_prompt_override,
            req.scope,
            req.domain,
            pipeline,
            step,
            content,
            mode,
            req.note,
        )
    except PromptVersionExhaustedError as exc:
        raise HTTPException(409, "prompt version limit reached") from exc
    return {
        "status": "saved", "pipeline": pipeline, "step": step,
        "scope": req.scope, "domain": (req.domain or "") if req.scope == "domain" else "",
        "active_version": version,
    }


@router.post("/{pipeline}/{step}/activate")
async def activate_prompt(
    pipeline: str,
    step: str,
    req: PromptActivateRequest,
    config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    """切换该步 (scope,domain) 的激活指针(非破坏,历史版本始终保留)。
    - version=数字:把该历史版本设为当前激活(派发用它);该版本不存在则 404。
    - version=null:停用覆盖回内置默认(deactivate;主表指针清掉,历史全留,下拉仍能再激活)。
    返回新 active_version(null=已回内置默认)。"""
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
    if req.version is None:
        await asyncio.to_thread(
            db.deactivate_prompt_override, req.scope, req.domain, pipeline, step
        )
        return {
            "status": "deactivated", "pipeline": pipeline, "step": step,
            "scope": req.scope, "domain": (req.domain or "") if req.scope == "domain" else "",
            "active_version": None,
        }
    ok = await asyncio.to_thread(
        db.set_active_prompt_version, req.scope, req.domain, pipeline, step, req.version
    )
    if not ok:
        raise HTTPException(404, f"version {req.version} not found for {pipeline}/{step}")
    return {
        "status": "activated", "pipeline": pipeline, "step": step,
        "scope": req.scope, "domain": (req.domain or "") if req.scope == "domain" else "",
        "active_version": req.version,
    }


@router.delete("/{pipeline}/{step}")
async def delete_prompt(
    pipeline: str,
    step: str,
    scope: str = "global",
    domain: str | None = None,
    db: Database = Depends(get_db),
):
    """彻底删除该步 (scope,domain) 覆盖,连同其全部历史版本一并清除。无则 no-op。
    注:恢复默认/回内置默认请用 POST .../activate {version:null}(非破坏,保留历史);
    此 DELETE 仅用于真正要丢弃所有版本的场景。"""
    validate_path_segment(pipeline, "pipeline")
    validate_path_segment(step, "step")
    if domain:
        validate_path_segment(domain, "domain")
    await asyncio.to_thread(db.delete_prompt_override, scope, domain, pipeline, step)
    return {"status": "deleted", "pipeline": pipeline, "step": step}
