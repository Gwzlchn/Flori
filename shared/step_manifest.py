"""step manifest v1 契约纯逻辑:canonical JSON、schema 校验、路径模板与复用判定。

manifest 是步骤完成的持久权威(设计稿 §2.1),本模块只做无 IO 的契约层:
Storage/Redis/DB 集成由后续单元实现。所有校验 fail-closed,发现违规抛 ManifestError,
不做静默截断或修正。

集成分工约定:
- definition_digest 由 worker 侧从归一化模板 config 计算(shared.step_semantic_definition),
  随 step_cfg 注入步骤子进程;子进程不重算。
- check_reusable 的 dependencies 参数仅 scheduler 对账/恢复路径使用(DAG 闭包在中心可见);
  步骤子进程恒传空,依赖新鲜度由调度序保证。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Mapping

# _SEGMENT_RE 是 scope 身份的单一来源;job_id/part_id/step 名共用同一约束,
# 复制正则会造成两处漂移,故这里直接引用内部常量。
from .step_scope import (
    JOB_SCOPE,
    _SEGMENT_RE,
    execution_step_key,
    part_id_from_scope,
)


MANIFEST_FORMAT = "flori-step-manifest"
MANIFEST_FORMAT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

OUTCOME_DONE = "done"
OUTCOME_SKIPPED = "skipped"

# 契约上限(设计稿 §2.3):超限必须显式拆步骤,不允许静默截断。
MANIFEST_MAX_CANONICAL_BYTES = 1024 * 1024
MANIFEST_MAX_OUTPUTS = 100_000

# input fingerprints 的有界约束(设计稿 §2.4"有界、JSON-safe、无密钥")。
# 多 Part reduce 步(09_merge_parts)按 Part 数线性增长,上限给足余量但仍封顶。
MAX_INPUT_FINGERPRINT_ENTRIES = 10_000
MAX_INPUT_FINGERPRINT_KEY_CHARS = 300
MAX_INPUT_FINGERPRINT_VALUE_CHARS = 2_000
MAX_INPUT_FINGERPRINT_CANONICAL_BYTES = 512 * 1024

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# RFC3339 且必须显式 UTC;先正则限定字面形态,再用 fromisoformat 验日历合法性。
_RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|\+00:00)$"
)
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# 环境性 skip 不是可持久复用的业务完成事实(设计稿 §2.8),schema 层直接拒绝。
NON_DURABLE_SKIP_REASONS = frozenset({"no_worker"})

# manifest 内部命名空间:manifest 本体与 staging 都在 .flori/ 下,
# outputs 指向其中任何路径都构成自引用或越权,一律拒绝。公开常量与谓词供 Storage 集成复用。
INTERNAL_NAMESPACE = ".flori"

# JSON 可安全表示的整数上界(int64):防超大整数进入 canonical 层与下游消费方。
MAX_SAFE_INT = 2 ** 63 - 1

# 密钥样式扫描(设计稿 §2.12"manifest 不泄密"):宁可误杀,fail-closed。
# token 用前后非字母数字的 lookaround:拦 "registration_token",放过 "tokens.json"。
_SECRET_NAME_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|passwd|password|credential|authorization"
    r"|cookie|private[_-]?key|(?<![a-z0-9])token(?![a-z0-9]))"
)
_SECRET_VALUE_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{17,}\.eyJ[A-Za-z0-9_-]{10,}"),
)

_TOP_KEYS = frozenset({
    "format", "format_version", "job_id", "scope", "step", "outcome",
    "execution", "compatibility", "producer", "outputs", "skip",
})
_SCOPE_KEYS = frozenset({"kind", "scope_key", "part_id", "part_index"})
_EXECUTION_KEYS = frozenset({
    "exec_id", "job_generation", "attempt", "started_at", "committed_at",
    "duration_sec",
})
_COMPAT_KEYS = frozenset({"input_fingerprints", "input_digest", "definition_digest"})
_PRODUCER_REQUIRED_KEYS = frozenset({
    "flori_version", "build_sha", "worker_id", "runner", "image",
    "image_digest", "tool_versions",
})
# producer.kind 供 legacy backfill 标注来源(设计稿 §2.11 阶段 B),正常执行可省略。
_PRODUCER_OPTIONAL_KEYS = frozenset({"kind"})
_OUTPUT_KEYS = frozenset({"path", "size_bytes", "sha256", "media_type"})
_SKIP_KEYS = frozenset({"reason_code", "rule_digest", "condition_digest"})


class ManifestError(ValueError):
    """manifest 契约违规;统一 fail-closed,消费方不得捕获后降级放行。"""


def _has_forbidden_codepoints(text: str) -> bool:
    """控制字符或 lone surrogate 即为禁。surrogate 能通过 json.loads(\\ud800 转义)进入
    Python str,却在 encode('utf-8') 时抛 UnicodeEncodeError 而非 ManifestError,
    击穿 fail-closed;必须在字符串校验层就拒绝。"""
    return any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in text
    )


def is_internal_namespace_path(path: str) -> bool:
    """相对路径任一段命中 .flori(大小写不敏感)即属内部命名空间;供 Storage 集成复用。"""
    return any(
        segment.casefold() == INTERNAL_NAMESPACE
        for segment in path.split("/") if segment
    )


def ensure_no_secret_name(name: str, field: str) -> None:
    """拒绝密钥样式的键名;供 fingerprints 与语义定义共用。"""
    if _SECRET_NAME_RE.search(name):
        raise ManifestError(f"{field}: key looks like credential material: {name!r}")


def ensure_no_secret_text(text: str, field: str) -> None:
    """拒绝密钥样式的字符串值;供 fingerprints 与语义定义共用。"""
    for pattern in _SECRET_VALUE_RES:
        if pattern.search(text):
            raise ManifestError(f"{field}: value looks like credential material")


def _canonical_ready(value: object, field: str) -> object:
    """canonical 化前置:校验 JSON 原生类型并返回规范化副本。

    dict 键必须是 str;float 必须有限且 -0.0 归一为 0.0(加 +0.0 消除符号位,
    否则同值 manifest 会产生两个摘要);str 拒绝 lone surrogate。
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise ManifestError(f"{field}: lone surrogate is not canonical JSON")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ManifestError(f"{field}: NaN/Infinity is not canonical JSON")
        return value + 0.0
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ManifestError(f"{field}: dict key must be str, got {type(key).__name__}")
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in key):
                raise ManifestError(f"{field}: lone surrogate in dict key")
            result[key] = _canonical_ready(item, f"{field}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _canonical_ready(item, f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ManifestError(f"{field}: unsupported JSON type {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """规范化 JSON 字节:UTF-8、sort_keys、紧凑分隔符、禁 NaN/Infinity。

    双保险:_canonical_ready 已拒绝 surrogate/超类型,dumps/encode 仍可能因
    Python 侧限制(如 int 转字符串位数上限)抛 ValueError,一律归一为 ManifestError。
    """
    ready = _canonical_ready(value, "value")
    try:
        return json.dumps(
            ready, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise ManifestError(f"value: not canonical JSON serializable: {exc}") from exc


def canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def validate_digest(value: object, field: str) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        raise ManifestError(f"{field}: digest must be lowercase sha256:{{64hex}}")
    return value


def validate_job_id(job_id: object) -> str:
    if type(job_id) is not str or not _SEGMENT_RE.fullmatch(job_id):
        raise ManifestError("job_id: invalid identifier")
    return job_id


def _validate_utc_timestamp(value: object, field: str) -> str:
    if type(value) is not str or not _RFC3339_UTC_RE.fullmatch(value):
        raise ManifestError(f"{field}: timestamp must be UTC RFC3339 (Z or +00:00)")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ManifestError(f"{field}: invalid calendar timestamp") from exc
    return value


def _validate_bounded_int(
    value: object, field: str, *, minimum: int, maximum: int = MAX_SAFE_INT,
) -> int:
    # bool 是 int 子类,必须显式排除,否则 True 会伪装成 1 通过。
    if type(value) is not int or value < minimum or value > maximum:
        raise ManifestError(f"{field}: must be int in [{minimum}, {maximum}]")
    return value


def _validate_opt_str(value: object, field: str, *, max_chars: int = 200) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > max_chars:
        raise ManifestError(f"{field}: must be null or non-empty str <= {max_chars} chars")
    if _has_forbidden_codepoints(value):
        raise ManifestError(f"{field}: control characters are not allowed")
    return value


def _validate_str(value: object, field: str, *, max_chars: int = 200) -> str:
    if value is None:
        raise ManifestError(f"{field}: must not be null")
    return _validate_opt_str(value, field, max_chars=max_chars)  # type: ignore[return-value]


def _require_exact_keys(
    data: Mapping, field: str, required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(data)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise ManifestError(f"{field}: missing keys {missing}")
    if unknown:
        raise ManifestError(f"{field}: unknown keys {unknown}")


def validate_input_fingerprints(fingerprints: object) -> dict[str, str]:
    """校验 input_hashes 返回的指纹 map:有界、JSON-safe str->str、无密钥样式。"""
    if type(fingerprints) is not dict:
        raise ManifestError("input_fingerprints: must be a str->str object")
    if len(fingerprints) > MAX_INPUT_FINGERPRINT_ENTRIES:
        raise ManifestError(
            f"input_fingerprints: entries exceed {MAX_INPUT_FINGERPRINT_ENTRIES}"
        )
    for key, value in fingerprints.items():
        if type(key) is not str or not key or len(key) > MAX_INPUT_FINGERPRINT_KEY_CHARS:
            raise ManifestError("input_fingerprints: invalid key")
        if _has_forbidden_codepoints(key):
            raise ManifestError(f"input_fingerprints: control characters in key {key!r}")
        # 空串是既有指纹语义("该输入不存在",如 provider=""/evidence="",与 .done 的
        # input_hashes 完全对齐),必须放行;None/非字符串/超长仍拒绝。
        if type(value) is not str or len(value) > MAX_INPUT_FINGERPRINT_VALUE_CHARS:
            raise ManifestError(f"input_fingerprints[{key}]: invalid value")
        if _has_forbidden_codepoints(value):
            raise ManifestError(f"input_fingerprints[{key}]: control characters in value")
        ensure_no_secret_name(key, "input_fingerprints")
        ensure_no_secret_text(key, "input_fingerprints")
        ensure_no_secret_text(value, f"input_fingerprints[{key}]")
    encoded = canonical_json_bytes(fingerprints)
    if len(encoded) > MAX_INPUT_FINGERPRINT_CANONICAL_BYTES:
        raise ManifestError(
            f"input_fingerprints: canonical size exceeds {MAX_INPUT_FINGERPRINT_CANONICAL_BYTES} bytes"
        )
    return dict(fingerprints)


def compute_input_digest(fingerprints: object) -> str:
    """聚合输入指纹为单一摘要;先过 fail-closed 校验,再 canonicalize。"""
    return canonical_digest(validate_input_fingerprints(fingerprints))


def validate_output_path(path: object, *, scope_kind: str | None = None) -> str:
    """校验 outputs[].path:相对 scope 根、无穿越、无内部命名空间自引用。

    scope 感知:Job scope 的 manifest 不得声明 parts/ 下路径(那是 Part scope 的
    领地,Job 步经此越界会绕开 Part manifest 的所有权与失效边界);Part scope 的
    路径本就相对 Part 根,不受此限制。
    """
    if type(path) is not str or not path:
        raise ManifestError("outputs.path: must be a non-empty str")
    if "\x00" in path or _has_forbidden_codepoints(path):
        raise ManifestError(f"outputs.path: control characters are not allowed: {path!r}")
    if "\\" in path:
        raise ManifestError(f"outputs.path: backslash is not allowed: {path!r}")
    if path.startswith("/"):
        raise ManifestError(f"outputs.path: absolute path is not allowed: {path!r}")
    segments = path.split("/")
    for segment in segments:
        if not segment:
            raise ManifestError(f"outputs.path: empty segment in {path!r}")
        if segment in (".", ".."):
            raise ManifestError(f"outputs.path: traversal segment in {path!r}")
    # 任意一段命中 .flori 即拒,且大小写不敏感:对象存储大小写敏感,但消费方
    # 文件系统(macOS/Windows)不一定,.FLORI 与 .flori 会在恢复时撞车。
    if is_internal_namespace_path(path):
        raise ManifestError(
            f"outputs.path: manifest namespace self-reference: {path!r}"
        )
    if scope_kind == "job" and segments[0] == "parts":
        raise ManifestError(
            f"outputs.path: job scope must not claim part-scoped path: {path!r}"
        )
    return path


def _assert_safe_path_segment(value: str, field: str) -> str:
    """路径插值前的纵深防御:即使 _SEGMENT_RE 未来放宽,分隔符与穿越段也不得入模板。"""
    if (
        not value
        or "/" in value or "\\" in value or "\x00" in value
        or value in (".", "..")
    ):
        raise ManifestError(f"{field}: unsafe path segment {value!r}")
    return value


def manifest_relative_path(scope_key: str, step: str) -> str:
    """manifest 在 Job 根下的固定相对路径(设计稿 §2.2 两种模板,不拼 scope_key 原文)。"""
    try:
        execution_step_key(scope_key, step)
        part_id = part_id_from_scope(scope_key)
    except ValueError as exc:
        raise ManifestError(f"manifest path: {exc}") from exc
    _assert_safe_path_segment(step, "step")
    if part_id is None:
        return f"{INTERNAL_NAMESPACE}/steps/{step}/{MANIFEST_FILENAME}"
    _assert_safe_path_segment(part_id, "part_id")
    return f"parts/{part_id}/{INTERNAL_NAMESPACE}/steps/{step}/{MANIFEST_FILENAME}"


def manifest_object_key(
    job_id: str,
    scope_key: str,
    step: str,
    *,
    is_known_part: Callable[[str, str], bool] | None = None,
) -> str:
    """中心存储完整对象键。is_known_part(job_id, part_id) 由集成层注入 DB 事实,
    防伪造 part_id 与跨 Job Part 串写;纯逻辑层不直接查库。"""
    validate_job_id(job_id)
    _assert_safe_path_segment(job_id, "job_id")
    relative = manifest_relative_path(scope_key, step)
    part_id = part_id_from_scope(scope_key)
    if part_id is not None and is_known_part is not None and not is_known_part(job_id, part_id):
        raise ManifestError(f"scope: unknown part {part_id!r} for job {job_id!r}")
    return f"{job_id}/{relative}"


def _validate_scope_block(
    scope: object, job_id: str,
    is_known_part: Callable[[str, str], bool] | None,
) -> str:
    if type(scope) is not dict:
        raise ManifestError("scope: must be an object")
    _require_exact_keys(scope, "scope", _SCOPE_KEYS)
    kind = scope["kind"]
    scope_key = scope["scope_key"]
    if kind == "job":
        if scope_key != JOB_SCOPE:
            raise ManifestError("scope: job scope_key must be 'job'")
        if scope["part_id"] is not None or scope["part_index"] is not None:
            raise ManifestError("scope: job scope must not carry part identity")
        return scope_key
    if kind == "part":
        if type(scope_key) is not str:
            raise ManifestError("scope: scope_key must be str")
        try:
            derived = part_id_from_scope(scope_key)
        except ValueError as exc:
            raise ManifestError(f"scope: {exc}") from exc
        if derived is None or scope["part_id"] != derived:
            raise ManifestError("scope: part_id does not match scope_key")
        _validate_bounded_int(scope["part_index"], "scope.part_index", minimum=1)
        if is_known_part is not None and not is_known_part(job_id, derived):
            raise ManifestError(f"scope: unknown part {derived!r} for job {job_id!r}")
        return scope_key
    raise ManifestError("scope: kind must be 'job' or 'part'")


def _validate_execution_block(execution: object) -> None:
    if type(execution) is not dict:
        raise ManifestError("execution: must be an object")
    _require_exact_keys(execution, "execution", _EXECUTION_KEYS)
    _validate_str(execution["exec_id"], "execution.exec_id")
    _validate_bounded_int(execution["job_generation"], "execution.job_generation", minimum=0)
    _validate_bounded_int(execution["attempt"], "execution.attempt", minimum=1)
    _validate_utc_timestamp(execution["started_at"], "execution.started_at")
    _validate_utc_timestamp(execution["committed_at"], "execution.committed_at")
    duration = execution["duration_sec"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise ManifestError("execution.duration_sec: must be a finite non-negative number")


def _validate_compatibility_block(compatibility: object) -> None:
    if type(compatibility) is not dict:
        raise ManifestError("compatibility: must be an object")
    _require_exact_keys(compatibility, "compatibility", _COMPAT_KEYS)
    fingerprints = validate_input_fingerprints(compatibility["input_fingerprints"])
    declared = validate_digest(compatibility["input_digest"], "compatibility.input_digest")
    # 原始指纹与聚合摘要同存(设计稿 §2.4);两者不一致即 manifest 损坏,直接拒绝。
    if declared != canonical_digest(fingerprints):
        raise ManifestError(
            "compatibility.input_digest: does not match canonical digest of input_fingerprints"
        )
    validate_digest(compatibility["definition_digest"], "compatibility.definition_digest")


def _validate_producer_block(producer: object) -> None:
    if type(producer) is not dict:
        raise ManifestError("producer: must be an object")
    _require_exact_keys(
        producer, "producer", _PRODUCER_REQUIRED_KEYS, _PRODUCER_OPTIONAL_KEYS,
    )
    _validate_str(producer["flori_version"], "producer.flori_version")
    _validate_str(producer["runner"], "producer.runner")
    for key in ("build_sha", "worker_id", "image", "image_digest"):
        value = _validate_opt_str(producer[key], f"producer.{key}")
        if value is not None:
            ensure_no_secret_text(value, f"producer.{key}")
    if "kind" in producer:
        _validate_str(producer["kind"], "producer.kind")
    tool_versions = producer["tool_versions"]
    if type(tool_versions) is not dict or len(tool_versions) > 200:
        raise ManifestError("producer.tool_versions: must be an object with <= 200 entries")
    for name, version in tool_versions.items():
        _validate_str(name, "producer.tool_versions key")
        _validate_str(version, f"producer.tool_versions[{name}]")
        ensure_no_secret_text(version, f"producer.tool_versions[{name}]")


def _validate_outputs_block(outputs: object, outcome: str, scope_kind: str) -> None:
    if type(outputs) is not list:
        raise ManifestError("outputs: must be an array")
    # 数量门先于逐项校验:超限是契约违规,不做任何截断或部分接受。
    if len(outputs) > MANIFEST_MAX_OUTPUTS:
        raise ManifestError(f"outputs: entries exceed {MANIFEST_MAX_OUTPUTS}")
    if outcome == OUTCOME_SKIPPED and outputs:
        raise ManifestError("outputs: skipped manifest must declare no outputs")
    paths: list[str] = []
    for index, entry in enumerate(outputs):
        if type(entry) is not dict:
            raise ManifestError(f"outputs[{index}]: must be an object")
        _require_exact_keys(entry, f"outputs[{index}]", _OUTPUT_KEYS)
        paths.append(validate_output_path(entry["path"], scope_kind=scope_kind))
        _validate_bounded_int(entry["size_bytes"], f"outputs[{index}].size_bytes", minimum=0)
        validate_digest(entry["sha256"], f"outputs[{index}].sha256")
        media_type = entry["media_type"]
        if media_type is not None:
            _validate_str(media_type, f"outputs[{index}].media_type", max_chars=255)
    if len(set(paths)) != len(paths):
        raise ManifestError("outputs: duplicate paths")
    if paths != sorted(paths, key=lambda item: item.encode("utf-8")):
        raise ManifestError("outputs: paths must be sorted ascending by UTF-8 bytes")


def _validate_skip_block(skip: object, outcome: str) -> None:
    if outcome == OUTCOME_DONE:
        if skip is not None:
            raise ManifestError("skip: done manifest must have skip=null")
        return
    if type(skip) is not dict:
        raise ManifestError("skip: skipped manifest requires a skip object")
    _require_exact_keys(skip, "skip", _SKIP_KEYS)
    reason = skip["reason_code"]
    if type(reason) is not str or not _REASON_CODE_RE.fullmatch(reason):
        raise ManifestError("skip.reason_code: must match ^[a-z][a-z0-9_]{0,63}$")
    if reason in NON_DURABLE_SKIP_REASONS:
        raise ManifestError(
            f"skip.reason_code: environmental skip {reason!r} is not a durable completion fact"
        )
    for key in ("rule_digest", "condition_digest"):
        if skip[key] is not None:
            validate_digest(skip[key], f"skip.{key}")


def validate_manifest(
    data: object,
    *,
    is_known_part: Callable[[str, str], bool] | None = None,
) -> bytes:
    """按 manifest-v1 契约全量校验,返回 canonical 字节(供上层计算 manifest digest)。

    大小门在最后:canonical 字节超 1 MiB 报错,不截断。校验失败一律抛 ManifestError。
    """
    if type(data) is not dict:
        raise ManifestError("manifest: must be an object")
    _require_exact_keys(data, "manifest", _TOP_KEYS)
    if data["format"] != MANIFEST_FORMAT:
        raise ManifestError(f"format: must be {MANIFEST_FORMAT!r}")
    if type(data["format_version"]) is not int or data["format_version"] != MANIFEST_FORMAT_VERSION:
        raise ManifestError(f"format_version: must be {MANIFEST_FORMAT_VERSION}")
    job_id = validate_job_id(data["job_id"])
    scope_key = _validate_scope_block(data["scope"], job_id, is_known_part)
    step = data["step"]
    if type(step) is not str:
        raise ManifestError("step: must be str")
    try:
        execution_step_key(scope_key, step)
    except ValueError as exc:
        raise ManifestError(f"step: {exc}") from exc
    outcome = data["outcome"]
    if outcome not in (OUTCOME_DONE, OUTCOME_SKIPPED):
        raise ManifestError("outcome: must be 'done' or 'skipped'")
    _validate_execution_block(data["execution"])
    _validate_compatibility_block(data["compatibility"])
    _validate_producer_block(data["producer"])
    _validate_outputs_block(data["outputs"], outcome, data["scope"]["kind"])
    _validate_skip_block(data["skip"], outcome)
    encoded = canonical_json_bytes(data)
    if len(encoded) > MANIFEST_MAX_CANONICAL_BYTES:
        raise ManifestError(
            f"manifest: canonical size {len(encoded)} exceeds {MANIFEST_MAX_CANONICAL_BYTES} bytes"
        )
    return encoded


def manifest_digest(
    data: object,
    *,
    is_known_part: Callable[[str, str], bool] | None = None,
) -> str:
    """校验后的 manifest 本体摘要;由 Storage/DB cache 记录,不写入自身(防自引用)。"""
    return "sha256:" + hashlib.sha256(
        validate_manifest(data, is_known_part=is_known_part)
    ).hexdigest()


@dataclass(frozen=True)
class ObservedOutput:
    """集成层观察到的已提交输出事实(stat + 流式 SHA-256)。"""
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ReusabilityDecision:
    reusable: bool
    reason: str | None


def check_reusable(
    manifest: object,
    *,
    job_id: str,
    scope_key: str,
    step: str,
    current_input_fingerprints: Mapping[str, str],
    current_definition_digest: str,
    observe_output: Callable[[str], ObservedOutput | None],
    dependencies: Iterable[tuple[str, bool]] = (),
    is_known_part: Callable[[str, str], bool] | None = None,
) -> ReusabilityDecision:
    """统一可复用判定(设计稿 §2.4 公式,AI/CPU 不分支),返回判定与首个失败原因。

    判定顺序严格按公式:schema -> outcome -> exact outputs -> input digest ->
    definition digest -> DAG 依赖 -> scope/part/job 身份。current_* 参数属于调用方
    契约,本身非法时直接抛错而不是返回 False(那是 bug,不是不兼容)。
    """
    validate_digest(current_definition_digest, "current_definition_digest")
    current_input_digest = compute_input_digest(dict(current_input_fingerprints))
    try:
        validate_manifest(manifest, is_known_part=is_known_part)
    except ManifestError as exc:
        return ReusabilityDecision(False, f"manifest_invalid:{exc}")
    assert type(manifest) is dict  # validate_manifest 已保证
    # outcome 属公式第二条;schema 已限定 done|skipped,此处不会再失败。
    for entry in manifest["outputs"]:
        path = entry["path"]
        observed = observe_output(path)
        if observed is None:
            return ReusabilityDecision(False, f"output_missing:{path}")
        if observed.size_bytes != entry["size_bytes"]:
            return ReusabilityDecision(False, f"output_size_mismatch:{path}")
        if observed.sha256 != entry["sha256"]:
            return ReusabilityDecision(False, f"output_sha256_mismatch:{path}")
    if manifest["compatibility"]["input_digest"] != current_input_digest:
        return ReusabilityDecision(False, "input_digest_mismatch")
    if manifest["compatibility"]["definition_digest"] != current_definition_digest:
        return ReusabilityDecision(False, "definition_digest_mismatch")
    for dependency_name, dependency_reusable in dependencies:
        if not dependency_reusable:
            return ReusabilityDecision(False, f"dependency_not_reusable:{dependency_name}")
    if manifest["job_id"] != job_id:
        return ReusabilityDecision(False, "identity_mismatch:job_id")
    if manifest["scope"]["scope_key"] != scope_key:
        return ReusabilityDecision(False, "identity_mismatch:scope_key")
    if manifest["step"] != step:
        return ReusabilityDecision(False, "identity_mismatch:step")
    return ReusabilityDecision(True, None)
