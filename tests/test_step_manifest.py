"""step manifest v1 契约测试:canonical 稳定性、schema fail-closed、复用判定矩阵。"""

import json

import pytest

from shared.step_manifest import (
    INTERNAL_NAMESPACE,
    MANIFEST_MAX_OUTPUTS,
    ManifestError,
    ObservedOutput,
    canonical_digest,
    canonical_json_bytes,
    check_reusable,
    compute_input_digest,
    is_internal_namespace_path,
    manifest_digest,
    manifest_object_key,
    manifest_relative_path,
    validate_manifest,
)


HEX_A = "sha256:" + "a" * 64
HEX_B = "sha256:" + "b" * 64
HEX_C = "sha256:" + "c" * 64
HEX_D = "sha256:" + "d" * 64
HEX_E = "sha256:" + "e" * 64
HEX_F = "sha256:" + "f" * 64


def make_fingerprints() -> dict[str, str]:
    return {"job": HEX_A, "pt_abcd:input/metadata.json": HEX_B}


def make_manifest() -> dict:
    fingerprints = make_fingerprints()
    return {
        "format": "flori-step-manifest",
        "format_version": 1,
        "job_id": "jobs_live_001",
        "scope": {
            "kind": "part",
            "scope_key": "part:pt_abcd",
            "part_id": "pt_abcd",
            "part_index": 2,
        },
        "step": "01_download",
        "outcome": "done",
        "execution": {
            "exec_id": "exec_xxx",
            "job_generation": 7,
            "attempt": 1,
            "started_at": "2026-07-18T04:00:00Z",
            "committed_at": "2026-07-18T04:12:00+00:00",
            "duration_sec": 720.0,
        },
        "compatibility": {
            "input_fingerprints": fingerprints,
            "input_digest": compute_input_digest(fingerprints),
            "definition_digest": HEX_D,
        },
        "producer": {
            "flori_version": "2.1.0",
            "build_sha": "ac5d8c6",
            "worker_id": "nas-dl-1",
            "runner": "subprocess",
            "image": "flori/step-base",
            "image_digest": None,
            "tool_versions": {"yt-dlp": "2026.6.1"},
        },
        "outputs": [
            {
                "path": "input/metadata.json",
                "size_bytes": 2048,
                "sha256": HEX_C,
                "media_type": "application/json",
            },
            {
                "path": "input/source.mp4",
                "size_bytes": 123456789,
                "sha256": HEX_B,
                "media_type": "video/mp4",
            },
        ],
        "skip": None,
    }


def make_skipped_manifest() -> dict:
    manifest = make_manifest()
    manifest["outcome"] = "skipped"
    manifest["outputs"] = []
    manifest["skip"] = {
        "reason_code": "rule_false",
        "rule_digest": HEX_E,
        "condition_digest": HEX_F,
    }
    return manifest


# canonical JSON


def test_canonical_bytes_are_sorted_compact_utf8() -> None:
    encoded = canonical_json_bytes({"b": 1, "a": [1, 2], "z": "中文"})
    assert encoded == '{"a":[1,2],"b":1,"z":"中文"}'.encode("utf-8")


def test_canonical_digest_ignores_key_insertion_order() -> None:
    first = {"a": 1, "nested": {"x": [1, 2], "y": "v"}, "b": 2}
    second = {"b": 2, "a": 1, "nested": {"y": "v", "x": [1, 2]}}
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_digest(first) == canonical_digest(second)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_rejects_nan_infinity(value: float) -> None:
    with pytest.raises(ManifestError):
        canonical_json_bytes({"v": value})


@pytest.mark.parametrize("value", [{1: "x"}, {"k": {1, 2}}, {"k": object()}, {"k": b"bytes"}])
def test_canonical_rejects_non_json_types(value) -> None:
    with pytest.raises(ManifestError):
        canonical_json_bytes(value)


