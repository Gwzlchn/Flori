"""Profile 管理路由。"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException

from shared.config import AppConfig
from api.deps import get_config, validate_path_segment, verify_token
from api.schemas import ProfileUpdateRequest, TermAddRequest

router = APIRouter(prefix="/api/profiles", tags=["profiles"], dependencies=[Depends(verify_token)])


def _profiles_dir(config: AppConfig) -> Path:
    return config.prompts_dir / "profiles"


def sync_term_to_profile(
    config: AppConfig, domain: str, term: str, definition: str = ""
) -> None:
    """把一条术语写进该 domain 的 Profile.terminology(供术语采纳时复用)。
    Profile 不存在则新建;条目格式 "术语: 定义",按裸 term 前缀去重,幂等。"""
    validate_path_segment(domain, "domain")
    pdir = _profiles_dir(config)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{domain}.yaml"

    data: dict = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["domain"] = domain

    entry = f"{term}: {definition}" if definition else term
    terms = data.get("terminology", [])
    # 同一 term(带或不带定义)只保留一条,新定义覆盖旧条目。
    terms = [t for t in terms if t != term and not t.startswith(f"{term}: ")]
    terms.append(entry)
    data["terminology"] = terms

    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


@router.get("")
async def list_profiles(config: AppConfig = Depends(get_config)):
    pdir = _profiles_dir(config)
    if not pdir.exists():
        return []
    profiles = []
    for f in sorted(pdir.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        profiles.append({
            "domain": f.stem,
            "role": data.get("role", ""),
            "terminology_count": len(data.get("terminology", [])),
        })
    return profiles


@router.get("/{domain}")
async def get_profile(domain: str, config: AppConfig = Depends(get_config)):
    validate_path_segment(domain, "domain")
    path = _profiles_dir(config) / f"{domain}.yaml"
    if not path.exists():
        raise HTTPException(404, f"profile '{domain}' not found")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@router.put("/{domain}")
async def update_profile(domain: str, req: ProfileUpdateRequest, config: AppConfig = Depends(get_config)):
    validate_path_segment(domain, "domain")
    pdir = _profiles_dir(config)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{domain}.yaml"

    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    data["domain"] = domain
    if req.role is not None:
        data["role"] = req.role
    if req.domain_context is not None:
        data["domain_context"] = req.domain_context
    if req.output_style is not None:
        data["output_style"] = req.output_style
    if req.terminology is not None:
        data["terminology"] = req.terminology
    if req.do_not is not None:
        data["do_not"] = req.do_not
    for k in ("display_name", "icon", "color", "description"):
        v = getattr(req, k)
        if v is not None:
            data[k] = v

    path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    return data


@router.post("/{domain}/terms")
async def add_term(domain: str, req: TermAddRequest, config: AppConfig = Depends(get_config)):
    validate_path_segment(domain, "domain")
    path = _profiles_dir(config) / f"{domain}.yaml"
    if not path.exists():
        raise HTTPException(404, f"profile '{domain}' not found")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    terms = data.get("terminology", [])
    if req.term not in terms:
        terms.append(req.term)
        data["terminology"] = terms
        path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8")

    return {"terminology": terms}


@router.delete("/{domain}/terms/{term}")
async def delete_term(domain: str, term: str, config: AppConfig = Depends(get_config)):
    validate_path_segment(domain, "domain")
    path = _profiles_dir(config) / f"{domain}.yaml"
    if not path.exists():
        raise HTTPException(404, f"profile '{domain}' not found")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    terms = data.get("terminology", [])
    terms = [t for t in terms if t != term]
    data["terminology"] = terms
    path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    return {"terminology": terms}
