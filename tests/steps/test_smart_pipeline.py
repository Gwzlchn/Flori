"""分层论文智能笔记的构包、证据闭包和步骤接线测试。"""

from __future__ import annotations

import json
import hashlib
import shutil
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import steps.document.step_05_smart as smart_step_module

from shared.errors import InputInvalidError
from shared.ai_gateway import DryRunProvider
from shared.models import LLMRequest, LLMResponse
from shared.step_ai import AIInvocation
from steps.document.smart_pipeline import (
    FINAL_MARKDOWN_BEGIN,
    FINAL_MARKDOWN_END,
    MAX_IMAGE_ATTACHMENTS,
    MAX_PACKAGE_SOURCE_ALIASES,
    MAX_PACKAGES,
    MAX_STAGE_PROMPT_BYTES,
    build_chapter_packages,
    canonical_json,
    parse_stage_result,
    parse_final_stage_result,
    inject_source_markers,
    render_figures,
    render_model_synthesis,
    validate_chapter_card,
    validate_final,
    validate_introduction,
)
from steps.document.smart_checkpoint import (
    build_stage_retry_prompt,
    build_chapter_checkpoint,
    build_chapter_input_identity,
    build_stage_checkpoint,
    build_stage_input_identity,
    restore_chapter_attempts,
    restore_chapter_checkpoint,
    restore_legacy_chapter_record,
    restore_stage_attempts,
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


def _framed_final(value: dict | None = None) -> str:
    result = deepcopy(value or _final())
    markdown = result.pop("note_markdown")
    result.pop("used_knowledge_refs")
    return (
        json.dumps(result, ensure_ascii=False)
        + f"\n{FINAL_MARKDOWN_BEGIN}\n{markdown}\n{FINAL_MARKDOWN_END}"
    )


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
    if self.audit_name == "03-final":
        metadata = {
            key: item for key, item in value.items()
            if key not in {"note_markdown", "used_knowledge_refs"}
        }
        return (
            json.dumps(metadata, ensure_ascii=False)
            + f"\n{FINAL_MARKDOWN_BEGIN}\n"
            + value["note_markdown"]
            + f"\n{FINAL_MARKDOWN_END}"
        )
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


def _checkpoint_contract(tmp_path):
    job = tmp_path / "job"
    image = job / "intermediate/figures/f001.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"figure-v1")
    package = {"package_id": "p001", "sources": ["s001"]}
    schema = {"type": "object", "required": ["package_id"]}
    prompt = "frozen chapter prompt"
    template = SimpleNamespace(
        name="05_smart_document", source="image", sha256="sha256:template",
        version=7,
    )
    selection = {"tiers": [{
        "provider": "qoder-cli", "model": "ultimate",
        "reasoning_effort": "max",
    }]}
    identity = build_chapter_input_identity(
        job_dir=job, package=package, prompt=prompt, schema=schema,
        images=[image], template=template, selection=selection,
    )
    assert identity is not None
    result = {"package_id": "p001"}
    raw = json.dumps(result)
    record = {
        "phase": "final", "ok": True, "audit_stage": "01-chapter-p001",
        "job_id": job.name, "step": "05_smart",
        "exec_id": "exec:01-chapter-p001:0", "session_id": "session-p001",
        "call_index": 0,
        "prompt": {
            "rendered": {"user": prompt},
            "template": {
                "name": template.name, "source": template.source,
                "sha256": template.sha256, "version": template.version,
            },
            "images": [{
                "path": str(image), "bytes": image.stat().st_size,
                "hash": "sha256:" + hashlib.sha256(image.read_bytes()).hexdigest(),
            }],
        },
        "routing": dict(identity["routing"]),
        "output": {"content": raw},
        "output_processed": {"contract": "valid", "attempt": 1},
    }
    checkpoint = build_chapter_checkpoint(
        record=record, identity=identity, result=result, job_dir=job,
    )
    assert checkpoint is not None
    record["chapter_checkpoint"] = checkpoint
    return job, package, schema, prompt, template, selection, identity, record


def _restore_contract(job, identity, schema, record):
    return restore_chapter_checkpoint(
        record=record,
        identity=identity,
        schema=schema,
        validator=lambda value: value,
        parser=parse_stage_result,
        job_dir=job,
    )


def _retry_chain_contract(tmp_path, failures: int, *, legacy: bool = False):
    job, _package, schema, prompt, _template, _selection, identity, success = (
        _checkpoint_contract(tmp_path)
    )
    validator = lambda value: value
    records = []
    expected_prompt = prompt
    for attempt in range(failures):
        invalid = deepcopy(success)
        invalid.pop("chapter_checkpoint")
        invalid["exec_id"] = f"exec:01-chapter-p001:{attempt}"
        invalid["session_id"] = f"session-p001-{attempt}"
        invalid["prompt"]["rendered"]["user"] = expected_prompt
        invalid["output"]["content"] = "{}"
        invalid["output_processed"] = {
            "contract": "invalid", "attempt": attempt + 1,
            "error": "untrusted producer text",
        }
        records.append(invalid)
        with pytest.raises(ValueError) as caught:
            validator(parse_stage_result("{}", schema))
        expected_prompt = build_stage_retry_prompt(prompt, str(caught.value))
    success["exec_id"] = f"exec:01-chapter-p001:{failures}"
    success["session_id"] = f"session-p001-{failures}"
    success["prompt"]["rendered"]["user"] = expected_prompt
    success["output_processed"]["attempt"] = failures + 1
    success["chapter_checkpoint"] = build_chapter_checkpoint(
        record=success, identity=identity, result={"package_id": "p001"},
        job_dir=job,
    )
    assert success["chapter_checkpoint"] is not None
    if legacy:
        success.pop("chapter_checkpoint")
    return job, schema, prompt, identity, [*records, success]


def test_chapter_checkpoint_revalidates_exact_frozen_identity(tmp_path):
    (
        job, package, schema, prompt, template, selection, identity, record,
    ) = _checkpoint_contract(tmp_path)
    assert _restore_contract(job, identity, schema, record) == {"package_id": "p001"}

    prompt_drift = build_chapter_input_identity(
        job_dir=job, package=package, prompt=prompt + " changed", schema=schema,
        images=[job / "intermediate/figures/f001.png"], template=template,
        selection=selection,
    )
    schema_drift = build_chapter_input_identity(
        job_dir=job, package=package, prompt=prompt,
        schema={**schema, "additionalProperties": False},
        images=[job / "intermediate/figures/f001.png"], template=template,
        selection=selection,
    )
    provider_drift = build_chapter_input_identity(
        job_dir=job, package=package, prompt=prompt, schema=schema,
        images=[job / "intermediate/figures/f001.png"], template=template,
        selection={"tiers": [{
            "provider": "claude-cli", "model": "opus5",
            "reasoning_effort": "xhigh",
        }]},
    )
    model_drift = build_chapter_input_identity(
        job_dir=job, package=package, prompt=prompt, schema=schema,
        images=[job / "intermediate/figures/f001.png"], template=template,
        selection={"tiers": [{
            "provider": "qoder-cli", "model": "cantus",
            "reasoning_effort": "max",
        }]},
    )
    effort_drift = build_chapter_input_identity(
        job_dir=job, package=package, prompt=prompt, schema=schema,
        images=[job / "intermediate/figures/f001.png"], template=template,
        selection={"tiers": [{
            "provider": "qoder-cli", "model": "ultimate",
            "reasoning_effort": "high",
        }]},
    )
    package_drift = build_chapter_input_identity(
        job_dir=job, package={**package, "sources": ["s002"]}, prompt=prompt,
        schema=schema, images=[job / "intermediate/figures/f001.png"],
        template=template, selection=selection,
    )
    assert prompt_drift is not None
    assert schema_drift is not None
    assert provider_drift is not None
    assert model_drift is not None
    assert effort_drift is not None
    assert package_drift is not None
    assert _restore_contract(job, prompt_drift, schema, record) is None
    assert _restore_contract(job, schema_drift, schema, record) is None
    assert _restore_contract(job, provider_drift, schema, record) is None
    assert _restore_contract(job, model_drift, schema, record) is None
    assert _restore_contract(job, effort_drift, schema, record) is None
    assert _restore_contract(job, package_drift, schema, record) is None

    image = job / "intermediate/figures/f001.png"
    image.write_bytes(b"figure-v2")
    image_drift = build_chapter_input_identity(
        job_dir=job, package=package, prompt=prompt, schema=schema,
        images=[image], template=template, selection=selection,
    )
    assert image_drift is not None
    assert _restore_contract(job, image_drift, schema, record) is None


def test_chapter_checkpoint_rejects_corruption_and_failed_audit(tmp_path):
    job, _package, schema, _prompt, _template, _selection, identity, record = (
        _checkpoint_contract(tmp_path)
    )
    corrupted = deepcopy(record)
    corrupted["output"]["content"] = '{"package_id":'
    assert _restore_contract(job, identity, schema, corrupted) is None

    failed = deepcopy(record)
    failed.pop("chapter_checkpoint")
    failed["ok"] = False
    failed["output_processed"] = {"contract": "invalid"}
    assert build_chapter_checkpoint(
        record=failed, identity=identity, result={"package_id": "p001"},
        job_dir=job,
    ) is None
    assert "chapter_checkpoint" not in failed


def test_complete_legacy_chapter_record_is_revalidated_without_metadata(tmp_path):
    job, _package, schema, _prompt, _template, _selection, identity, record = (
        _checkpoint_contract(tmp_path)
    )
    record.pop("chapter_checkpoint")
    record["prompt"]["images"][0]["path"] = (
        f"/tmp/old-worker/{job.name}/intermediate/figures/f001.png"
    )
    assert restore_legacy_chapter_record(
        record=record, identity=identity, schema=schema,
        validator=lambda value: value, parser=parse_stage_result, job_dir=job,
    ) == {"package_id": "p001"}
    record["prompt"]["images"][0].pop("hash")
    assert restore_legacy_chapter_record(
        record=record, identity=identity, schema=schema,
        validator=lambda value: value, parser=parse_stage_result, job_dir=job,
    ) is None


def test_stage_checkpoint_restores_result_and_original_session(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    prompt = "frozen theme prompt"
    template = SimpleNamespace(
        name="05_smart_document.theme", source="image",
        sha256="sha256:template", version=1,
    )
    selection = {"tiers": [{
        "provider": "qoder-cli", "model": "ultimate",
        "reasoning_effort": "max",
    }]}
    identity = build_stage_input_identity(
        job_dir=job, stage="02-theme-t01", prompt=prompt, schema=schema,
        images=[], template=template, selection=selection,
    )
    assert identity is not None
    raw = '{"ok":true}'
    record = {
        "phase": "final", "ok": True, "job_id": job.name,
        "step": "05_smart", "audit_stage": "02-theme-t01",
        "exec_id": "exec:02-theme-t01:0", "session_id": "real-session-t01",
        "prompt": {
            "rendered": {"user": prompt}, "images": [],
            "template": {
                "name": template.name, "source": template.source,
                "sha256": template.sha256, "version": template.version,
            },
        },
        "routing": dict(identity["routing"]),
        "output": {"content": raw},
        "output_processed": {"contract": "valid", "attempt": 1},
    }
    record["stage_checkpoint"] = build_stage_checkpoint(
        record=record, identity=identity, result={"ok": True}, job_dir=job,
    )
    assert record["stage_checkpoint"] is not None
    assert restore_stage_attempts(
        records=[record], identity=identity, base_prompt=prompt, schema=schema,
        validator=lambda value: value, parser=parse_stage_result, job_dir=job,
    ) == ({"ok": True}, "real-session-t01")
    record["session_id"] = "tampered-session"
    assert restore_stage_attempts(
        records=[record], identity=identity, base_prompt=prompt, schema=schema,
        validator=lambda value: value, parser=parse_stage_result, job_dir=job,
    ) is None


def test_stage_restore_rejects_legacy_record_without_checkpoint(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    prompt = "frozen theme prompt"
    template = SimpleNamespace(
        name="05_smart_document.theme", source="image",
        sha256="sha256:template", version=1,
    )
    identity = build_stage_input_identity(
        job_dir=job, stage="02-theme-t01", prompt=prompt, schema=schema,
        images=[], template=template,
        selection={"tiers": [{
            "provider": "qoder-cli", "model": "ultimate",
            "reasoning_effort": "max",
        }]},
    )
    assert identity is not None
    record = {
        "phase": "final", "ok": True, "job_id": job.name,
        "step": "05_smart", "audit_stage": "02-theme-t01",
        "exec_id": "exec:02-theme-t01:0", "session_id": "legacy-session",
        "prompt": {
            "rendered": {"user": prompt}, "images": [],
            "template": {
                "name": template.name, "source": template.source,
                "sha256": template.sha256, "version": template.version,
            },
        },
        "routing": dict(identity["routing"]),
        "output": {"content": '{"ok":true}'},
        "output_processed": {"contract": "valid", "attempt": 1},
    }
    assert restore_stage_attempts(
        records=[record], identity=identity, base_prompt=prompt, schema=schema,
        validator=lambda value: value, parser=parse_stage_result, job_dir=job,
    ) is None


def test_stage_checkpoint_skips_ai_across_new_step_instance(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["ai"] = {"primary": {"provider": "qoder-cli", "model": "ultimate"}}
    config["providers"] = {"providers": {"qoder-cli": {
        "model": "ultimate", "reasoning_effort": "max", "features": [],
    }}}
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }

    def audited_call(self, prompt, images=None, **kwargs):
        raw = '{"ok":true}'
        response = LLMResponse(
            content=raw, model="ultimate", provider="qoder-cli",
            session_id="original-theme-session", tier_used="primary",
            reasoning_effort="max", reasoning_effort_source="provider_default",
        )
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}], images=images or [],
            response_format=kwargs.get("response_format"), temperature=0,
            max_tokens=32768,
        )
        now = datetime.now()
        record = self._build_log_record(
            prompt, None, images or [], request, response, now, now, None,
        )
        record["phase"] = "final"
        self.ai_log_records.append(record)
        self.last_provider = response.provider
        self.last_model = response.model
        self.last_response = response
        self.call_index += 1
        self._flush_logs()
        return raw

    monkeypatch.setattr(AIInvocation, "call", audited_call)
    monkeypatch.setenv("STEP_EXEC_ID", "deferred-run-1")
    first = DocumentSmartStep("05_smart", job, config)
    invocation = first.ai.fork("02-theme-t01")
    assert first._call_stage_validated(
        invocation, "05_smart_document.theme",
        {
            "OUTPUT_SCHEMA": canonical_json(schema), "THEME": "{}",
            "PAPER_MAP": "{}", "EXPECTED_KNOWLEDGE_REFS": "[]",
            "FIGURE_CATALOG": "{}", "CHAPTER_CARDS": "[]",
        },
        schema, lambda value: value,
    ) == {"ok": True}
    assert "stage_checkpoint" in invocation.ai_log_records[-1]

    second = DocumentSmartStep("05_smart", job, deepcopy(config))
    restored_invocation = second.ai.fork("02-theme-t01")
    monkeypatch.setattr(
        AIInvocation, "call",
        lambda *_args, **_kwargs: pytest.fail("verified stage must not call AI"),
    )
    assert second._call_stage_validated(
        restored_invocation, "05_smart_document.theme",
        {
            "OUTPUT_SCHEMA": canonical_json(schema), "THEME": "{}",
            "PAPER_MAP": "{}", "EXPECTED_KNOWLEDGE_REFS": "[]",
            "FIGURE_CATALOG": "{}", "CHAPTER_CARDS": "[]",
        },
        schema, lambda value: value,
    ) == {"ok": True}
    assert restored_invocation._restored_session_id == "original-theme-session"