def test_manifest_digest_stable_across_field_order() -> None:
    manifest = make_manifest()
    reordered = {key: manifest[key] for key in reversed(list(manifest))}
    reordered["execution"] = {
        key: manifest["execution"][key] for key in reversed(list(manifest["execution"]))
    }
    assert validate_manifest(manifest) == validate_manifest(reordered)
    assert manifest_digest(manifest) == manifest_digest(reordered)


def test_manifest_digest_json_round_trip_stable() -> None:
    manifest = make_manifest()
    round_tripped = json.loads(json.dumps(manifest, ensure_ascii=False))
    assert manifest_digest(manifest) == manifest_digest(round_tripped)


def test_canonical_rejects_lone_surrogate_as_manifest_error() -> None:
    # json.loads 合法接受 \ud800 转义;必须归一为 ManifestError,不能漏出 UnicodeEncodeError。
    surrogate = json.loads('"\\ud800"')
    with pytest.raises(ManifestError):
        canonical_json_bytes({"v": surrogate})
    with pytest.raises(ManifestError):
        canonical_json_bytes({surrogate: "v"})


def test_canonical_rejects_astronomical_int() -> None:
    with pytest.raises(ManifestError):
        canonical_json_bytes({"v": 10 ** 5000})


def test_canonical_normalizes_negative_zero() -> None:
    assert canonical_json_bytes({"v": -0.0}) == b'{"v":0.0}'
    assert canonical_digest({"v": -0.0}) == canonical_digest({"v": 0.0})


def test_internal_namespace_predicate() -> None:
    assert INTERNAL_NAMESPACE == ".flori"
    assert is_internal_namespace_path(".flori/steps/01_download/manifest.json")
    assert is_internal_namespace_path("parts/pt_x/.FLORI/steps/x/manifest.json")
    assert is_internal_namespace_path("a/.Flori/b")
    assert not is_internal_namespace_path("input/source.mp4")
    assert not is_internal_namespace_path("output/flori/x")


# 路径模板


def test_manifest_paths_follow_fixed_templates() -> None:
    assert manifest_relative_path("job", "11_smart") == ".flori/steps/11_smart/manifest.json"
    assert (
        manifest_relative_path("part:pt_abcd", "01_download")
        == "parts/pt_abcd/.flori/steps/01_download/manifest.json"
    )
    assert (
        manifest_object_key("jobs_live_001", "part:pt_abcd", "01_download")
        == "jobs_live_001/parts/pt_abcd/.flori/steps/01_download/manifest.json"
    )


@pytest.mark.parametrize("job_id", ["", "a/b", "a..b", "a\x00b", "a b", "../x"])
def test_object_key_rejects_bad_job_id(job_id: str) -> None:
    with pytest.raises(ManifestError):
        manifest_object_key(job_id, "job", "01_download")


@pytest.mark.parametrize(
    ("scope_key", "step"),
    [
        ("part:../pt", "01_download"),
        ("part:a/b", "01_download"),
        ("part:pt\x00", "01_download"),
        ("other:pt_abcd", "01_download"),
        ("job", "01/download"),
        ("job", "01 download"),
        ("job", ""),
    ],
)
def test_manifest_path_rejects_bad_scope_or_step(scope_key: str, step: str) -> None:
    with pytest.raises(ManifestError):
        manifest_relative_path(scope_key, step)


def test_object_key_unknown_part_fail_closed() -> None:
    calls: list[tuple[str, str]] = []

    def is_known_part(job_id: str, part_id: str) -> bool:
        calls.append((job_id, part_id))
        return False

    with pytest.raises(ManifestError, match="unknown part"):
        manifest_object_key(
            "jobs_live_001", "part:pt_abcd", "01_download", is_known_part=is_known_part,
        )
    assert calls == [("jobs_live_001", "pt_abcd")]
    # Job scope 没有 Part 身份,不得触发 Part 存在性查询。
    key = manifest_object_key(
        "jobs_live_001", "job", "11_smart",
        is_known_part=lambda *_: pytest.fail("job scope must not consult parts"),
    )
    assert key == "jobs_live_001/.flori/steps/11_smart/manifest.json"


# schema 正例


