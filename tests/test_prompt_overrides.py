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
        # 1.1.5:resolve 返回 {step: {content, version}}(含激活版本号快照)。
        r_fin = pdb.resolve_prompt_overrides("video", "finance")
        assert r_fin["11_smart"]["content"] == "D"   # domain 覆盖优先
        assert r_fin["11_smart"]["version"] == 1
        assert r_fin["12_review"]["content"] == "GR"  # 该步无 domain 覆盖 → global 兜底
        r_ml = pdb.resolve_prompt_overrides("video", "ml")
        assert r_ml["11_smart"]["content"] == "G"     # ml 无 domain 覆盖 → global

    def test_resolve_filters_empty_and_other_pipeline(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "")     # 空 = 无覆盖
        pdb.set_prompt_override("global", None, "paper", "05_smart_paper", "P")
        assert pdb.resolve_prompt_overrides("video", "general") == {}
        r = pdb.resolve_prompt_overrides("paper", "general")
        assert r["05_smart_paper"]["content"] == "P" and r["05_smart_paper"]["version"] == 1

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


class TestPromptOverrideVersions:
    """版本管理(类 Grafana save):首版/覆盖当前版本/另存为新版本/查历史/删清空历史。"""

    def test_first_save_is_v1(self, pdb):
        v = pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        assert v == 1
        assert pdb.get_prompt_override("global", None, "video", "11_smart")["version"] == 1
        hist = pdb.list_prompt_override_versions("global", None, "video", "11_smart")
        assert [h["version"] for h in hist] == [1]

    def test_overwrite_keeps_same_version(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A", note="v1note")
        v = pdb.set_prompt_override("global", None, "video", "11_smart", "A2", mode="overwrite")
        assert v == 1                              # 版本号不变
        ov = pdb.get_prompt_override("global", None, "video", "11_smart")
        assert ov["content"] == "A2" and ov["version"] == 1
        hist = pdb.list_prompt_override_versions("global", None, "video", "11_smart")
        assert [h["version"] for h in hist] == [1]  # 仍只有 1 个版本
        # overwrite 未给 note → 保留原 note
        assert pdb.get_prompt_override_version("global", None, "video", "11_smart", 1)["note"] == "v1note"

    def test_save_as_new_bumps_version_and_activates(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        v2 = pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new", note="第二版")
        assert v2 == 2
        ov = pdb.get_prompt_override("global", None, "video", "11_smart")
        assert ov["content"] == "B" and ov["version"] == 2     # 主表指向新激活版本
        # 两版历史 content 各自独立
        assert pdb.get_prompt_override_version("global", None, "video", "11_smart", 1)["content"] == "A"
        assert pdb.get_prompt_override_version("global", None, "video", "11_smart", 2)["content"] == "B"
        meta = {h["version"]: h["note"] for h in pdb.list_prompt_override_versions("global", None, "video", "11_smart")}
        assert set(meta) == {1, 2} and meta[2] == "第二版"   # v2 note 记录

    def test_overwrite_active_after_new_targets_latest(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new")  # 激活 v2
        v = pdb.set_prompt_override("global", None, "video", "11_smart", "B2", mode="overwrite")
        assert v == 2
        assert pdb.get_prompt_override_version("global", None, "video", "11_smart", 2)["content"] == "B2"
        assert pdb.get_prompt_override_version("global", None, "video", "11_smart", 1)["content"] == "A"  # v1 不动

    def test_get_unknown_version_none(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        assert pdb.get_prompt_override_version("global", None, "video", "11_smart", 9) is None

    def test_delete_clears_all_versions(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new")
        pdb.delete_prompt_override("global", None, "video", "11_smart")
        assert pdb.get_prompt_override("global", None, "video", "11_smart") is None
        assert pdb.list_prompt_override_versions("global", None, "video", "11_smart") == []

    def test_resolve_carries_active_version(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new")  # 激活 v2
        r = pdb.resolve_prompt_overrides("video", "general")
        assert r["11_smart"] == {"content": "B", "version": 2}


# ── step_base 注入回退 ──


class _Step(StepBase):
    def execute(self):
        return None


def _mk_step(tmp_path, prompt_overrides=None, prompts_dir=None, step="11_smart"):
    (tmp_path / "job.json").write_text(
        json.dumps({"prompt_overrides": prompt_overrides or {}}), encoding="utf-8"
    )
    cfg: dict = {}
    if prompts_dir is not None:
        cfg = {"paths": {"prompts_dir": str(prompts_dir)}}
    return _Step(step, tmp_path, cfg)


class TestSystemPromptFallback:
    """无外置模板的步(评审等 prompt 内联):覆盖回落为 system prompt(_load_system_prompt)。
    回退序 = DB 注入(仅无模板步)> {step}.md 钩子 > None。这些用例不建 templates/ → 走无模板路径。"""

    def test_injected_override_wins(self, tmp_path):
        # 旧格式:job.json.prompt_overrides[step] 为纯字符串(历史 job 兼容)。
        s = _mk_step(tmp_path, {"11_smart": "INJECTED"})
        assert s._injected_prompt_override() == "INJECTED"
        assert s._load_system_prompt() == "INJECTED"

    def test_injected_override_new_dict_format(self, tmp_path):
        # 1.1.5 新格式:{content, version} → 注入取出正文(版本只供 Job 详情比对)。
        s = _mk_step(tmp_path, {"11_smart": {"content": "INJECTED", "version": 3}})
        assert s._injected_prompt_override() == "INJECTED"
        assert s._load_system_prompt() == "INJECTED"

    def test_injected_override_dict_missing_content_safe(self, tmp_path):
        s = _mk_step(tmp_path, {"11_smart": {"version": 2}})
        assert s._injected_prompt_override() == ""

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

    def test_template_step_injection_not_used_as_system(self, tmp_path):
        # 有外置模板的步:覆盖作用于 user 模板层,不再当 system(避免双重套用)。
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("TPL", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": "INJECTED"}, prompts_dir=pd)
        assert s._has_step_template() is True
        assert s._load_system_prompt() is None


class TestPromptTemplateOverride:
    """所见即所改:覆盖替换的就是展示的默认 user-prompt 模板。
    回退序 = DB 注入覆盖 > templates/{name}.md > 内联 default(本类直测 _load_prompt_template)。"""

    def test_fallback_order_override_beats_file_and_default(self, tmp_path):
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("FROM_FILE", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": "FROM_OVERRIDE"}, prompts_dir=pd)
        # ① 有覆盖 → 用覆盖(压过模板文件与内联默认)
        assert s._load_prompt_template("11_smart", "INLINE_DEFAULT") == "FROM_OVERRIDE"

    def test_fallback_file_when_no_override(self, tmp_path):
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("FROM_FILE", encoding="utf-8")
        s = _mk_step(tmp_path, {}, prompts_dir=pd)
        # ② 无覆盖、有模板文件 → 用文件
        assert s._load_prompt_template("11_smart", "INLINE_DEFAULT") == "FROM_FILE"

    def test_fallback_inline_default_when_nothing(self, tmp_path):
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        s = _mk_step(tmp_path, {}, prompts_dir=pd)
        # ③ 无覆盖、无文件 → 内联默认
        assert s._load_prompt_template("11_smart", "INLINE_DEFAULT") == "INLINE_DEFAULT"

    def test_variant_not_overridden_when_main_template_exists(self, tmp_path):
        # 11_smart 有主模板 → 变体 11_smart.vision 不吃覆盖(两 pass 同 job 都跑,只改主笔记)。
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("MAIN", encoding="utf-8")
        (pd / "templates" / "11_smart.vision.md").write_text("VISION_FILE", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": "OV"}, prompts_dir=pd)
        assert s._load_prompt_template("11_smart", "d") == "OV"            # 主吃覆盖
        assert s._load_prompt_template("11_smart.vision", "d") == "VISION_FILE"  # 变体不吃

    def test_variant_overridden_when_no_main_template(self, tmp_path):
        # 08_punctuate 只有 .zh/.translate 变体、无主模板 → 覆盖落到被加载的变体(同 job 只跑一个)。
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "08_punctuate.zh.md").write_text("ZH", encoding="utf-8")
        (pd / "templates" / "08_punctuate.translate.md").write_text("TR", encoding="utf-8")
        s = _mk_step(tmp_path, {"08_punctuate": "OV"}, prompts_dir=pd, step="08_punctuate")
        assert s._load_prompt_template("08_punctuate.zh", "d") == "OV"
        assert s._load_prompt_template("08_punctuate.translate", "d") == "OV"


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

    async def test_get_default_falls_back_to_baked_configs(self, client):
        # 白盒核心修复:prompts_dir/templates 为空(模拟 api 没挂 templates),仍从镜像烤入
        # config_dir/prompts/templates 读到默认 → GET 不再回 null(白盒能看到默认)。
        g = (await client.get("/api/prompts/paper/05_smart_paper")).json()
        assert g["default_template"]                      # 非空
        names = {t["name"] for t in g["default_templates"]}
        assert "05_smart_paper" in names
        assert g["default_templates"][0]["content"].strip()

    async def test_get_review_steps_return_nonempty_default(self, client):
        # 评审步白盒化:外置骨架模板(05/06/12_review)→ GET 回非空 default(含 {{ref_block}} 占位),
        # 经镜像烤入 config_dir/prompts/templates 兜底读到(prompts_dir 未挂时)。
        for pipeline, step in [
            ("article", "06_review"), ("paper", "06_review"),
            ("audio", "05_review"), ("video", "12_review"),
        ]:
            g = (await client.get(f"/api/prompts/{pipeline}/{step}")).json()
            assert g["default_template"], f"{pipeline}/{step} default 为空"
            assert "{{ref_block}}" in g["default_template"]
            assert step in {t["name"] for t in g["default_templates"]}
            assert g["is_ai"] is True

    async def test_review_step_override_roundtrip(self, client):
        # 评审步可存/取/删覆盖(与 smart 步同机制,验白盒可编辑闭环)。
        r = await client.put(
            "/api/prompts/paper/06_review", json={"scope": "global", "content": "评审覆盖"}
        )
        assert r.status_code == 200 and r.json()["status"] == "saved"
        g = (await client.get("/api/prompts/paper/06_review")).json()
        assert g["override"]["content"] == "评审覆盖"
        await client.delete("/api/prompts/paper/06_review?scope=global")
        assert (await client.get("/api/prompts/paper/06_review")).json()["override"] is None

    async def test_get_variant_step_returns_all_variants(self, client):
        # 变体步(08_punctuate 只有 .zh/.translate 变体,无主模板)也非空,且列出全变体。
        g = (await client.get("/api/prompts/video/08_punctuate")).json()
        assert g["default_template"]                      # 取首个变体兜底,非空
        names = {t["name"] for t in g["default_templates"]}
        assert {"08_punctuate.zh", "08_punctuate.translate"} <= names

    async def test_pipelines_endpoint_has_is_ai_and_override(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "x"})
        data = (await client.get("/api/pipelines")).json()
        video = next(p for p in data["pipelines"] if p["name"] == "video")
        smart = next(s for s in video["steps"] if s["key"] == "11_smart")
        assert smart["is_ai"] is True and smart["has_override"] is True
        dl = next(s for s in video["steps"] if s["key"] == "01_download")
        assert dl["is_ai"] is False and dl["has_override"] is False


@pytest.mark.asyncio
class TestPromptVersionAPI:
    """C2:单步 GET 透出 active_version + versions、新 versions/{version} 查历史、PUT mode/note 返回版本。"""

    async def test_get_exposes_active_version_and_versions(self, client):
        await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "A", "note": "首版"}
        )
        await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "B", "mode": "new", "note": "第二版"},
        )
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] == 2
        assert [v["version"] for v in g["versions"]] == [1, 2]
        notes = {v["version"]: v["note"] for v in g["versions"]}
        assert notes == {1: "首版", 2: "第二版"}
        assert g["override"]["content"] == "B" and g["override"]["version"] == 2

    async def test_get_no_override_active_version_none(self, client):
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] is None and g["versions"] == []

    async def test_put_overwrite_keeps_version(self, client):
        r1 = await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "A"}
        )
        assert r1.json()["active_version"] == 1
        r2 = await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "A2", "mode": "overwrite"},
        )
        assert r2.json()["active_version"] == 1
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] == 1 and g["override"]["content"] == "A2"
        assert [v["version"] for v in g["versions"]] == [1]

    async def test_put_new_bumps_and_activates(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        r = await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "B", "mode": "new"},
        )
        assert r.json()["active_version"] == 2

    async def test_get_version_returns_content(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "B", "mode": "new", "note": "n2"},
        )
        v1 = (await client.get("/api/prompts/video/11_smart/versions/1")).json()
        assert v1["content"] == "A" and v1["version"] == 1
        v2 = (await client.get("/api/prompts/video/11_smart/versions/2")).json()
        assert v2["content"] == "B" and v2["note"] == "n2"

    async def test_get_version_unknown_404(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        r = await client.get("/api/prompts/video/11_smart/versions/9")
        assert r.status_code == 404

    async def test_version_history_scoped_to_domain(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "G"})
        await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "domain", "domain": "finance", "content": "D"},
        )
        gv = (await client.get("/api/prompts/video/11_smart/versions/1?scope=domain&domain=finance")).json()
        assert gv["content"] == "D"
        # global 历史与 domain 历史互不干扰
        gg = (await client.get("/api/prompts/video/11_smart/versions/1")).json()
        assert gg["content"] == "G"


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
        # 1.1.5:注入快照含版本号 {content, version}。
        assert doc["prompt_overrides"]["04_smart_article"]["content"] == "ART OVERRIDE"
        assert doc["prompt_overrides"]["04_smart_article"]["version"] == 1

    async def test_create_job_without_override_has_no_key(self, client, app):
        resp = await client.post(
            "/api/jobs",
            json={"url": "https://example.com/post2", "content_type": "article", "domain": "general"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        doc = json.loads(await app.state.storage.read_file(job_id, "job.json"))
        assert "prompt_overrides" not in doc
