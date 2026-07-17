"""api/routes/notes.py 测试。"""

from __future__ import annotations

import copy
import json

import pytest

from api.routes.notes import _read_verification_artifact
from shared.evidence_contract import MAX_EVIDENCE_BYTES, MAX_MECHANICAL_EVIDENCE_BYTES
from shared.models import Job, LLMResponse
from shared.review_contract import MAX_REVIEW_SOURCE_BYTES, parse_review, source_record


def _create_job_files(jobs_dir, job_id):
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "output").mkdir()
    (job_dir / "assets").mkdir()
    (job_dir / "input").mkdir()
    # 智能笔记已版本化:/notes/smart 默认取最新版本(output/versions/notes_smart_*.md)。
    (job_dir / "output" / "versions").mkdir()
    smart_ver = "output/versions/notes_smart_claude-cli_claude-opus-4-8_20260101-000000.md"
    (job_dir / smart_ver).write_text("# Smart Notes\n")
    (job_dir / "output" / "notes_mechanical.md").write_text("# Mechanical\n")
    (job_dir / "output" / "transcript.md").write_text("[00:00] Hello\n")
    (job_dir / "output" / "review.json").write_text(f'{{"overall": 4.0, "note_file": "{smart_ver}"}}')
    (job_dir / "assets" / "scene_0001.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    return job_dir


def _write_valid_review(job_dir, rel="output/review.json"):
    note_file = "output/versions/notes_smart_claude-cli_claude-opus-4-8_20260101-000000.md"
    smart_text, smart_record = source_record(job_dir, note_file, label="smart")
    (job_dir / "intermediate").mkdir(exist_ok=True)
    document_rel = "intermediate/document.json"
    quality_rel = "intermediate/quality.json"
    fingerprint = "sha256:" + "a" * 64
    locator = {
        "html": {
            "source_id": "html", "source_fingerprint": fingerprint,
            "dom_path": "article > p:nth-of-type(1)", "exact": "source body",
        },
    }
    document = {
        "schema_version": 2,
        "job_id": job_dir.name,
        "content_type": "document",
        "document_kind": "article",
        "classification": {"method": "user", "confidence": 1.0},
        "source_profile": "generic_html",
        "capabilities": ["html", "embedded_media"],
        "primary_source_id": "html",
        "sources": [{
            "source_id": "html",
            "source_profile": "generic_html",
            "capabilities": ["html", "embedded_media"],
            "fingerprint": fingerprint,
            "path": "input/source.html",
            "mime_type": "text/html",
            "immutable": True,
        }],
        "metadata": {
            "titles": {"original": "Source title", "zh": "来源标题"},
            "authors": [], "affiliations": [], "author_notes": [],
            "abstract": "", "keywords": [], "lang": "en",
            "license": "", "source_license": "", "rights_notices": [],
            "identifiers": {},
        },
        "blocks": [{
            "block_id": "S1.P1", "parent_id": None, "order": 0,
            "kind": "paragraph", "level": None, "text": "source body",
            "locator": locator,
        }],
        "references": [], "assets": [], "figures": [], "tables": [],
    }
    quality = {
        "schema_version": 1,
        "job_id": job_dir.name,
        "status": "complete",
        "reasons": [],
        "metrics": {"source_block_count": 1, "registry_block_count": 1},
    }
    (job_dir / document_rel).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8",
    )
    (job_dir / quality_rel).write_text(
        json.dumps(quality, ensure_ascii=False), encoding="utf-8",
    )
    document_text, document_record = source_record(
        job_dir, document_rel, label="document",
    )
    quality_text, quality_record = source_record(job_dir, quality_rel, label="quality")
    prompt_rel = "output/versions/review_input_claude-cli_claude-opus-4-8_20260101-000000.md"
    (job_dir / prompt_rel).write_text(
        "strict review prompt\n" + smart_text + document_text + quality_text,
        encoding="utf-8",
    )
    _, prompt_record = source_record(job_dir, prompt_rel, label="prompt")
    prompt_record.pop("label")
    prompt_record["sources"] = [smart_record, document_record, quality_record]
    score_keys = [
        "completeness", "accuracy", "structure", "terminology",
        "formula_integrity", "visual_references", "traceability",
    ]
    raw = json.dumps({
        "completeness": 5, "accuracy": 5, "structure": 4, "terminology": 4,
        "formula_integrity": 5, "visual_references": 5, "traceability": 5,
        "key_terms": [{"term": "FTS", "definition": "全文检索"}],
        "missing_concepts": [], "top3_improvements": ["a", "b", "c"],
        "issues": [{
            "type": "traceability", "severity": "warning", "dimension": "accuracy",
            "claim": "标题可追溯", "message": "已定位", "evidence_status": "supported",
            "locator": {"source": "smart", "quote": "Smart Notes"},
        }],
    }, ensure_ascii=False)
    review, _ = parse_review(
        raw,
        score_keys,
        LLMResponse(
            content=raw, model="m", provider="openai", finish_reason="stop",
            tier_used="primary", attempts=[{
                "tier": "primary", "provider": "openai", "model": "m", "ok": True,
            }],
        ),
        review_input=prompt_record,
        review_source_texts={
            "smart": smart_text,
            "document": document_text,
            "quality": quality_text,
        },
    )
    review.update({
        "note_file": note_file,
        "provider": "openai", "model": "m", "generated_at": "2026/07/14 12:00:00",
        "review_coverage": {
            "note_chars": len(smart_text), "reviewed_chars": len(smart_text), "truncated": False,
        },
    })
    (job_dir / rel).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    return review