def test_valid_done_manifest_passes_and_digests() -> None:
    encoded = validate_manifest(make_manifest())
    assert isinstance(encoded, bytes)
    digest = manifest_digest(make_manifest())
    assert digest.startswith("sha256:") and len(digest) == 71


def test_valid_skipped_manifest_passes() -> None:
    validate_manifest(make_skipped_manifest())
    nullable = make_skipped_manifest()
    nullable["skip"]["rule_digest"] = None
    nullable["skip"]["condition_digest"] = None
    validate_manifest(nullable)


def test_valid_job_scope_manifest_passes() -> None:
    manifest = make_manifest()
    manifest["scope"] = {"kind": "job", "scope_key": "job", "part_id": None, "part_index": None}
    manifest["step"] = "09_merge_parts"
    validate_manifest(manifest)


def test_producer_kind_allows_legacy_backfill_marker() -> None:
    manifest = make_manifest()
    manifest["producer"]["kind"] = "legacy_done_backfill"
    validate_manifest(manifest)


def test_validate_manifest_checks_part_against_injected_facts() -> None:
    with pytest.raises(ManifestError, match="unknown part"):
        validate_manifest(make_manifest(), is_known_part=lambda job_id, part_id: False)
    validate_manifest(make_manifest(), is_known_part=lambda job_id, part_id: True)


# schema 拒绝矩阵


def _set(path: str, value):
    def mutate(manifest: dict) -> None:
        node = manifest
        *parents, leaf = path.split(".")
        for parent in parents:
            node = node[parent]
        node[leaf] = value
    return mutate


def _output0(field: str, value):
    def mutate(manifest: dict) -> None:
        manifest["outputs"][0][field] = value
    return mutate


