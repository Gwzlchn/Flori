"""steps/video/step_12_review.py 的测试。"""

import copy
import json
import hashlib

import pytest

from shared.models import LLMResponse
from shared.review_contract import verify_persisted_review
from steps.video.step_12_review import ReviewStep
from tests.steps.conftest import make_step_config


class TestReviewStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["output", "logs"]:
            (job_dir / d).mkdir()
        (job_dir / "output" / "notes_mechanical.md").write_text("## 机械版\n\n内容\n")
        # 智能笔记已版本化:评审读 output/versions/notes_smart_*.md 的最新一版。
        (job_dir / "output" / "versions").mkdir()
        (job_dir / "output" / "versions" / "notes_smart_claude-cli_claude-opus-4-8_20260101-000000.md").write_text("## 智能版\n\n重组后内容\n")
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "output").mkdir()
        config = make_step_config(tmp_path, step_name="12_review")
        step = ReviewStep("12_review", job_dir, config)
        missing = step.validate_inputs()
        assert "output/versions/notes_smart_*.md" in missing

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="12_review", pool="ai")
        step = ReviewStep("12_review", job_dir, config)
        result = step.execute()
        assert (job_dir / "output" / "review.json").exists()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert "overall" in review

    def test_parse_fallback(self, tmp_path, monkeypatch):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="12_review", pool="ai")
        step = ReviewStep("12_review", job_dir, config)
        monkeypatch.setattr(step.ai, "call", lambda *a, **k: "not json at all")
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert review["overall"] is None
        assert "raw_response" in review
        assert review["review_reliable"] is False
        assert review["parse"]["mode"] == "fallback"
        assert result["parse_failed"] is True

    def test_citation_without_manifest_is_rejected(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        smart = next((job_dir / "output/versions").glob("notes_smart_*.md"))
        smart.write_text("罚款 123 万元 [E1]。", encoding="utf-8")
        step = ReviewStep(
            "12_review", job_dir, make_step_config(tmp_path, step_name="12_review", pool="ai"),
        )
        *_prefix, citation, manifest_record = step._evidence_for_review()
        assert citation["status"] == "invalid"
        assert citation["items"][0]["errors"] == ["unknown_or_ineligible_evidence"]
        assert manifest_record is None

    def test_execute_loads_smart_mechanical_and_manifest_once(self, tmp_path, monkeypatch):
        from shared import review_contract

        job = self._setup_job(tmp_path)
        manifest_path = job / "output/evidence.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 2,
            "job_id": job.name,
            "ocr_refs": [],
            "evidence": [],
            "rejected": [],
            "total_bytes": 0,
            "candidate_parse_failed": False,
            "provider": "claude-cli",
        }), encoding="utf-8")
        keys = [
            "completeness", "accuracy", "structure", "terminology",
            "visual_integration", "readability",
        ]
        raw = json.dumps({
            **{key: 5 for key in keys},
            "key_terms": [], "missing_concepts": [],
            "top3_improvements": ["a", "b", "c"], "issues": [],
        })
        calls: list[str] = []
        real_read = review_contract.read_path_bounded

        def counted(path, *args, **kwargs):
            calls.append(str(path))
            return real_read(path, *args, **kwargs)

        monkeypatch.setattr(review_contract, "read_path_bounded", counted)
        step = ReviewStep(
            "12_review", job, make_step_config(tmp_path, step_name="12_review", pool="ai"),
        )
        step.ai.last_response = LLMResponse(
            content=raw, model="m", provider="openai", finish_reason="stop",
            tier_used="primary", attempts=[{
                "tier": "primary", "provider": "openai", "model": "m", "ok": True,
            }],
        )
        step.ai.call = lambda *_args, **_kwargs: raw
        step.execute()

        smart = next((job / "output/versions").glob("notes_smart_*.md"))
        assert calls.count(str(smart)) == 1
        assert calls.count(str(job / "output/notes_mechanical.md")) == 1
        assert calls.count(str(manifest_path)) == 1

    @pytest.mark.asyncio
    async def test_read_time_revalidates_current_evidence_and_citation(self, tmp_path):
        job = self._setup_job(tmp_path)
        ref = "〔2018〕88号"
        mechanical = job / "output/notes_mechanical.md"
        mechanical.write_text(f"案例 {ref}\n", encoding="utf-8")
        smart = next((job / "output/versions").glob("notes_smart_*.md"))
        smart.write_text("罚款 5 万元 [E1]。", encoding="utf-8")
        artifact = job / "output/evidence/evidence-01.md"
        artifact.parent.mkdir()
        artifact.write_text(f"处罚决定 {ref}，罚款 5 万元。\n", encoding="utf-8")
        body = artifact.read_bytes()
        text = body.decode()
        manifest = {
            "schema_version": 2, "job_id": job.name, "ocr_refs": [ref],
            "evidence": [{
                "id": "E1", "job_id": job.name, "title": "处罚决定",
                "artifact": "output/evidence/evidence-01.md",
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                "bytes": len(body), "chars": len(text),
                "source_tier": "一手官方", "confidence": "high", "eligible": True,
                "eligibility_reasons": [],
                "matches": [{"anchor": ref, "offset": text.find(ref)}],
                "original_url": "https://www.csrc.gov.cn/case",
                "final_url": "https://www.csrc.gov.cn/case",
            }],
            "rejected": [], "total_bytes": len(body),
            "candidate_parse_failed": False, "provider": "claude-cli",
        }
        (job / "output/evidence.json").write_text(json.dumps(manifest, ensure_ascii=False))
        keys = ["completeness", "accuracy", "structure", "terminology", "visual_integration", "readability"]
        raw = json.dumps({
            **{key: 5 for key in keys},
            "key_terms": [{"term": "处罚", "definition": "行政罚款"}],
            "missing_concepts": [], "top3_improvements": ["a", "b", "c"], "issues": [],
        }, ensure_ascii=False)
        step = ReviewStep(
            "12_review", job, make_step_config(tmp_path, step_name="12_review", pool="ai"),
        )
        step.ai.last_response = LLMResponse(
            content=raw, model="m", provider="openai", finish_reason="stop",
            tier_used="primary", attempts=[{
                "tier": "primary", "provider": "openai", "model": "m", "ok": True,
            }],
        )
        step.ai.last_provider = "openai"
        step.ai.last_model = "m"
        step.ai.call = lambda *_a, **_k: raw
        step.execute()
        review = json.loads((job / "output/review.json").read_text())

        async def reader(rel):
            path = job / rel
            return path.read_bytes() if path.exists() else None

        assert (await verify_persisted_review(
            review, job_id=job.name, pipeline="video", read_file=reader,
        ))["review_reliable"] is True
        copied_rel = "output/evidence/copied.md"
        (job / copied_rel).write_bytes(body)
        for field, value in (
            ("artifact", copied_rel),
            ("sha256", "sha256:" + "0" * 64),
            ("bytes", len(body) + 1),
            ("chars", len(text) + 1),
        ):
            forged = copy.deepcopy(review)
            evidence_source = next(
                source for source in forged["review_input"]["sources"]
                if source["label"] == "E1"
            )
            evidence_source[field] = value
            forged_result = await verify_persisted_review(
                forged, job_id=job.name, pipeline="video", read_file=reader,
            )
            assert forged_result["review_reliable"] is False
            assert "evidence_source_record_mismatch:E1" in forged_result["reliability_reasons"]
        manifest["evidence"][0]["confidence"] = "low"
        (job / "output/evidence.json").write_text(json.dumps(manifest, ensure_ascii=False))
        verified = await verify_persisted_review(
            review, job_id=job.name, pipeline="video", read_file=reader,
        )
        assert verified["review_reliable"] is False
        assert "evidence_manifest_trust_mismatch" in verified["reliability_reasons"]

    @pytest.mark.asyncio
    async def test_video_manifest_errors_downgrade_review_without_citations(self, tmp_path):
        job = self._setup_job(tmp_path)
        ref = "〔2018〕88号"
        (job / "output/notes_mechanical.md").write_text(f"案例 {ref}\n", encoding="utf-8")
        smart = next((job / "output/versions").glob("notes_smart_*.md"))
        smart.write_text("智能版没有证据引用。", encoding="utf-8")
        artifact = job / "output/evidence/evidence-01.md"
        artifact.parent.mkdir()
        artifact.write_text(f"处罚决定 {ref}，罚款 5 万元。\n", encoding="utf-8")
        body = artifact.read_bytes()
        text = body.decode()
        manifest = {
            "schema_version": 2, "job_id": job.name, "ocr_refs": [ref],
            "evidence": [{
                "id": "E1", "job_id": job.name, "title": "处罚决定",
                "artifact": "output/evidence/evidence-01.md",
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                "bytes": len(body), "chars": len(text),
                "source_tier": "一手官方", "confidence": "high", "eligible": True,
                "eligibility_reasons": [],
                "matches": [{"anchor": ref, "offset": text.find(ref)}],
                "original_url": "https://www.csrc.gov.cn/case",
                "final_url": "https://www.csrc.gov.cn/case",
            }],
            "rejected": [], "total_bytes": len(body),
            "candidate_parse_failed": False, "provider": "claude-cli",
        }
        low_artifact = job / "output/evidence/evidence-02.md"
        low_artifact.write_text(f"外部转载 {ref}，罚款 5 万元。\n", encoding="utf-8")
        low_body = low_artifact.read_bytes()
        low_text = low_body.decode()
        manifest["evidence"].append({
            "id": "E2", "job_id": job.name, "title": "外部转载",
            "artifact": "output/evidence/evidence-02.md",
            "sha256": "sha256:" + hashlib.sha256(low_body).hexdigest(),
            "bytes": len(low_body), "chars": len(low_text),
            "source_tier": "外部来源", "confidence": "low", "eligible": False,
            "eligibility_reasons": ["source_not_authoritative"],
            "matches": [{"anchor": ref, "offset": low_text.find(ref)}],
            "original_url": "https://example.com/case",
            "final_url": "https://example.com/case",
        })
        manifest["total_bytes"] += len(low_body)
        manifest_path = job / "output/evidence.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
        keys = [
            "completeness", "accuracy", "structure", "terminology",
            "visual_integration", "readability",
        ]
        raw = json.dumps({
            **{key: 5 for key in keys},
            "key_terms": [], "missing_concepts": [],
            "top3_improvements": ["a", "b", "c"], "issues": [],
        }, ensure_ascii=False)
        step = ReviewStep(
            "12_review", job, make_step_config(tmp_path, step_name="12_review", pool="ai"),
        )
        step.ai.last_response = LLMResponse(
            content=raw, model="m", provider="openai", finish_reason="stop",
            tier_used="primary", attempts=[{
                "tier": "primary", "provider": "openai", "model": "m", "ok": True,
            }],
        )
        step.ai.last_provider = "openai"
        step.ai.last_model = "m"
        step.ai.call = lambda *_a, **_k: raw
        step.execute()
        review = json.loads((job / "output/review.json").read_text())
        assert review["citation_validation"]["status"] == "not_applicable"
        assert review["citation_validation"]["manifest_errors"] == ["ineligible_evidence:E2"]

        async def reader(rel):
            path = job / rel
            return path.read_bytes() if path.exists() else None

        initially_verified = await verify_persisted_review(
            review, job_id=job.name, pipeline="video", read_file=reader,
        )
        assert initially_verified["review_reliable"] is True

        manifest["ocr_refs"] = ["〔2020〕1号"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))

        verified = await verify_persisted_review(
            review, job_id=job.name, pipeline="video", read_file=reader,
        )
        assert verified["review_reliable"] is False
        assert "manifest_ocr_refs_mismatch" in verified["reliability_reasons"]

        for malformed in (None, [], False, 0, "manifest"):
            manifest_path.write_text(json.dumps(malformed), encoding="utf-8")
            malformed_result = await verify_persisted_review(
                review, job_id=job.name, pipeline="video", read_file=reader,
            )
            assert malformed_result["review_reliable"] is False
            assert "evidence_manifest_invalid" in malformed_result["reliability_reasons"]
            assert "legacy_or_invalid_schema" in malformed_result["reliability_reasons"]

    @pytest.mark.parametrize("payload", [None, [], False, 0, "manifest", {}])
    def test_present_non_object_or_invalid_manifest_downgrades_review_without_citations(
        self, tmp_path, payload,
    ):
        job = self._setup_job(tmp_path)
        (job / "output/evidence.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
        keys = [
            "completeness", "accuracy", "structure", "terminology",
            "visual_integration", "readability",
        ]
        raw = json.dumps({
            **{key: 5 for key in keys},
            "key_terms": [], "missing_concepts": [],
            "top3_improvements": ["a", "b", "c"], "issues": [],
        }, ensure_ascii=False)
        step = ReviewStep(
            "12_review", job, make_step_config(tmp_path, step_name="12_review", pool="ai"),
        )
        step.ai.last_response = LLMResponse(
            content=raw, model="m", provider="openai", finish_reason="stop",
            tier_used="primary", attempts=[{
                "tier": "primary", "provider": "openai", "model": "m", "ok": True,
            }],
        )
        step.ai.last_provider = "openai"
        step.ai.last_model = "m"
        step.ai.call = lambda *_a, **_k: raw

        step.execute()

        review = json.loads((job / "output/review.json").read_text())
        assert review["review_reliable"] is False
        assert review["citation_validation"] == {
            "status": "invalid", "checked": 0, "items": [],
            "manifest_errors": ["legacy_or_invalid_schema"],
        }