class TestBoundedVerificationReads:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("rel", "limit"), [
        ("output/evidence/evidence-E1.json", MAX_EVIDENCE_BYTES),
        ("output/notes_mechanical.md", MAX_MECHANICAL_EVIDENCE_BYTES),
        ("output/versions/review_input_x.md", MAX_REVIEW_SOURCE_BYTES),
    ])
    async def test_size_gate_never_opens_or_uses_unbounded_reader(self, rel, limit):
        class Storage:
            async def file_size(self, job_id, rel_path):
                return limit + 1

            async def open_stream(self, *args, **kwargs):
                raise AssertionError("oversized metadata must stop before stream")

            async def read_file(self, *args, **kwargs):
                raise AssertionError("verification must not use read_file")

        data = await _read_verification_artifact(Storage(), "j1", rel)
        assert len(data) == limit + 1

    @pytest.mark.asyncio
    async def test_unknown_size_requests_only_limit_plus_one_and_truncates(self):
        calls = []

        class Storage:
            async def file_size(self, job_id, rel_path):
                return None

            async def open_stream(self, job_id, rel_path, **kwargs):
                calls.append(kwargs)

                async def chunks():
                    yield b"x" * MAX_EVIDENCE_BYTES
                    yield b"overflow"

                return chunks()

            async def read_file(self, *args, **kwargs):
                raise AssertionError("verification must not use read_file")

        data = await _read_verification_artifact(
            Storage(), "j1", "output/evidence/evidence-E1.json",
        )
        assert len(data) == MAX_EVIDENCE_BYTES + 1
        assert calls == [{
            "length": MAX_EVIDENCE_BYTES + 1,
            "chunk_size": 256 * 1024,
        }]


