"""分层论文智能笔记的构包、证据闭包和步骤接线测试。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import steps.document.step_05_smart as smart_step_module

from shared.errors import InputInvalidError
from shared.ai_gateway import DryRunProvider
from shared.models import LLMRequest, LLMResponse
from shared.step_ai import AIInvocation
from steps.document.smart_pipeline import (
    MAX_IMAGE_ATTACHMENTS,
    MAX_PACKAGE_SOURCE_ALIASES,
    MAX_PACKAGES,
    MAX_STAGE_PROMPT_BYTES,
    build_chapter_packages,
    parse_stage_result,
    inject_source_markers,
    render_figures,
    render_model_synthesis,
    validate_chapter_card,
    validate_final,
)
from steps.document.step_05_smart import DocumentSmartStep
from steps.document.provenance import (
    extract_attestable_document_markers,
    load_document_source_manifest,
)
from tests.steps.conftest import make_step_config
from tests.steps.test_step_document import _fixture


def _chapter(package_id: str = "p001", source: str = "s002") -> dict:
    return {
        "package_id": package_id,
        "overview": "本包说明论文的问题和一个可回查结果。",
        "knowledge": [
            {
                "kind": "result", "topic": "延迟", "claim": "延迟为 3 ms。",
                "explanation": "这是来源直接报告的结果。",
                "why_it_matters": "它给出方法成本的量级。",
                "author_claim": True, "source_refs": [source],
            },
            {
                "kind": "method", "topic": "实现", "claim": "实现包含 x = 3。",
                "explanation": "代码片段给出最小实现。",
                "why_it_matters": "它让方法可以回查。",
                "author_claim": True, "source_refs": ["s003"],
            },
        ],
        "cross_section_links": [{
            "relation": "supports", "target_hint": "后续结果",
            "explanation": "该数字用于支持效率讨论。", "source_refs": [source],
        }],
        "figures": [],
        "unresolved": [],
        "coverage_refs": ["s001", "s002", "s003"],
        "synthesis": {
            "analysis": "来源给出一个明确结果。", "basis": "延迟段落。",
            "uncertainty": "小样本只用于协议测试。",
        },
    }


def _theme() -> dict:
    return {
        "theme_id": "t01", "overview": "从问题到结果的主题。",
        "learning_sections": [{
            "title": "结果", "purpose": "解释核心结果",
            "explanation": "延迟结果给出方法成本。",
            "knowledge_refs": ["p001-k001", "p001-k002"], "figure_refs": [],
        }],
        "cross_theme_links": [], "tensions": [], "limitations": [],
        "figure_guides": [], "coverage_refs": ["p001-k001", "p001-k002"],
        "synthesis": {
            "analysis": "结果可作为效率依据。", "basis": "p001-k001",
            "uncertainty": "没有更多实验。",
        },
    }


def _knowledge_catalog(*refs: str) -> dict[str, dict]:
    sources = {
        "p001-k001": ["s002"],
        "p001-k002": ["s003"],
        "p001-k003": ["s004"],
    }
    return {ref: {"source_refs": sources[ref]} for ref in refs}


def _final() -> dict:
    paragraph = (
        "论文先提出一个需要测量的问题，再给出方法和实验结果。"
        "这一组织方式让读者能够从研究动机走到可验证结论，同时保留适用边界。"
    ) * 12
    return {
        "title": "Title：从问题到验证", "subtitle": "忠实且可回查的学习笔记",
        "note_markdown": (
            f"## 一、问题与方法\n\n{paragraph}[证据: p001-k001]"
        ),
        "used_knowledge_refs": ["p001-k001", "p001-k002"],
        "theme_coverage_refs": ["t01"], "figure_placements": [],
        "synthesis": {
            "analysis": "方法和结果形成闭环。", "basis": "主题综合。",
            "uncertainty": "只依据冻结来源。",
            "knowledge_refs": ["p001-k002"],
        },
        "audit_summary": {
            "scope": "覆盖全部章节卡。", "known_gaps": [],
            "evidence_note": "详细调用审计由系统折叠展示。",
        },
    }


def _introduction() -> dict:
    body = (
        "这篇论文从现实中的测量缺口出发，说明为什么需要一个可复查的研究方案。"
        "作者随后给出实现路径，并用来源中的实验数字检查方案是否成立。"
    ) * 5
    markdown = (
        "## 论文导读：这篇论文要解决什么\n\n"
        f"### 背景与问题\n\n背景：{body}[证据: p001-k001]\n\n"
        f"### 解决思路\n\n方案：{body}\n\n"
        f"### 如何验证\n\n验证：{body}\n\n"
        f"### 主要结论与阅读边界\n\n边界：{body}"
    )
    return {"introduction_markdown": markdown, "used_knowledge_refs": ["p001-k001"]}


def _fake_layered_call_with_value(
    self: AIInvocation, prompt: str, value: dict,
) -> str:
    self.last_provider = "qoder-cli"
    self.last_model = "ultimate"
    self.last_response = LLMResponse(
        content="{}", model="ultimate", provider="qoder-cli",
        session_id=f"session-{self.audit_name}",
    )
    self.ai_log_records.append({
        "call_index": self.call_index,
        "audit_stage": self.audit_name,
        "exec_id": f"exec:{self.audit_name}:{self.call_index}",
        "prompt": {"rendered": {"user": prompt}},
        "ok": True,
    })
    self.call_index += 1
    return json.dumps(value, ensure_ascii=False)


def _fake_layered_call(self: AIInvocation, prompt: str, **_kwargs) -> str:
    if self.audit_name and "chapter" in self.audit_name:
        value = _chapter()
    elif self.audit_name and "theme" in self.audit_name:
        value = _theme()
    elif self.audit_name == "03-final":
        value = _final()
    elif self.audit_name == "04-introduction":
        value = _introduction()
    else:
        raise AssertionError(f"unexpected audit stage: {self.audit_name}")
    return _fake_layered_call_with_value(self, prompt, value)


def test_layered_step_uses_original_once_and_publishes_folded_audit(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    prompts: list[tuple[str | None, str]] = []
    provenance_sessions: list[tuple[str | None, str | None]] = []
    original_extract = smart_step_module.extract_attestable_document_markers

    def call(self, prompt, **kwargs):
        prompts.append((self.audit_name, prompt))
        return _fake_layered_call(self, prompt, **kwargs)

    def extract(markdown, source_manifest, *, ai, **kwargs):
        provenance_sessions.append((ai.audit_name, ai.last_response.session_id))
        return original_extract(markdown, source_manifest, ai=ai, **kwargs)

    monkeypatch.setattr(AIInvocation, "call", call)
    monkeypatch.setattr(
        smart_step_module, "extract_attestable_document_markers", extract,
    )
    stale = job / "output/smart_pipeline/chapter-card-p999.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"stale":true}', encoding="utf-8")
    (stale.parent / "chapter-card-p999.json.tmp").write_text("partial")
    (stale.parent / "old.bin").write_bytes(b"old")
    (stale.parent / "old-subdir").mkdir()
    (stale.parent / "old-subdir/fragment.json").write_text("{}")
    result = step.execute()

    note = (job / result["note_file"]).read_text(encoding="utf-8")
    assert "## 论文导读：这篇论文要解决什么" in note
    assert "### 背景与问题" in note
    assert "Latency is 3 ms." not in note
    assert "translation" not in step.step_input_hashes()
    chapter_prompt = next(prompt for stage, prompt in prompts if stage == "01-chapter-p001")
    assert chapter_prompt.count("Latency is 3 ms.") == 1
    assert len(chapter_prompt.encode("utf-8")) < 100_000
    assert {stage for stage, _ in prompts} == {
        "01-chapter-p001", "02-theme-t01", "03-final", "04-introduction",
    }
    audit = [
        json.loads(line)
        for line in (job / "output/ai_logs/05_smart.jsonl").read_text().splitlines()
    ]
    assert [item["audit_stage"] for item in audit] == sorted(
        item["audit_stage"] for item in audit
    )
    assert len({item["exec_id"] for item in audit}) == 4
    assert provenance_sessions == [
        ("04-introduction", "session-04-introduction"),
        ("03-final", "session-03-final"),
    ]
    assert (job / "output/smart_pipeline/manifest.json").is_file()
    pipeline_manifest = json.loads(
        (job / "output/smart_pipeline/manifest.json").read_text()
    )
    assert pipeline_manifest["artifacts"]
    assert all({"path", "bytes", "sha256"} == set(item) for item in pipeline_manifest["artifacts"])
    assert not stale.exists()
    assert all("p999" not in item["path"] for item in pipeline_manifest["artifacts"])
    expected_pipeline_files = {
        Path(item["path"]).name for item in pipeline_manifest["artifacts"]
    } | {"manifest.json"}
    assert {path.name for path in stale.parent.iterdir()} == expected_pipeline_files
    assert (job / "output/provenance_exact/smart.json").is_file()
    semantic = json.loads(
        (job / "output/provenance_candidates/smart.json").read_text()
    )
    assert {item["producer_invocation_id"] for item in semantic["candidates"]} == {
        "session-03-final", "session-04-introduction",
    }


def test_layered_step_dry_run_contract_stays_schema_aware(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)

    def call(_self, prompt, **_kwargs):
        _self.last_provider = "dry-run"
        _self.last_model = "dry-run"
        _self.last_response = LLMResponse(
            content="{}", model="dry-run", provider="dry-run",
            session_id=f"session-{_self.audit_name}",
        )
        return DryRunProvider._content(LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            response_format="json",
        ))

    monkeypatch.setattr(AIInvocation, "call", call)
    result = step.execute()

    note = (job / result["note_file"]).read_text(encoding="utf-8")
    assert "## 论文导读：这篇论文要解决什么" in note
    assert result["chapter_packages"] == 1
    assert result["themes"] == 1


def test_chapter_builder_splits_six_images_without_repeating_source():
    blocks = [{
        "block_id": "S1", "order": 0, "kind": "heading", "level": 1,
        "text": "Method",
    }]
    figures = []
    segments = []
    for index in range(1, 8):
        block_id = f"B{index}"
        blocks.append({
            "block_id": block_id, "order": index, "kind": "paragraph",
            "level": None, "text": f"content {index}",
        })
        segments.append({
            "segment_id": block_id, "section": "S1", "support_text": f"content {index}",
        })
        if index <= 6:
            figures.append({
                "figure_id": f"fig-{index}", "block_id": block_id,
                "label": f"Figure {index}", "caption": "caption",
                "media": [{"artifact": f"assets/f{index}.png", "width": 100, "height": 100}],
            })
    document = {
        "job_id": "job", "metadata": {"titles": {"original": "Paper"}, "abstract": "A"},
        "blocks": blocks, "figures": figures, "tables": [],
    }
    paper_map, packages, receipt = build_chapter_packages(
        document, {"segments": segments},
    )
    assert paper_map["title"] == "Paper"
    assert len(packages) == 2
    assert {item["logical_parent"] for item in packages} == {"p001"}
    assert max(
        len({media["artifact_path"] for figure in package["figures"] for media in figure["media"]})
        for package in packages
    ) <= MAX_IMAGE_ATTACHMENTS
    identities = [
        (item["segment_id"], item["part"]) for item in receipt["assignments"]
    ]
    assert len(identities) == len(set(identities)) == 7


def test_long_segment_attaches_visual_only_to_first_part():
    text = "long-source " * 4000
    document = {
        "job_id": "job", "metadata": {"titles": {"original": "Paper"}},
        "blocks": [
            {"block_id": "S1", "order": 0, "kind": "heading", "level": 1, "text": "Method"},
            {"block_id": "B1", "order": 1, "kind": "paragraph", "text": text},
        ],
        "figures": [{
            "figure_id": "fig-1", "block_id": "B1", "label": "Figure 1",
            "caption": "caption", "media": [{"artifact": "assets/f1.png"}],
        }],
        "tables": [],
    }
    _, packages, receipt = build_chapter_packages(document, {"segments": [{
        "segment_id": "B1", "section": "S1", "support_text": text,
    }]})
    assert receipt["covered_segment_parts"] > 1
    occurrences = [
        figure
        for package in packages
        for figure in package["figures"]
        if figure["visual_id"] == "fig-1"
    ]
    assert len(occurrences) == 1


def test_chapter_builder_splits_source_aliases_at_output_contract_limit():
    blocks = [{
        "block_id": "S1", "order": 0, "kind": "heading", "level": 1,
        "text": "Method",
    }]
    segments = []
    for index in range(MAX_PACKAGE_SOURCE_ALIASES + 1):
        block_id = f"B{index:02d}"
        text = f"source {index}"
        blocks.append({
            "block_id": block_id, "order": index + 1,
            "kind": "paragraph", "text": text,
        })
        segments.append({
            "segment_id": block_id, "section": "S1", "support_text": text,
        })
    document = {
        "job_id": "job", "metadata": {"titles": {"original": "Paper"}},
        "blocks": blocks, "figures": [], "tables": [],
    }

    _, packages, receipt = build_chapter_packages(
        document, {"segments": segments},
    )

    assert [item["package_id"] for item in packages] == ["p001", "p002"]
    assert [item["logical_parent"] for item in packages] == ["p001", "p002"]
    assert [len(item["source_aliases"]) for item in packages] == [32, 1]
    identities = [
        (item["segment_id"], item["part"]) for item in receipt["assignments"]
    ]
    assert len(identities) == len(set(identities)) == 33


def test_chapter_builder_caps_short_sources_before_child_package_suffixes():
    total = MAX_PACKAGE_SOURCE_ALIASES * 26 + 1
    blocks = [{
        "block_id": "S1", "order": 0, "kind": "heading", "level": 1,
        "text": "Method",
    }]
    segments = []
    for index in range(total):
        block_id = f"B{index:03d}"
        blocks.append({
            "block_id": block_id, "order": index + 1,
            "kind": "paragraph", "text": "x",
        })
        segments.append({
            "segment_id": block_id, "section": "S1", "support_text": "x",
        })
    document = {
        "job_id": "job", "metadata": {"titles": {"original": "Paper"}},
        "blocks": blocks, "figures": [], "tables": [],
    }

    _, packages, receipt = build_chapter_packages(
        document, {"segments": segments},
    )

    assert len(packages) == 27
    assert max(len(item["source_aliases"]) for item in packages) == 32
    identities = [
        (item["segment_id"], item["part"]) for item in receipt["assignments"]
    ]
    assert len(identities) == len(set(identities)) == total


def test_image_split_supports_more_than_single_letter_package_suffixes():
    blocks = [{
        "block_id": "S1", "order": 0, "kind": "heading", "level": 1,
        "text": "Method",
    }]
    segments = []
    figures = []
    for index in range(27):
        block_id = f"B{index:02d}"
        blocks.append({
            "block_id": block_id, "order": index + 1,
            "kind": "paragraph", "text": "x",
        })
        segments.append({
            "segment_id": block_id, "section": "S1", "support_text": "x",
        })
        for image in range(MAX_IMAGE_ATTACHMENTS):
            figures.append({
                "figure_id": f"fig-{index}-{image}", "block_id": block_id,
                "label": "Figure", "caption": "caption",
                "media": [{"artifact": f"assets/{index}-{image}.png"}],
            })
    document = {
        "job_id": "job", "metadata": {"titles": {"original": "Paper"}},
        "blocks": blocks, "figures": figures, "tables": [],
    }

    _, packages, receipt = build_chapter_packages(
        document, {"segments": segments},
    )

    assert len(packages) == 27
    assert packages[-1]["package_id"] == "p001aa"
    assert all(len(item["source_aliases"]) == 1 for item in packages)
    assert len(receipt["assignments"]) == 27


def test_image_split_rechecks_final_package_limit():
    blocks = []
    segments = []
    figures = []
    order = 0
    for section in range(33):
        heading = f"S{section}"
        blocks.append({
            "block_id": heading, "order": order, "kind": "heading",
            "level": 1, "text": f"Section {section}",
        })
        order += 1
        for part in range(2):
            block_id = f"B{section}-{part}"
            text = chr(65 + part) * (16 * 1024)
            blocks.append({
                "block_id": block_id, "order": order, "kind": "paragraph",
                "text": text,
            })
            order += 1
            segments.append({
                "segment_id": block_id, "section": heading, "support_text": text,
            })
            for image in range(3):
                figures.append({
                    "figure_id": f"fig-{section}-{part}-{image}",
                    "block_id": block_id, "label": "Figure", "caption": "caption",
                    "media": [{
                        "artifact": f"assets/{section}-{part}-{image}.png",
                    }],
                })
    document = {
        "job_id": "job", "metadata": {"titles": {"original": "Paper"}},
        "blocks": blocks, "figures": figures, "tables": [],
    }
    assert 33 * 2 > MAX_PACKAGES
    with pytest.raises(ValueError, match="after image split"):
        build_chapter_packages(document, {"segments": segments})


@pytest.mark.parametrize(
    "heading",
    ["9 References", "9. References", "9) References", "Bibliography", "参考文献", "参考书目"],
)
def test_chapter_builder_excludes_bibliography_variants(heading):
    document = {
        "job_id": "job", "metadata": {"titles": {"original": "Paper"}},
        "blocks": [
            {"block_id": "S1", "order": 0, "kind": "heading", "level": 1, "text": "Method"},
            {"block_id": "B1", "order": 1, "kind": "paragraph", "text": "method body"},
            {"block_id": "R", "order": 2, "kind": "heading", "level": 1, "text": heading},
            {"block_id": "R1", "order": 3, "kind": "paragraph", "text": "citation list"},
        ],
        "figures": [], "tables": [],
    }
    _, packages, receipt = build_chapter_packages(document, {"segments": [
        {"segment_id": "B1", "section": "S1", "support_text": "method body"},
        {"segment_id": "R1", "section": "R", "support_text": "citation list"},
    ]})
    assert len(packages) == 1
    assert [item["segment_id"] for item in receipt["excluded"]] == ["R1"]


def test_chapter_contract_rejects_unknown_source_and_missing_figure(tmp_path):
    schema = json.loads((
        Path(__file__).parents[2]
        / "configs/prompts/schemas/05_smart_document.chapter.json"
    ).read_text())
    package = {
        "package_id": "p001", "source_aliases": {"s001": {"segment_id": "S1"}},
        "figures": [{
            "figure_alias": "f01", "source_alias": "s001",
            "media": [{"artifact_path": "assets/a.png"}],
        }],
    }
    result = _chapter(source="unknown")
    result["coverage_refs"] = ["s001"]
    result["figures"] = []
    parsed = parse_stage_result(json.dumps(result), schema)
    with pytest.raises(ValueError, match="unknown source ref|figure closure"):
        validate_chapter_card(parsed, package)


def test_final_contract_rejects_model_written_synthesis_heading():
    result = _final()
    result["note_markdown"] += "\n\n## 模型综合\n\n模型自己写的段落。"
    with pytest.raises(ValueError, match="leave model synthesis"):
        validate_final(
            result, ["t01"], _knowledge_catalog("p001-k001", "p001-k002"), [],
        )


def test_model_synthesis_is_rendered_with_basis_uncertainty_and_evidence():
    rendered = render_model_synthesis(_final())
    section = rendered.split("## 模型综合", 1)[1]
    assert "方法和结果形成闭环" in section
    assert "**依据：** 主题综合" in section
    assert "**不确定性：** 只依据冻结来源" in section
    assert "[证据: p001-k002]" in section


def test_final_contract_rejects_unused_or_untracked_evidence_refs():
    result = _final()
    result["used_knowledge_refs"].append("p001-k003")
    with pytest.raises(ValueError, match="evidence closure"):
        validate_final(
            result, ["t01"],
            _knowledge_catalog("p001-k001", "p001-k002", "p001-k003"), [],
        )


@pytest.mark.parametrize(
    ("target", "value", "message"),
    [
        ("markdown", "\n\n![track](https://attacker.example/pixel)", "direct images"),
        ("markdown", "\n\n<img src=https://attacker.example/pixel>", "direct images"),
        ("title", "Safe\n![track](https://attacker.example/pixel)", "single line"),
        ("title", "[证据: p001-k001]", "evidence markup"),
    ],
)
def test_final_contract_rejects_model_rendered_image_or_title_injection(
    target, value, message,
):
    result = _final()
    if target == "markdown":
        result["note_markdown"] += value
    else:
        result["title"] = value
    with pytest.raises(ValueError, match=message):
        validate_final(
            result, ["t01"], _knowledge_catalog("p001-k001", "p001-k002"), [],
        )


def test_final_contract_rejects_joint_source_evidence_before_marker_extraction():
    result = _final()
    result["note_markdown"] = result["note_markdown"].replace(
        "[证据: p001-k001]", "[证据: p001-k001, p001-k002]",
    )
    with pytest.raises(ValueError, match="exactly one source"):
        validate_final(
            result, ["t01"], _knowledge_catalog("p001-k001", "p001-k002"), [],
        )


def test_figure_renderer_adds_placement_evidence_for_provenance(tmp_path):
    job = _fixture(tmp_path)
    result = _final()
    result["note_markdown"] += "\n\n{{FIGURE:p001-f01}}"
    result["figure_placements"] = [{
        "figure_ref": "p001-f01", "placement_reason": "解释结果",
        "reading_guide": "先看横轴，再看趋势。", "supports": "支持效率结论。",
        "limits": "不能推出因果关系。", "knowledge_refs": ["p001-k001"],
    }]
    validate_final(
        result, ["t01"], _knowledge_catalog("p001-k001", "p001-k002"),
        ["p001-f01"],
    )
    asset = job / "assets/f01.png"
    asset.write_bytes(b"image")
    rendered = render_figures(
        render_model_synthesis(result), result["figure_placements"],
        {"p001-f01": {
            "figure_ref": "p001-f01", "label": "Figure 1",
            "artifact_paths": ["assets/f01.png"],
        }},
        job,
    )
    assert "![Figure 1](assets/f01.png)" in rendered
    assert "[证据: p001-k001]" in rendered
    marked = inject_source_markers(
        rendered,
        {"p001-k001": {"source_refs": ["s002"]}, "p001-k002": {"source_refs": ["s003"]}},
        {"s002": "S1.P1", "s003": "S1.C1"},
        deduplicate_sources_by_evidence=False,
    )
    assert "[[source:S1.P1]]" in marked
    marker_line = next(
        line for line in marked.splitlines()
        if "先看横轴" in line and "[[source:S1.P1]]" in line
    )
    assert "先看横轴，再看趋势" in marker_line
    assert "不能推出因果关系" in marker_line
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    step = DocumentSmartStep("05_smart", job, config)
    step.ai.last_response = LLMResponse(
        content="{}", model="ultimate", provider="qoder-cli",
        session_id="session-03-final",
    )
    _clean, _exact, semantic = extract_attestable_document_markers(
        marked, load_document_source_manifest(job), ai=step.ai,
        deduplicate_sources_by_anchor=True,
    )
    guide = next(item for item in semantic if "先看横轴" in item["anchor"])
    assert "不能推出因果关系" in guide["anchor"]
    assert guide["producer_invocation_id"] == "session-03-final"


def test_model_synthesis_marker_covers_analysis_basis_and_uncertainty():
    rendered = render_model_synthesis(_final())
    marked = inject_source_markers(
        rendered,
        {
            "p001-k001": {"source_refs": ["s002"]},
            "p001-k002": {"source_refs": ["s003"]},
        },
        {"s002": "B1", "s003": "B2"},
    )
    marker_line = next(line for line in marked.splitlines() if "[[source:B2]]" in line)
    assert "方法和结果形成闭环" in marker_line
    assert "主题综合" in marker_line
    assert "只依据冻结来源" in marker_line


def test_stage_prompt_rendering_is_single_pass():
    rendered = DocumentSmartStep._render_stage_prompt(
        "A={{A}} B={{B}}", {"A": "{{B}}", "B": "safe"},
    )
    assert rendered == "A={{B}} B=safe"


def test_layered_step_retries_invalid_stage_twice_and_publishes_nothing(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    calls = 0

    def invalid(self, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        self.last_provider = "qoder-cli"
        self.last_model = "ultimate"
        return json.dumps(_chapter(package_id="p999"), ensure_ascii=False)

    monkeypatch.setattr(AIInvocation, "call", invalid)
    with pytest.raises(ValueError, match="identity mismatch"):
        step.execute()
    assert calls == 3
    assert not list((job / "output/versions").glob("notes_smart_*"))
    assert not (job / "output/smart_pipeline/manifest.json").exists()


def test_layered_step_rejects_duplicate_anchor_across_intro_and_final(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)

    def call(self, prompt, **kwargs):
        if self.audit_name == "03-final":
            value = _final()
            value["note_markdown"] = (
                "## 方法与验证\n\nLatency is 3 ms. [证据: p001-k001]\n\n"
                + "这一节解释问题、方法、验证与边界。" * 40
            )
            return _fake_layered_call_with_value(self, prompt, value)
        if self.audit_name == "04-introduction":
            value = _introduction()
            value["introduction_markdown"] = value["introduction_markdown"].replace(
                "背景：" + (
                    "这篇论文从现实中的测量缺口出发，说明为什么需要一个可复查的研究方案。"
                    "作者随后给出实现路径，并用来源中的实验数字检查方案是否成立。"
                ) * 5,
                "Latency is 3 ms. ",
                1,
            )
            return _fake_layered_call_with_value(self, prompt, value)
        return _fake_layered_call(self, prompt, **kwargs)

    monkeypatch.setattr(AIInvocation, "call", call)
    with pytest.raises(ValueError, match="globally unique"):
        step.execute()
    assert not list((job / "output/versions").glob("notes_smart_*"))
    assert not (job / "output/provenance_exact/smart.json").exists()
    assert not (job / "output/provenance_candidates/smart.json").exists()
    assert not (job / "output/smart_pipeline/manifest.json").exists()


def test_stage_prompt_budget_rejects_before_ai(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    step = DocumentSmartStep("05_smart", job, config)
    monkeypatch.setattr(
        step.ai, "load_prompt_template",
        lambda _name: "x" * (MAX_STAGE_PROMPT_BYTES + 1),
    )
    monkeypatch.setattr(
        step.ai, "call",
        lambda *_args, **_kwargs: pytest.fail("oversized prompt must not call AI"),
    )
    with pytest.raises(InputInvalidError, match="stage prompt exceeds byte limit"):
        step._call_validated(
            step.ai, "test", {}, {"type": "object"}, lambda value: value,
        )


def test_stage_third_attempt_can_recover_from_truncated_json(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    step = DocumentSmartStep("05_smart", job, config)
    responses = iter((
        '{"ok":',
        '{"ok":',
        '{"ok":true}',
    ))
    prompts: list[str] = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(step.ai, "load_prompt_template", lambda _name: "fixed")
    monkeypatch.setattr(step.ai, "call", call)
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    assert step._call_validated(
        step.ai, "test", {}, schema, lambda value: value,
    ) == {"ok": True}
    assert len(prompts) == 3
    assert "校验反馈=" not in prompts[0]
    assert "AI stage result is not valid JSON" in prompts[1]
    assert "line 1 column 7" in prompts[1]
    assert "complete RFC 8259 JSON object" in prompts[1]
    assert "AI stage result is not valid JSON" in prompts[2]


def test_stage_invalid_escape_feedback_is_precise_and_does_not_echo_raw(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    step = DocumentSmartStep("05_smart", job, config)
    bad = '{"ok":"\\ 具体待确认项"}'
    responses = iter((bad, '{"ok":true}'))
    prompts: list[str] = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(step.ai, "load_prompt_template", lambda _name: "fixed")
    monkeypatch.setattr(step.ai, "call", call)
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    assert step._call_validated(
        step.ai, "test", {}, schema, lambda value: value,
    ) == {"ok": True}
    assert len(prompts) == 2
    assert "Invalid \\\\escape" in prompts[1]
    assert "line 1 column" in prompts[1]
    assert "literal backslashes" in prompts[1]
    assert "具体待确认项" not in prompts[1]


@pytest.mark.parametrize("raw", (
    '```json\n{"ok":true}\n```',
    '```json\n{"ok":true}',
))
def test_stage_result_rejects_markdown_fences(raw):
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_stage_result(raw, schema)


@pytest.mark.parametrize(("raw", "reason"), (
    ('{"ok":"unfinished}', "Unterminated string starting at"),
    ('{"ok":"\\ detail"}', "Invalid \\escape"),
    ('{"ok":"closed"},"knowledge":[]', "Extra data"),
))
def test_stage_result_reports_p015_json_failure_reason_and_location(raw, reason):
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "string"}},
    }
    with pytest.raises(ValueError) as caught:
        parse_stage_result(raw, schema)
    message = str(caught.value)
    assert reason in message
    assert "line 1 column" in message
    assert "complete RFC 8259 JSON object" in message


def test_parallel_stage_uses_bounded_sliding_window(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    step = DocumentSmartStep(
        "05_smart", job,
        make_step_config(
            tmp_path, step_name="05_smart", pool="ai", pipeline="document",
        ),
    )
    monkeypatch.setattr(step, "_stage_parallelism", lambda: 8)
    lock = threading.Lock()
    first_window = threading.Barrier(8)
    active = 0
    peak = 0
    started: list[str] = []

    def call(invocation, *_args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            started.append(invocation)
        if invocation in {f"p{index:03d}" for index in range(8)}:
            first_window.wait(timeout=2)
        with lock:
            active -= 1
        return {"id": invocation}

    monkeypatch.setattr(step, "_call_validated", call)
    tasks = [
        (f"p{index:03d}", f"p{index:03d}", "template", {}, [], lambda x: x)
        for index in range(24)
    ]
    results = step._parallel_validated(tasks, {})
    assert len(results) == 24
    assert len(started) == 24
    assert peak == 8


def test_parallel_stage_stops_submitting_after_first_final_failure(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    step = DocumentSmartStep(
        "05_smart", job,
        make_step_config(
            tmp_path, step_name="05_smart", pool="ai", pipeline="document",
        ),
    )
    monkeypatch.setattr(step, "_stage_parallelism", lambda: 4)
    first_window = threading.Barrier(4)
    wait_observed_failure = threading.Event()
    started: list[str] = []
    lock = threading.Lock()
    real_wait = smart_step_module.wait

    def observable_wait(*args, **kwargs):
        completed, pending = real_wait(*args, **kwargs)
        wait_observed_failure.set()
        return completed, pending

    def call(invocation, *_args):
        with lock:
            started.append(invocation)
        first_window.wait(timeout=2)
        if invocation == "p000":
            raise ValueError("p015 exhausted three attempts")
        assert wait_observed_failure.wait(timeout=2)
        return {"id": invocation}

    monkeypatch.setattr(smart_step_module, "wait", observable_wait)
    monkeypatch.setattr(step, "_call_validated", call)
    tasks = [
        (f"p{index:03d}", f"p{index:03d}", "template", {}, [], lambda x: x)
        for index in range(20)
    ]
    with pytest.raises(ValueError, match="exhausted three attempts"):
        step._parallel_validated(tasks, {})
    assert set(started) == {"p000", "p001", "p002", "p003"}


@pytest.mark.parametrize(("provider", "expected"), (
    ("qoder-cli", 8),
    ("claude-cli", 4),
    ("codex-cli", 4),
))
def test_document_smart_parallelism_follows_materialized_provider(
    tmp_path, monkeypatch, provider, expected,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["providers"] = {"providers": {
        "qoder-cli": {"document_smart_parallelism": 8},
        "claude-cli": {"document_smart_parallelism": 4},
        "codex-cli": {},
    }}
    step = DocumentSmartStep("05_smart", job, config)
    monkeypatch.setattr(
        step.ai, "selection",
        lambda: {"override": {"provider": provider}, "tiers": []},
    )
    assert step._stage_parallelism() == expected


def test_stage_retry_reports_all_unknown_and_allowed_fields(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    step = DocumentSmartStep("05_smart", job, config)
    responses = iter((
        json.dumps({
            "items": [{"claim": "x", "claim_note": ""}],
            "synthesis": {"analysis": "x", "additional_note": ""},
        }),
        json.dumps({
            "items": [{"claim": "x"}],
            "synthesis": {"analysis": "x"},
        }),
    ))
    prompts: list[str] = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(step.ai, "load_prompt_template", lambda _name: "fixed")
    monkeypatch.setattr(step.ai, "call", call)
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["items", "synthesis"],
        "properties": {
            "items": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["claim"],
                    "properties": {"claim": {"type": "string"}},
                },
            },
            "synthesis": {
                "type": "object", "additionalProperties": False,
                "required": ["analysis"],
                "properties": {"analysis": {"type": "string"}},
            },
        },
    }
    assert step._call_validated(
        step.ai, "test", {}, schema, lambda value: value,
    ) == {"items": [{"claim": "x"}], "synthesis": {"analysis": "x"}}
    assert len(prompts) == 2
    feedback = json.loads(prompts[1].split("校验反馈=", 1)[1])["validation_error"]
    assert 'result.items[0] contains unknown fields ["claim_note"]' in feedback
    assert 'allowed fields are ["claim"]' in feedback
    assert 'result.synthesis contains unknown fields ["additional_note"]' in feedback
    assert 'allowed fields are ["analysis"]' in feedback


def test_unknown_field_feedback_escapes_lone_surrogate():
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"ok": {"type": "boolean"}},
    }
    raw = json.dumps({"ok": True, "\ud800": None})
    with pytest.raises(ValueError, match="unknown fields") as caught:
        parse_stage_result(raw, schema)
    feedback = str(caught.value)
    assert "\\ud800" in feedback
    feedback.encode("utf-8")


def test_stage_retry_rechecks_prompt_budget(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    step = DocumentSmartStep("05_smart", job, config)
    monkeypatch.setattr(
        step.ai, "load_prompt_template",
        lambda _name: "x" * MAX_STAGE_PROMPT_BYTES,
    )
    calls = 0

    def invalid(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "{}"

    monkeypatch.setattr(step.ai, "call", invalid)
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    with pytest.raises(InputInvalidError, match="stage prompt exceeds byte limit"):
        step._call_validated(step.ai, "test", {}, schema, lambda value: value)
    assert calls == 1
