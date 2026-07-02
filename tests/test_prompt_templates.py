"""externalize-prompt:模板文件与代码内 _DEFAULT 常量一致(防漂移)+ _load_prompt_template/template_hash 行为。"""
from __future__ import annotations
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO / "configs" / "prompts" / "templates"


def test_templates_match_constants():
    """每个 templates/*.md 与生成它的代码常量逐字一致 —— 改代码常量必须同步重生成模板,反之亦然。
    跑:python scripts/gen_prompt_templates.py 重生成。"""
    from scripts.gen_prompt_templates import TEMPLATES
    for name, content in TEMPLATES.items():
        f = TEMPLATES_DIR / name
        assert f.exists(), f"模板缺失:{name}(跑 scripts/gen_prompt_templates.py 生成)"
        assert f.read_text(encoding="utf-8") == content, f"模板与常量漂移:{name}"


def test_all_templates_present():
    from scripts.gen_prompt_templates import TEMPLATES
    # 11 个生成步 + pdf 直喂翻译变体(04_translate_paper.pdf)+ 3 个评审步(共享骨架)= 15。
    assert len(TEMPLATES) == 15
    assert {f.name for f in TEMPLATES_DIR.glob("*.md")} >= set(TEMPLATES)


def test_review_templates_share_skeleton():
    """三个评审模板内容 = StepBase.review_prompt_skeleton(),且彼此逐字相同(共享骨架)。"""
    from shared.step_base import StepBase
    sk = StepBase.review_prompt_skeleton()
    bodies = [
        (TEMPLATES_DIR / f"{n}.md").read_text(encoding="utf-8")
        for n in ("05_review", "06_review", "12_review")
    ]
    for b in bodies:
        assert b == sk
    assert bodies[0] == bodies[1] == bodies[2]
    # 骨架含运行期注入的占位符(build_review_prompt 用 str.replace 填)。
    for ph in ("{{intro}}", "{{dimensions}}", "{{score_example}}", "{{ref_block}}"):
        assert ph in sk


def _mk_step(tmp_path: Path):
    """构造一个最小 StepBase 实例(只为测 _load_prompt_template/template_hash)。"""
    from shared.step_base import StepBase
    s = StepBase.__new__(StepBase)
    s.config = {"paths": {"prompts_dir": str(tmp_path)}}
    s.step_name = "x"
    return s


def test_load_prompt_template_fallback_to_default(tmp_path):
    s = _mk_step(tmp_path)
    # 模板不存在 → 回退 default(空卷/旧部署兜底)
    assert s._load_prompt_template("nope", "DEFAULT-TEXT") == "DEFAULT-TEXT"


def test_load_prompt_template_reads_file(tmp_path):
    s = _mk_step(tmp_path)
    td = tmp_path / "templates"
    td.mkdir()
    (td / "foo.md").write_text("FROM-FILE <<BODY>>", encoding="utf-8")
    assert s._load_prompt_template("foo", "DEFAULT") == "FROM-FILE <<BODY>>"
    # 占位用 replace 注入(prompt 含字面 {},不可 format)
    assert s._load_prompt_template("foo", "D").replace("<<BODY>>", "X{a}") == "FROM-FILE X{a}"


def test_template_hash_changes_on_edit(tmp_path):
    s = _mk_step(tmp_path)
    td = tmp_path / "templates"
    td.mkdir()
    f = td / "foo.md"
    f.write_text("v1", encoding="utf-8")
    h1 = s.template_hash("foo")
    assert h1  # 非空
    f.write_text("v2", encoding="utf-8")
    assert s.template_hash("foo") != h1  # 改模板则指纹变,should_run 重跑
    assert s.template_hash("absent") == ""  # 全缺 → 空串(不影响指纹)


# 评审 prompt 白盒:build_review_prompt 骨架 + 运行期注入


def _mk_review_step(tmp_path, step="06_review"):
    from shared.step_base import StepBase
    s = StepBase.__new__(StepBase)
    s.config = {"paths": {"prompts_dir": str(tmp_path)}}  # 无 templates 子目录 → 走内联骨架
    s.step_name = step
    return s


def test_build_review_prompt_default_injects_all_placeholders(tmp_path):
    """无模板/覆盖 → 内联骨架渲染:intro/维度表/score 示例/参照块全注入,无残留占位。"""
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
    s._injected_prompt_override = lambda: "自定义评审指令\n\n{{ref_block}}"
    p = s.build_review_prompt(intro="X", dimensions=[("a", "A")], ref_block="REFBLK")
    assert "自定义评审指令" in p
    assert "REFBLK" in p


def test_build_review_prompt_appends_refblock_when_placeholder_missing(tmp_path):
    """覆盖把 {{ref_block}} 删了 → 兜底把参照块补在末尾,被评内容绝不丢。"""
    s = _mk_review_step(tmp_path)
    s._injected_prompt_override = lambda: "覆盖里没有占位符"
    p = s.build_review_prompt(intro="X", dimensions=[("a", "A")], ref_block="REFBLK")
    assert "覆盖里没有占位符" in p
    assert p.rstrip().endswith("REFBLK")


def test_build_review_prompt_reads_template_file(tmp_path):
    """有 templates/{step}.md → 用文件骨架渲染(模板文件 = 白盒展示的默认)。"""
    s = _mk_review_step(tmp_path)
    td = tmp_path / "templates"
    td.mkdir()
    (td / "06_review.md").write_text("FILE骨架 {{intro}} || {{ref_block}}", encoding="utf-8")
    p = s.build_review_prompt(intro="INTRO", dimensions=[("a", "A")], ref_block="RB")
    assert p == "FILE骨架 INTRO || RB"
