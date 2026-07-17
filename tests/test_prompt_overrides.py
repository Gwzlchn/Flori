"""Prompt 白盒:DB 覆盖,统一 resolver,API 展示与任务注入.

覆盖:
- DB 层:set/get/list/delete/resolve 的 global↔domain 优先级 + 归一。
- user template 为任务覆盖 > hot > image;system hook 是独立可选契约.
- API 端点:列/读/写/删/校验。
- GET /api/pipelines 的 is_ai/has_override,create_job 注入。
"""

from __future__ import annotations

import json

import pytest

from shared.db import Database
from shared.step_base import StepBase


# DB 层


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
        # resolve 返回 {step: {content, version}},含激活版本号快照。
        r_fin = pdb.resolve_prompt_overrides("video", "finance")
        assert r_fin["11_smart"]["content"] == "D"   # domain 覆盖优先
        assert r_fin["11_smart"]["version"] == 1
        assert r_fin["12_review"]["content"] == "GR"  # 该步无 domain 覆盖 → global 兜底
        r_ml = pdb.resolve_prompt_overrides("video", "ml")
        assert r_ml["11_smart"]["content"] == "G"     # ml 无 domain 覆盖 → global

    def test_resolve_filters_empty_and_other_pipeline(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "")     # 空 = 无覆盖
        pdb.set_prompt_override(
            "global", None, "document", "05_smart", "P",
            document_kind="research_paper",
        )
        assert pdb.resolve_prompt_overrides("video", "general") == {}
        r = pdb.resolve_prompt_overrides("document", "general", "research_paper")
        assert r["05_smart"]["content"] == "P" and r["05_smart"]["version"] == 1

    def test_delete_restores_default(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "x")
        pdb.delete_prompt_override("global", None, "video", "11_smart")
        assert pdb.get_prompt_override("global", None, "video", "11_smart") is None

    def test_list_all(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "a")
        pdb.set_prompt_override(
            "domain", "finance", "document", "05_smart", "b",
            document_kind="research_paper",
        )
        rows = pdb.list_prompt_overrides()
        assert {(r["pipeline"], r["document_kind"], r["step"]) for r in rows} == {
            ("video", "", "11_smart"),
            ("document", "research_paper", "05_smart"),
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
        assert r["11_smart"] == {
            "content": "B", "version": 2,
            "document_kind": None, "scope": "global",
        }


class TestPromptActivateDeactivateDB:
    """非破坏的「回到内置默认」(deactivate) + 「设为当前激活」(set_active):
    deactivate 删激活指针但保留历史;set_active 切激活;re-activate 后 resolve 返回该版本。"""

    def test_deactivate_clears_active_but_keeps_history(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new")  # 激活 v2
        pdb.deactivate_prompt_override("global", None, "video", "11_smart")
        # 激活指针清掉后主表无行,resolve 为空,回内置默认
        assert pdb.get_prompt_override("global", None, "video", "11_smart") is None
        assert pdb.resolve_prompt_overrides("video", "general") == {}
        # 但历史版本完整保留(下拉仍能看到 v1/v2,可再激活)
        hist = pdb.list_prompt_override_versions("global", None, "video", "11_smart")
        assert [h["version"] for h in hist] == [1, 2]
        assert pdb.get_prompt_override_version("global", None, "video", "11_smart", 2)["content"] == "B"

    def test_deactivate_noop_when_no_pointer(self, pdb):
        # 从未覆盖时 deactivate 是 no-op,不报错
        pdb.deactivate_prompt_override("global", None, "video", "11_smart")
        assert pdb.get_prompt_override("global", None, "video", "11_smart") is None

    def test_set_active_switches_pointer(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new")  # 激活 v2
        assert pdb.set_active_prompt_version("global", None, "video", "11_smart", 1) is True
        ov = pdb.get_prompt_override("global", None, "video", "11_smart")
        assert ov["version"] == 1 and ov["content"] == "A"
        assert pdb.resolve_prompt_overrides("video", "general") == {
            "11_smart": {
                "content": "A", "version": 1,
                "document_kind": None, "scope": "global",
            },
        }

    def test_set_active_unknown_version_false(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        assert pdb.set_active_prompt_version("global", None, "video", "11_smart", 9) is False
        # 原激活不动
        assert pdb.get_prompt_override("global", None, "video", "11_smart")["version"] == 1

    def test_reactivate_after_deactivate(self, pdb):
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new")  # 激活 v2
        pdb.deactivate_prompt_override("global", None, "video", "11_smart")
        assert pdb.resolve_prompt_overrides("video", "general") == {}
        # 重新激活 v2 → 主表指针重建,resolve 返回该版本
        assert pdb.set_active_prompt_version("global", None, "video", "11_smart", 2) is True
        assert pdb.resolve_prompt_overrides("video", "general") == {
            "11_smart": {
                "content": "B", "version": 2,
                "document_kind": None, "scope": "global",
            },
        }

    def test_delete_still_clears_history(self, pdb):
        # delete_prompt_override 是真删整个(含历史),与 deactivate 区分
        pdb.set_prompt_override("global", None, "video", "11_smart", "A")
        pdb.set_prompt_override("global", None, "video", "11_smart", "B", mode="new")
        pdb.delete_prompt_override("global", None, "video", "11_smart")
        assert pdb.list_prompt_override_versions("global", None, "video", "11_smart") == []


# step_base 注入回退


class _Step(StepBase):
    def execute(self):
        return None


def _mk_step(tmp_path, prompt_overrides=None, prompts_dir=None, step="11_smart"):
    (tmp_path / "job.json").write_text(
        json.dumps({"prompt_overrides": prompt_overrides or {}}), encoding="utf-8"
    )
    cfg: dict = {
        "paths": {
            "prompts_dir": str(prompts_dir or (tmp_path / "prompts")),
            "config_dir": str(tmp_path / "image-config"),
        },
        "step": {"name": step},
    }
    if prompts_dir is not None:
        cfg["paths"]["prompts_dir"] = str(prompts_dir)
    return _Step(step, tmp_path, cfg)


class TestSystemPromptFallback:
    """无外置模板的步(评审等 prompt 内联):覆盖回落为 system prompt,即 _load_system_prompt。
    回退序 = DB 注入(仅无模板步)> {step}.md 钩子 > None。这些用例不建 templates/ → 走无模板路径。"""

    def test_injected_override_wins(self, tmp_path):
        # 纯字符串格式:job.json.prompt_overrides[step] 为 str,兼容存量 job,不可去掉。
        s = _mk_step(tmp_path, {"custom_ai": "INJECTED"}, step="custom_ai")
        assert s.ai.injected_prompt_override() == "INJECTED"
        assert s.ai.load_system_prompt() == "INJECTED"

    def test_injected_override_new_dict_format(self, tmp_path):
        # 派发快照携带 kind/scope 元数据,Worker 只消费正文与版本。
        s = _mk_step(
            tmp_path,
            {"custom_ai": {
                "content": "INJECTED", "version": 3,
                "document_kind": "article", "scope": "domain",
            }},
            step="custom_ai",
        )
        assert s.ai.injected_prompt_override() == "INJECTED"
        assert s.ai.load_system_prompt() == "INJECTED"

    def test_injected_override_dict_missing_content_fails_closed(self, tmp_path):
        s = _mk_step(tmp_path, {"11_smart": {"version": 2}})
        from shared.prompt_resolver import PromptResolutionError
        with pytest.raises(PromptResolutionError, match="shape"):
            s.ai.injected_prompt_override()

    def test_file_hook_used_when_no_injection(self, tmp_path):
        pd = tmp_path / "prompts"
        pd.mkdir()
        (pd / "11_smart.md").write_text("FROMFILE", encoding="utf-8")
        s = _mk_step(tmp_path, {}, prompts_dir=pd)
        assert s.ai.load_system_prompt() == "FROMFILE"

    def test_injection_overrides_file_hook(self, tmp_path):
        pd = tmp_path / "prompts"
        pd.mkdir()
        (pd / "custom_ai.md").write_text("FROMFILE", encoding="utf-8")
        s = _mk_step(
            tmp_path, {"custom_ai": "INJECTED"},
            prompts_dir=pd, step="custom_ai",
        )
        assert s.ai.load_system_prompt() == "INJECTED"

    def test_none_when_no_override_no_file(self, tmp_path):
        s = _mk_step(tmp_path, {})
        assert s.ai.load_system_prompt() is None

    def test_other_step_injection_ignored(self, tmp_path):
        s = _mk_step(tmp_path, {"12_review": "X"})
        assert s.ai.injected_prompt_override() == ""
        assert s.ai.load_system_prompt() is None

    def test_missing_job_json_safe(self, tmp_path):
        s = _Step("11_smart", tmp_path, {})   # 无 job.json
        assert s.ai.injected_prompt_override() == ""

    def test_explicit_null_prompt_override_map_fails_closed(self, tmp_path):
        (tmp_path / "job.json").write_text('{"prompt_overrides":null}', encoding="utf-8")
        s = _Step("11_smart", tmp_path, {
            "step": {"name": "11_smart"},
            "paths": {"prompts_dir": str(tmp_path / "prompts"),
                      "config_dir": str(tmp_path / "image")},
        })
        from shared.prompt_resolver import PromptResolutionError
        with pytest.raises(PromptResolutionError, match="map"):
            s.ai.injected_prompt_override()

    def test_template_step_injection_not_used_as_system(self, tmp_path):
        # 有外置模板的步:覆盖作用于 user 模板层,不当 system,避免双重套用。
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("TPL", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": "INJECTED"}, prompts_dir=pd)
        assert s.ai.has_step_template() is True
        assert s.ai.load_system_prompt() is None


class TestPromptTemplateOverride:
    """所见即所改:覆盖替换的就是展示的默认 user-prompt 模板。
    回退序 = DB 注入覆盖 > hot template > image template;全缺结构化失败."""

    def test_fallback_order_override_beats_file_and_default(self, tmp_path):
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("FROM_FILE", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": "FROM_OVERRIDE"}, prompts_dir=pd)
        # 有覆盖 → 用覆盖(压过 hot 与 image 模板)
        assert s.ai.load_prompt_template("11_smart") == "FROM_OVERRIDE"

    def test_fallback_file_when_no_override(self, tmp_path):
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("FROM_FILE", encoding="utf-8")
        s = _mk_step(tmp_path, {}, prompts_dir=pd)
        # 2. 无覆盖、有模板文件 → 用文件
        assert s.ai.load_prompt_template("11_smart") == "FROM_FILE"

    def test_all_template_sources_missing_fails_closed(self, tmp_path):
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        s = _mk_step(tmp_path, {}, prompts_dir=pd)
        from shared.prompt_resolver import PromptResolutionError
        with pytest.raises(PromptResolutionError, match="missing"):
            s.ai.load_prompt_template("11_smart")

    def test_variant_not_overridden_when_main_template_exists(self, tmp_path):
        # 11_smart 有主模板 → 变体 11_smart.vision 不吃覆盖(两 pass 同 job 都跑,只改主笔记)。
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "11_smart.md").write_text("MAIN", encoding="utf-8")
        (pd / "templates" / "11_smart.vision.md").write_text("VISION_FILE", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": "OV"}, prompts_dir=pd)
        assert s.ai.load_prompt_template("11_smart") == "OV"            # 主吃覆盖
        assert s.ai.load_prompt_template("11_smart.vision") == "VISION_FILE"  # 变体不吃

    def test_variant_overridden_when_no_main_template(self, tmp_path):
        # 08_punctuate 只有 .zh/.translate 变体、无主模板 → 覆盖落到被加载的变体(同 job 只跑一个)。
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "08_punctuate.zh.md").write_text("ZH", encoding="utf-8")
        (pd / "templates" / "08_punctuate.translate.md").write_text("TR", encoding="utf-8")
        s = _mk_step(tmp_path, {"08_punctuate": "OV"}, prompts_dir=pd, step="08_punctuate")
        assert s.ai.load_prompt_template("08_punctuate.zh") == "OV"
        assert s.ai.load_prompt_template("08_punctuate.translate") == "OV"

    def test_variant_only_override_is_not_duplicated_as_system(self, tmp_path):
        pd = tmp_path / "prompts"
        (pd / "templates").mkdir(parents=True)
        (pd / "templates" / "08_punctuate.zh.md").write_text("ZH", encoding="utf-8")
        (pd / "templates" / "08_punctuate.translate.md").write_text(
            "TRANSLATE", encoding="utf-8",
        )
        s = _mk_step(
            tmp_path, {"08_punctuate": "OVERRIDE"},
            prompts_dir=pd, step="08_punctuate",
        )
        assert s.ai.load_prompt_template("08_punctuate.zh") == "OVERRIDE"
        assert s.ai.load_system_prompt() is None


# API 端点


@pytest.mark.asyncio
class TestPromptAPI:
    async def test_list_prompts_only_ai_steps(self, client):
        data = (await client.get("/api/prompts")).json()
        steps = data["steps"]
        keys = {(s["pipeline"], s["step"]) for s in steps}
        assert ("video", "11_smart") in keys
        assert ("document", "04_translate") in keys
        assert ("document", "05_smart") in keys
        assert ("document", "08_review") in keys
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
        template = next(t for t in g["default_templates"] if t["name"] == "11_smart")
        assert template["source"] == "hot"
        assert template["bytes"] == len(b"DEFAULT TEMPLATE BODY")
        assert template["sha256"].startswith("sha256:")
        assert template["version"] is None

    async def test_api_and_worker_resolve_the_same_bytes_hash_and_source(
        self, client, test_config, tmp_path,
    ):
        from shared.config import build_step_config
        from shared.step_base import StepBase

        tdir = test_config.prompts_dir / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        raw = "同一份热模板\r\n".encode("utf-8")
        (tdir / "11_smart.md").write_bytes(raw)
        api_data = (await client.get("/api/prompts/video/11_smart")).json()
        api_template = next(
            item for item in api_data["default_templates"] if item["name"] == "11_smart"
        )

        job = tmp_path / "worker-job"
        job.mkdir()
        (job / "job.json").write_text("{}", encoding="utf-8")
        step = StepBase(
            "11_smart", job,
            build_step_config(test_config, "video", "11_smart"),
        )
        worker_template = step.ai.resolve_prompt_template("11_smart")
        assert worker_template.raw == raw
        assert api_template["content"] == worker_template.text
        assert api_template["sha256"] == worker_template.sha256
        assert api_template["source"] == worker_template.source == "hot"

    async def test_bad_hot_template_does_not_fall_back_in_api(self, client, test_config):
        from shared.prompt_resolver import PromptResolutionError

        tdir = test_config.prompts_dir / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "11_smart.md").write_bytes(b"\xff")
        with pytest.raises(PromptResolutionError, match="UTF-8"):
            await client.get("/api/prompts/video/11_smart")

    async def test_get_default_falls_back_to_baked_configs(self, client):
        # prompts_dir/templates 为空(模拟 api 没挂 templates)时,仍从镜像烤入的
        # config_dir/prompts/templates 读到默认 → GET 回非空,白盒能看到默认。
        g = (await client.get(
            "/api/prompts/document/05_smart?document_kind=research_paper"
        )).json()
        assert g["default_template"]                      # 非空
        names = {t["name"] for t in g["default_templates"]}
        assert "05_smart_document" in names
        assert g["default_templates"][0]["content"].strip()

    async def test_get_review_steps_return_nonempty_default(self, client):
        # 评审步外置骨架模板(05/08/12_review):GET 回非空 default,含 {{ref_block}} 占位。
        # prompts_dir 未挂时经镜像烤入的 config_dir/prompts/templates 兜底读到。
        for pipeline, step, query in [
            ("document", "08_review", "?document_kind=article"),
            ("audio", "05_review", ""), ("video", "12_review", ""),
        ]:
            g = (await client.get(f"/api/prompts/{pipeline}/{step}{query}")).json()
            assert g["default_template"], f"{pipeline}/{step} default 为空"
            assert "{{ref_block}}" in g["default_template"]
            assert step in {t["name"] for t in g["default_templates"]}
            assert g["is_ai"] is True

    async def test_review_step_override_roundtrip(self, client):
        # 评审步可存/取/删覆盖(与 smart 步同机制,验白盒可编辑闭环)。
        r = await client.put(
            "/api/prompts/document/08_review",
            json={
                "scope": "global", "content": "评审覆盖",
                "document_kind": "research_paper",
            },
        )
        assert r.status_code == 200 and r.json()["status"] == "saved"
        query = "?document_kind=research_paper"
        g = (await client.get(f"/api/prompts/document/08_review{query}")).json()
        assert g["override"]["content"] == "评审覆盖"
        await client.delete(
            "/api/prompts/document/08_review?scope=global&document_kind=research_paper"
        )
        assert (await client.get(
            f"/api/prompts/document/08_review{query}"
        )).json()["override"] is None

    async def test_get_variant_step_returns_all_variants(self, client):
        # 变体步(08_punctuate 只有 .zh/.translate 变体,无主模板)也非空,且列出全变体。
        g = (await client.get("/api/prompts/video/08_punctuate")).json()
        assert g["default_template"]                      # 取首个变体兜底,非空
        names = {t["name"] for t in g["default_templates"]}
        assert {"08_punctuate.zh", "08_punctuate.translate"} <= names

    async def test_video_concepts_displays_mapped_tracked_template(self, client):
        g = (await client.get("/api/prompts/video/12_concepts")).json()
        assert g["default_templates"][0]["name"] == "05_concepts"
        assert g["default_template"] == g["default_templates"][0]["content"]

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
    """单步 GET 透出 active_version + versions,versions/{version} 查历史,PUT mode/note 返回版本。"""

    async def test_get_exposes_active_version_and_versions(self, client):
        await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "A", "note": "首版"}
        )
        await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "B", "mode": "new", "note": "第二版"},
        )
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] == "2"
        assert [v["version"] for v in g["versions"]] == ["1", "2"]
        notes = {v["version"]: v["note"] for v in g["versions"]}
        assert notes == {"1": "首版", "2": "第二版"}
        assert g["override"]["content"] == "B" and g["override"]["version"] == "2"

    async def test_get_no_override_active_version_none(self, client):
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] is None and g["versions"] == []

    async def test_put_overwrite_keeps_version(self, client):
        r1 = await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "A"}
        )
        assert r1.json()["active_version"] == "1"
        r2 = await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "A2", "mode": "overwrite"},
        )
        assert r2.json()["active_version"] == "1"
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] == "1" and g["override"]["content"] == "A2"
        assert [v["version"] for v in g["versions"]] == ["1"]

    async def test_put_new_bumps_and_activates(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        r = await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "B", "mode": "new"},
        )
        assert r.json()["active_version"] == "2"

    async def test_get_version_returns_content(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "B", "mode": "new", "note": "n2"},
        )
        v1 = (await client.get("/api/prompts/video/11_smart/versions/1")).json()
        assert v1["content"] == "A" and v1["version"] == "1"
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
class TestPromptActivateAPI:
    """POST .../activate:version=null 停用回内置默认(非破坏,留历史);version=数字 设激活;未知版本 404。"""

    async def test_deactivate_keeps_versions(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A", "note": "首版"})
        await client.put(
            "/api/prompts/video/11_smart",
            json={"scope": "global", "content": "B", "mode": "new", "note": "第二版"},
        )
        r = await client.post("/api/prompts/video/11_smart/activate", json={"scope": "global", "version": None})
        assert r.status_code == 200
        assert r.json()["status"] == "deactivated" and r.json()["active_version"] is None
        # GET:active_version 归 null,但 versions[] 仍非空(历史保留),override 为 null
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] is None
        assert [v["version"] for v in g["versions"]] == ["1", "2"]
        assert g["override"] is None

    async def test_activate_sets_active_version(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "B", "mode": "new"},
        )  # 激活 v2
        r = await client.post("/api/prompts/video/11_smart/activate", json={"scope": "global", "version": 1})
        assert r.status_code == 200 and r.json()["active_version"] == "1"
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] == "1" and g["override"]["content"] == "A"

    async def test_reactivate_after_deactivate(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        await client.put(
            "/api/prompts/video/11_smart", json={"scope": "global", "content": "B", "mode": "new"},
        )
        await client.post("/api/prompts/video/11_smart/activate", json={"scope": "global", "version": None})
        # 再激活 v2 → override 回来,active_version=2
        r = await client.post("/api/prompts/video/11_smart/activate", json={"scope": "global", "version": 2})
        assert r.status_code == 200 and r.json()["active_version"] == "2"
        g = (await client.get("/api/prompts/video/11_smart")).json()
        assert g["active_version"] == "2" and g["override"]["content"] == "B"

    async def test_activate_unknown_version_404(self, client):
        await client.put("/api/prompts/video/11_smart", json={"scope": "global", "content": "A"})
        r = await client.post("/api/prompts/video/11_smart/activate", json={"scope": "global", "version": 9})
        assert r.status_code == 404

    async def test_activate_unknown_step_404(self, client):
        r = await client.post("/api/prompts/video/nope_step/activate", json={"scope": "global", "version": None})
        assert r.status_code == 404

    async def test_activate_domain_scope_requires_domain_400(self, client):
        r = await client.post("/api/prompts/video/11_smart/activate", json={"scope": "domain", "version": None})
        assert r.status_code == 400

    async def test_deactivate_does_not_touch_default_resolved_job(self, client):
        # deactivate 后 resolve 空 → 该步派发回内置默认(借 create_job 注入验证不带覆盖)
        await client.put(
            "/api/prompts/document/05_smart",
            json={"scope": "global", "content": "ART", "document_kind": "article"},
        )
        await client.post(
            "/api/prompts/document/05_smart/activate",
            json={"scope": "global", "version": None, "document_kind": "article"},
        )
        g = (await client.get(
            "/api/prompts/document/05_smart?document_kind=article"
        )).json()
        assert g["active_version"] is None and g["versions"]  # 历史还在


@pytest.mark.asyncio
class TestCreateJobInjection:
    async def test_create_job_injects_resolved_overrides(self, client, app):
        await client.put(
            "/api/prompts/document/05_smart",
            json={
                "scope": "global", "content": "ART OVERRIDE",
                "document_kind": "article",
            },
        )
        resp = await client.post(
            "/api/jobs",
            json={"url": "https://example.com/post", "content_type": "document",
                  "document_kind": "article", "domain": "general"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        raw = await app.state.storage.read_file(job_id, "job.json")
        doc = json.loads(raw)
        # 注入快照含版本号 {content, version}。
        snapshot = doc["prompt_overrides"]["05_smart"]
        assert snapshot["content"] == "ART OVERRIDE" and snapshot["version"] == 1
        assert snapshot["document_kind"] == "article" and snapshot["scope"] == "global"

    async def test_create_job_without_override_has_no_key(self, client, app):
        resp = await client.post(
            "/api/jobs",
            json={"url": "https://example.com/post2", "content_type": "document",
                  "document_kind": "article", "domain": "general"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        doc = json.loads(await app.state.storage.read_file(job_id, "job.json"))
        assert "prompt_overrides" not in doc

    async def test_job_detail_exposes_prompt_versions(self, client):
        # 建覆盖后新建 job,详情 prompt_versions 含该步派发时的版本快照。
        await client.put(
            "/api/prompts/document/05_smart",
            json={"scope": "global", "content": "OV", "document_kind": "article"},
        )
        # 再 new 一版 → 激活 v2,新 job 应快照 v2。
        await client.put(
            "/api/prompts/document/05_smart",
            json={
                "scope": "global", "content": "OV2", "mode": "new",
                "document_kind": "article",
            },
        )
        resp = await client.post(
            "/api/jobs",
            json={"url": "https://example.com/pv", "content_type": "document",
                  "document_kind": "article", "domain": "general"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        d = (await client.get(f"/api/jobs/{job_id}")).json()
        assert d["prompt_versions"]["05_smart"] == "2"

    async def test_job_detail_prompt_versions_empty_without_override(self, client):
        resp = await client.post(
            "/api/jobs",
            json={"url": "https://example.com/pv2", "content_type": "document",
                  "document_kind": "article", "domain": "general"},
        )
        job_id = resp.json()["job_id"]
        d = (await client.get(f"/api/jobs/{job_id}")).json()
        assert d["prompt_versions"] == {}


@pytest.mark.asyncio
class TestPromptLockedAPI:
    """prompt_locked 协议步(semantic_attestation):模板可读,任何覆盖写入 403。"""

    async def test_list_marks_locked_and_template(self, client):
        steps = (await client.get("/api/prompts")).json()["steps"]
        by_key = {(s["pipeline"], s["step"]): s for s in steps}
        sem = by_key[("video", "11_semantic_attestation")]
        assert sem["locked"] is True and sem["has_template"] is True
        assert by_key[("video", "11_smart")]["locked"] is False

    async def test_detail_readable_with_locked_flag(self, client):
        g = (await client.get("/api/prompts/video/11_semantic_attestation")).json()
        assert g["locked"] is True
        assert g["default_templates"][0]["name"] == "semantic_attestation"
        assert "独立证据核验器" in g["default_template"]

    async def test_put_locked_403(self, client):
        r = await client.put(
            "/api/prompts/video/11_semantic_attestation",
            json={"scope": "global", "content": "x"},
        )
        assert r.status_code == 403

    async def test_activate_locked_403(self, client):
        r = await client.post(
            "/api/prompts/document/06_semantic_attestation/activate",
            json={"scope": "global", "version": None},
        )
        assert r.status_code == 403

    async def test_delete_locked_403(self, client):
        r = await client.delete(
            "/api/prompts/audio/04_semantic_attestation?scope=global"
        )
        assert r.status_code == 403

    async def test_pipelines_endpoint_exposes_prompt_locked(self, client):
        data = (await client.get("/api/pipelines")).json()
        video = next(p for p in data["pipelines"] if p["name"] == "video")
        sem = next(s for s in video["steps"] if s["key"] == "11_semantic_attestation")
        assert sem["prompt_locked"] is True
        smart = next(s for s in video["steps"] if s["key"] == "11_smart")
        assert smart["prompt_locked"] is False


class TestPromptLockedResolution:
    """worker 兜底:prompt_locked 步解析时跳过 job 覆盖,存量脏覆盖也不得生效。"""

    def _mk_locked_step(self, tmp_path, prompt_overrides):
        (tmp_path / "job.json").write_text(
            json.dumps({"prompt_overrides": prompt_overrides}), encoding="utf-8"
        )
        hot = tmp_path / "prompts" / "templates"
        hot.mkdir(parents=True, exist_ok=True)
        (hot / "semantic_attestation.md").write_text("PROTOCOL BODY", encoding="utf-8")
        return _Step("11_semantic_attestation", tmp_path, {
            "paths": {
                "prompts_dir": str(tmp_path / "prompts"),
                "config_dir": str(tmp_path / "image-config"),
            },
            "step": {
                "name": "11_semantic_attestation",
                "prompt_template": "semantic_attestation",
                "prompt_locked": True,
            },
        })

    def test_locked_step_ignores_injected_override(self, tmp_path):
        s = self._mk_locked_step(tmp_path, {
            "11_semantic_attestation": {"content": "EVIL OVERRIDE", "version": 1},
        })
        resolved = s.ai.resolve_prompt_template("semantic_attestation")
        assert resolved.text == "PROTOCOL BODY"
        assert resolved.source == "hot"

    def test_unlocked_step_still_honors_override(self, tmp_path):
        hot = tmp_path / "prompts" / "templates"
        hot.mkdir(parents=True, exist_ok=True)
        (hot / "11_smart.md").write_text("TEMPLATE BODY", encoding="utf-8")
        s = _mk_step(tmp_path, {"11_smart": {"content": "MY OVERRIDE", "version": 1}})
        resolved = s.ai.resolve_prompt_template("11_smart")
        assert resolved.text == "MY OVERRIDE"
        assert resolved.source == "override"