REJECTED_MUTATIONS = {
    "unknown_top_key": lambda m: m.update(extra=1),
    "missing_top_key": lambda m: m.pop("skip"),
    "bad_format": _set("format", "flori-manifest"),
    "format_version_str": _set("format_version", "1"),
    "format_version_bool": _set("format_version", True),
    "format_version_2": _set("format_version", 2),
    "bad_outcome": _set("outcome", "failed"),
    "bad_job_id": _set("job_id", "jobs/live"),
    "scope_kind_unknown": _set("scope.kind", "session"),
    "scope_job_with_part": lambda m: m.update(scope={
        "kind": "job", "scope_key": "job", "part_id": "pt_abcd", "part_index": 1,
    }),
    "scope_part_mismatch": _set("scope.part_id", "pt_other"),
    "scope_part_index_zero": _set("scope.part_index", 0),
    "scope_part_index_bool": _set("scope.part_index", True),
    "scope_key_traversal": lambda m: m.update(scope={
        "kind": "part", "scope_key": "part:../x", "part_id": "../x", "part_index": 1,
    }),
    "step_with_slash": _set("step", "01/download"),
    "exec_id_empty": _set("execution.exec_id", ""),
    "generation_negative": _set("execution.job_generation", -1),
    "generation_bool": _set("execution.job_generation", True),
    "attempt_zero": _set("execution.attempt", 0),
    "started_naive": _set("execution.started_at", "2026-07-18T04:00:00"),
    "started_non_utc": _set("execution.started_at", "2026-07-18T04:00:00+08:00"),
    "started_garbage": _set("execution.started_at", "yesterday"),
    "committed_bad_calendar": _set("execution.committed_at", "2026-13-40T04:00:00Z"),
    "duration_negative": _set("execution.duration_sec", -1),
    "duration_nan": _set("execution.duration_sec", float("nan")),
    "input_digest_inconsistent": _set("compatibility.input_digest", HEX_A),
    "input_digest_uppercase": _set(
        "compatibility.input_digest", "sha256:" + "A" * 64,
    ),
    "definition_digest_short": _set("compatibility.definition_digest", "sha256:" + "d" * 63),
    "definition_digest_no_prefix": _set("compatibility.definition_digest", "d" * 64),
    "fingerprints_not_dict": _set("compatibility.input_fingerprints", ["job"]),
    "fingerprint_int_value": _set("compatibility.input_fingerprints", {"job": 1}),
    "fingerprint_secret_value": _set(
        "compatibility.input_fingerprints", {"job": "AKIA" + "A" * 16},
    ),
    "fingerprint_secret_key": _set(
        "compatibility.input_fingerprints", {"api_key": HEX_A},
    ),
    "fingerprint_control_char": _set(
        "compatibility.input_fingerprints", {"job\n": HEX_A},
    ),
    "producer_missing_key": lambda m: m["producer"].pop("tool_versions"),
    "producer_unknown_key": lambda m: m["producer"].update(hostname="nas"),
    "producer_tool_version_int": _set("producer.tool_versions", {"yt-dlp": 1}),
    "producer_surrogate": _set("producer.worker_id", "nas-\ud800-1"),
    "fingerprint_surrogate_value": _set(
        "compatibility.input_fingerprints", {"job": "sha256:\ud800"},
    ),
    "outputs_not_list": _set("outputs", {}),
    "output_entry_not_dict": _set("outputs", ["input/source.mp4"]),
    "output_unknown_key": lambda m: m["outputs"][0].update(mode=0o644),
    "output_traversal": _output0("path", "../escape.mp4"),
    "output_absolute": _output0("path", "/etc/passwd"),
    "output_empty_segment": _output0("path", "input//source.mp4"),
    "output_dot_segment": _output0("path", "input/./source.mp4"),
    "output_dotdot_only": _output0("path", ".."),
    "output_backslash": _output0("path", "input\\source.mp4"),
    "output_nul": _output0("path", "input/\x00.mp4"),
    "output_manifest_self_reference": _output0(
        "path", ".flori/steps/01_download/manifest.json",
    ),
    "output_internal_namespace": _output0("path", ".flori/staging/x"),
    "output_size_negative": _output0("size_bytes", -1),
    "output_size_bool": _output0("size_bytes", True),
    "output_size_float": _output0("size_bytes", 1.5),
    "output_size_over_int64": _output0("size_bytes", 2 ** 63),
    "output_size_astronomical": _output0("size_bytes", 10 ** 5000),
    "generation_over_int64": _set("execution.job_generation", 2 ** 63),
    "output_media_type_surrogate": _output0("media_type", "video/\ud800"),
    "output_flori_uppercase": _output0("path", ".FLORI/steps/x/manifest.json"),
    "output_flori_nested_case": _output0("path", "assets/.Flori/x"),
    "output_sha_uppercase": _output0("sha256", "sha256:" + "C" * 64),
    "output_sha_short": _output0("sha256", "sha256:" + "c" * 63),
    "output_media_type_empty": _output0("media_type", ""),
    "outputs_duplicate": lambda m: m.update(
        outputs=[m["outputs"][0], dict(m["outputs"][0])],
    ),
    "outputs_unsorted": lambda m: m.update(outputs=list(reversed(m["outputs"]))),
    "done_with_skip_block": _set("skip", {
        "reason_code": "rule_false", "rule_digest": None, "condition_digest": None,
    }),
}


@pytest.mark.parametrize("mutate", REJECTED_MUTATIONS.values(), ids=REJECTED_MUTATIONS.keys())
def test_schema_rejects_contract_violations(mutate) -> None:
    manifest = make_manifest()
    mutate(manifest)
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


SKIPPED_REJECTED_MUTATIONS = {
    "skipped_with_outputs": lambda m: m.update(outputs=[{
        "path": "input/x", "size_bytes": 1, "sha256": HEX_B, "media_type": None,
    }]),
    "skipped_without_skip": _set("skip", None),
    "skip_unknown_key": lambda m: m["skip"].update(note="x"),
    "skip_reason_no_worker": _set("skip.reason_code", "no_worker"),
    "skip_reason_bad_charset": _set("skip.reason_code", "Rule-False"),
    "skip_rule_digest_bad": _set("skip.rule_digest", "md5:abc"),
}