def test_deferred_stage_checkpoint_is_not_restored_until_evidence_gate_persists_it(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["ai"] = {"primary": {"provider": "qoder-cli", "model": "ultimate"}}
    config["providers"] = {"providers": {"qoder-cli": {
        "model": "ultimate", "reasoning_effort": "max", "features": [],
    }}}
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    calls = 0

    def audited_call(self, prompt, images=None, **kwargs):
        nonlocal calls
        calls += 1
        raw = '{"ok":true}'
        response = LLMResponse(
            content=raw, model="ultimate", provider="qoder-cli",
            session_id=f"session-{calls}", tier_used="primary",
            reasoning_effort="max", reasoning_effort_source="provider_default",
        )
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}], images=images or [],
            response_format=kwargs.get("response_format"), temperature=0,
            max_tokens=32768,
        )
        now = datetime.now()
        record = self._build_log_record(
            prompt, None, images or [], request, response, now, now, None,
        )
        record["phase"] = "final"
        self.ai_log_records.append(record)
        self.last_provider = response.provider
        self.last_model = response.model
        self.last_response = response
        self.call_index += 1
        self._flush_logs()
        return raw

    monkeypatch.setattr(AIInvocation, "call", audited_call)
    first = DocumentSmartStep("05_smart", job, config)
    invocation = first.ai.fork("04-introduction")
    assert first._call_stage_validated(
        invocation, "05_smart_document.introduction",
        {
            "OUTPUT_SCHEMA": canonical_json(schema), "ABSTRACT": "{}",
            "INTRODUCTION_CATALOG": "[]", "VALID_REFS": "[]",
        },
        schema, lambda value: value, defer_checkpoint=True,
    ) == {"ok": True}
    assert "stage_checkpoint" not in invocation.ai_log_records[-1]
    first.ai.merge_forks([invocation])

    monkeypatch.setenv("STEP_EXEC_ID", "deferred-run-2")
    second = DocumentSmartStep("05_smart", job, deepcopy(config))
    second_invocation = second.ai.fork("04-introduction")
    assert second._call_stage_validated(
        second_invocation, "05_smart_document.introduction",
        {
            "OUTPUT_SCHEMA": canonical_json(schema), "ABSTRACT": "{}",
            "INTRODUCTION_CATALOG": "[]", "VALID_REFS": "[]",
        },
        schema, lambda value: value, defer_checkpoint=True,
    ) == {"ok": True}
    assert calls == 2
    assert second._persist_pending_stage_checkpoint(second_invocation)
    second.ai.merge_forks([second_invocation])

    monkeypatch.setenv("STEP_EXEC_ID", "deferred-run-3")
    third = DocumentSmartStep("05_smart", job, deepcopy(config))
    third_invocation = third.ai.fork("04-introduction")
    assert third._call_stage_validated(
        third_invocation, "05_smart_document.introduction",
        {
            "OUTPUT_SCHEMA": canonical_json(schema), "ABSTRACT": "{}",
            "INTRODUCTION_CATALOG": "[]", "VALID_REFS": "[]",
        },
        schema, lambda value: value, defer_checkpoint=True,
    ) == {"ok": True}
    assert calls == 2
    assert third_invocation._restored_session_id == "session-2"