class TestNotes:
    @pytest.mark.asyncio
    async def test_media_without_range_streams_complete_pdf(self, client, test_config):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        payload = b"%PDF-1.7\n" + b"x" * (2 * 1024 * 1024 + 17)
        (job / "input/source.pdf").write_bytes(payload)

        resp = await client.get("/api/jobs/j_test/media?path=input/source.pdf")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-length"] == str(len(payload))
        assert resp.headers["accept-ranges"] == "bytes"
        assert "content-range" not in resp.headers
        assert resp.content == payload

    @pytest.mark.asyncio
    async def test_media_range_is_bounded_and_supports_suffix(self, client, test_config):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        payload = bytes(range(64))
        (job / "input/source.pdf").write_bytes(payload)

        middle = await client.get(
            "/api/jobs/j_test/media?path=input/source.pdf",
            headers={"Range": "bytes=5-9"},
        )
        suffix = await client.get(
            "/api/jobs/j_test/media?path=input/source.pdf",
            headers={"Range": "bytes=-4"},
        )

        assert middle.status_code == 206
        assert middle.headers["content-range"] == "bytes 5-9/64"
        assert middle.content == payload[5:10]
        assert suffix.status_code == 206
        assert suffix.headers["content-range"] == "bytes 60-63/64"
        assert suffix.content == payload[-4:]

    @pytest.mark.asyncio
    async def test_media_rejects_invalid_range_with_size(self, client, test_config):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        (job / "input/source.pdf").write_bytes(b"%PDF")

        resp = await client.get(
            "/api/jobs/j_test/media?path=input/source.pdf",
            headers={"Range": "bytes=99-100"},
        )

        assert resp.status_code == 416
        assert resp.headers["content-range"] == "bytes */4"

    @pytest.mark.asyncio
    async def test_smart_notes(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/notes/smart")
        assert resp.status_code == 200
        assert "Smart Notes" in resp.text

    @pytest.mark.asyncio
    async def test_mechanical_notes(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/notes/mechanical")
        assert resp.status_code == 200
        assert "# Mechanical" in resp.text   # 取到的是机械笔记内容,而非空/错文件

    @pytest.mark.asyncio
    async def test_transcript(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/notes/transcript")
        assert resp.status_code == 200
        assert "[00:00] Hello" in resp.text   # 取到逐字稿正文

    @pytest.mark.asyncio
    async def test_review(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/review")
        assert resp.status_code == 200
        assert resp.json()["overall"] is None
        assert resp.json()["reliability_state"] == "legacy_unverified"

    @pytest.mark.asyncio
    async def test_unreliable_review_hides_score_and_terms(self, client, test_config):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        (job / "output/review.json").write_text(json.dumps({
            "schema_version": 2, "review_reliable": False, "overall": 4.8,
            "key_terms": [{"term": "unsafe", "definition": "x"}],
        }))
        data = (await client.get("/api/jobs/j_test/review")).json()
        assert data["reliability_state"] == "unreliable"
        assert data["overall"] is None and data.get("diagnostic_overall") is None
        assert data["key_terms"] == []
        assert all(data.get(key) is None for key in (
            "completeness", "accuracy", "structure", "terminology",
            "formula_integrity", "visual_references", "traceability",
        ))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("schema_version", [None, 2])
    async def test_review_projection_normalizes_hostile_nested_shapes_and_strips_links(
        self, client, test_config, schema_version,
    ):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        payload = {
            "review_reliable": schema_version == 2,
            "review_input": True,
            "reliability_reasons": "forged",
            "missing_concepts": {"nested": True},
            "top3_improvements": "forged",
            "issues": [{
                "type": "traceability", "severity": "warning", "dimension": "accuracy",
                "claim": "金额需核验", "message": "定位未验证",
                "evidence_status": "supported",
                "locator": {"source": "E1", "quote": "罚款 100 万元", "offset": 1},
            }],
            "note_file": "output/versions/notes_smart_forged.md",
        }
        if schema_version is not None:
            payload["schema_version"] = schema_version
        (job / "output/review.json").write_text(json.dumps(payload, ensure_ascii=False))

        response = await client.get("/api/jobs/j_test/review")

        assert response.status_code == 200
        data = response.json()
        assert data["review_reliable"] is False
        assert data["reliability_state"] == (
            "unreliable" if schema_version == 2 else "legacy_unverified"
        )
        assert data["review_input"]["sources"] == []
        assert data["issues"][0]["message"] == "定位未验证"
        assert data["issues"][0]["locator"] is None
        assert data["missing_concepts"] == [] and data["top3_improvements"] == []
        assert data["note_file"] is None

    @pytest.mark.asyncio
    async def test_review_and_evidence_reject_non_object_json(self, client, test_config):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        (job / "output/review.json").write_text("[]")
        (job / "output/evidence.json").write_text("[]")
        assert (await client.get("/api/jobs/j_test/review")).status_code == 422
        assert (await client.get("/api/jobs/j_test/evidence")).status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize("items", [True, False, 1, 1.5, {}, "E1", None])
    async def test_evidence_projection_non_list_shape_never_500_or_links(
        self, client, test_config, items,
    ):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        (job / "output/evidence.json").write_text(json.dumps({
            "schema_version": 2, "job_id": "j_test", "evidence": items,
        }))
        response = await client.get("/api/jobs/j_test/evidence")
        assert response.status_code == 200
        assert response.json()["evidence"] == []
        assert response.json()["manifest_state"] == "invalid"
        assert response.json()["reliability_state"] == "unreliable"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", [True, False, 1, 1.5, "x", {}, None])
    async def test_evidence_projection_normalizes_hostile_nested_item_shapes(
        self, client, test_config, shape,
    ):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        (job / "output/evidence.json").write_text(json.dumps({
            "schema_version": 2, "job_id": "j_test", "ocr_refs": [],
            "evidence": [{
                "id": "E1", "job_id": "j_test", "title": shape,
                "publisher": shape, "source_tier": shape, "confidence": shape,
                "eligible": shape, "eligibility_reasons": shape, "matches": shape,
                "artifact": "output/evidence/missing.md",
                "final_url": "javascript:alert(1)",
            }],
        }, ensure_ascii=False))

        response = await client.get("/api/jobs/j_test/evidence")

        assert response.status_code == 200
        data = response.json()
        assert data["manifest_state"] == "invalid"
        assert isinstance(data["manifest_errors"], list) and data["manifest_errors"]
        item = data["evidence"][0]
        assert item["matches"] == []
        assert isinstance(item["eligibility_reasons"], list)
        assert isinstance(item["verification_reasons"], list)
        assert item["artifact"] is None and item["final_url"] is None
        assert item["link_safe"] is False

    @pytest.mark.asyncio
    async def test_legacy_evidence_does_not_expose_unsafe_link(self, client, test_config):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        (job / "output/evidence.json").write_text(json.dumps({
            "evidence": [{"id": "E1", "url": "javascript:alert(1)"}],
        }))
        data = (await client.get("/api/jobs/j_test/evidence")).json()
        assert data["reliability_state"] == "legacy_unverified"
        assert data["evidence"][0]["link_safe"] is False
        assert data["evidence"][0]["url"] is None

    @pytest.mark.asyncio
    async def test_v2_evidence_link_requires_current_artifact_hash(self, client, test_config):
        import hashlib

        job = _create_job_files(test_config.jobs_dir, "j_test")
        ref = "〔2018〕88号"
        (job / "output/notes_mechanical.md").write_text(f"案例 {ref}\n")
        artifact = job / "output/evidence/evidence-01.md"
        artifact.parent.mkdir()
        artifact.write_text(f"official {ref} 123\n")
        body = artifact.read_bytes()
        manifest = {
            "schema_version": 2, "job_id": "j_test", "ocr_refs": [ref],
            "evidence": [{
                "id": "E1", "job_id": "j_test", "artifact": "output/evidence/evidence-01.md",
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(), "bytes": len(body),
                "chars": len(body.decode()), "eligible": True, "confidence": "high",
                "source_tier": "一手官方", "eligibility_reasons": [],
                "matches": [{"anchor": ref, "offset": body.decode().find(ref)}],
                "original_url": "https://www.csrc.gov.cn/x",
                "final_url": "https://www.csrc.gov.cn/x",
            }],
            "rejected": [], "total_bytes": len(body),
            "candidate_parse_failed": False, "provider": "claude-cli",
        }
        (job / "output/evidence.json").write_text(json.dumps(manifest))
        valid_data = (await client.get("/api/jobs/j_test/evidence")).json()
        valid = valid_data["evidence"][0]
        assert valid_data["manifest_state"] == "verified"
        assert valid["link_safe"] is True and valid["final_url"].startswith("https://")
        invalid_item = dict(manifest["evidence"][0])
        invalid_item.update({"id": "E2", "eligible": False, "confidence": "low"})
        manifest["evidence"].append(invalid_item)
        (job / "output/evidence.json").write_text(json.dumps(manifest))
        invalid = (await client.get("/api/jobs/j_test/evidence")).json()
        assert invalid["manifest_state"] == "invalid"
        assert invalid["manifest_errors"]
        assert all(item["link_safe"] is False for item in invalid["evidence"])
        assert invalid["evidence"][1]["verification_reasons"]
        manifest["evidence"].pop()
        manifest["evidence"][0]["original_url"] = "http://127.0.0.1/private"
        (job / "output/evidence.json").write_text(json.dumps(manifest))
        unsafe_original = (await client.get("/api/jobs/j_test/evidence")).json()["evidence"][0]
        assert unsafe_original["link_safe"] is False
        assert unsafe_original["original_url"] is None and unsafe_original["final_url"] is None
        manifest["evidence"][0]["original_url"] = "https://www.csrc.gov.cn/x"
        manifest["evidence"].append(dict(manifest["evidence"][0]))
        (job / "output/evidence.json").write_text(json.dumps(manifest))
        duplicated = (await client.get("/api/jobs/j_test/evidence")).json()["evidence"]
        assert all(item["link_safe"] is False for item in duplicated)
        manifest["evidence"].pop()
        (job / "output/evidence.json").write_text(json.dumps(manifest))
        artifact.write_text("tampered")
        tampered = (await client.get("/api/jobs/j_test/evidence")).json()["evidence"][0]
        assert tampered["link_safe"] is False and tampered["final_url"] is None

    @pytest.mark.asyncio
    async def test_asset(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/assets/scene_0001.jpg")
        assert resp.status_code == 200
        assert resp.content[:4] == b"\xff\xd8\xff\xe0"   # 取到的是真 JPEG 字节(非空/错文件)
        assert resp.headers["content-type"] in ("image/jpeg", "application/octet-stream")

    @pytest.mark.asyncio
    async def test_asset_path_traversal(self, client, test_config):
        # %2e%2e 解码为 ".." 仍在单段内,能真正到达守卫 → 严格断言 400(不接受 404 蒙混)
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/assets/%2e%2e_passwd")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_asset_null_byte(self, client, test_config):
        # 文件名含空字节(%00→\x00)会让 pathlib.resolve() 抛 ValueError;
        # _safe_path 拦下、_serve 映射为 400,不得裸 500(schemathesis fuzz 发现的回归)。
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/assets/x%00")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_note_versions_lists_with_overall(self, client, test_config, db):
        job_dir = _create_job_files(test_config.jobs_dir, "j_test")
        db.create_job(Job(id="j_test", content_type="document", document_kind="article",
                          pipeline="document"))
        # 与该版笔记 1:1 配对的版本化评审,使 note-versions 能读到 overall
        paired = "output/versions/review_claude-cli_claude-opus-4-8_20260101-000000.json"
        _write_valid_review(job_dir, paired)
        resp = await client.get("/api/jobs/j_test/note-versions")
        assert resp.status_code == 200
        versions = resp.json()["versions"]
        assert len(versions) == 1
        v = versions[0]
        assert v["provider"] == "claude-cli" and v["version"] == "20260101-000000"
        assert v["review_file"] == paired and v["overall"] == 4.7
        assert v["review_state"] == "reliable"

    @pytest.mark.asyncio
    async def test_minimal_forged_v2_review_is_downgraded(self, client, test_config):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        (job / "output/review.json").write_text(
            '{"schema_version":2,"review_reliable":true,"overall":5}', encoding="utf-8",
        )
        data = (await client.get("/api/jobs/j_test/review")).json()
        assert data["reliability_state"] == "unreliable"
        assert data["overall"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tamper", ["prompt", "source", "locator"])
    async def test_review_read_time_verifier_downgrades_artifact_drift(
        self, client, test_config, db, tamper,
    ):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        db.create_job(Job(id="j_test", content_type="document", document_kind="article",
                          pipeline="document"))
        review = _write_valid_review(job)
        assert (await client.get("/api/jobs/j_test/review")).json()["reliability_state"] == "reliable"
        if tamper == "prompt":
            (job / review["review_input"]["artifact"]).write_text("tampered prompt")
        elif tamper == "source":
            (job / review["note_file"]).write_text("tampered source")
        else:
            review["issues"][0]["locator"]["offset"] += 1
            (job / "output/review.json").write_text(json.dumps(review, ensure_ascii=False))
        data = (await client.get("/api/jobs/j_test/review")).json()
        assert data["reliability_state"] == "unreliable"
        assert data["overall"] is None and data["key_terms"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mutation", [
        "score_keys", "reasons", "issue_type", "top_extra", "supported_reason",
        "source_extra", "completion_extra", "completion_tier_nested",
    ])
    async def test_malicious_nested_review_types_never_500(
        self, client, test_config, db, mutation,
    ):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        db.create_job(Job(id="j_test", content_type="document", document_kind="article",
                          pipeline="document"))
        review = copy.deepcopy(_write_valid_review(job))
        if mutation == "score_keys":
            review["score_keys"] = [{}]
        elif mutation == "reasons":
            review["reliability_reasons"] = [{}]
        elif mutation == "issue_type":
            review["issues"][0]["type"] = []
        elif mutation == "top_extra":
            review["debug"] = {"trusted": True}
        elif mutation == "supported_reason":
            review["issues"][0]["reason"] = "与 locator 互斥"
        elif mutation == "source_extra":
            review["review_input"]["sources"][0]["debug"] = {"trusted": True}
        elif mutation == "completion_extra":
            review["completion"]["attempts"] = [{
                "tier": "primary", "provider": "openai", "model": "m", "ok": True,
                "debug": {"trusted": True},
            }]
            review["completion"]["tier_used"] = "primary"
        else:
            review["completion"]["tier_used"] = []
        paired = "output/versions/review_claude-cli_claude-opus-4-8_20260101-000000.json"
        for rel in ("output/review.json", paired):
            (job / rel).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        direct = await client.get("/api/jobs/j_test/review")
        versions = await client.get("/api/jobs/j_test/note-versions")
        assert direct.status_code == 200 and direct.json()["reliability_state"] == "unreliable"
        assert versions.status_code == 200
        assert versions.json()["versions"][0]["review_state"] == "unreliable"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pipeline", "reason"),
        [("video", "score_profile_mismatch"), ("unknown-pipeline", "review_pipeline_unknown")],
    )
    async def test_review_profile_is_bound_to_job_pipeline(
        self, client, test_config, db, pipeline, reason,
    ):
        job = _create_job_files(test_config.jobs_dir, "j_test")
        db.create_job(Job(id="j_test", content_type="video", pipeline=pipeline))
        _write_valid_review(job)

        data = (await client.get("/api/jobs/j_test/review")).json()

        assert data["reliability_state"] == "unreliable"
        assert reason in data["reliability_reasons"]
        assert data["overall"] is None and data["key_terms"] == []

    @pytest.mark.asyncio
    async def test_smart_version_select_valid(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        f = "output/versions/notes_smart_claude-cli_claude-opus-4-8_20260101-000000.md"
        resp = await client.get(f"/api/jobs/j_test/notes/smart?file={f}")
        assert resp.status_code == 200 and "Smart Notes" in resp.text

    @pytest.mark.asyncio
    async def test_smart_version_select_rejects_bad_file(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        # 穿越
        r1 = await client.get("/api/jobs/j_test/notes/smart?file=output/versions/../../x.md")
        assert r1.status_code == 400
        # 不在 notes_smart 版本前缀
        r2 = await client.get("/api/jobs/j_test/notes/smart?file=output/notes_mechanical.md")
        assert r2.status_code == 400

    @pytest.mark.asyncio
    async def test_review_version_select_rejects_bad_file(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        # review.json 不在 versions/review_ 版本前缀 → 400
        r = await client.get("/api/jobs/j_test/review?file=output/review.json")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        resp = await client.get("/api/jobs/nonexistent/notes/smart")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_notes_not_ready(self, client, test_config):
        job_dir = test_config.jobs_dir / "j_empty"
        job_dir.mkdir()
        (job_dir / "output").mkdir()
        resp = await client.get("/api/jobs/j_empty/notes/smart")
        assert resp.status_code == 404