@pytest.mark.parametrize(
    "mutate", SKIPPED_REJECTED_MUTATIONS.values(), ids=SKIPPED_REJECTED_MUTATIONS.keys(),
)
def test_schema_rejects_skipped_contract_violations(mutate) -> None:
    manifest = make_skipped_manifest()
    mutate(manifest)
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize(
    "path",
    [
        "parts/pt_abcd/input/source.mp4",
        "parts/pt_abcd/.flori/steps/01_download/manifest.json",
    ],
)
def test_job_scope_rejects_part_scoped_output_paths(path: str) -> None:
    # Job scope 越界声明 Part 领地会绕开 Part manifest 的所有权与失效边界。
    manifest = make_manifest()
    manifest["scope"] = {"kind": "job", "scope_key": "job", "part_id": None, "part_index": None}
    manifest["step"] = "09_merge_parts"
    manifest["outputs"] = [{
        "path": path, "size_bytes": 1, "sha256": HEX_B, "media_type": None,
    }]
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_part_scope_allows_normal_relative_paths() -> None:
    # Part scope 的 outputs 相对 Part 根,input/... 正常通过(即 make_manifest 本身)。
    validate_manifest(make_manifest())


def test_outputs_over_limit_rejected_not_truncated() -> None:
    manifest = make_manifest()
    entry = {"path": "input/x", "size_bytes": 1, "sha256": HEX_B, "media_type": None}
    manifest["outputs"] = [entry] * (MANIFEST_MAX_OUTPUTS + 1)
    with pytest.raises(ManifestError, match="exceed"):
        validate_manifest(manifest)


def test_oversize_manifest_rejected_not_truncated() -> None:
    manifest = make_manifest()
    manifest["outputs"] = [{
        "path": "output/" + "a" * (1024 * 1024 + 64),
        "size_bytes": 1,
        "sha256": HEX_B,
        "media_type": None,
    }]
    with pytest.raises(ManifestError, match="canonical size"):
        validate_manifest(manifest)


# input digest


def test_compute_input_digest_order_insensitive() -> None:
    forward = {"a": HEX_A, "b": HEX_B}
    backward = {"b": HEX_B, "a": HEX_A}
    assert compute_input_digest(forward) == compute_input_digest(backward)
    assert compute_input_digest(forward) != compute_input_digest({"a": HEX_A})


@pytest.mark.parametrize(
    "fingerprints",
    [
        "not-a-dict",
        {"job": 1},
        {"": HEX_A},
        {"k" * 301: HEX_A},
        {"job": "v" * 2001},
        {"job": "ghp_" + "a" * 24},
        {"registration_token": HEX_A},
        {"job\t": HEX_A},
        {f"k{i}": HEX_A for i in range(10_001)},
    ],
)
def test_compute_input_digest_fail_closed(fingerprints) -> None:
    with pytest.raises(ManifestError):
        compute_input_digest(fingerprints)


# reusable 判定矩阵


def observer_for(manifest: dict):
    table = {
        entry["path"]: ObservedOutput(entry["size_bytes"], entry["sha256"])
        for entry in manifest["outputs"]
    }
    return lambda path: table.get(path)


def green_kwargs(manifest: dict) -> dict:
    return {
        "job_id": "jobs_live_001",
        "scope_key": "part:pt_abcd",
        "step": "01_download",
        "current_input_fingerprints": make_fingerprints(),
        "current_definition_digest": HEX_D,
        "observe_output": observer_for(manifest),
        "dependencies": [("00_seed", True)],
    }


def test_reusable_all_green() -> None:
    manifest = make_manifest()
    decision = check_reusable(manifest, **green_kwargs(manifest))
    assert decision.reusable is True
    assert decision.reason is None


def test_reusable_rejects_invalid_schema_first() -> None:
    manifest = make_manifest()
    manifest["outcome"] = "failed"
    decision = check_reusable(manifest, **green_kwargs(make_manifest()))
    assert decision.reusable is False
    assert decision.reason.startswith("manifest_invalid:")


