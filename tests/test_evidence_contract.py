"""受控取证的 SSRF、完整性和 citation fail-closed 测试。"""

from __future__ import annotations

import json
import socket

import pytest

from shared.evidence_contract import (
    MAX_EVIDENCE_BYTES,
    MAX_EVIDENCE_ITEMS,
    MAX_TOTAL_EVIDENCE_BYTES,
    SafeEvidenceFetcher,
    materialize_evidence,
    project_evidence,
    validate_citations,
    validate_citations_with_reader,
    validate_manifest,
    validate_manifest_with_reader,
)


def _resolver(ip_by_host):
    def resolve(host, _port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, type, 6, "", (ip_by_host[host], 0))]
    return resolve


class _Response:
    def __init__(self, status=200, body=b"fact 123", content_type="text/plain", location=None):
        self.status_code = status
        self.body = body
        self.headers = {"content-type": content_type}
        if location:
            self.headers["location"] = location

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError("http error")
    def iter_bytes(self):
        yield self.body


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
    def stream(self, _method, url, headers=None):
        self.urls.append(url)
        return self.responses.pop(0)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://u:p@example.com/x", "ftp://example.com/x",
    "http://example.com:0/x", "http://example.com/\nHost:metadata",
])
def test_scheme_and_userinfo_rejected(url):
    with pytest.raises(ValueError):
        SafeEvidenceFetcher(client=_Client([])).fetch(url)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0." + "1", "169.254.169.254", "224.0.0.1", "0.0.0.0",
])
def test_non_global_dns_rejected(ip):
    fetcher = SafeEvidenceFetcher(resolver=_resolver({"bad.example": ip}), client=_Client([]))
    with pytest.raises(ValueError, match="non-global"):
        fetcher.fetch("http://bad.example/x")


def test_every_redirect_hop_is_revalidated():
    client = _Client([_Response(302, location="http://metadata.example/latest"), _Response()])
    fetcher = SafeEvidenceFetcher(
        resolver=_resolver({"public.example": "93.184.216.34", "metadata.example": "169.254.169.254"}),
        client=client,
    )
    with pytest.raises(ValueError, match="non-global"):
        fetcher.fetch("https://public.example/start")
    assert client.urls == ["https://public.example/start"]


@pytest.mark.parametrize(("response", "message"), [
    (_Response(content_type="application/octet-stream"), "MIME"),
    (_Response(body=b"x" * (MAX_EVIDENCE_BYTES + 1)), "size"),
    (_Response(body=b"   \n"), "readable"),
])
def test_mime_size_and_empty_body_rejected(response, message):
    fetcher = SafeEvidenceFetcher(
        resolver=_resolver({"public.example": "93.184.216.34"}), client=_Client([response]))
    with pytest.raises(ValueError, match=message):
        fetcher.fetch("https://public.example/x")


def test_declared_gbk_body_is_normalized_to_utf8_text():
    fetcher = SafeEvidenceFetcher(
        resolver=_resolver({"public.example": "93.184.216.34"}),
        client=_Client([_Response(
            body="处罚文号〔2018〕88号".encode("gbk"),
            content_type="text/plain; charset=gbk",
        )]),
    )
    result = fetcher.fetch("https://public.example/x")
    assert result["charset"] == "gbk"
    assert "处罚文号〔2018〕88号" in result["text"]


def _manifest(job, *, eligible=True, job_id="job"):
    ref = "〔2018〕88号"
    (job / "output").mkdir(parents=True, exist_ok=True)
    (job / "output/notes_mechanical.md").write_text(f"案例 {ref}\n", encoding="utf-8")
    artifact = job / "output/evidence/evidence-01.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(f"official fact {ref}; bare 123; 罚款 5 万元; 比例 12%\n", encoding="utf-8")
    import hashlib
    data = artifact.read_bytes()
    return {
        "schema_version": 2, "job_id": job_id, "ocr_refs": [ref],
        "evidence": [{
            "id": "E1", "job_id": job_id, "artifact": "output/evidence/evidence-01.md",
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "chars": len(data.decode("utf-8")),
            "source_tier": "一手官方", "eligible": eligible,
            "confidence": "high" if eligible else "low",
            "eligibility_reasons": [],
            "matches": [{"anchor": ref, "offset": data.decode("utf-8").find(ref)}],
            "original_url": "https://www.csrc.gov.cn/x",
            "final_url": "https://www.csrc.gov.cn/x",
        }],
        "rejected": [],
        "total_bytes": len(data),
        "candidate_parse_failed": False,
        "provider": "claude-cli",
    }