def test_framed_stage_retry_uses_framed_feedback(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    step = DocumentSmartStep(
        "05_smart", job,
        make_step_config(
            tmp_path, step_name="05_smart", pool="ai", pipeline="document",
        ),
    )
    prompts = []
    responses = iter(("broken", _framed_final()))

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(step.ai, "load_prompt_template", lambda _name: "fixed")
    monkeypatch.setattr(step.ai, "call", call)
    full_schema = step._schema("final")
    metadata_schema = step._schema("final_metadata")
    bundle = {"metadata": metadata_schema, "full": full_schema}
    result = step._call_validated(
        step.ai, "final", {}, bundle, lambda value: value,
        parser=lambda raw, schemas: parse_final_stage_result(
            raw, schemas["metadata"], schemas["full"],
        ),
        response_format="text",
    )
    assert result["note_markdown"]
    assert len(prompts) == 2
    assert "重新生成完整的 metadata JSON" in prompts[1]
    assert "重新生成完整 JSON" not in prompts[1]


@pytest.mark.parametrize(("failures", "legacy"), (
    (1, False), (2, False), (1, True), (2, True),
))
def test_retry_checkpoint_rebuilds_each_feedback_prompt(
    tmp_path, failures, legacy,
):
    job, schema, prompt, identity, records = _retry_chain_contract(
        tmp_path, failures, legacy=legacy,
    )
    assert restore_chapter_attempts(
        records=records, identity=identity, base_prompt=prompt, schema=schema,
        validator=lambda value: value, parser=parse_stage_result, job_dir=job,
    ) == {"package_id": "p001"}


@pytest.mark.parametrize(
    "mutation", ("missing", "tampered", "reordered", "wrong_feedback"),
)
def test_retry_checkpoint_rejects_broken_attempt_history(tmp_path, mutation):
    job, schema, prompt, identity, records = _retry_chain_contract(tmp_path, 2)
    if mutation == "missing":
        records = records[1:]
    elif mutation == "tampered":
        records[0]["output"]["content"] = '{"package_id":"p001"}'
    elif mutation == "reordered":
        records = [records[1], records[0], records[2]]
    else:
        records[1]["prompt"]["rendered"]["user"] += "tampered"
    assert restore_chapter_attempts(
        records=records, identity=identity, base_prompt=prompt, schema=schema,
        validator=lambda value: value, parser=parse_stage_result, job_dir=job,
    ) is None


def test_checkpoint_flush_failure_removes_in_memory_authority(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    step = DocumentSmartStep("05_smart", job, config)
    invocation = step.ai.fork("01-chapter-p001")
    invocation.ai_log_records = [{"exec_id": "exec:p001"}]
    monkeypatch.setattr(
        invocation, "_flush_logs",
        lambda: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert step._persist_chapter_checkpoint(
        invocation, {"digest": "sha256:checkpoint"},
    ) is False
    assert "chapter_checkpoint" not in invocation.ai_log_records[-1]


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
        ("03-final", "session-03-final"),
        ("04-introduction", "session-04-introduction"),
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


@pytest.mark.parametrize(("legacy", "fragment", "chapter_failures"), (
    (False, False, 1), (True, False, 2),
    (False, True, 2), (True, True, 1),
))
def test_later_failure_reuses_verified_chapters_without_new_ai_calls(
    tmp_path, monkeypatch, legacy, fragment, chapter_failures,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    config["ai"] = {
        "primary": {"provider": "qoder-cli", "model": "ultimate"},
    }
    config["providers"] = {"providers": {
        "qoder-cli": {
            "model": "ultimate", "reasoning_effort": "max", "features": [],
        },
    }}
    run = 1
    calls: list[tuple[int, str | None]] = []
    chapter_attempts = 0

    def audited_call(self, prompt, images=None, **kwargs):
        nonlocal chapter_attempts
        calls.append((run, self.audit_name))
        if self.audit_name and "chapter" in self.audit_name:
            chapter_attempts += 1
            value = (
                _chapter(package_id="p999")
                if run == 1 and chapter_attempts <= chapter_failures
                else _chapter()
            )
        elif self.audit_name and "theme" in self.audit_name:
            value = {"invalid": True} if run == 1 else _theme()
        elif self.audit_name == "03-final":
            value = _final()
        elif self.audit_name == "04-introduction":
            value = _introduction()
        else:
            raise AssertionError(f"unexpected audit stage: {self.audit_name}")
        raw = _fake_layered_call_with_value(self, prompt, value)
        # helper 已追加审计记录；本测试需要走真实 record builder。
        self.ai_log_records.pop()
        self.call_index -= 1
        response = LLMResponse(
            content=raw, model="ultimate", provider="qoder-cli",
            session_id=f"session-{run}-{self.audit_name}-{self.call_index}",
            tier_used="primary", reasoning_effort="max",
            reasoning_effort_source="provider_default",
        )
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            images=images or [], response_format=kwargs.get("response_format"),
            temperature=kwargs.get("temperature", 0),
            max_tokens=kwargs.get("max_tokens", 32768),
        )
        now = datetime.now()
        record = self._build_log_record(
            prompt, None, images or [], request, response, now, now, None,
        )
        record["phase"] = "final"
        self.ai_log_records.append(record)
        self.last_provider = response.provider
        self.last_model = response.model
        self.last_response = response
        self.call_index += 1
        self._flush_logs()
        return raw

    monkeypatch.setattr(AIInvocation, "call", audited_call)
    monkeypatch.setenv("STEP_EXEC_ID", "exec-run-1")
    first = DocumentSmartStep("05_smart", job, config)
    with pytest.raises(ValueError):
        first.execute()
    first_audit = [
        json.loads(line)
        for line in (job / "output/ai_logs/05_smart.jsonl").read_text().splitlines()
    ]
    chapter_records = [
        item for item in first_audit if item["audit_stage"] == "01-chapter-p001"
    ]
    chapter_record = next(
        item for item in chapter_records if "chapter_checkpoint" in item
    )
    assert chapter_record["chapter_checkpoint"]["format"] == (
        "flori-document-chapter-checkpoint"
    )
    if legacy:
        chapter_record.pop("chapter_checkpoint")
        (job / "output/ai_logs/05_smart.jsonl").write_text(
            "".join(
                json.dumps(item, ensure_ascii=False) + "\n"
                for item in first_audit
            ),
            encoding="utf-8",
        )
    assert not (job / "output/smart_pipeline/manifest.json").exists()
    assert chapter_attempts == chapter_failures + 1

    run = 2
    monkeypatch.setenv("STEP_EXEC_ID", "exec-run-2")
    pulled = tmp_path / "new-worker" / job.name
    for relative in (
        "input", "intermediate", "assets", "output/provenance",
        "output/provenance_candidates",
    ):
        source = job / relative
        if source.exists():
            shutil.copytree(source, pulled / relative, dirs_exist_ok=True)
    pulled_log = pulled / "output/ai_logs/05_smart.jsonl"
    pulled_log.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(job / "output/ai_logs/05_smart.jsonl", pulled_log)
    if fragment:
        records = [
            json.loads(line) for line in pulled_log.read_text().splitlines()
        ]
        chapters = [
            item for item in records if item["audit_stage"] == "01-chapter-p001"
        ]
        pulled_log.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False) + "\n"
                for item in records if item not in chapters
            ),
            encoding="utf-8",
        )
        (pulled_log.parent / "05_smart.01-chapter-p001.jsonl").write_text(
            "".join(
                json.dumps(item, ensure_ascii=False) + "\n" for item in chapters
            ),
            encoding="utf-8",
        )
    stale = pulled / "output/smart_pipeline/chapter-card-p999.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"stale":true}', encoding="utf-8")
    second_config = deepcopy(config)
    second_config["paths"]["data_dir"] = str(pulled.parent)
    second = DocumentSmartStep("05_smart", pulled, second_config)
    result = second.execute()
    assert result["chapter_packages"] == 1
    assert not any(
        attempt == 2 and stage == "01-chapter-p001"
        for attempt, stage in calls
    )
    manifest = json.loads(
        (pulled / "output/smart_pipeline/manifest.json").read_text()
    )
    assert not stale.exists()
    assert all("p999" not in item["path"] for item in manifest["artifacts"])
    assert all("checkpoint" not in item["path"] for item in manifest["artifacts"])
    assert not (pulled_log.parent / "05_smart.01-chapter-p001.jsonl").exists()


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


