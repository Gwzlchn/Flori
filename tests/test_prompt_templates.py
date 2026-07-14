"""tracked 模板清单与 StepBase resolver 行为."""
from __future__ import annotations
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
    assert len(TEMPLATE_NAMES) == 15
    assert {f.stem for f in TEMPLATES_DIR.glob("*.md")} == set(TEMPLATE_NAMES)


def test_review_templates_share_skeleton():
    """三个评审模板彼此逐字相同,动态占位符由 StepBase 注入."""
    bodies = [
        (TEMPLATES_DIR / f"{n}.md").read_text(encoding="utf-8")
        for n in ("05_review", "06_review", "12_review")
    ]
    assert bodies[0] == bodies[1] == bodies[2]
    # 骨架含运行期注入的占位符(build_review_prompt 用 str.replace 填)。
    for ph in ("{{intro}}", "{{dimensions}}", "{{score_example}}", "{{ref_block}}"):
        assert ph in bodies[0]


def _mk_step(tmp_path: Path):
    """构造一个最小 StepBase 实例(只为测 _load_prompt_template/template_hash)。"""
    from shared.step_base import StepBase
    s = StepBase.__new__(StepBase)
    s.config = {"paths": {"prompts_dir": str(tmp_path), "config_dir": str(tmp_path / "image")},
                "step": {"name": "x"}}
    s.step_name = "x"
    s._resolved_prompts = {}
    s._prompt_overrides_snapshot = {}
    return s


def test_load_prompt_template_missing_fails_closed(tmp_path):
    s = _mk_step(tmp_path)
    from shared.prompt_resolver import PromptResolutionError
    with pytest.raises(PromptResolutionError, match="missing"):
        s._load_prompt_template("nope")


def test_load_prompt_template_reads_file(tmp_path):
    s = _mk_step(tmp_path)
    td = tmp_path / "templates"
    td.mkdir(parents=True)
    (td / "foo.md").write_text("FROM-FILE <<BODY>>", encoding="utf-8")
    assert s._load_prompt_template("foo") == "FROM-FILE <<BODY>>"
    # 占位用 replace 注入(prompt 含字面 {},不可 format)
    assert s._load_prompt_template("foo").replace("<<BODY>>", "X{a}") == "FROM-FILE X{a}"


def test_template_hash_changes_on_edit(tmp_path):
    s = _mk_step(tmp_path)
    td = tmp_path / "templates"
    td.mkdir()
    f = td / "foo.md"
    f.write_text("v1", encoding="utf-8")
    h1 = s.template_hash("foo")
    assert h1  # 非空
    f.write_text("v2", encoding="utf-8")
    assert s.template_hash("foo") == h1  # 同一次执行固定同一字节快照
    assert _mk_step(tmp_path).template_hash("foo") != h1


# 评审 prompt 白盒:build_review_prompt 骨架 + 运行期注入


def _mk_review_step(tmp_path, step="06_review"):
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
    p = s.build_review_prompt(intro="请评审本笔记。", dimensions=dims, ref_block="REF-XYZ")
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
        '{"prompt_overrides":{"06_review":{"content":"自定义评审指令\\n\\n{{ref_block}}","version":1}}}',
        encoding="utf-8",
    )
    s._prompt_overrides_snapshot = None
    s._resolved_prompts = {}
    p = s.build_review_prompt(intro="X", dimensions=[("a", "A")], ref_block="REFBLK")
    assert "自定义评审指令" in p
    assert "REFBLK" in p


def test_build_review_prompt_appends_refblock_when_placeholder_missing(tmp_path):
    """覆盖把 {{ref_block}} 删了 → 兜底把参照块补在末尾,被评内容绝不丢。"""
    s = _mk_review_step(tmp_path)
    (tmp_path / "job.json").write_text(
        '{"prompt_overrides":{"06_review":{"content":"覆盖里没有占位符","version":1}}}',
        encoding="utf-8",
    )
    s._prompt_overrides_snapshot = None
    s._resolved_prompts = {}
    p = s.build_review_prompt(intro="X", dimensions=[("a", "A")], ref_block="REFBLK")
    assert "覆盖里没有占位符" in p
    assert p.rstrip().endswith("REFBLK")


def test_build_review_prompt_reads_template_file(tmp_path):
    """有 templates/{step}.md → 用文件骨架渲染(模板文件 = 白盒展示的默认)。"""
    s = _mk_review_step(tmp_path)
    td = tmp_path / "hot" / "templates"
    td.mkdir(parents=True)
    (td / "06_review.md").write_text("FILE骨架 {{intro}} || {{ref_block}}", encoding="utf-8")
    p = s.build_review_prompt(intro="INTRO", dimensions=[("a", "A")], ref_block="RB")
    assert p == "FILE骨架 INTRO || RB"