def _replace_manifest_artifact(job, manifest, text):
    import hashlib

    artifact = job / manifest["evidence"][0]["artifact"]
    artifact.write_text(text, encoding="utf-8")
    data = artifact.read_bytes()
    item = manifest["evidence"][0]
    item["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
    item["bytes"] = len(data)
    item["chars"] = len(data.decode("utf-8"))
    anchor = manifest["ocr_refs"][0]
    item["matches"] = [{"anchor": anchor, "offset": text.find(anchor)}]
    manifest["total_bytes"] = len(data)


def test_manifest_tamper_low_duplicate_and_cross_job(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    valid, errors = validate_manifest(job, "job", manifest)
    assert set(valid) == {"E1"} and errors == []
    (job / "output/evidence/evidence-01.md").write_text("tampered", encoding="utf-8")
    assert "artifact_tampered:E1" in validate_manifest(job, "job", manifest)[1]

    forged = _manifest(job, eligible=False)
    assert "derived_eligible_mismatch:E1" in validate_manifest(job, "job", forged)[1]
    duplicate = _manifest(job)
    duplicate["evidence"].append(dict(duplicate["evidence"][0]))
    duplicate_valid, duplicate_errors = validate_manifest(job, "job", duplicate)
    assert duplicate_valid == {} and "duplicate_evidence_id:E1" in duplicate_errors
    cross = _manifest(job, job_id="other")
    assert "cross_job_manifest" in validate_manifest(job, "job", cross)[1]


@pytest.mark.parametrize("items", [False, 0, "E1", {}])
def test_manifest_evidence_requires_an_actual_list_for_path_reader(tmp_path, items):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["evidence"] = items

    assert validate_manifest(job, "job", manifest) == ({}, ["invalid_manifest_items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [False, 0, "E1", {}])
async def test_reader_manifest_evidence_requires_an_actual_list(tmp_path, items):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["evidence"] = items

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    assert await validate_manifest_with_reader("job", manifest, reader) == (
        {}, ["invalid_manifest_items"],
    )


def test_manifest_path_escape_is_rejected(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    outside = tmp_path / "outside.md"
    outside.write_text("official fact 123\n", encoding="utf-8")
    manifest["evidence"][0]["artifact"] = "output/evidence/../../../outside.md"
    valid, errors = validate_manifest(job, "job", manifest)
    assert valid == {} and "invalid_artifact_path:E1" in errors


def test_unknown_numeric_mismatch_repeated_and_semantic_unverified(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    assert validate_citations(job, "job", "罚款 5 万元 [E1]。", manifest)["status"] == "valid"
    assert validate_citations(job, "job", "比例 12% [E1]。", manifest)["status"] == "valid"
    assert validate_citations(job, "job", "值为 999 [E1]。", manifest)["status"] == "invalid"
    assert validate_citations(job, "job", "值为 12 [E1]。", manifest)["status"] == "invalid"
    assert validate_citations(job, "job", "值为 123 [E2]。", manifest)["status"] == "invalid"
    assert validate_citations(job, "job", "值为 123 [E1]。", manifest)["status"] == "unverified"
    assert validate_citations(job, "job", "该结论成立 [E1]。", manifest)["status"] == "unverified"
    assert validate_citations(job, "job", "无 manifest 的 123 [E1]。", None)["status"] == "invalid"


@pytest.mark.parametrize("note", [
    "罚款 5 亿元 [E1]。",
    "罚款 -5 万元 [E1]。",
    "比例 12 个百分点 [E1]。",
])
def test_quantity_unit_sign_and_percent_substitution_fails_closed(tmp_path, note):
    job = tmp_path / "job"; job.mkdir()
    assert validate_citations(job, "job", note, _manifest(job))["status"] == "invalid"
    assert validate_citations(job, "job", "罚款 5 万元 [E1]。", _manifest(job))["status"] == "valid"
    assert validate_citations(job, "job", "比例 12% [E1]。", _manifest(job))["status"] == "valid"


@pytest.mark.parametrize(("source", "note"), [
    ("重量 10公斤", "重量 10吨 [E1]。"),
    ("重量 10kg", "重量 10ton [E1]。"),
    ("金额 100欧元", "金额 100美元 [E1]。"),
    ("金额 USD 100", "金额 EUR 100 [E1]。"),
    ("金额 100 USD", "金额 100 EUR [E1]。"),
    ("金额 $100", "金额 100欧元 [E1]。"),
])
def test_unknown_unit_and_currency_substitution_never_degrades_to_bare_number(
    tmp_path, source, note,
):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    _replace_manifest_artifact(job, manifest, f"{ref}; {source}\n")
    assert validate_citations(job, "job", note, manifest)["status"] == "invalid"


@pytest.mark.parametrize(("source", "note"), [
    ("金额 USD 100", "金额 100 USD [E1]。"),
    ("金额 100欧元", "金额 欧元100 [E1]。"),
    ("金额 $100", "金额 100美元 [E1]。"),
    ("金额 人民币100元", "金额 CNY 100元 [E1]。"),
])
def test_same_currency_still_requires_exact_claim_after_quantity_normalization(tmp_path, source, note):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    _replace_manifest_artifact(job, manifest, f"{ref}; {source}\n")
    result = validate_citations(job, "job", note, manifest)
    assert result["status"] == "unverified"
    assert result["items"][0]["errors"] == ["claim_not_found"]


@pytest.mark.parametrize(("source", "note", "expected_status", "error_prefix"), [
    ("甲公司被处罚，罚款 100 万元", "乙公司被处罚，罚款 100 万元 [E1]。",
     "unverified", "claim_not_found"),
    ("甲公司 2023 年营业收入 100 万元", "甲公司 2022 年营业收入 100 万元 [E1]。",
     "invalid", "quantity_mismatch:"),
    ("甲公司 2023 年罚款 100 万元", "甲公司 2023 年营业收入 100 万元 [E1]。",
     "unverified", "claim_not_found"),
])
def test_same_quantity_different_subject_year_or_metric_fails_closed(
    tmp_path, source, note, expected_status, error_prefix,
):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    _replace_manifest_artifact(job, manifest, f"{ref}; {source}\n")

    result = validate_citations(job, "job", note, manifest)

    assert result["status"] == expected_status
    assert result["items"][0]["errors"][0].startswith(error_prefix)


def test_complete_claim_with_subject_year_metric_and_quantity_is_valid(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    claim = "甲公司 2023 年营业收入 100 万元"
    _replace_manifest_artifact(job, manifest, f"{ref}; {claim}\n")

    result = validate_citations(job, "job", f"{claim} [E1]。", manifest)

    assert result["status"] == "valid"
    assert result["items"][0]["status"] == "valid"


@pytest.mark.parametrize(("source", "note"), [
    (
        "甲公司 2023 年营业收入如下。金额为 100 万元",
        "甲公司 2023 年营业收入如下。金额为 100 万元 [E1]。",
    ),
    (
        "甲公司 2023 年营业收入\n金额为 100 万元",
        "甲公司 2023 年营业收入\n金额为 100 万元 [E1]。",
    ),
    (
        "甲公司 2023 年营业收入\n金额为 100 万元",
        "## 甲公司 2023 年营业收入\n\n- 金额为 100 万元 [E1]。",
    ),
    (
        "主体 年份 营业收入 甲公司 2023 100 万元",
        "| 主体 | 年份 | 营业收入 |\n"
        "| --- | --- | --- |\n"
        "| 甲公司 | 2023 | 100 万元 [E1] |",
    ),
])
def test_citation_context_binds_sentence_line_list_and_table_titles(tmp_path, source, note):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    _replace_manifest_artifact(job, manifest, f"{ref}\n{source}\n")

    assert validate_citations(job, "job", note, manifest)["status"] == "valid"


@pytest.mark.parametrize(("source", "note", "status", "error"), [
    (
        "乙公司 2023 年营业收入如下。金额为 100 万元",
        "甲公司 2023 年营业收入如下。金额为 100 万元 [E1]。",
        "unverified", "claim_not_found",
    ),
    (
        "甲公司 2022 年营业收入如下。金额为 100 万元",
        "甲公司 2023 年营业收入如下。金额为 100 万元 [E1]。",
        "invalid", "quantity_mismatch:2023年",
    ),
    (
        "甲公司 2023 年净利润如下。金额为 100 万元",
        "甲公司 2023 年营业收入如下。金额为 100 万元 [E1]。",
        "unverified", "claim_not_found",
    ),
    (
        "乙公司 2023 年营业收入\n金额为 100 万元",
        "甲公司 2023 年营业收入\n金额为 100 万元 [E1]。",
        "unverified", "claim_not_found",
    ),
    (
        "甲公司 2022 年营业收入\n金额为 100 万元",
        "甲公司 2023 年营业收入\n金额为 100 万元 [E1]。",
        "invalid", "quantity_mismatch:2023年",
    ),
    (
        "甲公司 2023 年净利润\n金额为 100 万元",
        "甲公司 2023 年营业收入\n金额为 100 万元 [E1]。",
        "unverified", "claim_not_found",
    ),
    (
        "甲公司 2023 年净利润 金额为 100 万元",
        "## 甲公司 2023 年营业收入\n\n- 金额为 100 万元 [E1]。",
        "unverified", "claim_not_found",
    ),
    (
        "主体 年份 净利润 甲公司 2023 100 万元",
        "| 主体 | 年份 | 营业收入 |\n"
        "| --- | --- | --- |\n"
        "| 甲公司 | 2023 | 100 万元 [E1] |",
        "unverified", "claim_not_found",
    ),
])
def test_citation_context_rejects_subject_year_or_metric_mismatch(
    tmp_path, source, note, status, error,
):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    _replace_manifest_artifact(job, manifest, f"{ref}\n{source}\n")

    result = validate_citations(job, "job", note, manifest)

    assert result["status"] == status
    assert result["items"][0]["errors"] == [error]


def test_comma_clauses_and_multiple_citations_require_the_complete_sentence(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    _replace_manifest_artifact(job, manifest, f"{ref}; 甲公司罚款 100 万元\n")
    note = "甲公司罚款 100 万元 [E1]，乙公司营业收入 100 万元 [E1]。"

    result = validate_citations(job, "job", note, manifest)

    assert result["status"] == "unverified"
    assert [item["status"] for item in result["items"]] == [
        "unverified_semantic", "unverified_semantic",
    ]
    assert all(item["errors"] == ["claim_not_found"] for item in result["items"])


@pytest.mark.parametrize(("field", "value", "error"), [
    ("eligible", False, "derived_eligible_mismatch:E1"),
    ("eligible", 1, "derived_eligible_mismatch:E1"),
    ("confidence", "low", "derived_confidence_mismatch:E1"),
    ("source_tier", "外部来源", "derived_source_tier_mismatch:E1"),
    ("matches", [{"anchor": "〔2018〕88号", "offset": 0}], "derived_matches_mismatch:E1"),
    ("matches", [{"anchor": "〔2018〕88号", "offset": True}], "derived_matches_mismatch:E1"),
])
def test_manifest_self_reported_trust_fields_are_rederived(tmp_path, field, value, error):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["evidence"][0][field] = value
    valid, errors = validate_manifest(job, "job", manifest)
    assert valid == {} and error in errors


def test_non_authoritative_url_cannot_forge_high_eligibility(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    item = manifest["evidence"][0]
    item["final_url"] = "https://example.com/x"
    valid, errors = validate_manifest(job, "job", manifest)
    assert valid == {}
    assert "derived_source_tier_mismatch:E1" in errors
    assert "derived_eligible_mismatch:E1" in errors


@pytest.mark.parametrize(("original", "final"), [
    ("http://www.csrc.gov.cn/x", "http://www.csrc.gov.cn/x"),
    ("https://www.csrc.gov.cn/start", "http://www.csrc.gov.cn/final"),
    ("http://www.csrc.gov.cn/start", "https://www.csrc.gov.cn/final"),
])
def test_official_domain_requires_https_for_entire_redirect_chain(tmp_path, original, final):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["evidence"][0]["original_url"] = original
    manifest["evidence"][0]["final_url"] = final
    valid, errors = validate_manifest(job, "job", manifest)
    assert valid == {}
    assert "derived_source_tier_mismatch:E1" in errors
    assert "derived_eligible_mismatch:E1" in errors


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://169.254.169.254/latest", "http://224.0.0.1/x",
    "http://127.1/x", "http://2130706433/x", "http://0x7f000001/x",
    "http://localhost/x", "http://metadata.google.internal/x",
])
def test_private_or_special_literal_ip_is_rejected_before_fetch(url):
    with pytest.raises(ValueError, match="global|local-only"):
        SafeEvidenceFetcher(client=_Client([])).fetch(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "https://user:pass@example.com/x",
])
def test_unsafe_original_url_cannot_hide_behind_safe_final_url(tmp_path, url):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["evidence"][0]["original_url"] = url
    valid, errors = validate_manifest(job, "job", manifest)
    assert valid == {} and "unsafe_original_url:E1" in errors


@pytest.mark.asyncio
async def test_manifest_refs_must_equal_current_mechanical_refs_for_path_and_reader(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["ocr_refs"] = ["〔2020〕1号"]
    assert validate_manifest(job, "job", manifest) == ({}, ["manifest_ocr_refs_mismatch"])

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    assert await validate_manifest_with_reader("job", manifest, reader) == (
        {}, ["manifest_ocr_refs_mismatch"],
    )
    no_citation = validate_citations(job, "job", "智能笔记没有证据引用。", manifest)
    assert no_citation["status"] == "invalid"
    assert no_citation["manifest_errors"] == ["manifest_ocr_refs_mismatch"]


def test_manifest_schema_and_size_types_are_exact(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["schema_version"] = 2.0
    assert validate_manifest(job, "job", manifest) == ({}, ["legacy_or_invalid_schema"])
    manifest = _manifest(job)
    manifest["evidence"][0]["bytes"] = True
    valid, errors = validate_manifest(job, "job", manifest)
    assert valid == {} and "artifact_tampered:E1" in errors


def test_unreferenced_low_evidence_does_not_poison_valid_citation(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    low = dict(manifest["evidence"][0])
    first = job / low["artifact"]
    second = job / "output/evidence/evidence-02.md"
    second.write_bytes(first.read_bytes())
    low.update({
        "id": "E2", "artifact": "output/evidence/evidence-02.md",
        "eligible": False, "confidence": "low",
    })
    manifest["evidence"].append(low)
    manifest["total_bytes"] += low["bytes"]
    result = validate_citations(job, "job", "罚款 5 万元 [E1]。", manifest)
    assert result["status"] == "valid"
    assert "derived_eligible_mismatch:E2" in result["manifest_errors"]


def test_legacy_and_low_projection_never_exposes_link(tmp_path):
    legacy = project_evidence({"evidence": [{"id": "E1", "url": "javascript:alert(1)"}]})
    assert legacy["reliability_state"] == "legacy_unverified"
    assert legacy["evidence"][0]["link_safe"] is False and legacy["evidence"][0]["url"] is None
    low = project_evidence({"schema_version": 2, "evidence": [{"id": "E1", "eligible": False,
                             "confidence": "low", "final_url": "https://example.com"}]})
    assert low["evidence"][0]["link_safe"] is False and low["evidence"][0]["final_url"] is None


@pytest.mark.parametrize("items", [True, False, 1, 1.5, {}, "E1", None])
def test_projection_is_total_for_non_list_evidence_and_disables_links(items):
    projected = project_evidence(
        {"schema_version": 2, "evidence": items}, verified_ids={"E1"},
    )
    assert projected["evidence"] == []
    assert projected["manifest_state"] == "invalid"
    assert projected["reliability_state"] == "unreliable"


@pytest.mark.parametrize("manifest", [True, 1, 1.5, [], "manifest", None])
def test_projection_is_total_for_non_object_manifest(manifest):
    projected = project_evidence(manifest, verified_ids={"E1"})
    assert projected["evidence"] == []
    assert projected["reliability_state"] == "legacy_unverified"


def test_projection_distinguishes_verified_partial_invalid_and_legacy(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    verified = project_evidence(
        manifest, verified_ids={"E1"}, validation_errors=[],
    )
    assert verified["manifest_state"] == "verified"
    assert verified["evidence"][0]["verification_state"] == "verified"
    assert verified["evidence"][0]["link_safe"] is True

    partial_manifest = json.loads(json.dumps(manifest))
    invalid_item = dict(partial_manifest["evidence"][0])
    invalid_item.update({"id": "E2", "eligible": False, "confidence": "low"})
    partial_manifest["evidence"].append(invalid_item)
    partial = project_evidence(
        partial_manifest, verified_ids={"E1"},
        validation_errors=["derived_eligible_mismatch:E2", "ineligible_evidence:E2"],
    )
    assert partial["manifest_state"] == "partial"
    assert partial["evidence"][0]["link_safe"] is True
    assert partial["evidence"][1]["link_safe"] is False
    assert partial["evidence"][1]["verification_reasons"] == [
        "derived_eligible_mismatch:E2", "ineligible_evidence:E2",
    ]

    invalid = project_evidence(
        manifest, verified_ids=set(), validation_errors=["artifact_tampered:E1"],
    )
    assert invalid["manifest_state"] == "invalid"
    assert invalid["evidence"][0]["verification_reasons"] == ["artifact_tampered:E1"]
    assert invalid["evidence"][0]["artifact"] is None

    legacy = project_evidence(manifest | {"schema_version": 1}, validation_errors=[])
    assert legacy["manifest_state"] == "legacy"


@pytest.mark.parametrize("shape", [True, False, 1, 1.5, "x", {}, None])
def test_projection_normalizes_arbitrary_nested_evidence_item_shapes(shape):
    projected = project_evidence({
        "schema_version": 2,
        "evidence": [{
            "id": "E1", "title": shape, "publisher": shape,
            "source_tier": shape, "confidence": shape, "eligible": shape,
            "eligibility_reasons": shape, "matches": shape,
            "artifact": "output/evidence/forged.md",
            "final_url": "javascript:alert(1)",
        }],
    }, verified_ids=set(), validation_errors=["derived_matches_mismatch:E1"])
    item = projected["evidence"][0]
    assert item["title"] is None or isinstance(item["title"], str)
    assert item["publisher"] is None or isinstance(item["publisher"], str)
    assert isinstance(item["eligibility_reasons"], list)
    assert item["matches"] == []
    assert isinstance(item["verification_reasons"], list)
    assert item["artifact"] is None and item["final_url"] is None
    assert item["link_safe"] is False


def test_service_derives_case_match_and_stable_artifact(tmp_path):
    class Fetcher:
        def fetch(self, url):
            return {"original_url": url, "final_url": "https://www.csrc.gov.cn/case",
                    "resolved_addresses": ["203.0.113.9"], "redirects": 0,
                    "mime": "text/plain", "text": "处罚文号〔2018〕88号，罚款 123 万元。\n"}

    job = tmp_path / "job"; job.mkdir()
    manifest = materialize_evidence(
        job, "job", [{"url": "https://www.csrc.gov.cn/case", "title": "处罚决定"}],
        fetcher=Fetcher(), anchors=["〔2018〕88号"],
    )
    item = manifest["evidence"][0]
    assert item["id"] == "E1" and item["artifact"] == "output/evidence/evidence-01.md"
    assert item["eligible"] is True and item["confidence"] == "high"
    assert item["matches"] == [{"anchor": "〔2018〕88号", "offset": 4}]
    assert item["retrieved_at"] and item["sha256"].startswith("sha256:")
    assert (job / item["artifact"]).read_text().endswith("123 万元。\n")


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "http://www.csrc.gov.cn/case",
])
def test_verified_v2_unsafe_or_http_url_is_still_not_linked(tmp_path, url):
    projected = project_evidence({
        "schema_version": 2,
        "evidence": [{"id": "E1", "eligible": True, "confidence": "high",
                      "source_tier": "一手官方", "original_url": url,
                      "final_url": url}],
    }, verified_ids={"E1"}, validation_errors=[])
    assert projected["evidence"][0]["link_safe"] is False
    assert projected["evidence"][0]["final_url"] is None


@pytest.mark.parametrize("bad_ref", [
    "E0", "E01", "E-1", "Eabc", "E", "E 1", "E1 ", "E" + "9" * 5000,
])
def test_malformed_evidence_references_are_invalid_not_not_applicable(tmp_path, bad_ref):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    result = validate_citations(job, "job", f"伪造引用 [{bad_ref}]", manifest)
    assert result["status"] == "invalid"
    assert result["checked"] == 1
    assert result["items"][0]["errors"] == ["invalid_evidence_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ref", [
    "E0", "E01", "E-1", "Eabc", "E", "E 1", "E1 ", "E" + "9" * 5000,
])
async def test_reader_malformed_evidence_references_are_invalid(tmp_path, bad_ref):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    result = await validate_citations_with_reader(
        "job", f"伪造引用 [{bad_ref}]", manifest, reader,
    )
    assert result["status"] == "invalid"
    assert result["items"][0]["errors"] == ["invalid_evidence_id"]


@pytest.mark.parametrize("ordinary", [
    "[Example]", "[ERROR]",
    "[Example](https://example.com)", "[ERROR][ref]",
    "[E Example]", "[E = mc^2]",
])
def test_ordinary_markdown_brackets_are_not_evidence_citations(tmp_path, ordinary):
    job = tmp_path / "job"; job.mkdir()
    result = validate_citations(job, "job", f"普通正文 {ordinary}", _manifest(job))
    assert result["status"] == "not_applicable"
    assert result["checked"] == 0


@pytest.mark.asyncio
async def test_unknown_canonical_reference_remains_invalid(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    result = await validate_citations_with_reader(
        "job", "未知来源 123 [E2]", manifest, reader,
    )
    assert result["status"] == "invalid"


@pytest.mark.asyncio
async def test_evidence_id_is_bound_to_its_canonical_artifact_in_both_readers(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    item = manifest["evidence"][0]
    old = job / item["artifact"]
    replacement = job / "output/evidence/evidence-02.md"
    replacement.write_bytes(old.read_bytes())
    item["artifact"] = "output/evidence/evidence-02.md"

    valid, errors = validate_manifest(job, "job", manifest)
    assert valid == {}
    assert "evidence_artifact_id_mismatch:E1" in errors

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    valid, errors = await validate_manifest_with_reader("job", manifest, reader)
    assert valid == {}
    assert "evidence_artifact_id_mismatch:E1" in errors


def test_manifest_requires_exact_top_level_and_parse_success(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    expected = {
        "schema_version", "job_id", "ocr_refs", "evidence", "rejected",
        "total_bytes", "candidate_parse_failed", "provider",
    }
    assert set(manifest) == expected
    for field in expected:
        broken = dict(manifest)
        broken.pop(field)
        assert validate_manifest(job, "job", broken)[0] == {}
    assert validate_manifest(job, "job", {**manifest, "debug": True})[0] == {}

    failed = {**manifest, "evidence": [], "total_bytes": 0, "candidate_parse_failed": True}
    valid, errors = validate_manifest(job, "job", failed)
    assert valid == {}
    assert "candidate_parse_failed" in errors
    projected = project_evidence(failed, verified_ids=set(), validation_errors=errors)
    assert projected["manifest_state"] == "invalid"
    assert projected["reliability_state"] == "unreliable"


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_candidate_parse_failed_requires_exact_bool(tmp_path, value):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    manifest["candidate_parse_failed"] = value
    assert validate_manifest(job, "job", manifest)[0] == {}


def test_invalid_evidence_projection_clears_every_trust_bearing_field(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    projected = project_evidence(
        manifest, verified_ids=set(), validation_errors=["artifact_tampered:E1"],
    )
    item = projected["evidence"][0]
    assert item["verification_state"] == "invalid"
    assert item["source_tier"] is None
    assert item["confidence"] is None
    assert item["eligible"] is False
    assert item["matches"] == []
    assert item["artifact"] is None
    assert item["original_url"] is None and item["final_url"] is None
    assert item["link_safe"] is False


@pytest.mark.asyncio
async def test_item_size_gate_runs_before_decode_and_hash_in_both_readers(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    item = manifest["evidence"][0]
    data = b"x" * (MAX_EVIDENCE_BYTES + 1)
    path = job / item["artifact"]
    path.write_bytes(data)
    import hashlib
    item.update({
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "bytes": len(data), "chars": len(data),
    })
    manifest["total_bytes"] = len(data)

    assert "evidence_item_too_large:E1" in validate_manifest(job, "job", manifest)[1]

    async def reader(rel):
        target = job / rel
        return target.read_bytes() if target.exists() else None

    _, errors = await validate_manifest_with_reader("job", manifest, reader)
    assert "evidence_item_too_large:E1" in errors


@pytest.mark.asyncio
async def test_total_size_gate_rejects_individually_valid_items_in_both_readers(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    ref = manifest["ocr_refs"][0]
    items = []
    total = 0
    import hashlib
    for number in range(1, 6):
        text = (ref + "\n" + "x" * 900_000).encode()
        rel = f"output/evidence/evidence-{number:02d}.md"
        path = job / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text)
        total += len(text)
        items.append({
            **manifest["evidence"][0],
            "id": f"E{number}", "artifact": rel,
            "sha256": "sha256:" + hashlib.sha256(text).hexdigest(),
            "bytes": len(text), "chars": len(text.decode()),
            "matches": [{"anchor": ref, "offset": 0}],
        })
    assert total > MAX_TOTAL_EVIDENCE_BYTES
    manifest["evidence"] = items
    manifest["total_bytes"] = total

    assert "evidence_total_too_large" in validate_manifest(job, "job", manifest)[1]

    async def reader(rel):
        target = job / rel
        return target.read_bytes() if target.exists() else None

    _, errors = await validate_manifest_with_reader("job", manifest, reader)
    assert "evidence_total_too_large" in errors


@pytest.mark.asyncio
async def test_manifest_item_count_and_declared_size_fail_before_storage_reads(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    template = manifest["evidence"][0]
    manifest["evidence"] = [
        {**template, "id": f"E{number}",
         "artifact": f"output/evidence/evidence-{number:02d}.md"}
        for number in range(1, MAX_EVIDENCE_ITEMS + 2)
    ]
    calls = []

    async def reader(rel):
        calls.append(rel)
        return None

    valid, errors = await validate_manifest_with_reader("job", manifest, reader)
    assert valid == {}
    assert "too_many_evidence_items" in errors
    assert calls == []

    manifest = _manifest(job)
    manifest["evidence"][0]["bytes"] = MAX_EVIDENCE_BYTES + 1
    manifest["total_bytes"] = MAX_EVIDENCE_BYTES + 1
    calls.clear()
    valid, errors = await validate_manifest_with_reader("job", manifest, reader)
    assert valid == {}
    assert "evidence_item_too_large:E1" in errors
    assert calls == []


@pytest.mark.asyncio
async def test_storage_reader_stops_after_first_actual_oversized_artifact(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    first = manifest["evidence"][0]
    second = {
        **first, "id": "E2", "artifact": "output/evidence/evidence-02.md",
    }
    manifest["evidence"] = [first, second]
    manifest["total_bytes"] = first["bytes"] + second["bytes"]
    mechanical = (job / "output/notes_mechanical.md").read_bytes()
    calls = []

    async def reader(rel):
        calls.append(rel)
        if rel == "output/notes_mechanical.md":
            return mechanical
        if rel == first["artifact"]:
            return b"x" * (MAX_EVIDENCE_BYTES + 1)
        raise AssertionError("reader must stop before the second artifact")

    valid, errors = await validate_manifest_with_reader("job", manifest, reader)
    assert valid == {}
    assert "evidence_item_too_large:E1" in errors
    assert calls == ["output/notes_mechanical.md", first["artifact"]]


@pytest.mark.asyncio
async def test_extreme_evidence_id_is_total_in_manifest_and_api_projection(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    manifest = _manifest(job)
    huge_id = "E" + "9" * 5000
    manifest["evidence"][0]["id"] = huge_id
    manifest["evidence"][0]["artifact"] = "output/evidence/evidence-01.md"

    assert validate_manifest(job, "job", manifest)[0] == {}

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    valid, errors = await validate_manifest_with_reader("job", manifest, reader)
    assert valid == {}
    assert "invalid_evidence_id" in errors
    projected = project_evidence(manifest, verified_ids=set(), validation_errors=errors)
    assert projected["reliability_state"] == "unreliable"
    assert projected["evidence"][0]["verification_state"] == "invalid"
    assert projected["evidence"][0]["artifact"] is None