def test_framed_final_keeps_raw_markdown_and_derives_used_refs():
    full_schema = json.loads((
        Path(__file__).parents[2]
        / "configs/prompts/schemas/05_smart_document.final.json"
    ).read_text())
    metadata_schema = json.loads((
        Path(__file__).parents[2]
        / "configs/prompts/schemas/05_smart_document.final_metadata.json"
    ).read_text())
    raw = _framed_final().replace("一个需要测量的问题", "一个包含 `x\\y` 和 \"引号\" 的问题")
    parsed = parse_final_stage_result(raw, metadata_schema, full_schema)
    assert "`x\\y`" in parsed["note_markdown"]
    assert parsed["used_knowledge_refs"] == ["p001-k001", "p001-k002"]


@pytest.mark.parametrize("mutate", (
    lambda raw: raw.replace(FINAL_MARKDOWN_END, ""),
    lambda raw: raw + f"\n{FINAL_MARKDOWN_END}",
    lambda raw: "preamble\n" + raw,
    lambda raw: raw + "\ntrailing",
))
def test_framed_final_rejects_truncation_duplicates_and_extra_content(mutate):
    full_schema = json.loads((
        Path(__file__).parents[2]
        / "configs/prompts/schemas/05_smart_document.final.json"
    ).read_text())
    metadata_schema = json.loads((
        Path(__file__).parents[2]
        / "configs/prompts/schemas/05_smart_document.final_metadata.json"
    ).read_text())
    with pytest.raises(ValueError):
        parse_final_stage_result(mutate(_framed_final()), metadata_schema, full_schema)


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
        ("markdown", "\n\n[[source:S1.P1]]", "source markers"),
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


def test_introduction_contract_rejects_model_source_marker():
    result = _introduction()
    result["introduction_markdown"] += "\n\n[[source:S1.P1]]"
    with pytest.raises(ValueError, match="source markers"):
        validate_introduction(result, _knowledge_catalog("p001-k001"))


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
