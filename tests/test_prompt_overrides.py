"""prompt 白盒 Phase 2:DB prompt_overrides + 解析注入 + API + step_base 回退顺序。

覆盖:DB 层(set/get/list/delete/resolve 的 global↔domain 优先级 + 归一)、step_base
_load_system_prompt 回退(DB 注入 > {step}.md > None)+ template.source、API 端点
(列/读/写/删/校验)、扩展后的 GET /api/pipelines(is_ai/has_override)、create_job 注入。
"""

from __future__ import annotations

import json

import pytest

from shared.db import Database
from shared.step_base import StepBase


# ── DB 层 ──


@pytest.fixture
def pdb(tmp_path):
    d = Database(tmp_path / "p.db")
    d.init_schema()
    yield d
    d.close()


class TestPromptOverrideDB:
    def test_set_get_roundtrip(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "hello")
        o = pdb.get_prompt_override("global", None, "video", "11_smart")
        assert o["content"] == "hello"
        assert o["scope"] == "global" and o["domain"] == ""

    def test_global_scope_ignores_domain(self, pdb):
        # scope=global 时传入的 domain 被归一到 ''(同一条记录)
        pdb.set_prompt_override("global", "finance", "video", "11_smart", "g")
        assert pdb.get_prompt_override("global", "anything", "video", "11_smart")["content"] == "g"

    def test_domain_scope_without_domain_falls_back_global(self, pdb):
        pdb.set_prompt_override("domain", "", "video", "11_smart", "x")
        assert pdb.get_prompt_override("global", None, "video", "11_smart")["content"] == "x"
        assert pdb.get_prompt_override("domain", "finance", "video", "11_smart") is None

    def test_resolve_domain_wins_over_global(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "G")
        pdb.set_prompt_override("domain", "finance", "video", "11_smart", "D")
        pdb.set_prompt_override("global", None, "video", "12_review", "GR")
        r_fin = pdb.resolve_prompt_overrides("video", "finance")
        assert r_fin["11_smart"] == "D"      # domain 覆盖优先
        assert r_fin["12_review"] == "GR"     # 该步无 domain 覆盖 → global 兜底
        r_ml = pdb.resolve_prompt_overrides("video", "ml")
        assert r_ml["11_smart"] == "G"        # ml 无 domain 覆盖 → global

    def test_resolve_filters_empty_and_other_pipeline(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "")     # 空 = 无覆盖
        pdb.set_prompt_override("global", None, "paper", "05_smart_paper", "P")
        assert pdb.resolve_prompt_overrides("video", "general") == {}
        assert pdb.resolve_prompt_overrides("paper", "general") == {"05_smart_paper": "P"}

    def test_delete_restores_default(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "x")
        pdb.delete_prompt_override("global", None, "video", "11_smart")
        assert pdb.get_prompt_override("global", None, "video", "11_smart") is None

    def test_list_all(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "a")
        pdb.set_prompt_override("domain", "finance", "paper", "05_smart_paper", "b")
        rows = pdb.list_prompt_overrides()
        assert {(r["pipeline"], r["step"]) for r in rows} == {
            ("video", "11_smart"), ("paper", "05_smart_paper")
        }


# ── step_base 注入回退(DB 注入 > {step}.md > None)──


class _Step(StepBase):
    def execute(self):
        return None


def _mk_step(tmp_path, prompt_overrides=None, prompts_dir=None):
    (tmp_path / "job.json").write_text(
        json.dumps({"prompt_overrides": prompt_overrides or {}}), encoding="utf-8"
    )
    cfg: dict = {}
    if prompts_dir is not None:
        cfg = {"paths": {"prompts_dir": str(prompts_dir)}}
    return _Step("11_smart", tmp_path, cfg)


