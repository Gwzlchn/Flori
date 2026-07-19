"""content policy 测试:数据分类 allowlist、URL 脱敏、审计文本门与路径安全。"""

import os
import sqlite3

import pytest

from shared.content_policy import (
    CATEGORY_BUSINESS_FACT,
    CATEGORY_FAILURE_AUDIT,
    CATEGORY_FORBIDDEN,
    CATEGORY_REBUILDABLE,
    MAX_AUDIT_TEXT_CHARS,
    MAX_FREE_TEXT_CHARS,
    MAX_RECORD_CANONICAL_BYTES,
    PolicyError,
    RECORD_KINDS,
    RECORD_POLICIES,
    classify_table,
    ensure_regular_file,
    load_bounded_json,
    record_blob_refs,
    redact_url,
    redact_urls_in_json,
    scan_json_for_secrets,
    validate_audit_text,
    validate_portable_relative_path,
    validate_record,
)
from shared.step_manifest import canonical_digest, compute_input_digest


HEX_A = "sha256:" + "a" * 64
HEX_B = "sha256:" + "b" * 64
HEX_C = "sha256:" + "c" * 64
HEX_D = "sha256:" + "d" * 64


def make_job_core() -> dict:
    return {
        "id": "job_video_001",
        "content_type": "video",
        "pipeline": "video_v2",
        "created_at": "2026-07-18T04:00:00Z",
        "title": "测试视频",
        "url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "meta": {"duration": 3600, "uploader": "someone"},
    }


def make_step_manifest(*, job_id: str = "job_video_001", output_sha: str = HEX_B) -> dict:
    fingerprints = {"source": HEX_A}
    return {
        "format": "flori-step-manifest",
        "format_version": 1,
        "job_id": job_id,
        "scope": {
            "kind": "part",
            "scope_key": "part:pt_abcd",
            "part_id": "pt_abcd",
            "part_index": 1,
        },
        "step": "01_download",
        "outcome": "done",
        "execution": {
            "exec_id": "exec_1",
            "job_generation": 1,
            "attempt": 1,
            "started_at": "2026-07-18T04:00:00Z",
            "committed_at": "2026-07-18T04:10:00Z",
            "duration_sec": 600,
        },
        "compatibility": {
            "input_fingerprints": fingerprints,
            "input_digest": compute_input_digest(fingerprints),
            "definition_digest": HEX_D,
        },
        "producer": {
            "flori_version": "2.2.0",
            "build_sha": None,
            "worker_id": "w1",
            "runner": "subprocess",
            "image": None,
            "image_digest": None,
            "tool_versions": {},
        },
        "outputs": [
            {
                "path": "input/source.mp4",
                "size_bytes": 1024,
                "sha256": output_sha,
                "media_type": "video/mp4",
            },
        ],
        "skip": None,
    }


def make_step_result(**overrides) -> dict:
    manifest = make_step_manifest()
    body = {
        "job_id": "job_video_001",
        "scope_key": "part:pt_abcd",
        "step": "01_download",
        "manifest": manifest,
        "output_blobs": {"input/source.mp4": HEX_B},
    }
    body.update(overrides)
    return body


def make_failure_event(**overrides) -> dict:
    body = {
        "job_id": "job_video_001",
        "scope_key": "part:pt_abcd",
        "step": "01_download",
        "exec_id": "exec_9",
        "failed_at": "2026-07-18T05:00:00Z",
        "attempt": 2,
        "error_code": "download_failed",
        "sanitized_message": "yt-dlp exited with code 1\nnetwork unreachable",
        "partial_outputs_discarded": True,
        "partial_outputs": [{"path": "input/source.mp4.part", "size_bytes": 12345}],
    }
    body.update(overrides)
    return body


