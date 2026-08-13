"""tracked 模板清单与 StepBase resolver 行为."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO / "configs" / "prompts" / "templates"


def test_template_manifest_reads_all_tracked_bytes():
    from scripts.gen_prompt_templates import TEMPLATE_NAMES, template_manifest

    manifest = template_manifest(TEMPLATES_DIR)
    assert set(manifest) == set(TEMPLATE_NAMES)
    assert all(item["bytes"] > 0 and item["sha256"].startswith("sha256:")
               for item in manifest.values())


def test_all_templates_present():
    from scripts.gen_prompt_templates import TEMPLATE_NAMES
    assert {f.stem for f in TEMPLATES_DIR.glob("*.md")} == set(TEMPLATE_NAMES)


def test_document_smart_note_preserves_source_internal_conflicts():
    chapter = (TEMPLATES_DIR / "05_smart_document.md").read_text(encoding="utf-8")
    final = (TEMPLATES_DIR / "05_smart_document.final.md").read_text(encoding="utf-8")
    introduction = (TEMPLATES_DIR / "05_smart_document.introduction.md").read_text(encoding="utf-8")
    assert "来源内部冲突" in chapter
    assert "每段有一个短 source alias" in chapter
    assert "不设统一字数、段落数或章节数限制" in final
    assert "无法归因" in final
    assert "caption 冲突" in final
    assert "最小集合" in final
    assert "不得自行写“模型综合”小节" in final
    assert "来源内部未决矛盾" in final
    assert "完整调用审计、哈希和覆盖清单不进入 note_markdown" in final
    assert "论文导读：这篇论文要解决什么" in introduction
    assert "背景与问题" in introduction and "解决思路" in introduction
    assert "不得升级为因果解释" in introduction


def test_semantic_attestation_template_uses_short_decision_refs():
    """模型只回传稳定短引用,完整 candidate ID 不进入 prompt。"""
    from shared.provenance import build_semantic_attestation_prompt

    protocol = (TEMPLATES_DIR / "semantic_attestation.md").read_text(encoding="utf-8")
    manifest = {
        "note_type": "smart",
        "candidates": [{
            "candidate_id": "c1",
            "source_segment_id": "seg-1",
            "transform_kind": "cross_language",
            "anchor": "CLAIM",
        }],
    }
    source_manifest = {"segments": [{
        "segment_id": "seg-1", "support_text": "SOURCE", "locator": {"t": 1},
    }]}
    prompt = build_semantic_attestation_prompt(
        manifest, source_manifest, protocol=protocol,
    )
    request = json.loads(prompt.split("INPUT=", 1)[1])
    assert request == {"schema_version": 3, "items": [{
        "decision_id": "d000", "note_type": "smart",
        "transform_kind": "cross_language", "claim": "CLAIM",
        "canonical_source": "SOURCE", "locator": {"t": 1},
    }]}
    assert "candidate_id" not in prompt
    assert prompt.startswith(protocol.rstrip() + "\n\nINPUT=")


def test_semantic_attestation_empty_protocol_fails_closed():
    from shared.provenance import build_semantic_attestation_prompt

    with pytest.raises(ValueError, match="protocol is empty"):
        build_semantic_attestation_prompt(
            {"note_type": "smart", "candidates": []},
            {"segments": []},
            protocol="   ",
        )


def test_review_templates_share_skeleton():
    """通用与 Document 评审骨架共享占位契约,来源约束各自独立."""
    names = ("05_review", "08_review", "12_review")
    bodies = {
        name: (TEMPLATES_DIR / f"{name}.md").read_text(encoding="utf-8")
        for name in names
    }
    assert bodies["05_review"] == bodies["12_review"]
    assert '"source":"document"' in bodies["08_review"]
    # 骨架含运行期注入的占位符(build_review_prompt 用 str.replace 填)。
    for ph in ("{{intro}}", "{{dimensions}}", "{{score_example}}", "{{ref_block}}"):
        assert all(ph in body for body in bodies.values())


def _mk_step(tmp_path: Path):
    """构造一个最小 StepBase 实例以验证 Prompt 解析快照。"""
    from shared.step_base import StepBase
    return StepBase("x", tmp_path, {
        "paths": {
            "prompts_dir": str(tmp_path),
            "config_dir": str(tmp_path / "image"),
        },
        "step": {"name": "x"},
    })


def test_load_prompt_template_missing_fails_closed(tmp_path):
    s = _mk_step(tmp_path)
    from shared.prompt_resolver import PromptResolutionError
    with pytest.raises(PromptResolutionError, match="missing"):
        s.ai.load_prompt_template("nope")


def test_load_prompt_template_reads_file(tmp_path):
    s = _mk_step(tmp_path)
    td = tmp_path / "templates"
    td.mkdir(parents=True)
    (td / "foo.md").write_text("FROM-FILE <<BODY>>", encoding="utf-8")
    assert s.ai.load_prompt_template("foo") == "FROM-FILE <<BODY>>"
    # 占位用 replace 注入(prompt 含字面 {},不可 format)
    assert s.ai.load_prompt_template("foo").replace("<<BODY>>", "X{a}") == "FROM-FILE X{a}"


def test_template_hash_changes_on_edit(tmp_path):
    s = _mk_step(tmp_path)
    td = tmp_path / "templates"
    td.mkdir()
    f = td / "foo.md"
    f.write_text("v1", encoding="utf-8")
    h1 = s.ai.template_hash("foo")
    assert h1  # 非空
    f.write_text("v2", encoding="utf-8")
    assert s.ai.template_hash("foo") == h1  # 同一次执行固定同一字节快照
    assert _mk_step(tmp_path).ai.template_hash("foo") != h1


def test_fork_inherits_frozen_template_and_override_snapshot(tmp_path):
    s = _mk_step(tmp_path)
    s.config["step"]["prompt_template"] = "foo"
    templates = tmp_path / "templates"
    templates.mkdir()
    template = templates / "foo.md"
    template.write_text("frozen", encoding="utf-8")
    (tmp_path / "job.json").write_text(
        '{"prompt_overrides":{"x":{"content":"override-a","version":1}}}',
        encoding="utf-8",
    )
    assert s.ai.load_prompt_template("foo") == "override-a"
    forked = s.ai.fork("child")

    template.write_text("changed", encoding="utf-8")
    (tmp_path / "job.json").write_text(
        '{"prompt_overrides":{"x":{"content":"override-b","version":2}}}',
        encoding="utf-8",
    )
    assert forked.load_prompt_template("foo") == "override-a"
    assert forked.job_prompt_overrides() == {
        "x": {"content": "override-a", "version": 1},
    }


# 评审 prompt 白盒:build_review_prompt 骨架 + 运行期注入


def _mk_review_step(tmp_path, step="08_review"):
    from shared.step_base import StepBase
    (tmp_path / "job.json").write_text("{}", encoding="utf-8")
    return StepBase(step, tmp_path, {
        "paths": {"prompts_dir": str(tmp_path / "hot"),
                  "config_dir": str(REPO / "configs")},
        "step": {"name": step, "pool": "ai"},
        "domain": {"name": "general"},
    })


def test_build_review_prompt_default_injects_all_placeholders(tmp_path):
    """镜像 tracked 骨架渲染 intro/维度表/score 示例/参照块."""
    s = _mk_review_step(tmp_path)
    dims = [("completeness", "信息完整性"), ("accuracy", "准确性")]
    p = s.review.build_prompt(intro="请评审本笔记。", dimensions=dims, ref_block="REF-XYZ")
    assert "请评审本笔记。" in p
    assert "1. completeness: 信息完整性" in p
    assert "2. accuracy: 准确性" in p
    assert '"completeness": 4' in p and '"accuracy": 4' in p
    assert "REF-XYZ" in p
    assert "{{" not in p  # 占位符已全部替换


def test_build_review_prompt_uses_db_override_with_refblock(tmp_path):
    """DB 注入覆盖替换骨架;保留 {{ref_block}} → 参照块仍按本步实参注入(所见即所改)。"""
    s = _mk_review_step(tmp_path)
    (tmp_path / "job.json").write_text(
        '{"prompt_overrides":{"08_review":{"content":"自定义评审指令\\n\\n{{ref_block}}","version":1}}}',
        encoding="utf-8",
    )
    s.ai.prompt_overrides_snapshot = None
    s.ai.resolved_prompts = {}
    p = s.review.build_prompt(intro="X", dimensions=[("a", "A")], ref_block="REFBLK")
    assert "自定义评审指令" in p
    assert "REFBLK" in p


def test_build_review_prompt_appends_refblock_when_placeholder_missing(tmp_path):
    """覆盖把 {{ref_block}} 删了 → 兜底把参照块补在末尾,被评内容绝不丢。"""
    s = _mk_review_step(tmp_path)
    (tmp_path / "job.json").write_text(
        '{"prompt_overrides":{"08_review":{"content":"覆盖里没有占位符","version":1}}}',
        encoding="utf-8",
    )
    s.ai.prompt_overrides_snapshot = None
    s.ai.resolved_prompts = {}
    p = s.review.build_prompt(intro="X", dimensions=[("a", "A")], ref_block="REFBLK")
    assert "覆盖里没有占位符" in p
    assert p.rstrip().endswith("REFBLK")


def test_build_review_prompt_reads_template_file(tmp_path):
    """有 templates/{step}.md → 用文件骨架渲染(模板文件 = 白盒展示的默认)。"""
    s = _mk_review_step(tmp_path)
    td = tmp_path / "hot" / "templates"
    td.mkdir(parents=True)
    (td / "08_review.md").write_text("FILE骨架 {{intro}} || {{ref_block}}", encoding="utf-8")
    p = s.review.build_prompt(intro="INTRO", dimensions=[("a", "A")], ref_block="RB")
    assert p == "FILE骨架 INTRO || RB"