class TestSystemPromptFallback:
    def test_injected_override_wins(self, tmp_path):
        s = _mk_step(tmp_path, {"11_smart": "INJECTED"})
        assert s._injected_prompt_override() == "INJECTED"
        assert s._load_system_prompt() == "INJECTED"

    def test_file_hook_used_when_no_injection(self, tmp_path):
        pd = tmp_path / "prompts"
        pd.mkdir()
        (pd / "11_smart.md").write_text("FROMFILE", encoding="utf-8")
        s = _mk_step(tmp_path, {}, prompts_dir=pd)
        assert s._load_system_prompt() == "FROMFILE"

    def test_injection_overrides_file_hook(self, tmp_path):
        pd = tmp_path / "prompts"
        pd.mkdir()
        (pd / "11_smart.md").write_text("FROMFILE", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": "INJECTED"}, prompts_dir=pd)
        assert s._load_system_prompt() == "INJECTED"

    def test_none_when_no_override_no_file(self, tmp_path):
        s = _mk_step(tmp_path, {})
        assert s._load_system_prompt() is None

    def test_other_step_injection_ignored(self, tmp_path):
        s = _mk_step(tmp_path, {"12_review": "X"})
        assert s._injected_prompt_override() == ""
        assert s._load_system_prompt() is None

    def test_missing_job_json_safe(self, tmp_path):
        s = _Step("11_smart", tmp_path, {})   # 无 job.json
        assert s._injected_prompt_override() == ""


# ── API 端点 ──


@pytest.mark.asyncio
class TestPromptAPI:
    async def test_list_prompts_only_ai_steps(self, client):
        data = (await client.get("/api/prompts")).json()
        steps = data["steps"]
        keys = {(s["pipeline"], s["step"]) for s in steps}
        assert ("video", "11_smart") in keys
        assert ("article", "04_smart_article") in keys
        assert ("video", "01_download") not in keys   # io 步不在列
        assert all(s["is_ai"] for s in steps)

    async def test_put_get_delete_roundtrip(self, client):
        r = await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "MY OVERRIDE"}
        )
        assert r.status_code == 200 and r.json()["status"] == "saved"
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["override"]["content"] == "MY OVERRIDE"
        assert g["override"]["scope"] == "global"
        d = await client.delete("/api/prompts/video/11_smart?scope=global")
        assert d.status_code == 200
        assert (await client.get("/api/prompts/video/11_smart")).json()["override"] is None

    async def test_put_blank_content_deletes(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "x"})
        r = await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "   "}
        )
        assert r.json()["status"] == "deleted"
        assert (await client.get("/api/prompts/video/11_smart")).json()["override"] is None

    async def test_domain_scope_requires_domain(self, client):
        r = await client.put(
            "/api/prompts/video/11_smart", json={"scope": "domain", "content": "x"}
        )
        assert r.status_code == 400

    async def test_domain_scope_roundtrip_independent_of_global(self, client):
        r = await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "domain", "domain": "finance", "content": "D"},
        )
        assert r.status_code == 200
        g = (
            await client.get("/api/prompts/video/11_smart?scope=domain&domain=finance")
        ).json()
        assert g["override"]["content"] == "D"
        assert (await client.get("/api/prompts/video/11_smart")).json()["override"] is None

    async def test_non_ai_step_rejected(self, client):
        r = await client.put(
            "/api/prompts/video/01_download", json={"scope": "global", "content": "x"}
        )
        assert r.status_code == 400

    async def test_unknown_step_404(self, client):
        r = await client.put(
            "/api/prompts/video/nope", json={"scope": "global", "content": "x"}
        )
        assert r.status_code == 404

    async def test_get_exposes_default_template(self, client, test_config):
        # 写一个外置默认模板 → GET 应回显为 default_template
        tdir = test_config.prompts_dir / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "11_smart.md").write_text("DEFAULT TEMPLATE BODY", encoding="utf-8")
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["default_template"] == "DEFAULT TEMPLATE BODY"

    async def test_pipelines_endpoint_has_is_ai_and_override(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "x"})
        data = (await client.get("/api/pipelines")).json()
        video = next(p for p in data["pipelines"] if p["name"] == "video")
        smart = next(s for s in video["steps"] if s["key"] == "11_smart")
        assert smart["is_ai"] is True and smart["has_override"] is True
        dl = next(s for s in video["steps"] if s["key"] == "01_download")
        assert dl["is_ai"] is False and dl["has_override"] is False


@pytest.mark.asyncio
class TestCreateJobInjection:
    async def test_create_job_injects_resolved_overrides(self, client, app):
        await client.put(
            "/api/prompts/article/04_smart_article",
            json={"scope": "global", "content": "ART OVERRIDE"},
        )
        resp = await client.post(
            "/api/jobs",
            json={"url": "https://example.com/post", "content_type": "article", "domain": "general"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        raw = await app.state.storage.read_file(job_id, "job.json")
        doc = json.loads(raw)
        assert doc["prompt_overrides"]["04_smart_article"] == "ART OVERRIDE"

    async def test_create_job_without_override_has_no_key(self, client, app):
        resp = await client.post(
            "/api/jobs",
            json={"url": "https://example.com/post2", "content_type": "article", "domain": "general"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        doc = json.loads(await app.state.storage.read_file(job_id, "job.json"))
        assert "prompt_overrides" not in doc