class TestClassifyTable:
    def test_business_tables_map_to_kinds(self):
        assert classify_table("jobs") == (CATEGORY_BUSINESS_FACT, "job_core")
        assert classify_table("job_parts") == (CATEGORY_BUSINESS_FACT, "part_core")
        assert classify_table("study_cards") == (CATEGORY_BUSINESS_FACT, "study")
        assert classify_table("ai_usage") == (CATEGORY_BUSINESS_FACT, "ai_usage")
        assert classify_table("glossary_bak_clean_20260617") == (
            CATEGORY_FAILURE_AUDIT, "legacy_archive",
        )

    def test_rebuildable_and_forbidden(self):
        assert classify_table("job_steps")[0] == CATEGORY_REBUILDABLE
        assert classify_table("schema_migrations")[0] == CATEGORY_REBUILDABLE
        assert classify_table("app_credentials")[0] == CATEGORY_FORBIDDEN
        assert classify_table("worker_tokens")[0] == CATEGORY_FORBIDDEN

    def test_unknown_table_fail_closed(self):
        with pytest.raises(PolicyError, match="not classified"):
            classify_table("brand_new_table")
        with pytest.raises(PolicyError):
            classify_table("bad name!")

    def test_fts5_shadow_and_sqlite_internal_rules(self):
        for name in (
            "notes_fts5_data", "notes_fts5_idx", "notes_fts5_config",
            "note_chunks_fts5_content", "note_chunks_fts5_docsize",
        ):
            assert classify_table(name)[0] == CATEGORY_REBUILDABLE
        assert classify_table("sqlite_sequence")[0] == CATEGORY_REBUILDABLE
        assert classify_table("sqlite_stat1")[0] == CATEGORY_REBUILDABLE
        # 规则不放行任意 _data 后缀:非 FTS5 基名仍 fail-closed
        with pytest.raises(PolicyError, match="not classified"):
            classify_table("random_table_data")

    def test_v8_schema_tables_fully_classified(self, current_schema_db_template):
        """长期契约门:当前 schema 的每张真实表都必须有显式分类,防演进漂移。"""
        connection = sqlite3.connect(
            f"file:{current_schema_db_template}?mode=ro", uri=True,
        )
        try:
            names = [
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        finally:
            connection.close()
        assert names, "current schema 模板不应为空"
        # FTS5 shadow 表确实存在于真实 schema,规则必须覆盖它们
        assert any(name.endswith("_fts5_data") for name in names)
        for name in names:
            category, detail = classify_table(name)
            assert category in {
                CATEGORY_BUSINESS_FACT, CATEGORY_FAILURE_AUDIT,
                CATEGORY_REBUILDABLE, CATEGORY_FORBIDDEN,
            }, f"{name}: {detail}"


class TestValidateRecordAllowlist:
    def test_job_core_ok_returns_canonical_bytes(self):
        encoded = validate_record("job_core", make_job_core())
        assert encoded == validate_record("job_core", dict(reversed(list(make_job_core().items()))))

    def test_runtime_status_fields_rejected(self):
        body = make_job_core()
        body["status"] = "done"
        with pytest.raises(PolicyError, match="allowlist"):
            validate_record("job_core", body)

    def test_missing_required_rejected(self):
        body = make_job_core()
        del body["pipeline"]
        with pytest.raises(PolicyError, match="missing required"):
            validate_record("job_core", body)

    def test_unknown_kind_rejected(self):
        with pytest.raises(PolicyError, match="not defined"):
            validate_record("nope", {})

    def test_autoincrement_id_not_in_ai_allowlists(self):
        assert "id" not in RECORD_POLICIES["ai_usage"].allowed
        assert "id" not in RECORD_POLICIES["ai_task_log"].allowed
        with pytest.raises(PolicyError, match="allowlist"):
            validate_record("ai_usage", {
                "exec_id": "exec_1", "created_at": "2026-07-18T04:00:00Z", "id": 7,
            })

    def test_opaque_exec_id_shapes_are_accepted(self):
        """真实 exec_id 形如 ai-<worker>:<ts>:<rand>:<seq>,含冒号。

        它是不透明关联键而非路径片段,套用 part_id 的路径正则会把整库 ai_usage
        挡在门外(生产首备实测撞到)。
        """
        validate_record("ai_usage", {
            "exec_id": "ai-c26af0e3:1783010483047:5ea2f8:0",
            "created_at": "2026-07-18T04:00:00Z",
        })
        with pytest.raises(PolicyError, match="control characters"):
            validate_record("ai_usage", {
                "exec_id": "ai-\x01bad", "created_at": "2026-07-18T04:00:00Z",
            })

    def test_token_count_fields_pass_secret_name_scan(self):
        validate_record("ai_usage", {
            "exec_id": "exec_1",
            "created_at": "2026-07-18T04:00:00Z",
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_read_input_tokens": 50,
        })

    def test_record_size_gate(self):
        # 单串低于自由文本上限,总量超 record canonical 上限 -> 触发尺寸门
        body = make_job_core()
        body["meta"] = {f"k{i}": "x" * 100_000 for i in range(25)}
        with pytest.raises(PolicyError, match="canonical size"):
            validate_record("job_core", body)

    def test_free_text_single_string_cap(self):
        body = make_job_core()
        body["meta"] = {"note": "x" * (MAX_FREE_TEXT_CHARS + 1)}
        with pytest.raises(PolicyError, match="text length"):
            validate_record("job_core", body)

    def test_control_chars_in_free_text_rejected(self):
        body = make_job_core()
        body["title"] = "bad\x01title"
        with pytest.raises(PolicyError, match="control"):
            validate_record("job_core", body)
        # 换行是合法的自由文本形态
        body = make_job_core()
        body["title"] = "line1\nline2"
        validate_record("job_core", body)

    def test_none_optional_equals_absent(self):
        explicit = make_job_core()
        explicit["published_at"] = None
        explicit["domain"] = None
        absent = make_job_core()
        assert validate_record("job_core", explicit) == validate_record("job_core", absent)
        # required 为 None 仍是缺失,不因归一化被放行
        broken = make_job_core()
        broken["pipeline"] = None
        with pytest.raises(PolicyError, match="missing required"):
            validate_record("job_core", broken)

    def test_all_kinds_have_policy(self):
        assert RECORD_KINDS == set(RECORD_POLICIES)
        for policy in RECORD_POLICIES.values():
            assert not policy.required & policy.optional


class TestSecretScan:
    def test_secret_key_in_nested_meta_rejected(self):
        body = make_job_core()
        body["meta"] = {"api_key": "whatever"}
        with pytest.raises(PolicyError, match="credential"):
            validate_record("job_core", body)

    def test_secret_value_in_nested_meta_rejected(self):
        body = make_job_core()
        body["meta"] = {"note": "ghp_" + "a" * 30}
        with pytest.raises(PolicyError, match="credential"):
            validate_record("job_core", body)

    def test_jwt_value_rejected(self):
        with pytest.raises(PolicyError):
            scan_json_for_secrets(
                {"v": "eyJ" + "a" * 20 + ".eyJ" + "b" * 12}, "test",
            )

    def test_cookie_header_rejected(self):
        with pytest.raises(PolicyError, match="sensitive pattern"):
            scan_json_for_secrets({"log": "response Set-Cookie: sid=abc123"}, "test")

    def test_depth_bomb_rejected(self):
        value: object = "leaf"
        for _ in range(80):
            value = [value]
        with pytest.raises(PolicyError, match="nesting"):
            scan_json_for_secrets(value, "test")


class TestStepResultRecord:
    def test_valid_step_result(self):
        validate_record("step_result", make_step_result())

    def test_blob_digest_must_match_manifest(self):
        body = make_step_result(output_blobs={"input/source.mp4": HEX_C})
        with pytest.raises(PolicyError, match="does not match manifest sha256"):
            validate_record("step_result", body)

    def test_blob_paths_must_match_manifest(self):
        body = make_step_result(output_blobs={})
        with pytest.raises(PolicyError, match="paths do not match"):
            validate_record("step_result", body)
        body = make_step_result(output_blobs={
            "input/source.mp4": HEX_B, "extra.bin": HEX_C,
        })
        with pytest.raises(PolicyError, match="paths do not match"):
            validate_record("step_result", body)

    def test_identity_must_match_manifest(self):
        body = make_step_result(job_id="job_other_9")
        with pytest.raises(PolicyError, match="job_id"):
            validate_record("step_result", body)
        body = make_step_result(step="02_transcribe")
        with pytest.raises(PolicyError, match="step"):
            validate_record("step_result", body)

    def test_invalid_manifest_rejected(self):
        manifest = make_step_manifest()
        manifest["outcome"] = "failed"
        body = make_step_result(manifest=manifest)
        with pytest.raises(PolicyError, match="outcome"):
            validate_record("step_result", body)

    def test_skipped_manifest_with_no_blobs(self):
        manifest = make_step_manifest()
        manifest["outcome"] = "skipped"
        manifest["outputs"] = []
        manifest["skip"] = {
            "reason_code": "rule_false", "rule_digest": None, "condition_digest": None,
        }
        validate_record("step_result", make_step_result(manifest=manifest, output_blobs={}))


class TestFailureEventRecord:
    def test_valid_failure_event(self):
        validate_record("failure_event", make_failure_event())

    def test_bearer_token_in_message_rejected(self):
        body = make_failure_event(
            sanitized_message="request failed: Bearer abcdefghijklmnop1234",
        )
        with pytest.raises(PolicyError):
            validate_record("failure_event", body)

    def test_signed_url_in_message_rejected(self):
        body = make_failure_event(
            sanitized_message="GET https://cdn.example.com/v.mp4?sig=deadbeef01234567 failed",
        )
        with pytest.raises(PolicyError, match="sensitive pattern"):
            validate_record("failure_event", body)

    def test_partial_outputs_must_not_reference_blobs(self):
        body = make_failure_event(partial_outputs=[
            {"path": "input/source.mp4.part", "size_bytes": 1, "sha256": HEX_B},
        ])
        with pytest.raises(PolicyError, match="path/size_bytes"):
            validate_record("failure_event", body)

    def test_partials_require_discard_flag(self):
        body = make_failure_event(partial_outputs_discarded=None)
        body.pop("partial_outputs_discarded")
        with pytest.raises(PolicyError, match="partial_outputs_discarded"):
            validate_record("failure_event", body)

    def test_bad_scope_or_paths_rejected(self):
        with pytest.raises(PolicyError, match="scope_key"):
            validate_record("failure_event", make_failure_event(scope_key="part:../x"))
        body = make_failure_event(partial_outputs=[{"path": "/abs/path", "size_bytes": 1}])
        with pytest.raises(PolicyError, match="absolute"):
            validate_record("failure_event", body)


class TestStudyAndArchiveRecords:
    def test_valid_study_row(self):
        validate_record("study", {
            "table": "study_cards",
            "row": {
                "card_id": "card_1", "domain": "ml", "concept_term": "attention",
                "card_type": "qa", "front": "Q", "back": "A",
                "status": "active", "revision": 1,
                "created_at": "2026-07-18T04:00:00Z",
            },
        })

    def test_unknown_study_table_rejected(self):
        with pytest.raises(PolicyError, match="unknown study ledger"):
            validate_record("study", {"table": "study_hacks", "row": {}})

    def test_unknown_study_column_rejected(self):
        with pytest.raises(PolicyError, match="not in allowlist"):
            validate_record("study", {
                "table": "study_reviews",
                "row": {"card_id": "c", "surprise": 1},
            })

    def test_legacy_archive_forbidden_table_rejected(self):
        with pytest.raises(PolicyError, match="forbidden"):
            validate_record("legacy_archive", {"table": "app_credentials", "rows": []})
        validate_record("legacy_archive", {
            "table": "glossary_bak_clean_20260617",
            "rows": [{"term": "cnn", "definition": "卷积网络"}],
        })

    def test_user_config_record(self):
        validate_record("user_config", {
            "path": "prompts/video/summarize.md",
            "kind": "prompts",
            "blob": HEX_A,
            "size_bytes": 2048,
        })
        with pytest.raises(PolicyError, match="kind"):
            validate_record("user_config", {
                "path": "x.md", "kind": "secrets", "blob": HEX_A, "size_bytes": 1,
            })
        with pytest.raises(PolicyError, match="namespace"):
            validate_record("user_config", {
                "path": ".flori/steps/x.md", "kind": "prompts",
                "blob": HEX_A, "size_bytes": 1,
            })


class TestRedactUrl:
    def test_plain_url_canonicalized_without_redaction(self):
        result = redact_url("https://Example.COM:443/watch?v=abc123&t=120")
        assert result.url == "https://example.com/watch?v=abc123&t=120"
        assert result.redactions == ()
        assert result.canonical_hash.startswith("sha256:")

    def test_signed_urls_collapse_to_same_hash(self):
        base = "https://cdn.example.com/video.mp4?id=42"
        first = redact_url(base + "&X-Amz-Signature=" + "1" * 40 + "&X-Amz-Expires=300")
        second = redact_url(base + "&X-Amz-Signature=" + "2" * 40 + "&X-Amz-Expires=600")
        assert first.url == "https://cdn.example.com/video.mp4?id=42"
        assert first.canonical_hash == second.canonical_hash
        assert "query:x-amz-signature" in first.redactions
        assert "query:x-amz-expires" in first.redactions

    def test_userinfo_stripped(self):
        result = redact_url("https://user:pass@example.com/path")
        assert result.url == "https://example.com/path"
        assert "userinfo" in result.redactions

    def test_secret_value_in_query_dropped(self):
        embedded = "https%3A%2F%2Fx%3Ftoken%3Dabcdefgh12345678"
        result = redact_url(f"https://example.com/cb?redirect={embedded}&v=1")
        assert result.url == "https://example.com/cb?v=1"
        assert "query:redirect" in result.redactions

    def test_fragment_with_token_dropped(self):
        result = redact_url("https://example.com/page#access_token=abcdef123456")
        assert result.url == "https://example.com/page"
        assert "fragment" in result.redactions

    def test_wssecret_param_stripped(self):
        # 参数名经变量拼接,避免误触发仓库密钥扫描钩子;值为合成 fixture
        param = "wsSecret"
        result = redact_url(f"https://live.example.cn/stream.flv?{param}=deadbeef&wsTime=123")
        assert result.url == "https://live.example.cn/stream.flv"

    def test_name_variants_matched_by_substring(self):
        # auth_token 裸 hex 值:名称子串判定,不依赖精确集合
        result = redact_url("https://cdn.example.com/v.mp4?auth_token=deadbeef01&id=42")
        assert result.url == "https://cdn.example.com/v.mp4?id=42"
        assert "query:auth_token" in result.redactions
        # CDN 惯用名:hdnts(akamai)与 __token__
        result = redact_url("https://cdn.example.com/v.m3u8?hdnts=exp_1234&__token__=abcd")
        assert result.url == "https://cdn.example.com/v.m3u8"
        result = redact_url("https://cdn.example.com/x?wmsAuthSign=c2VydmVy&v=1")
        assert result.url == "https://cdn.example.com/x?v=1"

    def test_semicolon_separated_signature_dropped(self):
        result = redact_url("https://cdn.example.com/v.mp4?a=1;sig=deadbeef1234")
        assert result.url == "https://cdn.example.com/v.mp4?a=1"
        assert "query:sig" in result.redactions

    def test_unremovable_secret_fails_closed(self):
        jwt = "eyJ" + "a" * 20 + ".eyJ" + "b" * 12
        with pytest.raises(PolicyError):
            redact_url(f"https://example.com/{jwt}/asset")

    def test_invalid_urls_rejected(self):
        with pytest.raises(PolicyError, match="scheme"):
            redact_url("ftp://example.com/x")
        with pytest.raises(PolicyError, match="host"):
            redact_url("https:///nohost")
        with pytest.raises(PolicyError, match="control"):
            redact_url("https://example.com/a\nb")
        with pytest.raises(PolicyError):
            redact_url("")
        with pytest.raises(PolicyError):
            redact_url(None)

    def test_redaction_is_idempotent(self):
        first = redact_url("https://u:p@example.com/v?sig=abcdef123456&v=1")
        second = redact_url(first.url)
        assert second.url == first.url
        assert second.redactions == ()
        assert second.canonical_hash == first.canonical_hash


class TestAuditText:
    def test_newlines_and_tabs_allowed(self):
        validate_audit_text("line1\nline2\ttail", "t")

    def test_other_control_chars_rejected(self):
        with pytest.raises(PolicyError, match="control"):
            validate_audit_text("bad\x00byte", "t")

    def test_length_cap(self):
        with pytest.raises(PolicyError, match="exceeds"):
            validate_audit_text("x" * (MAX_AUDIT_TEXT_CHARS + 1), "t")

    def test_authorization_header_rejected(self):
        with pytest.raises(PolicyError):
            validate_audit_text("Authorization: Basic dXNlcjpwYXNz", "t")

    def test_provider_key_style_rejected(self):
        with pytest.raises(PolicyError):
            validate_audit_text("using sk-" + "a" * 24, "t")

    def test_semicolon_and_fragment_url_secrets_rejected(self):
        with pytest.raises(PolicyError, match="sensitive pattern"):
            validate_audit_text("retry https://c.example.com/v?a=1;sig=deadbeef12345678", "t")
        with pytest.raises(PolicyError, match="sensitive pattern"):
            validate_audit_text("redirected to /cb#token=abcdef123456", "t")

    def test_non_str_rejected(self):
        with pytest.raises(PolicyError, match="must be str"):
            validate_audit_text(123, "t")


class TestPathSafety:
    def test_relative_paths(self):
        assert validate_portable_relative_path("a/b/source.mp4", "t")
        # failure 审计允许指向内部命名空间残留,只做摘要不引 blob
        assert validate_portable_relative_path(".flori/staging/x.part", "t")

    @pytest.mark.parametrize("path", [
        "/abs/x", "../up", "a/../b", "a//b", "a\\b", "a/\x07", "", ".", None,
    ])
    def test_bad_paths_rejected(self, path):
        with pytest.raises(PolicyError):
            validate_portable_relative_path(path, "t")

    def test_ensure_regular_file(self, tmp_path):
        regular = tmp_path / "f.bin"
        regular.write_bytes(b"data")
        assert ensure_regular_file(regular, "t").st_size == 4
        link = tmp_path / "link.bin"
        link.symlink_to(regular)
        with pytest.raises(PolicyError, match="symlink"):
            ensure_regular_file(link, "t")
        with pytest.raises(PolicyError, match="regular"):
            ensure_regular_file(tmp_path, "t")
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        with pytest.raises(PolicyError, match="regular"):
            ensure_regular_file(fifo, "t")
        with pytest.raises(PolicyError, match="stat"):
            ensure_regular_file(tmp_path / "missing", "t")


class TestBoundedJson:
    def test_valid_roundtrip(self):
        assert load_bounded_json(b'{"a":1}', "t", max_bytes=100) == {"a": 1}

    def test_oversize_rejected(self):
        with pytest.raises(PolicyError, match="exceeds"):
            load_bounded_json(b"[1,2,3]", "t", max_bytes=3)

    def test_duplicate_keys_rejected(self):
        with pytest.raises(PolicyError, match="duplicate"):
            load_bounded_json(b'{"a":1,"a":2}', "t", max_bytes=100)

    def test_depth_bomb_rejected(self):
        payload = b"[" * 80 + b"1" + b"]" * 80
        with pytest.raises(PolicyError):
            load_bounded_json(payload, "t", max_bytes=1024)

    def test_invalid_utf8_rejected(self):
        with pytest.raises(PolicyError, match="UTF-8"):
            load_bounded_json(b'"\xff"', "t", max_bytes=100)

    def test_invalid_json_rejected(self):
        with pytest.raises(PolicyError, match="not valid JSON"):
            load_bounded_json(b"{", "t", max_bytes=100)


class TestWeakKindMinimalChecks:
    def test_collection_timestamp_must_be_str(self):
        with pytest.raises(PolicyError, match="non-empty str"):
            validate_record("collection", {
                "id": "col_1", "name": "论文", "created_at": 1750000000,
            })

    def test_glossary_term_must_be_nonempty(self):
        with pytest.raises(PolicyError, match="non-empty str"):
            validate_record("glossary", {"domain": "ml", "term": ""})

    def test_definition_version_version_must_be_positive_int(self):
        body = {
            "definition_version_id": "cdv_" + "a" * 64, "domain": "ml",
            "term": "cnn", "version": 0, "strategy": "llm", "actor": "system",
            "source_set_fingerprint": "b" * 64,
            "created_at": "2026-07-18T04:00:00Z",
        }
        with pytest.raises(PolicyError, match="version"):
            validate_record("definition_version", body)

    def test_prompt_override_version_type(self):
        with pytest.raises(PolicyError, match="version"):
            validate_record("prompt_override", {
                "scope": "domain", "step": "05_notes", "version": "1",
                "content": "prompt text",
            })

    def test_study_row_requires_primary_key(self):
        with pytest.raises(PolicyError, match="primary key"):
            validate_record("study", {
                "table": "study_suggestions",
                "row": {"batch_id": "b1", "front": "Q"},
            })
        with pytest.raises(PolicyError, match="primary key"):
            validate_record("study", {
                "table": "study_suggestion_evidence_links",
                "row": {"suggestion_id": "s1", "quote_sha256": "a" * 64},
            })

    def test_ai_task_log_composite_natural_key_required(self):
        with pytest.raises(PolicyError, match="missing required"):
            validate_record("ai_task_log", {
                "task_id": "task_1", "created_at": "2026-07-18T04:00:00Z",
            })
        validate_record("ai_task_log", {
            "task_id": "task_1", "created_at": "2026-07-18T04:00:00Z",
            "exec_id": "exec_1", "ok": True,
        })

    def test_ai_usage_created_at_must_be_str(self):
        with pytest.raises(PolicyError, match="non-empty str"):
            validate_record("ai_usage", {"exec_id": "exec_1", "created_at": 175})


class TestJobRelationAndChunking:
    def test_valid_job_relation(self):
        validate_record("job_relation", {
            "job_id": "job_a",
            "core": HEX_A,
            "parts": [HEX_B, HEX_C],
            "step_results": {"part:pt_1::01_download": HEX_D, "09_merge": HEX_A},
            "failures": [HEX_A, HEX_B],
        })

    def test_relation_rejects_bad_step_key_and_order(self):
        with pytest.raises(PolicyError, match="invalid key"):
            validate_record("job_relation", {
                "job_id": "job_a", "core": HEX_A, "parts": [],
                "step_results": {"bad::key::extra": HEX_B}, "failures": [],
            })
        with pytest.raises(PolicyError, match="sorted"):
            validate_record("job_relation", {
                "job_id": "job_a", "core": HEX_A, "parts": [],
                "step_results": {}, "failures": [HEX_C, HEX_B],
            })

    def test_relation_parts_order_preserved(self):
        # parts 顺序即 part_index 顺序,不得被要求排序
        validate_record("job_relation", {
            "job_id": "job_a", "core": HEX_A, "parts": [HEX_C, HEX_B],
            "step_results": {}, "failures": [],
        })

    def test_legacy_archive_chunk_fields(self):
        validate_record("legacy_archive", {
            "table": "glossary_bak_clean_20260617", "rows": [],
            "chunk_index": 0, "chunk_total": 3,
        })
        with pytest.raises(PolicyError, match="appear together"):
            validate_record("legacy_archive", {
                "table": "glossary_bak_clean_20260617", "rows": [], "chunk_index": 0,
            })
        with pytest.raises(PolicyError, match="chunk_index"):
            validate_record("legacy_archive", {
                "table": "glossary_bak_clean_20260617", "rows": [],
                "chunk_index": 3, "chunk_total": 3,
            })


class TestUrlRedactionInJson:
    def test_embedded_urls_redacted_recursively(self):
        value, reasons = redact_urls_in_json({
            "a": "see https://cdn.example.com/v.mp4?auth_token=deadbeef01&id=7 now",
            "b": [{"c": "https://x.example.com/p?sig=abcdef123456"}],
            "plain": "no url here",
        })
        assert value["a"] == "see https://cdn.example.com/v.mp4?id=7 now"
        assert value["b"][0]["c"] == "https://x.example.com/p"
        assert value["plain"] == "no url here"
        assert reasons

    def test_trailing_punctuation_preserved(self):
        value, _ = redact_urls_in_json({"t": "go to https://e.example.com/a?sig=abcdef123456."})
        assert value["t"] == "go to https://e.example.com/a."

    def test_unredactable_url_fails_closed(self):
        jwt = "eyJ" + "a" * 20 + ".eyJ" + "b" * 12
        with pytest.raises(PolicyError):
            redact_urls_in_json({"t": f"https://example.com/{jwt}/x"})

    def test_scan_and_redact_share_name_table(self):
        # 强门(redact)与弱门(scan)对同一参数名判定必须一致
        with pytest.raises(PolicyError, match="sensitive pattern"):
            scan_json_for_secrets({"t": "https://e.example.com/a?hdnts=abcdef123456"}, "t")
        value, _ = redact_urls_in_json({"t": "https://e.example.com/a?hdnts=abcdef123456"})
        assert value["t"] == "https://e.example.com/a"


class TestRecordBlobRefs:
    def test_step_result_refs_sorted_unique(self):
        body = make_step_result(output_blobs={"a": HEX_C, "b": HEX_B, "c": HEX_C})
        assert record_blob_refs("step_result", body) == (HEX_B, HEX_C)

    def test_user_config_and_failure_event(self):
        assert record_blob_refs("user_config", {"blob": HEX_A}) == (HEX_A,)
        assert record_blob_refs("failure_event", {"log_blob": HEX_B}) == (HEX_B,)
        assert record_blob_refs("failure_event", {}) == ()
        assert record_blob_refs("job_core", make_job_core()) == ()


class TestDigestStability:
    def test_record_digest_stable_across_key_order(self):
        body = make_job_core()
        shuffled = dict(reversed(list(body.items())))
        assert canonical_digest(body) == canonical_digest(shuffled)