def test_reusable_surrogate_manifest_returns_decision_not_exception() -> None:
    # 一份坏 manifest 不能崩掉 reconcile 循环:surrogate 必须落进 manifest_invalid 分支。
    manifest = make_manifest()
    manifest["producer"]["worker_id"] = json.loads('"nas-\\ud800"')
    decision = check_reusable(manifest, **green_kwargs(make_manifest()))
    assert decision.reusable is False
    assert decision.reason.startswith("manifest_invalid:")


def test_reusable_output_missing() -> None:
    manifest = make_manifest()
    kwargs = green_kwargs(manifest)
    kwargs["observe_output"] = lambda path: None
    decision = check_reusable(manifest, **kwargs)
    assert decision.reusable is False
    assert decision.reason == "output_missing:input/metadata.json"


def test_reusable_output_size_and_hash_mismatch() -> None:
    manifest = make_manifest()
    kwargs = green_kwargs(manifest)
    kwargs["observe_output"] = lambda path: ObservedOutput(1, manifest["outputs"][0]["sha256"]) \
        if path == "input/metadata.json" else observer_for(manifest)(path)
    assert check_reusable(manifest, **kwargs).reason == "output_size_mismatch:input/metadata.json"
    kwargs["observe_output"] = lambda path: ObservedOutput(
        manifest["outputs"][0]["size_bytes"], HEX_F,
    ) if path == "input/metadata.json" else observer_for(manifest)(path)
    assert check_reusable(manifest, **kwargs).reason == "output_sha256_mismatch:input/metadata.json"


def test_reusable_input_and_definition_digest_mismatch() -> None:
    manifest = make_manifest()
    kwargs = green_kwargs(manifest)
    kwargs["current_input_fingerprints"] = {"job": HEX_F}
    assert check_reusable(manifest, **kwargs).reason == "input_digest_mismatch"
    kwargs = green_kwargs(manifest)
    kwargs["current_definition_digest"] = HEX_E
    assert check_reusable(manifest, **kwargs).reason == "definition_digest_mismatch"


def test_reusable_dependency_not_reusable() -> None:
    manifest = make_manifest()
    kwargs = green_kwargs(manifest)
    kwargs["dependencies"] = [("00_seed", True), ("01_upstream", False)]
    assert check_reusable(manifest, **kwargs).reason == "dependency_not_reusable:01_upstream"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("job_id", "jobs_other_001", "identity_mismatch:job_id"),
        ("scope_key", "job", "identity_mismatch:scope_key"),
        ("step", "02_whisper", "identity_mismatch:step"),
    ],
)
def test_reusable_identity_mismatch(field: str, value: str, reason: str) -> None:
    manifest = make_manifest()
    kwargs = green_kwargs(manifest)
    kwargs[field] = value
    assert check_reusable(manifest, **kwargs).reason == reason


def test_reusable_reports_first_failure_in_formula_order() -> None:
    # 输出缺失和输入摘要不匹配同时发生时,按 §2.4 公式顺序先报输出。
    manifest = make_manifest()
    kwargs = green_kwargs(manifest)
    kwargs["observe_output"] = lambda path: None
    kwargs["current_input_fingerprints"] = {"job": HEX_F}
    assert check_reusable(manifest, **kwargs).reason == "output_missing:input/metadata.json"


def test_reusable_skipped_manifest_needs_no_outputs() -> None:
    manifest = make_skipped_manifest()
    kwargs = green_kwargs(manifest)
    kwargs["observe_output"] = lambda path: pytest.fail("skipped manifest has no outputs")
    decision = check_reusable(manifest, **kwargs)
    assert decision.reusable is True


def test_reusable_caller_contract_errors_raise() -> None:
    manifest = make_manifest()
    kwargs = green_kwargs(manifest)
    kwargs["current_definition_digest"] = "not-a-digest"
    with pytest.raises(ManifestError):
        check_reusable(manifest, **kwargs)
    kwargs = green_kwargs(manifest)
    kwargs["current_input_fingerprints"] = {"job": "xoxb-" + "a" * 12}
    with pytest.raises(ManifestError):
        check_reusable(manifest, **kwargs)
