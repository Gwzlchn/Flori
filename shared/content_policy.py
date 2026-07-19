"""便携内容仓库的数据分类 allowlist、URL 脱敏与敏感信息边界(设计稿 05 号 §2.4/§2.13)。

便携仓库不加密,敏感数据只能靠两道门挡在入口:第一道是 allowlist serializer
(本模块的分类与字段校验),第二道是 secret scan(复用 shared.step_manifest 的
ensure_no_secret_* 原语,再叠加 cookie/签名 URL 等策略层样式)。任一道命中即
抛 PolicyError,整次 snapshot 失败,不做静默剥离后继续。

本模块保持纯逻辑:除 ensure_regular_file 的 lstat 外无文件 IO,不触碰 DB/网络。
DB 行到 record 的映射(serializer)由 content_backup 单元实现,这里只固化
"哪些表、哪些字段允许进入明文仓库"的契约。
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import unquote, urlsplit

# 单一来源引用(同 step_manifest 引 step_scope 的理由):canonical JSON、密钥样式、
# 时间戳与路径约束若在此复写,两处正则漂移会直接击穿备份的 fail-closed 边界。
from .step_manifest import (
    ManifestError,
    _validate_utc_timestamp,
    canonical_json_bytes,
    ensure_no_secret_name,
    ensure_no_secret_text,
    validate_digest,
    validate_job_id,
    validate_manifest,
    validate_output_path,
)
from .step_scope import (
    _SEGMENT_RE,
    execution_step_key,
    parse_execution_step,
    part_id_from_scope,
)


class PolicyError(ValueError):
    """便携备份策略违规;fail-closed,调用方不得捕获后降级放行。"""


# 数据分类(§2.4):A/B/C 可入库,D 可重建不入库,E 禁止入库。
CATEGORY_BUSINESS_FACT = "A"
CATEGORY_FAILURE_AUDIT = "B"
CATEGORY_ARTIFACT = "C"
CATEGORY_REBUILDABLE = "D"
CATEGORY_FORBIDDEN = "E"

# 尺寸/深度上限(§2.13.4 超限 JSON):record 上限须容纳内嵌 manifest(1 MiB)
# 加 blob 映射;snapshot 只存 digest 引用,32 MiB 已是数十万 record 的量级。
MAX_JSON_DEPTH = 64
MAX_RECORD_CANONICAL_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_CANONICAL_BYTES = 32 * 1024 * 1024
MAX_RECEIPT_CANONICAL_BYTES = 1024 * 1024
MAX_AUDIT_TEXT_CHARS = 16_384
# 自由文本单串上限:比审计文本宽(prompt/definition 可较长),但仍远低于
# record canonical 总量门,避免单字段吃满配额。
MAX_FREE_TEXT_CHARS = 262_144
MAX_URL_CHARS = 8_192
MAX_PORTABLE_PATH_CHARS = 1_024
_MAX_KEY_CHARS = 300  # 与 MAX_INPUT_FINGERPRINT_KEY_CHARS 对齐,manifest 指纹键可达此长

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,128}$")
# FTS5 shadow 表由虚表定义自动生成,随索引重建(§2.4D5),按命名规则识别
# 而非硬编码清单,防 schema 演进漂移。
_FTS5_SHADOW_RE = re.compile(r"^.+_fts5_(data|idx|content|docsize|config)$")

# URL query 签名/凭据参数名(§2.13.2):命中即剥离并记 redaction reason。
# 名称按子串判定(sign 含 sig、wmsAuthSign 含 auth、__token__ 含 token),
# 另补齐 CDN 惯用短名与 expire 系列:后者本身不敏感,但属于签名 URL 元组,
# 剥离后同资源不同签发时刻才能得到同一 canonical hash。宁可误删参数,
# 不可放行签名(剥离只是丢参数,不是报错)。
# 这张表是两道门的单一来源:redact_url 的参数名判定与 scan_text_for_secrets
# 的内嵌 URL 检测都从它派生,避免弱门覆盖大面(强门只管 URL 字段)的倒挂。
_SENSITIVE_NAME_SUBSTRINGS = (
    "token", "sign", "sig", "auth", "secret", "key", "session",
    "credential", "cookie", "passwd", "password",
)
_SENSITIVE_NAME_EXACT = frozenset({
    "sid", "policy", "expire", "expires", "expiration",
    "wstime", "uparams", "hdnts",
})
_SENSITIVE_NAME_PREFIXES = ("x-amz-", "x-goog-")

_NAME_SUBSTRING_ALT = "|".join(re.escape(part) for part in _SENSITIVE_NAME_SUBSTRINGS)
_NAME_EXACT_ALT = "|".join(re.escape(part) for part in sorted(_SENSITIVE_NAME_EXACT))
_NAME_PREFIX_ALT = "|".join(re.escape(part) for part in _SENSITIVE_NAME_PREFIXES)

# 策略层附加密钥样式:step_manifest 原语覆盖 key 名与裸 token 值,这里补
# HTTP 头形态与签名 URL 残留(§2.13.3 cookie/签名参数)。只增补,不复制原语。
# 第三条的参数名 alternation 由上面同一张表生成。
_POLICY_SECRET_RES = (
    re.compile(r"(?i)(?:^|[\s;,?&#\"'{(\[])(?:set-)?cookie\s*[:=]\s*\S"),
    re.compile(r"(?i)(?:^|[\s;,?&#\"'{(\[])authorization\s*[:=]\s*\S"),
    re.compile(
        r"(?i)[?&;#]\s*(?:[a-z0-9_.\-]*(?:" + _NAME_SUBSTRING_ALT + r")[a-z0-9_.\-]*"
        r"|" + _NAME_EXACT_ALT + r"|(?:" + _NAME_PREFIX_ALT + r")[a-z0-9_.\-]*"
        r")=[^&;\s\"']{8,}"
    ),
)

# http(s) URL 的粗定位:用于 meta/record_json 等自由结构里的内嵌 URL 脱敏。
_EMBEDDED_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>\\]{3,8192}")


def _sensitive_param_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _SENSITIVE_NAME_EXACT
        or lowered.startswith(_SENSITIVE_NAME_PREFIXES)
        or any(part in lowered for part in _SENSITIVE_NAME_SUBSTRINGS)
    )


def scan_text_for_secrets(text: str, field: str) -> None:
    """密钥样式值扫描:step_manifest 原语 + 策略层附加样式,命中即拒。"""
    try:
        ensure_no_secret_text(text, field)
    except ManifestError as exc:
        raise PolicyError(str(exc)) from exc
    for pattern in _POLICY_SECRET_RES:
        if pattern.search(text):
            raise PolicyError(f"{field}: value matches sensitive pattern")


class StreamingSecretScanner:
    """有界内存扫描完整 UTF-8 字节流,并保留跨 chunk 的匹配窗口。"""

    # 必须覆盖 _EMBEDDED_URL_RE 的最大匹配长度(8192)及前后键名/分隔符。
    # 低于这个窗口会让跨 1 MiB 存储 chunk 的长签名 URL 逃过扫描。
    _OVERLAP_CHARS = 16 * 1024

    def __init__(self, field: str, *, raise_on_match: bool = True):
        self.field = field
        self.raise_on_match = raise_on_match
        self.matched = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._tail = ""
        self._finished = False

    def feed(self, chunk: bytes) -> None:
        if self._finished:
            raise PolicyError(f"{self.field}: scanner is already finished")
        self._scan(self._decoder.decode(chunk, final=False))

    def finish(self) -> None:
        if self._finished:
            return
        self._scan(self._decoder.decode(b"", final=True))
        self._finished = True

    def _scan(self, text: str) -> None:
        combined = self._tail + text
        try:
            scan_text_for_secrets(combined, self.field)
        except PolicyError:
            self.matched = True
            if self.raise_on_match:
                raise
        self._tail = combined[-self._OVERLAP_CHARS:]


def _check_text_hygiene(text: str, field: str, *, is_key: bool = False) -> None:
    """自由文本统一卫生门:长度上限 + 控制字符 + lone surrogate。

    值允许换行/回车/制表(日志与多行 prompt 的合法形态);键一律不允许控制字符。
    """
    limit = _MAX_KEY_CHARS if is_key else MAX_FREE_TEXT_CHARS
    if len(text) > limit:
        raise PolicyError(f"{field}: text length {len(text)} exceeds {limit}")
    allowed = "" if is_key else "\n\r\t"
    for ch in text:
        code = ord(ch)
        if (code < 0x20 and ch not in allowed) or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            raise PolicyError(f"{field}: control characters are not allowed")


def scan_json_for_secrets(value: object, field: str, *, _depth: int = 0) -> None:
    """递归扫描 JSON 键名与字符串值,并统一执行控制字符/长度/深度门。

    深度门兼防压缩炸弹样式的超深嵌套;文本卫生门覆盖 title/meta/description
    等全部自由文本字段(§2.13.4),不再依赖各 kind 单独记得校验。
    """
    if _depth > MAX_JSON_DEPTH:
        raise PolicyError(f"{field}: nesting exceeds {MAX_JSON_DEPTH}")
    if isinstance(value, str):
        _check_text_hygiene(value, field)
        scan_text_for_secrets(value, field)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise PolicyError(f"{field}: dict key must be str")
            _check_text_hygiene(key, field, is_key=True)
            try:
                ensure_no_secret_name(key, field)
                ensure_no_secret_text(key, field)
            except ManifestError as exc:
                raise PolicyError(str(exc)) from exc
            scan_json_for_secrets(item, f"{field}.{key}", _depth=_depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            scan_json_for_secrets(item, f"{field}[{index}]", _depth=_depth + 1)
        return
    # None/bool/int/float 无密钥载体;其余类型交 canonical 层拒绝。


def ensure_bounded_depth(value: object, field: str, *, max_depth: int = MAX_JSON_DEPTH) -> None:
    """独立深度门:供不需要 secret 扫描的调用方(如已扫描过的重读路径)复用。"""
    if max_depth < 0:
        raise PolicyError(f"{field}: nesting exceeds limit")
    if isinstance(value, dict):
        for key, item in value.items():
            ensure_bounded_depth(item, f"{field}.{key}", max_depth=max_depth - 1)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            ensure_bounded_depth(item, f"{field}[{index}]", max_depth=max_depth - 1)


def validate_audit_text(
    text: object, field: str, *, max_chars: int = MAX_AUDIT_TEXT_CHARS,
) -> str:
    """失败信息/日志摘录的有界脱敏文本门:长度上限 + 控制字符 + 密钥样式。

    与 manifest 字符串不同,审计文本允许换行与制表符;其余控制字符和
    lone surrogate 仍拒绝。
    """
    if type(text) is not str:
        raise PolicyError(f"{field}: must be str")
    if len(text) > max_chars:
        raise PolicyError(f"{field}: length {len(text)} exceeds {max_chars}")
    for ch in text:
        code = ord(ch)
        if (code < 0x20 and ch not in "\n\r\t") or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            raise PolicyError(f"{field}: control characters are not allowed")
    scan_text_for_secrets(text, field)
    return text


@dataclass(frozen=True)
class RedactedUrl:
    """脱敏后的 canonical URL、其不可逆摘要与剥离原因清单。"""
    url: str
    canonical_hash: str
    redactions: tuple[str, ...]


def _split_query_parts(raw_query: str) -> list[tuple[str, str, str]]:
    """按 [;&] 切开原始 query,返回 (原文片段, 解码小写名, 解码值);保留原始编码。

    分号是历史合法的参数分隔符,CDN 签名链接仍在用(;sig=...),只按 & 切
    会让签名参数藏进前一个参数的值里。
    """
    parts: list[tuple[str, str, str]] = []
    for chunk in re.split(r"[;&]", raw_query):
        if not chunk:
            continue
        name, _, value = chunk.partition("=")
        parts.append((chunk, unquote(name).casefold(), unquote(value)))
    return parts


def redact_url(url: object, field: str = "url") -> RedactedUrl:
    """剥离 URL 中的签名/凭据参数,返回 canonical locator 与不可逆摘要(§2.13.2)。

    canonical hash 对"脱敏后"的 canonical URL 计算:同一资源不同时刻签发的
    签名 URL 得到同一 hash,可用于冲突核对;原始签名串不落任何字段。
    剥离后仍残留密钥样式(如路径内嵌 JWT)则整体拒绝,不猜测如何裁剪。
    """
    if type(url) is not str or not url:
        raise PolicyError(f"{field}: must be a non-empty str")
    if len(url) > MAX_URL_CHARS:
        raise PolicyError(f"{field}: length exceeds {MAX_URL_CHARS}")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF for ch in url):
        raise PolicyError(f"{field}: control characters or spaces are not allowed")
    try:
        split = urlsplit(url)
        hostname = split.hostname
        port = split.port
    except ValueError as exc:
        raise PolicyError(f"{field}: unparsable URL") from exc
    scheme = split.scheme.casefold()
    if scheme not in ("http", "https"):
        raise PolicyError(f"{field}: scheme {scheme!r} is not allowed")
    if not hostname:
        raise PolicyError(f"{field}: missing host")

    redactions: set[str] = set()
    host = hostname.casefold()
    if ":" in host:
        host = f"[{host}]"  # IPv6 字面量重加括号,否则与端口分隔符混淆
    if split.username is not None or split.password is not None:
        redactions.add("userinfo")
    default_port = 80 if scheme == "http" else 443
    netloc = host if port is None or port == default_port else f"{host}:{port}"

    kept_chunks: list[str] = []
    for chunk, name, value in _split_query_parts(split.query):
        if _sensitive_param_name(name):
            redactions.add(f"query:{name}")
            continue
        try:
            scan_text_for_secrets(value, field)
        except PolicyError:
            redactions.add(f"query:{name}")
            continue
        kept_chunks.append(chunk)

    fragment = split.fragment
    if fragment:
        # fragment 可携带 OAuth implicit flow 的 token(#access_token=...),
        # 按 query 同一套名称判定 + 值扫描,命中即整段丢弃。
        dirty = any(
            _sensitive_param_name(name)
            for chunk, name, _ in _split_query_parts(fragment)
            if "=" in chunk
        )
        if not dirty:
            try:
                scan_text_for_secrets(unquote(fragment), field)
            except PolicyError:
                dirty = True
        if dirty:
            redactions.add("fragment")
            fragment = ""

    result = f"{scheme}://{netloc}{split.path}"
    if kept_chunks:
        result += "?" + "&".join(kept_chunks)
    if fragment:
        result += "#" + fragment
    # 第二道门:剥离后仍命中样式说明凭据不在可剥离位置,fail-closed。
    scan_text_for_secrets(result, field)
    digest = "sha256:" + hashlib.sha256(result.encode("utf-8")).hexdigest()
    return RedactedUrl(url=result, canonical_hash=digest, redactions=tuple(sorted(redactions)))


def redact_urls_in_text(text: str, field: str = "text") -> tuple[str, tuple[str, ...]]:
    """脱敏自由文本里内嵌的 http(s) URL,返回 (新文本, redaction reason)。

    meta/record_json 这类自由结构会捎带签名 URL(§2.4E-3)。逐个替换为
    canonical locator;某个 URL 无法脱敏(如路径内嵌 JWT)时不猜测裁剪方式,
    直接抛错交给 fail-closed。
    """
    reasons: set[str] = set()

    def _replace(match: re.Match) -> str:
        # 结尾标点通常属句子而非 URL,回退剥离再解析。
        raw = match.group(0).rstrip(".,;:!?)]}'\"")
        trailing = match.group(0)[len(raw):]
        redacted = redact_url(raw, field)
        reasons.update(redacted.redactions)
        return redacted.url + trailing

    return _EMBEDDED_URL_RE.sub(_replace, text), tuple(sorted(reasons))


def redact_urls_in_json(value: object, field: str = "value") -> tuple[object, tuple[str, ...]]:
    """递归脱敏 JSON 结构里的内嵌 URL;返回新结构与合并后的 redaction reason。

    只改字符串值,不动键名与结构:调用方拿到的对象仍可直接进 validate_record。
    """
    reasons: set[str] = set()

    def _walk(node: object, path: str) -> object:
        if isinstance(node, str):
            if "://" not in node:
                return node
            text, found = redact_urls_in_text(node, path)
            reasons.update(found)
            return text
        if isinstance(node, dict):
            return {key: _walk(item, f"{path}.{key}") for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(item, f"{path}[{index}]") for index, item in enumerate(node)]
        return node

    return _walk(value, field), tuple(sorted(reasons))


def validate_portable_relative_path(
    path: object, field: str, *, max_chars: int = MAX_PORTABLE_PATH_CHARS,
) -> str:
    """通用相对路径门(§2.13.4):禁绝对路径/../反斜杠/控制字符/空段。

    与 validate_output_path 的差别:允许 .flori 内部命名空间(failure_event 的
    partial_outputs 可指向 staging 残留路径,只作摘要不引用 blob)。
    """
    if type(path) is not str or not path:
        raise PolicyError(f"{field}: must be a non-empty str")
    if len(path) > max_chars:
        raise PolicyError(f"{field}: length exceeds {max_chars}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF for ch in path):
        raise PolicyError(f"{field}: control characters are not allowed")
    if "\\" in path:
        raise PolicyError(f"{field}: backslash is not allowed: {path!r}")
    if path.startswith("/"):
        raise PolicyError(f"{field}: absolute path is not allowed: {path!r}")
    for segment in path.split("/"):
        if not segment:
            raise PolicyError(f"{field}: empty segment in {path!r}")
        if segment in (".", ".."):
            raise PolicyError(f"{field}: traversal segment in {path!r}")
    return path


def ensure_regular_file(path: Path, field: str):
    """lstat 验证普通文件:symlink/目录/设备/FIFO/socket 一律拒绝(§2.13.4)。

    用 lstat 而非 stat:symlink 指向合法文件同样拒绝,防止仓库外文件借链接混入。
    返回 stat 结果供调用方核对 size。
    """
    try:
        st = path.lstat()
    except OSError as exc:
        raise PolicyError(f"{field}: cannot stat {path.name!r}: {exc}") from exc
    if stat_module.S_ISLNK(st.st_mode):
        raise PolicyError(f"{field}: symlink is not allowed: {path.name!r}")
    if not stat_module.S_ISREG(st.st_mode):
        raise PolicyError(f"{field}: not a regular file: {path.name!r}")
    return st


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"json: duplicate key {key!r}")
        result[key] = value
    return result


def load_bounded_json(data: bytes, field: str, *, max_bytes: int) -> object:
    """有界 JSON 解析:先按字节封顶再解析,拒绝重复键与超深嵌套。

    重复键必须拒:json.loads 默认保留末值,同 digest 文件可携带两份键值
    绕过 canonical 校验。
    """
    if type(data) is not bytes:
        raise PolicyError(f"{field}: must be bytes")
    if len(data) > max_bytes:
        raise PolicyError(f"{field}: size {len(data)} exceeds {max_bytes} bytes")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"{field}: not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except PolicyError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PolicyError(f"{field}: not valid JSON: {exc}") from exc
    ensure_bounded_depth(value, field)
    return value


def _wrap_manifest_error(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except ManifestError as exc:
        raise PolicyError(str(exc)) from exc


def _validate_utc(value: object, field: str) -> str:
    return _wrap_manifest_error(_validate_utc_timestamp, value, field)


def _validate_identifier(value: object, field: str) -> str:
    if type(value) is not str or not _SEGMENT_RE.fullmatch(value):
        raise PolicyError(f"{field}: invalid identifier")
    return value


# study 九张不可变账本表(§2.4A7)整表进入 allowlist;列名来自 v8 真实 Schema
# (v0003/v0004/v0005 migration),新增列必须同步这里,否则备份 fail-closed。
STUDY_TABLE_COLUMNS: Mapping[str, frozenset[str]] = {
    "study_cards": frozenset({
        "card_id", "domain", "job_id", "concept_term", "card_type", "front",
        "back", "explanation", "evidence_json", "status", "source", "revision",
        "created_at", "updated_at",
    }),
    "study_reviews": frozenset({
        "card_id", "due_at", "due_at_epoch_us", "interval_days", "ease",
        "repetitions", "lapses", "last_grade", "last_reviewed_at",
        "last_reviewed_at_epoch_us", "updated_at",
    }),
    "study_review_logs": frozenset({
        "id", "card_id", "request_id", "request_fingerprint", "grade",
        "reviewed_at", "reviewed_at_epoch_us", "response_ms",
        "scheduled_due_at", "scheduled_due_at_epoch_us", "next_due_at",
        "next_due_at_epoch_us", "interval_days", "ease", "repetitions",
        "lapses", "revision_before", "revision_after", "outcome_json",
    }),
    "study_suggestion_batches": frozenset({
        "batch_id", "domain", "status", "revision", "attempt",
        "generator_fingerprint", "input_fingerprint", "task_id", "provider",
        "model", "max_cards", "llm_request_json", "result_json", "error_code",
        "error_message", "deadline_at", "deadline_at_epoch_us", "created_at",
        "updated_at",
    }),
    "study_suggestion_inputs": frozenset({
        "input_id", "batch_id", "ordinal", "kind", "concept_term_snapshot",
        "current_concept_term", "input_fingerprint", "created_at",
    }),
    # status/current_domain/invalid_reason/validated_at 是随 job 与 chunk 重算的
    # 派生列(§2.4D "可重建不备份"),导入后由 revalidate 产生;快照只留不可变
    # 证据快照本身,避免带回一份过期的有效性判断。
    "study_suggestion_evidence": frozenset({
        "evidence_id", "batch_id", "input_id", "job_id", "chunk_id",
        "note_type", "source_domain_snapshot",
        "title_snapshot", "section_snapshot", "body_snapshot", "body_sha256",
        "locator_json", "created_at", "canonical_evidence_id",
    }),
    "study_suggestions": frozenset({
        "suggestion_id", "batch_id", "ordinal", "status", "revision", "domain",
        "concept_term", "knowledge_key", "card_type", "front", "back",
        "explanation", "knowledge_fingerprint", "content_fingerprint",
        "accepted_card_id", "rejection_reason", "created_at", "updated_at",
    }),
    "study_suggestion_evidence_links": frozenset({
        "batch_id", "suggestion_id", "evidence_id", "ordinal",
        "quote_snapshot", "quote_sha256", "created_at",
    }),
    "study_suggestion_operations": frozenset({
        "request_id", "ledger_seq", "previous_ledger_sha256", "ledger_sha256",
        "request_fingerprint", "operation_kind", "batch_id", "request_json",
        "outcome_json", "created_at",
    }),
}

# study 信封必须携带该表主键列,缺主键的行无法在 import 侧做自然键幂等。
STUDY_TABLE_PRIMARY_KEYS: Mapping[str, tuple[str, ...]] = {
    "study_cards": ("card_id",),
    "study_reviews": ("card_id",),
    "study_review_logs": ("id",),
    "study_suggestion_batches": ("batch_id",),
    "study_suggestion_inputs": ("input_id",),
    "study_suggestion_evidence": ("evidence_id",),
    "study_suggestions": ("suggestion_id",),
    "study_suggestion_evidence_links": ("suggestion_id", "evidence_id"),
    "study_suggestion_operations": ("request_id",),
}

USER_CONFIG_KINDS = frozenset({
    "prompts", "profiles", "styles", "templates", "domain_config",
    "job_ai_config",
})

# E 类禁止入库(§2.4E):即使出现在 legacy_archive 的表名里也拒绝。
FORBIDDEN_TABLES = frozenset({"app_credentials", "worker_tokens"})

# D 类可重建投影(§2.4D):不是敏感数据,但备份它们会制造第二份状态。
# content_import* 是本机导入运维账本(§2.11-2/3),描述"这个 snapshot 在这台机器
# 物化到哪一步",跨机器无意义,清库即失效,绝不进快照。
REBUILDABLE_TABLES = frozenset({
    "job_steps", "workers", "schema_migrations", "note_chunks",
    "note_chunks_fts5", "notes_fts5", "canonical_evidence",
    "concept_occurrences", "concept_occurrence_projection",
    "restored_job_activations", "flori_stat_probe",
    "content_imports", "content_import_records",
})

# A/B 类表到 record kind 的映射;jobs 同时派生 job_user_state(用户归类状态
# 与不可变身份分离,§2.4A1)。
BACKUP_TABLE_KINDS: Mapping[str, str] = {
    "jobs": "job_core",
    "job_parts": "part_core",
    "collections": "collection",
    "ingested_items": "ingested_item",
    "prompt_overrides": "prompt_override",
    "prompt_override_versions": "prompt_override_version",
    "glossary": "glossary",
    "concept_definition_versions": "definition_version",
    "ai_usage": "ai_usage",
    "ai_task_logs": "ai_task_log",
    "glossary_bak_clean_20260617": "legacy_archive",
    **{table: "study" for table in STUDY_TABLE_COLUMNS},
}


def classify_table(table: object) -> tuple[str, str]:
    """把 DB 表分类为 (category, detail):A/B 给 record kind,D/E 给原因。

    未知表抛 PolicyError:备份面不允许"默认带走"或"默认忽略",Schema 新增表
    必须显式归类(§5.2.23 未知项必须为 0)。
    """
    if type(table) is not str or not _TABLE_NAME_RE.fullmatch(table):
        raise PolicyError("table: invalid table name")
    if table in FORBIDDEN_TABLES:
        return (CATEGORY_FORBIDDEN, "credential material must not enter plaintext repository")
    if table in REBUILDABLE_TABLES:
        return (CATEGORY_REBUILDABLE, "projection is rebuilt from records and manifests")
    # 规则而非清单:sqlite_% 内部簿记表与 FTS5 shadow 表随 schema/索引自动重建。
    if table.startswith("sqlite_"):
        return (CATEGORY_REBUILDABLE, "sqlite internal bookkeeping table")
    if _FTS5_SHADOW_RE.fullmatch(table):
        return (CATEGORY_REBUILDABLE, "FTS5 shadow table is rebuilt with its index")
    kind = BACKUP_TABLE_KINDS.get(table)
    if kind is None:
        raise PolicyError(f"table: {table!r} is not classified; refusing to guess")
    category = (
        CATEGORY_FAILURE_AUDIT if kind == "legacy_archive" else CATEGORY_BUSINESS_FACT
    )
    return (category, kind)


def _require_nonempty_str_fields(body: Mapping, kind: str, fields: tuple[str, ...]) -> None:
    """出现即须为非空 str 的最小值型门(id/时间戳);缺席由 required 集合另行把守。"""
    for field in fields:
        if field in body and (type(body[field]) is not str or not body[field]):
            raise PolicyError(f"{kind}.{field}: must be a non-empty str")


def _validate_job_core(body: Mapping) -> None:
    _wrap_manifest_error(validate_job_id, body["id"])
    for key in ("content_type", "pipeline", "created_at"):
        if type(body[key]) is not str or not body[key]:
            raise PolicyError(f"job_core.{key}: must be a non-empty str")
    _require_nonempty_str_fields(body, "job_core", ("published_at",))
    if "url" in body and body["url"] is not None:
        # url 必须已经是脱敏 canonical 形态;redact_url 幂等,原样通过即合规。
        redacted = redact_url(body["url"], "job_core.url")
        if redacted.url != body["url"]:
            raise PolicyError("job_core.url: must be a redacted canonical URL")


def _validate_job_user_state(body: Mapping) -> None:
    _wrap_manifest_error(validate_job_id, body["job_id"])


def _validate_part_core(body: Mapping) -> None:
    _validate_identifier(body["id"], "part_core.id")
    _wrap_manifest_error(validate_job_id, body["job_id"])
    part_index = body["part_index"]
    if type(part_index) is not int or part_index < 1:
        raise PolicyError("part_core.part_index: must be int >= 1")
    _require_nonempty_str_fields(body, "part_core", ("created_at", "updated_at"))
    if "source_url" in body and body["source_url"] is not None:
        redacted = redact_url(body["source_url"], "part_core.source_url")
        if redacted.url != body["source_url"]:
            raise PolicyError("part_core.source_url: must be a redacted canonical URL")
    if "source_blob" in body:
        _wrap_manifest_error(validate_digest, body["source_blob"], "part_core.source_blob")
        if body.get("source_digest") != body["source_blob"]:
            raise PolicyError("part_core.source_blob: must equal source_digest")


def _validate_step_result(body: Mapping) -> None:
    """manifest 与 output blob refs 是不可拆分单元(§2.4C3):逐项互证。"""
    manifest = body["manifest"]
    _wrap_manifest_error(validate_manifest, manifest)
    if manifest["job_id"] != body["job_id"]:
        raise PolicyError("step_result.job_id: does not match manifest")
    if manifest["scope"]["scope_key"] != body["scope_key"]:
        raise PolicyError("step_result.scope_key: does not match manifest")
    if manifest["step"] != body["step"]:
        raise PolicyError("step_result.step: does not match manifest")
    output_blobs = body["output_blobs"]
    if type(output_blobs) is not dict:
        raise PolicyError("step_result.output_blobs: must be an object")
    declared = {entry["path"]: entry["sha256"] for entry in manifest["outputs"]}
    if set(output_blobs) != set(declared):
        raise PolicyError("step_result.output_blobs: paths do not match manifest outputs")
    for path, blob in output_blobs.items():
        # blob key 就是文件字节 SHA-256(§2.2 规则 1),必须与 manifest 声明一致。
        _wrap_manifest_error(validate_digest, blob, f"step_result.output_blobs[{path}]")
        if blob != declared[path]:
            raise PolicyError(
                f"step_result.output_blobs[{path}]: does not match manifest sha256"
            )


def _validate_failure_event(body: Mapping) -> None:
    _wrap_manifest_error(validate_job_id, body["job_id"])
    scope_key = body["scope_key"]
    step = body["step"]
    if type(scope_key) is not str or type(step) is not str:
        raise PolicyError("failure_event: scope_key/step must be str")
    try:
        execution_step_key(scope_key, step)
        part_id_from_scope(scope_key)
    except ValueError as exc:
        raise PolicyError(f"failure_event.scope_key: {exc}") from exc
    _validate_exec_id(body["exec_id"], "failure_event.exec_id")
    _validate_utc(body["failed_at"], "failure_event.failed_at")
    if "started_at" in body and body["started_at"] is not None:
        _validate_utc(body["started_at"], "failure_event.started_at")
    for key in ("generation", "attempt"):
        if key in body and body[key] is not None:
            if type(body[key]) is not int or body[key] < 0:
                raise PolicyError(f"failure_event.{key}: must be int >= 0")
    if "sanitized_message" in body and body["sanitized_message"] is not None:
        validate_audit_text(body["sanitized_message"], "failure_event.sanitized_message")
    if "log_blob" in body and body["log_blob"] is not None:
        _wrap_manifest_error(validate_digest, body["log_blob"], "failure_event.log_blob")
    for key in ("ai_usage_refs", "ai_task_log_refs"):
        refs = body.get(key)
        if refs is None:
            continue
        if type(refs) is not list:
            raise PolicyError(f"failure_event.{key}: must be an array")
        for index, ref in enumerate(refs):
            _wrap_manifest_error(validate_digest, ref, f"failure_event.{key}[{index}]")
    partials = body.get("partial_outputs")
    if partials is not None:
        if type(partials) is not list:
            raise PolicyError("failure_event.partial_outputs: must be an array")
        if partials and body.get("partial_outputs_discarded") is not True:
            raise PolicyError(
                "failure_event.partial_outputs_discarded: must be true when partial outputs were seen"
            )
        for index, entry in enumerate(partials):
            field = f"failure_event.partial_outputs[{index}]"
            if type(entry) is not dict:
                raise PolicyError(f"{field}: must be an object")
            # 只留路径/大小摘要,不得引用业务 blob(§2.4B1)。
            if set(entry) != {"path", "size_bytes"}:
                raise PolicyError(f"{field}: only path/size_bytes summaries are allowed")
            validate_portable_relative_path(entry["path"], f"{field}.path")
            if type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0:
                raise PolicyError(f"{field}.size_bytes: must be int >= 0")


def _validate_ingested_item(body: Mapping) -> None:
    _require_nonempty_str_fields(
        body, "ingested_item", ("collection_id", "item_id", "ingested_at"),
    )


def _validate_collection(body: Mapping) -> None:
    _require_nonempty_str_fields(
        body, "collection", ("id", "name", "created_at", "updated_at"),
    )


def _validate_glossary(body: Mapping) -> None:
    _require_nonempty_str_fields(
        body, "glossary", ("domain", "term", "created_at", "updated_at"),
    )


def _validate_definition_version(body: Mapping) -> None:
    _require_nonempty_str_fields(body, "definition_version", (
        "definition_version_id", "domain", "term", "strategy", "actor",
        "source_set_fingerprint", "created_at",
    ))
    if type(body["version"]) is not int or body["version"] < 1:
        raise PolicyError("definition_version.version: must be int >= 1")


def _validate_prompt_override(body: Mapping) -> None:
    _require_nonempty_str_fields(
        body, "prompt_override", ("scope", "step", "content", "created_at", "updated_at"),
    )
    if type(body["version"]) is not int or body["version"] < 1:
        raise PolicyError("prompt_override.version: must be int >= 1")


def _validate_study(body: Mapping) -> None:
    table = body["table"]
    columns = STUDY_TABLE_COLUMNS.get(table) if type(table) is str else None
    if columns is None:
        raise PolicyError(f"study.table: unknown study ledger table {table!r}")
    row = body["row"]
    if type(row) is not dict:
        raise PolicyError("study.row: must be an object")
    unknown = sorted(set(row) - columns)
    if unknown:
        raise PolicyError(f"study.row: columns not in allowlist: {unknown}")
    for key_column in STUDY_TABLE_PRIMARY_KEYS[table]:
        if type(row.get(key_column)) is not str or not row[key_column]:
            raise PolicyError(
                f"study.row: primary key column {key_column!r} must be a non-empty str"
            )


def _validate_user_config(body: Mapping) -> None:
    if body["kind"] not in USER_CONFIG_KINDS:
        raise PolicyError(f"user_config.kind: must be one of {sorted(USER_CONFIG_KINDS)}")
    # 配置路径按产物规则走 validate_output_path:相对、无穿越、不许碰 .flori。
    _wrap_manifest_error(validate_output_path, body["path"])
    _wrap_manifest_error(validate_digest, body["blob"], "user_config.blob")
    if type(body["size_bytes"]) is not int or body["size_bytes"] < 0:
        raise PolicyError("user_config.size_bytes: must be int >= 0")


def _validate_legacy_archive(body: Mapping) -> None:
    table = body["table"]
    if type(table) is not str or not _TABLE_NAME_RE.fullmatch(table):
        raise PolicyError("legacy_archive.table: invalid table name")
    if table in FORBIDDEN_TABLES:
        raise PolicyError(f"legacy_archive.table: {table!r} is forbidden material")
    if type(body["rows"]) is not list:
        raise PolicyError("legacy_archive.rows: must be an array")
    # 分片是可选的;出现一个就必须两个都在且自洽(§C5 超限分片不中止备份)。
    has_index = "chunk_index" in body
    if has_index != ("chunk_total" in body):
        raise PolicyError("legacy_archive: chunk_index and chunk_total must appear together")
    if has_index:
        index, total = body["chunk_index"], body["chunk_total"]
        if type(index) is not int or type(total) is not int or total < 1 \
                or not 0 <= index < total:
            raise PolicyError("legacy_archive: chunk_index must be int in [0, chunk_total)")


def _validate_job_relation(body: Mapping) -> None:
    """每 Job 一条的关系记录:P3 可按 Job diff 定位冲突,不必解整张 relations 摘要。"""
    _wrap_manifest_error(validate_job_id, body["job_id"])
    _wrap_manifest_error(validate_digest, body["core"], "job_relation.core")
    if "user_state" in body:
        _wrap_manifest_error(
            validate_digest, body["user_state"], "job_relation.user_state",
        )
    parts = body["parts"]
    if type(parts) is not list:
        raise PolicyError("job_relation.parts: must be an array")
    for index, digest in enumerate(parts):
        # parts 顺序即 part_index 顺序(§2.5-2),不排序也不去重。
        _wrap_manifest_error(validate_digest, digest, f"job_relation.parts[{index}]")
    step_results = body["step_results"]
    if type(step_results) is not dict:
        raise PolicyError("job_relation.step_results: must be an object")
    for key, digest in step_results.items():
        try:
            scope_key, step = parse_execution_step(key)
            execution_step_key(scope_key, step)
        except ValueError as exc:
            raise PolicyError(f"job_relation.step_results: invalid key {key!r}") from exc
        _wrap_manifest_error(validate_digest, digest, f"job_relation.step_results[{key}]")
    failures = body["failures"]
    if type(failures) is not list:
        raise PolicyError("job_relation.failures: must be an array")
    for index, digest in enumerate(failures):
        _wrap_manifest_error(validate_digest, digest, f"job_relation.failures[{index}]")
    if failures != sorted(failures):
        raise PolicyError("job_relation.failures: digests must be sorted")


def _validate_exec_id(value: object, field: str) -> str:
    """exec_id 是不透明关联键,不是路径片段。

    真实数据形如 ai-<worker>:<ts>:<rand>:<seq>,含冒号;套用 part_id 的路径片段
    正则会把整库 ai_usage 挡在门外,而且比 step_manifest 自己的 execution.exec_id
    校验(有界字符串)更严,两套门对同一个值给出不同结论。这里与 manifest 对齐:
    有界、无控制字符、无密钥样式。
    """
    if type(value) is not str or not value:
        raise PolicyError(f"{field}: must be a non-empty str")
    if len(value) > 200:
        raise PolicyError(f"{field}: length exceeds 200")
    if _has_control_chars(value):
        raise PolicyError(f"{field}: control characters are not allowed")
    scan_text_for_secrets(value, field)
    return value


def _has_control_chars(text: str) -> bool:
    return any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in text
    )


def _validate_ai_usage(body: Mapping) -> None:
    _validate_exec_id(body["exec_id"], "ai_usage.exec_id")
    _require_nonempty_str_fields(body, "ai_usage", ("created_at",))


def _validate_ai_task_log(body: Mapping) -> None:
    # 自然键为复合 (task_id, created_at, exec_id):task_id 单独不保证唯一,
    # import 侧按复合键做幂等。
    _require_nonempty_str_fields(
        body, "ai_task_log", ("task_id", "created_at", "exec_id"),
    )
    if "error" in body and body["error"] is not None:
        validate_audit_text(body["error"], "ai_task_log.error")


@dataclass(frozen=True)
class RecordPolicy:
    """单一 record kind 的入库契约:分类 + 字段 allowlist + 附加校验。"""
    kind: str
    category: str
    required: frozenset[str]
    optional: frozenset[str]
    extra_validator: Callable[[Mapping], None] | None = None

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


# §2.2 records/ 布局的 16 个 kind;字段集合来自 §2.4 A/B/C 与 v8 真实 Schema。
# 活动状态字段(jobs.status/progress_pct/error/updated_at、collections.job_count
# /last_sync* )与自增 rowid(ai_usage.id/ai_task_logs.id)刻意不在 allowlist:
# 出现即拒,身份用自然键表达。
RECORD_POLICIES: Mapping[str, RecordPolicy] = {
    policy.kind: policy
    for policy in (
        RecordPolicy(
            "job_core", CATEGORY_BUSINESS_FACT,
            frozenset({"id", "content_type", "pipeline", "created_at"}),
            frozenset({
                "document_kind", "url", "title", "domain", "source",
                "style_tags", "meta", "published_at", "lineage_key",
                "is_current", "parent_job_id", "source_digest",
                "pipeline_digest",
            }),
            _validate_job_core,
        ),
        RecordPolicy(
            "job_user_state", CATEGORY_BUSINESS_FACT,
            frozenset({"job_id"}),
            frozenset({"collection_id", "revision"}),
            _validate_job_user_state,
        ),
        RecordPolicy(
            "part_core", CATEGORY_BUSINESS_FACT,
            frozenset({"id", "job_id", "part_index", "created_at"}),
            frozenset({
                "title", "source_url", "source_ref", "source_digest",
                "source_blob", "size_bytes", "duration_ms", "meta", "updated_at",
            }),
            _validate_part_core,
        ),
        RecordPolicy(
            "step_result", CATEGORY_ARTIFACT,
            frozenset({"job_id", "scope_key", "step", "manifest", "output_blobs"}),
            frozenset(),
            _validate_step_result,
        ),
        RecordPolicy(
            "failure_event", CATEGORY_FAILURE_AUDIT,
            frozenset({"job_id", "scope_key", "step", "exec_id", "failed_at"}),
            frozenset({
                "generation", "attempt", "error_code", "error_class",
                "sanitized_message", "started_at", "duration_sec",
                "worker_class", "ai_usage_refs", "ai_task_log_refs",
                "log_blob", "partial_outputs_discarded", "partial_outputs",
            }),
            _validate_failure_event,
        ),
        RecordPolicy(
            "collection", CATEGORY_BUSINESS_FACT,
            frozenset({"id", "name", "created_at"}),
            frozenset({
                "domain", "description", "tags", "source_type", "source_id",
                "sync_enabled", "updated_at",
            }),
            _validate_collection,
        ),
        RecordPolicy(
            "ingested_item", CATEGORY_BUSINESS_FACT,
            frozenset({"collection_id", "item_id", "ingested_at"}),
            frozenset(),
            _validate_ingested_item,
        ),
        RecordPolicy(
            "glossary", CATEGORY_BUSINESS_FACT,
            frozenset({"domain", "term"}),
            frozenset({
                "definition", "zh_name", "aliases", "occurrences", "related",
                "status", "watched", "is_topic", "definition_locked",
                "lock_revision", "current_definition_version_id",
                "created_at", "updated_at",
            }),
            _validate_glossary,
        ),
        RecordPolicy(
            "definition_version", CATEGORY_BUSINESS_FACT,
            frozenset({
                "definition_version_id", "domain", "term", "version",
                "strategy", "actor", "source_set_fingerprint", "created_at",
            }),
            frozenset({
                "definition", "source_evidence_ids_json", "provider", "model",
                "prompt_hash", "input_hash", "supersedes_version_id",
            }),
            _validate_definition_version,
        ),
        RecordPolicy(
            "prompt_override", CATEGORY_BUSINESS_FACT,
            frozenset({"scope", "step", "version", "content"}),
            frozenset({"domain", "pipeline", "document_kind", "updated_at"}),
            _validate_prompt_override,
        ),
        RecordPolicy(
            "prompt_override_version", CATEGORY_BUSINESS_FACT,
            frozenset({"scope", "step", "version", "content", "created_at"}),
            frozenset({"domain", "pipeline", "document_kind", "note"}),
            _validate_prompt_override,
        ),
        RecordPolicy(
            "study", CATEGORY_BUSINESS_FACT,
            frozenset({"table", "row"}),
            frozenset(),
            _validate_study,
        ),
        RecordPolicy(
            "ai_usage", CATEGORY_BUSINESS_FACT,
            frozenset({"exec_id", "created_at"}),
            frozenset({
                "job_id", "step", "worker_id", "provider", "model",
                "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
                "cost_usd", "duration_sec", "num_turns", "cached",
            }),
            _validate_ai_usage,
        ),
        RecordPolicy(
            "ai_task_log", CATEGORY_BUSINESS_FACT,
            frozenset({"task_id", "created_at", "exec_id"}),
            frozenset({
                "step_name", "domain", "provider", "model", "ok",
                "error", "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
                "cost_usd", "duration_sec", "num_turns", "record_json",
            }),
            _validate_ai_task_log,
        ),
        RecordPolicy(
            "user_config", CATEGORY_BUSINESS_FACT,
            frozenset({"path", "kind", "blob", "size_bytes"}),
            frozenset({"media_type"}),
            _validate_user_config,
        ),
        RecordPolicy(
            "legacy_archive", CATEGORY_FAILURE_AUDIT,
            frozenset({"table", "rows"}),
            frozenset({"chunk_index", "chunk_total"}),
            _validate_legacy_archive,
        ),
        RecordPolicy(
            "job_relation", CATEGORY_BUSINESS_FACT,
            frozenset({"job_id", "core", "parts", "step_results", "failures"}),
            frozenset({"user_state"}),
            _validate_job_relation,
        ),
    )
}

RECORD_KINDS = frozenset(RECORD_POLICIES)


def validate_record(kind: str, body: object) -> bytes:
    """record 入库总门:allowlist + secret 扫描 + 附加校验,返回 canonical 字节。

    两道门在此串联:字段 allowlist(未知键即拒,含被排除的活动状态字段)是
    第一道;scan_json_for_secrets 是第二道。返回值供仓库层计算 record digest,
    保证 digest 一定来自已通过策略的字节。

    顶层值为 None 的可选键在 canonical 化前丢弃:DB NULL 与"未导出"归一为
    同一形态,同一逻辑行不会因 serializer 是否写出 null 产生两个 digest。
    只归一顶层;嵌套结构(如 manifest 内的显式 null)是各自 schema 的语义。
    """
    policy = RECORD_POLICIES.get(kind)
    if policy is None:
        raise PolicyError(f"record kind {kind!r} is not defined")
    if type(body) is not dict:
        raise PolicyError(f"{kind}: record body must be an object")
    missing = sorted(key for key in policy.required if key not in body or body[key] is None)
    if missing:
        raise PolicyError(f"{kind}: missing required fields {missing}")
    unknown = sorted(set(body) - policy.allowed)
    if unknown:
        raise PolicyError(f"{kind}: fields not in allowlist: {unknown}")
    normalized = {key: value for key, value in body.items() if value is not None}
    scan_json_for_secrets(normalized, kind)
    if policy.extra_validator is not None:
        policy.extra_validator(normalized)
    try:
        encoded = canonical_json_bytes(normalized)
    except ManifestError as exc:
        raise PolicyError(f"{kind}: {exc}") from exc
    if len(encoded) > MAX_RECORD_CANONICAL_BYTES:
        raise PolicyError(
            f"{kind}: canonical size {len(encoded)} exceeds {MAX_RECORD_CANONICAL_BYTES}"
        )
    return encoded


def record_blob_refs(kind: str, body: Mapping) -> tuple[str, ...]:
    """列出 record 引用的 blob digest,供 snapshot 可达性闭包与 GC mark 使用。"""
    if kind == "step_result":
        return tuple(sorted(set(body["output_blobs"].values())))
    if kind == "user_config":
        return (body["blob"],)
    if kind == "part_core" and body.get("source_blob") is not None:
        return (body["source_blob"],)
    if kind == "failure_event":
        log_blob = body.get("log_blob")
        return (log_blob,) if log_blob is not None else ()
    return ()
