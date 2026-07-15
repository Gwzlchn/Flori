"""受控取证下载、manifest 校验与笔记引用核验。"""

from __future__ import annotations

import hashlib
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from .provenance import (
    DIRECT_LOCATOR_POLICY,
    EXACT_QUOTE_POLICY,
    MAX_SEMANTIC_AI_LOG_BYTES,
    MAX_SEMANTIC_AI_LOG_RECORDS,
    SEMANTIC_ATTESTATION_POLICY,
    SEMANTIC_BATCH_COMMIT_PATH,
    MAX_NOTE_MAPPINGS,
    MAX_PROVENANCE_BYTES,
    MAX_SOURCE_ARTIFACTS,
    MAX_SOURCE_SEGMENTS,
    canonical_json,
    canonical_json_bytes,
    build_semantic_attestation_prompt,
    semantic_attestation_batch_id,
    sha256_bytes,
    validate_locator,
    validate_exact_quote_mapping,
    validate_provenance_manifest,
    validate_provenance_candidate_manifest,
    validate_semantic_batch_commit,
    validate_source_manifest,
)
from .source_support import (
    MAX_SUPPORT_ARTIFACT_BYTES,
    support_text_from_artifact,
)
from .storage import read_path_bounded, write_path_atomic


EVIDENCE_SCHEMA_VERSION = 2
# canonical_evidence 的 DB 契约仍是 v1;provenance sidecar 可独立演进。
CANONICAL_EVIDENCE_SCHEMA_VERSION = 1
MAX_CANONICAL_SIDECAR_BYTES = MAX_PROVENANCE_BYTES
MAX_CANONICAL_SOURCE_ARTIFACTS = MAX_SOURCE_ARTIFACTS
MAX_CANONICAL_SEGMENTS = min(MAX_SOURCE_SEGMENTS, MAX_NOTE_MAPPINGS)
MAX_EVIDENCE_BYTES = 1_048_576
MAX_TOTAL_EVIDENCE_BYTES = 4 * 1_048_576
MAX_EVIDENCE_ITEMS = 12
MAX_MECHANICAL_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOWED_MIME = {"text/html", "text/plain", "text/markdown"}
_OFFICIAL_HOST_SUFFIXES = (
    ".gov.cn", ".court.gov.cn", ".csrc.gov.cn", ".sse.com.cn", ".szse.cn",
)
_OFFICIAL_HOSTS = {"gov.cn", "court.gov.cn", "csrc.gov.cn", "sse.com.cn", "szse.cn"}
_CASE_REF_RE = re.compile(r"[〔\[（(]\s*20\d{2}\s*[〕\]）)][^，。\s]{0,8}?\d{1,4}\s*号")
_CURRENCY_PATTERN = (
    r"人民币|美元|欧元|港元|英镑|日元|USD|EUR|CNY|RMB|HKD|GBP|JPY|￥|¥|\$|€|£"
)
_QUANTITY_UNIT_PATTERN = (
    r"万亿元|亿元|万元|千元|亿股|万股|千股|个百分点|百分点|"
    r"公斤|千克|克|吨|毫克|公里|千米|米|厘米|毫米|平方米|立方米|"
    r"元|股|亿|万|千|[%％]|倍|人|家|件|次|天|年|月|日|小时|分钟|秒|号"
)
_QUANTITY_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<sign>[-+−]?)\s*"
    rf"(?P<prefix>{_CURRENCY_PATTERN})?\s*"
    rf"(?P<number>\d[\d,]*(?:\.\d+)?)"
    rf"(?:\s*(?P<suffix>{_CURRENCY_PATTERN}|{_QUANTITY_UNIT_PATTERN}|"
    rf"[A-Za-z\u4e00-\u9fff°℃℉µμΩ][A-Za-z0-9\u4e00-\u9fff°℃℉µμΩ/^·²³_-]{{0,15}}))?",
    re.IGNORECASE,
)
_CURRENCY_ALIASES = {
    "￥": "CNY", "¥": "CNY", "人民币": "CNY", "CNY": "CNY", "RMB": "CNY",
    "$": "USD", "美元": "USD", "USD": "USD",
    "€": "EUR", "欧元": "EUR", "EUR": "EUR",
    "港元": "HKD", "HKD": "HKD",
    "£": "GBP", "英镑": "GBP", "GBP": "GBP",
    "日元": "JPY", "JPY": "JPY",
}
_MANIFEST_FIELDS = {
    "schema_version", "job_id", "ocr_refs", "evidence", "rejected",
    "total_bytes", "candidate_parse_failed", "provider",
}
# 普通 E 前缀标签不是证据引用。abc 仅保留为既有畸形引用的显式拒绝样例。
_EVIDENCE_REF_RE = re.compile(
    r"\[(E(?:\s*\d*\s*|-\d+|abc))\](?![\(\[])",
)


class CanonicalEvidenceError(ValueError):
    """显式 provenance 无法安全映射到当前 job 产物。"""


def _canonical_json(value: Any) -> str:
    return canonical_json(value)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_fingerprint(value: Any) -> str:
    return _sha256_hex(_canonical_json(value).encode("utf-8"))


def canonical_source_fingerprint(source_identity: dict[str, Any]) -> str:
    """从已重验来源身份生成稳定指纹，不包含有效性和链接。"""
    return _canonical_fingerprint(source_identity)


def canonical_evidence_fingerprint(evidence_identity: dict[str, Any]) -> str:
    """从内容绑定身份生成证据指纹，产物变化会得到新指纹。"""
    return _canonical_fingerprint(evidence_identity)


def canonical_evidence_id(identity: dict[str, Any]) -> str:
    """生成可复算 ID；validation 状态与服务端派生链接不进入身份。"""
    expected = {
        "schema_version", "job_id", "note_type", "chunk_id",
        "source_ref", "source_segment_id", "evidence_fingerprint",
    }
    if not isinstance(identity, dict) or set(identity) != expected:
        raise CanonicalEvidenceError("canonical evidence identity fields are invalid")
    return "ce_" + _canonical_fingerprint(identity)


def _safe_job_relative(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CanonicalEvidenceError("artifact path must be a normalized relative path")
    path = Path(value)
    if path.is_absolute() or "\x00" in value or any(part in {"", ".", ".."} for part in path.parts):
        raise CanonicalEvidenceError("artifact path escapes job root")
    return value


def validate_canonical_locator(
    value: Any,
    *,
    source_artifact: dict[str, Any],
) -> dict[str, Any]:
    """复用 provenance v1 的唯一 locator 规则。"""
    try:
        return validate_locator(value, source_artifact)
    except ValueError as exc:
        raise CanonicalEvidenceError(str(exc)) from exc


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _same_json_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right)
        )
    return left == right


def _valid_evidence_id(value: Any) -> bool:
    """E# 与生产落盘的最多 12 个候选同界,验证前不解析任意长整数。"""
    if type(value) is not str or re.fullmatch(r"E[1-9]\d?", value) is None:
        return False
    return int(value[1:]) <= MAX_EVIDENCE_ITEMS


def _canonical_url(url: str) -> str:
    raw = (url or "").strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("evidence URL contains control characters")
    parsed = urlsplit(raw)
    has_userinfo = parsed.username is not None or getattr(parsed, "password", None) is not None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or has_userinfo:
        raise ValueError("evidence URL must be http(s), have a host, and contain no userinfo")
    host = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            address = None
    if address is not None:
        if (
            not address.is_global or address.is_private or address.is_loopback
            or address.is_link_local or address.is_multicast or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("evidence URL IP must be global")
        host = address.compressed
        netloc_host = f"[{host}]" if address.version == 6 else host
    else:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("evidence URL host is invalid") from exc
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
            raise ValueError("evidence URL host is local-only")
        netloc_host = host
    port = parsed.port
    if port == 0:
        raise ValueError("evidence URL port is invalid")
    netloc = netloc_host if port is None else f"{netloc_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def _resolve_global(host: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> list[str]:
    try:
        infos = resolver(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"evidence host cannot be resolved: {host}") from exc
    addresses = sorted({str(item[4][0]).split("%", 1)[0] for item in infos})
    if not addresses:
        raise ValueError(f"evidence host has no address: {host}")
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if (
            not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or ip.is_loopback or ip.is_link_local or ip.is_private
        ):
            raise ValueError(f"evidence host resolves to non-global address: {host}")
    return addresses


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def canonical_markdown(data: bytes, mime: str, charset: str = "utf-8") -> str:
    """把受控响应归一成稳定 UTF-8 Markdown 文本。"""
    try:
        text = data.decode(charset, errors="replace")
    except LookupError as exc:
        raise ValueError(f"evidence charset is not supported: {charset}") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if mime == "text/html":
        parser = _TextExtractor()
        parser.feed(text)
        text = html.unescape("".join(parser.parts))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("evidence response has no readable text")
    return text + "\n"


class SafeEvidenceFetcher:
    """禁代理并逐跳校验 DNS/redirect/MIME/大小的证据下载器。"""

    def __init__(self, *, resolver: Callable[..., Any] = socket.getaddrinfo, client: Any = None):
        self.resolver = resolver
        self._client = client

    @staticmethod
    def _pinned_request(url: str, address: str):
        """连接已验证 IP,HTTPS 仍以原 hostname 做 SNI/证书校验,消除二次 DNS 窗口。"""
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = socket.create_connection((address, port), timeout=20.0)
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        connection = http.client.HTTPConnection(address, port, timeout=20.0)
        connection.sock = sock
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={
                "Host": parsed.netloc,
                "User-Agent": "Flori-Evidence/2",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            body = response.read(MAX_EVIDENCE_BYTES + 1)
            headers = {key.lower(): value for key, value in response.getheaders()}
            status = response.status
        finally:
            connection.close()
        return status, headers, body

    def fetch(self, url: str) -> dict[str, Any]:
        original = _canonical_url(url)
        current = original
        for redirect_count in range(MAX_REDIRECTS + 1):
            parsed = urlsplit(current)
            addresses = _resolve_global(parsed.hostname or "", self.resolver)
            if self._client is None:
                last_error: Exception | None = None
                for address in addresses:
                    try:
                        status, headers, raw_body = self._pinned_request(current, address)
                        break
                    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                        last_error = exc
                else:
                    raise ValueError("evidence host has no reachable validated address") from last_error
                response_ctx = None
            else:
                response_ctx = self._client.stream(
                    "GET", current, headers={"User-Agent": "Flori-Evidence/2"},
                )
            if response_ctx is not None:
                with response_ctx as response:
                    status = response.status_code
                    headers = response.headers
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > MAX_EVIDENCE_BYTES:
                            raise ValueError("evidence response exceeds size limit")
                        body.extend(chunk)
                    raw_body = bytes(body)
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise ValueError("evidence redirect is invalid or exceeds limit")
                current = _canonical_url(urljoin(current, location))
                continue
            if status < 200 or status >= 300:
                raise ValueError(f"evidence HTTP status is not successful: {status}")
            content_encoding = headers.get("content-encoding", "").strip().lower()
            if content_encoding not in {"", "identity"}:
                raise ValueError(f"evidence content encoding is not allowed: {content_encoding}")
            content_type = headers.get("content-type", "")
            mime = content_type.split(";", 1)[0].lower().strip()
            if mime not in ALLOWED_MIME:
                raise ValueError(f"evidence MIME is not allowed: {mime or '<missing>'}")
            if len(raw_body) > MAX_EVIDENCE_BYTES:
                raise ValueError("evidence response exceeds size limit")
            charset_match = re.search(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"'\s]+)", content_type, re.I)
            charset = charset_match.group(1).lower() if charset_match else "utf-8"
            text = canonical_markdown(raw_body, mime, charset)
            return {
                "original_url": original,
                "final_url": current,
                "resolved_addresses": addresses,
                "redirects": redirect_count,
                "mime": mime,
                "charset": charset,
                "text": text,
            }
        raise ValueError("evidence redirect loop")


def _source_confidence(url: str, original_url: str | None = None) -> tuple[str, str, bool]:
    parsed = urlsplit(url)
    original = urlsplit(original_url) if original_url is not None else None
    host = (parsed.hostname or "").lower()
    transport_safe = parsed.scheme == "https" and (
        original is None or original.scheme == "https"
    )
    if transport_safe and (host.endswith(_OFFICIAL_HOST_SUFFIXES) or host in _OFFICIAL_HOSTS):
        return "一手官方", "high", True
    return "外部来源", "low", False


def extract_case_refs(text: str) -> list[str]:
    """从当前机械稿重新提取案号/文号;不接受 manifest 自报锚点。"""
    return sorted({match.strip() for match in _CASE_REF_RE.findall(text or "")})


def _safe_artifact_rel(rel: Any) -> bool:
    return (
        isinstance(rel, str)
        and rel.startswith("output/evidence/")
        and "\x00" not in rel
        and ".." not in Path(rel).parts
        and not Path(rel).is_absolute()
    )


def _canonical_evidence_artifact(evidence_id: str) -> str:
    if not _valid_evidence_id(evidence_id):
        raise ValueError("invalid evidence id")
    number = int(evidence_id[1:])
    return f"output/evidence/evidence-{number:02d}.md"


def _manifest_envelope_errors(manifest: dict[str, Any]) -> list[str]:
    """严格校验 manifest 顶层;候选解析失败时整份信任链不可验证。"""
    errors: list[str] = []
    if set(manifest) != _MANIFEST_FIELDS:
        errors.append("manifest_top_level_fields_invalid")
    refs = manifest.get("ocr_refs")
    if (
        not isinstance(refs, list)
        or any(type(ref) is not str or not ref.strip() for ref in refs)
    ):
        errors.append("manifest_ocr_refs_invalid")
    rejected = manifest.get("rejected")
    if (
        not isinstance(rejected, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"url", "reason"}
            or type(item.get("url")) is not str
            or type(item.get("reason")) is not str
            for item in rejected
        )
        or len(rejected) > MAX_EVIDENCE_ITEMS
    ):
        errors.append("manifest_rejected_invalid")
    raw_items = manifest.get("evidence")
    if isinstance(raw_items, list) and len(raw_items) > MAX_EVIDENCE_ITEMS:
        errors.append("too_many_evidence_items")
    if (
        isinstance(raw_items, list)
        and isinstance(rejected, list)
        and len(raw_items) + len(rejected) > MAX_EVIDENCE_ITEMS
    ):
        errors.append("manifest_candidate_count_invalid")
    total_bytes = manifest.get("total_bytes")
    if type(total_bytes) is not int or total_bytes < 0:
        errors.append("manifest_total_bytes_invalid")
    elif total_bytes > MAX_TOTAL_EVIDENCE_BYTES:
        errors.append("evidence_total_too_large")
    parse_failed = manifest.get("candidate_parse_failed")
    if type(parse_failed) is not bool:
        errors.append("candidate_parse_failed_invalid")
    elif parse_failed:
        errors.append("candidate_parse_failed")
    provider = manifest.get("provider")
    if type(provider) is not str or not provider.strip():
        errors.append("manifest_provider_invalid")
    return errors


def _derived_evidence_item(
    item: dict[str, Any], job_id: str, data: bytes, anchors: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """从 URL、当前文件和机械稿锚点重算可用性;自报字段只用于对账。"""
    evidence_id = str(item.get("id") or "")
    errors: list[str] = []
    if item.get("job_id") != job_id:
        errors.append(f"cross_job_item:{evidence_id}")
    try:
        final_url = _canonical_url(str(item.get("final_url") or ""))
    except ValueError:
        errors.append(f"unsafe_final_url:{evidence_id}")
        return None, errors
    if final_url != item.get("final_url"):
        errors.append(f"noncanonical_final_url:{evidence_id}")
    try:
        original_url = _canonical_url(str(item.get("original_url") or ""))
    except ValueError:
        errors.append(f"unsafe_original_url:{evidence_id}")
        return None, errors
    if original_url != item.get("original_url"):
        errors.append(f"noncanonical_original_url:{evidence_id}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"artifact_not_utf8:{evidence_id}")
        return None, errors
    if (
        type(item.get("sha256")) is not str
        or _sha256(data) != item.get("sha256")
        or type(item.get("bytes")) is not int
        or len(data) != item.get("bytes")
    ):
        errors.append(f"artifact_tampered:{evidence_id}")
    if type(item.get("chars")) is not int or len(text) != item.get("chars"):
        errors.append(f"artifact_chars_mismatch:{evidence_id}")
    source_tier, base_confidence, authoritative = _source_confidence(final_url, original_url)
    matches = [
        {"anchor": anchor, "offset": text.find(anchor)}
        for anchor in anchors if anchor in text
    ]
    eligible = authoritative and bool(matches)
    confidence = base_confidence if eligible else "low"
    if not authoritative:
        reasons = ["source_not_authoritative"]
    elif not anchors:
        reasons = ["no_case_anchor"]
    elif not matches:
        reasons = ["case_anchor_not_found"]
    else:
        reasons = []
    expected = {
        "source_tier": source_tier,
        "confidence": confidence,
        "eligible": eligible,
        "eligibility_reasons": reasons,
        "matches": matches,
    }
    for field, value in expected.items():
        if not _same_json_value(item.get(field), value):
            errors.append(f"derived_{field}_mismatch:{evidence_id}")
    if errors or not eligible:
        if not eligible:
            errors.append(f"ineligible_evidence:{evidence_id}")
        return None, errors
    return {
        **item, "original_url": original_url, "final_url": final_url, **expected,
    }, []


def materialize_evidence(
    job_dir: Path,
    job_id: str,
    candidates: list[dict[str, Any]],
    *,
    fetcher: SafeEvidenceFetcher | None = None,
    anchors: list[str] | None = None,
) -> dict[str, Any]:
    """下载模型候选并落稳定 E# 文件;模型字段不决定 eligibility/confidence。"""
    fetcher = fetcher or SafeEvidenceFetcher()
    evidence_dir = job_dir / "output" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    total_bytes = 0
    anchors = [anchor.strip() for anchor in (anchors or []) if anchor.strip()]
    for candidate in candidates[:MAX_EVIDENCE_ITEMS]:
        url = candidate.get("url") if isinstance(candidate, dict) else None
        if not isinstance(url, str):
            rejected.append({"url": "", "reason": "candidate URL missing"})
            continue
        try:
            fetched = fetcher.fetch(url)
        except Exception as exc:
            rejected.append({"url": url[:500], "reason": str(exc)[:500]})
            continue
        number = len(items) + 1
        evidence_id = f"E{number}"
        rel = f"output/evidence/evidence-{number:02d}.md"
        data = fetched.pop("text").encode("utf-8")
        if total_bytes + len(data) > MAX_TOTAL_EVIDENCE_BYTES:
            rejected.append({"url": url[:500], "reason": "total evidence size limit exceeded"})
            break
        total_bytes += len(data)
        write_path_atomic(job_dir / rel, data)
        source_tier, confidence, authoritative = _source_confidence(
            fetched["final_url"], fetched["original_url"],
        )
        text = data.decode("utf-8")
        matches = [
            {"anchor": anchor, "offset": text.find(anchor)}
            for anchor in anchors if anchor in text
        ]
        eligible = authoritative and bool(matches)
        if not authoritative:
            eligibility_reasons = ["source_not_authoritative"]
        elif not anchors:
            eligibility_reasons = ["no_case_anchor"]
        elif not matches:
            eligibility_reasons = ["case_anchor_not_found"]
        else:
            eligibility_reasons = []
        if not eligible:
            confidence = "low"
        items.append({
            "id": evidence_id,
            "job_id": job_id,
            "title": str(candidate.get("title") or fetched["final_url"]),
            "publisher": str(candidate.get("publisher") or ""),
            "artifact": rel,
            "sha256": _sha256(data),
            "bytes": len(data),
            "chars": len(data.decode("utf-8")),
            "source_tier": source_tier,
            "confidence": confidence,
            "eligible": eligible,
            "eligibility_reasons": eligibility_reasons,
            "matches": matches,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            **fetched,
        })
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "job_id": job_id,
        "evidence": items,
        "rejected": rejected,
        "total_bytes": total_bytes,
    }


def validate_manifest_loaded(
    job_dir: Path, job_id: str, manifest: Any, *, mechanical_text: str | None = None,
) -> tuple[dict[str, dict], list[str], dict[str, str]]:
    """单次有界加载并重算 manifest;返回验证项、错误与已验证全文。"""
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
        return {}, ["legacy_or_invalid_schema"], {}
    if manifest.get("job_id") != job_id:
        return {}, ["cross_job_manifest"], {}
    raw_items = manifest.get("evidence")
    if not isinstance(raw_items, list):
        return {}, ["invalid_manifest_items"], {}
    errors = _manifest_envelope_errors(manifest)
    if errors:
        return {}, errors, {}
    items = [item for item in raw_items if isinstance(item, dict)]
    if len(items) != len(raw_items):
        errors.append("invalid_manifest_item")
    id_counts: dict[str, int] = {}
    for item in items:
        evidence_id = item.get("id")
        if isinstance(evidence_id, str):
            id_counts[evidence_id] = id_counts.get(evidence_id, 0) + 1
    reported_duplicates: set[str] = set()
    root = job_dir.resolve()
    prepared: list[tuple[dict[str, Any], str, Path]] = []
    declared_total = 0
    for item in items:
        evidence_id = item.get("id")
        if not _valid_evidence_id(evidence_id):
            errors.append("invalid_evidence_id")
            continue
        if id_counts.get(evidence_id) != 1:
            if evidence_id not in reported_duplicates:
                errors.append(f"duplicate_evidence_id:{evidence_id}")
                reported_duplicates.add(evidence_id)
            continue
        rel = item.get("artifact")
        if not _safe_artifact_rel(rel):
            errors.append(f"invalid_artifact_path:{evidence_id}")
            continue
        if rel != _canonical_evidence_artifact(evidence_id):
            errors.append(f"evidence_artifact_id_mismatch:{evidence_id}")
            continue
        declared_size = item.get("bytes")
        if type(declared_size) is not int or declared_size < 0:
            errors.append(f"artifact_tampered:{evidence_id}")
            continue
        declared_total += declared_size
        if declared_size > MAX_EVIDENCE_BYTES:
            errors.append(f"evidence_item_too_large:{evidence_id}")
            continue
        path = job_dir / rel
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            errors.append(f"missing_artifact:{evidence_id}")
            continue
        if root not in resolved.parents or path.is_symlink():
            errors.append(f"artifact_escape:{evidence_id}")
            continue
        prepared.append((item, evidence_id, path))
    if declared_total > MAX_TOTAL_EVIDENCE_BYTES:
        errors.append("evidence_total_too_large")
    if manifest.get("total_bytes") != declared_total:
        errors.append("manifest_total_bytes_mismatch")
    if errors:
        return {}, errors, {}

    if mechanical_text is None:
        mechanical = job_dir / "output/notes_mechanical.md"
        try:
            mechanical_data = read_path_bounded(
                mechanical, MAX_MECHANICAL_EVIDENCE_BYTES, trusted_root=job_dir,
            )
            if len(mechanical_data) > MAX_MECHANICAL_EVIDENCE_BYTES:
                return {}, ["mechanical_source_too_large"], {}
            mechanical_text = mechanical_data.decode("utf-8")
        except UnicodeDecodeError:
            return {}, ["missing_or_invalid_mechanical_source"], {}
        except OSError:
            return {}, ["missing_or_invalid_mechanical_source"], {}
    elif len(mechanical_text.encode("utf-8")) > MAX_MECHANICAL_EVIDENCE_BYTES:
        return {}, ["mechanical_source_too_large"], {}
    anchors = extract_case_refs(mechanical_text)
    if not _same_json_value(manifest.get("ocr_refs"), anchors):
        return {}, ["manifest_ocr_refs_mismatch"], {}

    result: dict[str, dict] = {}
    texts: dict[str, str] = {}
    actual_total = 0
    for item, evidence_id, path in prepared:
        try:
            data = read_path_bounded(
                path, MAX_EVIDENCE_BYTES, trusted_root=job_dir,
            )
        except OSError:
            errors.append(f"missing_artifact:{evidence_id}")
            continue
        if len(data) > MAX_EVIDENCE_BYTES:
            errors.append(f"evidence_item_too_large:{evidence_id}")
            continue
        actual_total += len(data)
        if actual_total > MAX_TOTAL_EVIDENCE_BYTES:
            errors.append("evidence_total_too_large")
            return {}, errors, {}
        derived, item_errors = _derived_evidence_item(item, job_id, data, anchors)
        errors.extend(item_errors)
        if derived is not None:
            result[evidence_id] = derived
            texts[evidence_id] = data.decode("utf-8")
    if manifest.get("total_bytes") != actual_total:
        errors.append("manifest_total_bytes_mismatch")
    if "manifest_total_bytes_mismatch" in errors:
        result.clear()
        texts.clear()
    return result, errors, texts


def validate_manifest(job_dir: Path, job_id: str, manifest: Any) -> tuple[dict[str, dict], list[str]]:
    """按当前机械稿、URL 和文件重算 manifest;所有自报信任字段都对账。"""
    result, errors, _ = validate_manifest_loaded(job_dir, job_id, manifest)
    return result, errors


async def validate_manifest_with_reader(
    job_id: str,
    manifest: Any,
    read_file: Callable[[str, int], Awaitable[bytes | None]],
) -> tuple[dict[str, dict], list[str]]:
    """StorageBackend 版 manifest 重验,与本地 Path 版共用派生规则。"""
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
        return {}, ["legacy_or_invalid_schema"]
    if manifest.get("job_id") != job_id:
        return {}, ["cross_job_manifest"]
    raw_items = manifest.get("evidence")
    if not isinstance(raw_items, list):
        return {}, ["invalid_manifest_items"]
    errors = _manifest_envelope_errors(manifest)
    if errors:
        return {}, errors
    items = [item for item in raw_items if isinstance(item, dict)]
    if len(items) != len(raw_items):
        errors.append("invalid_manifest_item")
        return {}, errors
    counts: dict[str, int] = {}
    for item in items:
        evidence_id = item.get("id")
        if isinstance(evidence_id, str):
            counts[evidence_id] = counts.get(evidence_id, 0) + 1
    reported_duplicates: set[str] = set()
    prepared: list[tuple[dict[str, Any], str]] = []
    declared_total = 0
    for item in items:
        evidence_id = item.get("id")
        if not _valid_evidence_id(evidence_id):
            errors.append("invalid_evidence_id")
            continue
        if counts.get(evidence_id) != 1:
            if evidence_id not in reported_duplicates:
                errors.append(f"duplicate_evidence_id:{evidence_id}")
                reported_duplicates.add(evidence_id)
            continue
        rel = item.get("artifact")
        if not _safe_artifact_rel(rel):
            errors.append(f"invalid_artifact_path:{evidence_id}")
            continue
        if rel != _canonical_evidence_artifact(evidence_id):
            errors.append(f"evidence_artifact_id_mismatch:{evidence_id}")
            continue
        declared_size = item.get("bytes")
        if type(declared_size) is not int or declared_size < 0:
            errors.append(f"artifact_tampered:{evidence_id}")
            continue
        declared_total += declared_size
        if declared_size > MAX_EVIDENCE_BYTES:
            errors.append(f"evidence_item_too_large:{evidence_id}")
            continue
        prepared.append((item, evidence_id))
    if declared_total > MAX_TOTAL_EVIDENCE_BYTES:
        errors.append("evidence_total_too_large")
    if declared_total != manifest.get("total_bytes"):
        errors.append("manifest_total_bytes_mismatch")
    if errors:
        return {}, errors
    try:
        mechanical = await read_file("output/notes_mechanical.md")
    except (OSError, ValueError):
        mechanical = None
    if mechanical is not None and not isinstance(mechanical, bytes):
        return {}, ["missing_or_invalid_mechanical_source"]
    if mechanical is not None and len(mechanical) > MAX_MECHANICAL_EVIDENCE_BYTES:
        return {}, ["mechanical_source_too_large"]
    try:
        mechanical_text = mechanical.decode("utf-8") if mechanical is not None else ""
    except UnicodeDecodeError:
        mechanical_text = ""
    if not mechanical_text:
        return {}, ["missing_or_invalid_mechanical_source"]
    anchors = extract_case_refs(mechanical_text)
    if not _same_json_value(manifest.get("ocr_refs"), anchors):
        return {}, ["manifest_ocr_refs_mismatch"]
    actual_total = 0
    result: dict[str, dict] = {}
    for item, evidence_id in prepared:
        rel = item["artifact"]
        try:
            data = await read_file(rel)
        except (OSError, ValueError):
            data = None
        if data is None:
            errors.append(f"missing_artifact:{evidence_id}")
            continue
        if not isinstance(data, bytes):
            errors.append(f"artifact_type_invalid:{evidence_id}")
            continue
        if len(data) > MAX_EVIDENCE_BYTES:
            errors.append(f"evidence_item_too_large:{evidence_id}")
            return {}, errors
        actual_total += len(data)
        if actual_total > MAX_TOTAL_EVIDENCE_BYTES:
            errors.append("evidence_total_too_large")
            return {}, errors
        derived, item_errors = _derived_evidence_item(item, job_id, data, anchors)
        errors.extend(item_errors)
        if derived is not None:
            result[evidence_id] = derived
    if manifest.get("total_bytes") != actual_total:
        errors.append("manifest_total_bytes_mismatch")
    if "manifest_total_bytes_mismatch" in errors:
        result.clear()
    return result, errors


def blocking_manifest_errors(errors: list[str]) -> list[str]:
    """返回会破坏 manifest 信任链的错误;合法低可信候选只保留诊断。"""
    return [
        error for error in errors
        if not error.startswith("ineligible_evidence:")
    ]


def _quantity_tokens(text: str) -> list[str]:
    """把数值与符号、币种、数量级/单位绑成不可拆的核验 token。"""
    result = []
    for match in _QUANTITY_RE.finditer(text or ""):
        sign = "-" if match.group("sign") in {"-", "−"} else match.group("sign")
        number = match.group("number").replace(",", "")
        prefix = match.group("prefix") or ""
        suffix = match.group("suffix") or ""
        prefix_key = prefix.upper() if prefix.isascii() else prefix
        suffix_key = suffix.upper() if suffix.isascii() else suffix
        prefix_currency = _CURRENCY_ALIASES.get(prefix_key, "")
        suffix_currency = _CURRENCY_ALIASES.get(suffix_key, "")
        if prefix_currency and suffix_currency and prefix_currency != suffix_currency:
            currency = f"{prefix_currency}>{suffix_currency}"
        else:
            currency = prefix_currency or suffix_currency
        unit = "" if suffix_currency else suffix.replace("％", "%")
        if unit.isascii():
            unit = unit.lower()
        result.append(f"{sign}{currency}{number}{unit}")
    return result


def _citation_claim(note: str, start: int, end: int) -> str:
    """绑定引用行及其确定性上下文;上下文无法逐字命中时宁可 unverified。"""
    del end
    lines = note.splitlines(keepends=True) or [note]
    offset = 0
    line_index = len(lines) - 1
    for index, line in enumerate(lines):
        if start < offset + len(line):
            line_index = index
            break
        offset += len(line)
    current = lines[line_index]
    context = []
    if current.lstrip().startswith("|"):
        first = line_index
        while first > 0 and lines[first - 1].lstrip().startswith("|"):
            first -= 1
        header = next(
            (line for line in lines[first:line_index]
             if not _is_markdown_table_separator(line)),
            None,
        )
        if header is not None:
            context.append(header)
    else:
        previous = _citation_context_line(lines, line_index)
        if previous is not None:
            context.append(previous)
    context.append(current)
    return _normalize_citation_text("".join(context))


def _citation_context_line(lines: list[str], line_index: int) -> str | None:
    """普通段落取紧邻前行;列表允许跨一个空行回溯标题。"""
    current_is_list = re.match(r"^\s*(?:[-*+]|\d+[.)、])\s+", lines[line_index]) is not None
    blank_seen = 0
    for index in range(line_index - 1, max(-1, line_index - 8), -1):
        candidate = lines[index]
        if not candidate.strip():
            blank_seen += 1
            if not current_is_list or blank_seen > 1:
                break
            continue
        if current_is_list and re.match(r"^\s*(?:[-*+]|\d+[.)、])\s+", candidate):
            continue
        return candidate
    return None


def _is_markdown_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _normalize_citation_text(text: str) -> str:
    normalized_lines = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or _is_markdown_table_separator(line):
            continue
        if line.startswith("|"):
            line = " ".join(cell.strip() for cell in line.strip("|").split("|") if cell.strip())
        else:
            line = re.sub(r"^\s*#{1,6}\s*", "", line)
            line = re.sub(r"^\s*(?:[-*+>]|\d+[.)、])\s*", "", line)
        normalized_lines.append(line)
    text = re.sub(r"\[E(?:[1-9]|1[0-2])\]", "", " ".join(normalized_lines))
    text = text.replace("`", "").replace("*", "").replace("_", "").replace("~", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n,，.。;；:：!?！？()（）[]【】")


def normalize_citation_text(text: str) -> str:
    """公开确定性引用文本归一化,供不同引用标签的薄适配器复用。"""
    return _normalize_citation_text(text)


def _claim_has_semantic_context(claim: str) -> bool:
    remainder = _QUANTITY_RE.sub("", claim)
    semantic = re.sub(r"[^A-Za-z\u4e00-\u9fff]", "", remainder)
    return len(semantic) >= 2


def _validate_citations_loaded(
    note: str,
    occurrences: list[re.Match[str]],
    by_id: dict[str, dict],
    artifact_texts: dict[str, str],
    manifest_errors: list[str],
) -> dict[str, Any]:
    items = []
    status = "valid"
    for match in occurrences:
        evidence_id = match.group(1)
        item = by_id.get(evidence_id)
        entry = {"id": evidence_id, "offset": match.start(), "status": "valid", "errors": []}
        if not _valid_evidence_id(evidence_id):
            entry["status"] = "invalid"
            entry["errors"].append("invalid_evidence_id")
            status = "invalid"
        elif item is None or evidence_id not in artifact_texts:
            entry["status"] = "invalid"
            entry["errors"].append("unknown_or_ineligible_evidence")
            status = "invalid"
        else:
            claim = _citation_claim(note, match.start(), match.end())
            quantities = _quantity_tokens(claim)
            source_quantities = set(_quantity_tokens(artifact_texts[evidence_id]))
            missing = [quantity for quantity in quantities if quantity not in source_quantities]
            if missing:
                entry["status"] = "invalid"
                entry["errors"].append("quantity_mismatch:" + ",".join(missing[:5]))
                status = "invalid"
            elif not quantities:
                entry["status"] = "unverified_semantic"
                entry["errors"].append("no_mechanical_locator")
                if status != "invalid":
                    status = "unverified"
            elif not _claim_has_semantic_context(claim):
                entry["status"] = "unverified_semantic"
                entry["errors"].append("insufficient_claim_context")
                if status != "invalid":
                    status = "unverified"
            elif claim not in _normalize_citation_text(artifact_texts[evidence_id]):
                entry["status"] = "unverified_semantic"
                entry["errors"].append("claim_not_found")
                if status != "invalid":
                    status = "unverified"
        items.append(entry)
    return {
        "status": status,
        "checked": len(occurrences),
        "items": items,
        "manifest_errors": manifest_errors,
    }


def validate_citations(job_dir: Path, job_id: str, note: str, manifest: Any) -> dict[str, Any]:
    """核验 [E#] 当前文件与同句数值+单位;无定位时返回 unverified。"""
    occurrences = list(_EVIDENCE_REF_RE.finditer(note or ""))
    if not occurrences:
        if manifest is None:
            return {"status": "not_applicable", "checked": 0, "items": []}
    by_id, manifest_errors, texts = validate_manifest_loaded(job_dir, job_id, manifest)
    return validate_citations_from_loaded(
        note, by_id, texts, manifest_errors, occurrences=occurrences,
    )


def validate_citations_from_loaded(
    note: str,
    by_id: dict[str, dict],
    texts: dict[str, str],
    manifest_errors: list[str],
    *,
    occurrences: list[re.Match] | None = None,
) -> dict[str, Any]:
    """使用同一次 manifest 加载结果核验引用,不再次打开证据文件。"""
    occurrences = occurrences if occurrences is not None else list(
        _EVIDENCE_REF_RE.finditer(note or ""),
    )
    if not occurrences:
        result = {
            "status": (
                "invalid" if blocking_manifest_errors(manifest_errors)
                else "not_applicable"
            ),
            "checked": 0,
            "items": [],
        }
        if manifest_errors:
            result["manifest_errors"] = manifest_errors
        return result
    return _validate_citations_loaded(note, occurrences, by_id, texts, manifest_errors)


async def validate_citations_with_reader(
    job_id: str,
    note: str,
    manifest: Any,
    read_file: Callable[[str], Awaitable[bytes | None]],
) -> dict[str, Any]:
    """StorageBackend 版引用重验,供 API/调度器读时 fail-closed。"""
    occurrences = list(_EVIDENCE_REF_RE.finditer(note or ""))
    if not occurrences:
        if manifest is None:
            return {"status": "not_applicable", "checked": 0, "items": []}
        _, manifest_errors = await validate_manifest_with_reader(job_id, manifest, read_file)
        result = {
            "status": (
                "invalid" if blocking_manifest_errors(manifest_errors)
                else "not_applicable"
            ),
            "checked": 0,
            "items": [],
        }
        if manifest_errors:
            result["manifest_errors"] = manifest_errors
        return result
    by_id, manifest_errors = await validate_manifest_with_reader(job_id, manifest, read_file)
    texts: dict[str, str] = {}
    for evidence_id, item in by_id.items():
        try:
            data = await read_file(item["artifact"])
        except (OSError, ValueError):
            data = None
        if data is None:
            continue
        if not isinstance(data, bytes):
            continue
        if len(data) > MAX_EVIDENCE_BYTES:
            continue
        try:
            texts[evidence_id] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return _validate_citations_loaded(note, occurrences, by_id, texts, manifest_errors)


def project_evidence(
    manifest: Any,
    *,
    verified_ids: set[str] | None = None,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    """投影固定证据 schema,并把 validator 结论与可点击链接绑定。"""
    data = manifest if isinstance(manifest, dict) else {}
    raw_items = data.get("evidence")
    items = raw_items if isinstance(raw_items, list) else []
    errors = _project_evidence_strings(validation_errors)
    schema_v2 = (
        type(data.get("schema_version")) is int
        and data.get("schema_version") == EVIDENCE_SCHEMA_VERSION
    )
    verified = set(verified_ids or ())
    if not schema_v2:
        manifest_state = "legacy"
    elif not isinstance(raw_items, list) or validation_errors is None:
        manifest_state = "invalid"
    elif errors:
        manifest_state = "partial" if verified else "invalid"
    else:
        manifest_state = "verified"

    projected = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        raw_evidence_id = _project_evidence_text(raw.get("id"))
        evidence_id = raw_evidence_id if _valid_evidence_id(raw_evidence_id) else None
        item_verified = evidence_id is not None and evidence_id in verified
        item_errors = _evidence_item_errors(errors, evidence_id)
        if not item_verified and not item_errors:
            item_errors = ["not_verified"]
        matches = []
        raw_matches = raw.get("matches")
        if item_verified and isinstance(raw_matches, list):
            for match in raw_matches:
                if not isinstance(match, dict):
                    continue
                anchor = _project_evidence_text(match.get("anchor"))
                offset = match.get("offset")
                if anchor is not None and type(offset) is int and offset >= 0:
                    matches.append({"anchor": anchor, "offset": offset})

        final_url = None
        original_url = None
        url_safe = False
        if item_verified:
            try:
                final_url = _canonical_url(str(raw.get("final_url") or ""))
                original_url = _canonical_url(str(raw.get("original_url") or ""))
                _, _, url_safe = _source_confidence(final_url, original_url)
            except ValueError:
                final_url = None
                original_url = None
        artifact = raw.get("artifact")
        artifact = artifact if item_verified and _safe_artifact_rel(artifact) else None
        link_safe = bool(
            item_verified and url_safe and artifact is not None
            and raw.get("eligible") is True
            and raw.get("confidence") == "high"
            and raw.get("source_tier") == "一手官方"
        )
        if not link_safe:
            final_url = None
            original_url = None
            artifact = None
        elif original_url != final_url:
            # redirect 起点无法在读时重放绑定,只暴露已重验的最终官方 URL.
            original_url = None
        projected.append({
            "id": evidence_id,
            "title": _project_evidence_text(raw.get("title")),
            "publisher": _project_evidence_text(raw.get("publisher")),
            "source_tier": (
                _project_evidence_text(raw.get("source_tier")) if item_verified else None
            ),
            "confidence": (
                _project_evidence_text(raw.get("confidence")) if item_verified else None
            ),
            "eligible": item_verified and raw.get("eligible") is True,
            "eligibility_reasons": _project_evidence_strings(raw.get("eligibility_reasons")),
            "matches": matches,
            "retrieved_at": _project_evidence_text(raw.get("retrieved_at")),
            "artifact": artifact,
            "original_url": original_url,
            "final_url": final_url,
            "url": None,
            "link_safe": link_safe,
            "verification_state": "verified" if item_verified else "invalid",
            "verification_reasons": [] if item_verified else item_errors,
        })
    return {
        "schema_version": (
            data.get("schema_version") if type(data.get("schema_version")) is int else None
        ),
        "job_id": _project_evidence_text(data.get("job_id")),
        "manifest_state": manifest_state,
        "reliability_state": (
            "verified" if manifest_state == "verified"
            else "legacy_unverified" if manifest_state == "legacy"
            else "unreliable"
        ),
        "manifest_errors": errors,
        "evidence": projected,
    }


def _project_evidence_text(value: Any) -> str | None:
    if type(value) is not str:
        return None
    value = value.strip()
    return value or None


def _project_evidence_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _project_evidence_text(item)) is not None]


def _evidence_item_errors(errors: list[str], evidence_id: str | None) -> list[str]:
    if evidence_id is None:
        return ["invalid_item_id", *errors]
    targeted = [error for error in errors if evidence_id in error.split(":")[1:]]
    global_errors = [error for error in errors if ":" not in error]
    return list(dict.fromkeys([*targeted, *global_errors]))


def _load_canonical_sidecar(data: bytes, *, name: str) -> dict[str, Any]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_CANONICAL_SIDECAR_BYTES:
        raise CanonicalEvidenceError(f"{name} is missing or exceeds size limit")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CanonicalEvidenceError(f"{name} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CanonicalEvidenceError(f"{name} root must be an object")
    return value


def _normalized_string(value: Any, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value or value != value.strip():
        raise CanonicalEvidenceError(f"{field} must be a normalized string")
    return value


def locate_provenance_anchor(
    body: str,
    *,
    anchor: str,
    prefix: str,
    suffix: str,
) -> tuple[int, int]:
    offsets: list[int] = []
    cursor = 0
    while True:
        offset = body.find(anchor, cursor)
        if offset < 0:
            break
        end = offset + len(anchor)
        if (
            (not prefix or body[:offset].endswith(prefix))
            and (not suffix or body[end:].startswith(suffix))
        ):
            offsets.append(offset)
        cursor = offset + 1
    if len(offsets) != 1:
        raise CanonicalEvidenceError("provenance anchor must resolve exactly once")
    return offsets[0], offsets[0] + len(anchor)


def canonical_evidence_content_identity(
    *,
    job_id: str,
    note_type: str,
    note_path: str,
    note_sha256: str,
    provenance_sha256: str,
    chunk_id: str,
    chunk_body_sha256: str,
    chunk_char_start: int,
    chunk_char_end: int,
    anchor_start: int,
    anchor_end: int,
    source_fingerprint: str,
    provenance_schema_version: int,
    verification_policy: str,
) -> dict[str, Any]:
    """构造 builder 与 resolver 共用的完整 evidence fingerprint 身份。"""
    identity = {
        "job_id": job_id,
        "note_type": note_type,
        "note_path": note_path,
        "note_sha256": note_sha256,
        "provenance_sha256": provenance_sha256,
        "chunk_id": chunk_id,
        "chunk_body_sha256": chunk_body_sha256,
        "chunk_char_start": chunk_char_start,
        "chunk_char_end": chunk_char_end,
        "anchor_start": anchor_start,
        "anchor_end": anchor_end,
        "source_fingerprint": source_fingerprint,
    }
    if provenance_schema_version >= 2:
        identity["verification_policy"] = verification_policy
    return identity


async def _verify_semantic_attestation_batch(
    *,
    job_id: str,
    pipeline: str,
    note_type: str,
    note_path: str,
    note_data: bytes,
    normalized_body: str,
    provenance_path: str,
    provenance_data: bytes,
    source_manifest: dict[str, Any],
    mappings: list[dict[str, Any]],
    read_file: Callable[[str, int], Awaitable[bytes | None]],
) -> None:
    """从 commit、candidate 和真实 ai_logs 重建完整 semantic 信任链。"""
    commit_data = await read_file(SEMANTIC_BATCH_COMMIT_PATH, MAX_CANONICAL_SIDECAR_BYTES)
    if not isinstance(commit_data, bytes) or len(commit_data) > MAX_CANONICAL_SIDECAR_BYTES:
        raise CanonicalEvidenceError("semantic batch commit is missing or exceeds size limit")
    commit_raw = _load_canonical_sidecar(commit_data, name="semantic batch commit")
    try:
        commit = validate_semantic_batch_commit(commit_raw)
    except ValueError as exc:
        raise CanonicalEvidenceError(str(exc)) from exc
    if (
        commit["job_id"] != job_id
        or commit["pipeline"] != pipeline
        or commit_data != canonical_json_bytes(commit)
    ):
        raise CanonicalEvidenceError("semantic batch commit identity is invalid")

    candidate_manifests: list[dict[str, Any]] = []
    candidates_by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for artifact in commit["candidate_manifests"]:
        data = await read_file(artifact["path"], MAX_CANONICAL_SIDECAR_BYTES)
        if (
            not isinstance(data, bytes)
            or len(data) > MAX_CANONICAL_SIDECAR_BYTES
            or _sha256_hex(data) != artifact["sha256"]
        ):
            raise CanonicalEvidenceError("semantic candidate artifact changed")
        raw = _load_canonical_sidecar(data, name="semantic candidates")
        candidate_note_path = raw.get("note_artifact")
        if type(candidate_note_path) is not str:
            raise CanonicalEvidenceError("semantic candidate note path is invalid")
        if raw.get("note_type") == note_type and candidate_note_path == note_path:
            candidate_note_data = note_data
            candidate_body = normalized_body
        else:
            candidate_note_data = await read_file(
                candidate_note_path, MAX_CANONICAL_SIDECAR_BYTES,
            )
            if (
                not isinstance(candidate_note_data, bytes)
                or not candidate_note_data
                or len(candidate_note_data) > MAX_CANONICAL_SIDECAR_BYTES
            ):
                raise CanonicalEvidenceError("semantic candidate note is unreadable")
            try:
                from .note_text import markdown_to_index_text
                candidate_body = markdown_to_index_text(candidate_note_data.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise CanonicalEvidenceError("semantic candidate note is not UTF-8") from exc
        try:
            manifest = validate_provenance_candidate_manifest(
                raw,
                source_manifest=source_manifest,
                note_bytes=candidate_note_data,
                normalized_body=candidate_body,
            )
        except ValueError as exc:
            raise CanonicalEvidenceError(str(exc)) from exc
        if manifest["job_id"] != job_id or manifest["note_type"] != artifact["note_type"]:
            raise CanonicalEvidenceError("semantic candidate identity is invalid")
        candidate_manifests.append(manifest)
        for candidate in manifest["candidates"]:
            if candidate["candidate_id"] in candidates_by_id:
                raise CanonicalEvidenceError("semantic candidate id is duplicated across batch")
            candidates_by_id[candidate["candidate_id"]] = (manifest, candidate)

    for artifact in commit["provenance_manifests"]:
        if artifact["note_type"] == note_type and artifact["path"] == provenance_path:
            data = provenance_data
        else:
            data = await read_file(artifact["path"], MAX_CANONICAL_SIDECAR_BYTES)
        if (
            not isinstance(data, bytes)
            or len(data) > MAX_CANONICAL_SIDECAR_BYTES
            or _sha256_hex(data) != artifact["sha256"]
        ):
            raise CanonicalEvidenceError("semantic batch provenance is incomplete")

    ai_log_binding = commit.get("ai_log")
    if not isinstance(ai_log_binding, dict):
        raise CanonicalEvidenceError("semantic batch has no attestor log")
    expected_batch_id = semantic_attestation_batch_id(
        job_id=job_id,
        pipeline=pipeline,
        attestor_component=commit["attestor_component"],
        candidate_manifests=commit["candidate_manifests"],
        ai_log=ai_log_binding,
    )
    if expected_batch_id != commit["batch_id"]:
        raise CanonicalEvidenceError("semantic batch identity changed")
    log_data = await read_file(ai_log_binding["path"], MAX_SEMANTIC_AI_LOG_BYTES)
    if not isinstance(log_data, bytes) or len(log_data) > MAX_SEMANTIC_AI_LOG_BYTES:
        raise CanonicalEvidenceError("semantic attestor ai_log is missing or exceeds size limit")
    lines = [line for line in log_data.splitlines() if line.strip()]
    if len(lines) > MAX_SEMANTIC_AI_LOG_RECORDS:
        raise CanonicalEvidenceError("semantic attestor ai_log has too many records")
    matched: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CanonicalEvidenceError("semantic attestor ai_log is invalid JSONL") from exc
        if not isinstance(record, dict):
            raise CanonicalEvidenceError("semantic attestor ai_log record is invalid")
        if record.get("call_index") == ai_log_binding["call_index"]:
            matched.append(record)
    if len(matched) != 1:
        raise CanonicalEvidenceError("semantic attestor ai_log record is not unique")
    record = matched[0]
    if _sha256_hex(canonical_json_bytes(record)) != ai_log_binding["record_sha256"]:
        raise CanonicalEvidenceError("semantic attestor ai_log record changed")
    routing = record.get("routing") or {}
    prompt = (record.get("prompt") or {}).get("rendered", {}).get("user")
    response_content = (record.get("output") or {}).get("content")
    if (
        record.get("ok") is not True
        or record.get("job_id") != job_id
        or record.get("step") != commit["attestor_component"]
        or record.get("session_id") != ai_log_binding["session_id"]
        or routing.get("provider") != ai_log_binding["provider"]
        or routing.get("model") != ai_log_binding["model"]
        or type(prompt) is not str
        or _sha256_hex(prompt.encode("utf-8")) != ai_log_binding["prompt_user_sha256"]
        or type(response_content) is not str
        or _sha256_hex(response_content.encode("utf-8"))
        != ai_log_binding["response_content_sha256"]
    ):
        raise CanonicalEvidenceError("semantic attestor ai_log identity changed")
    expected_prompt = build_semantic_attestation_prompt(candidate_manifests, source_manifest)
    if prompt != expected_prompt:
        raise CanonicalEvidenceError("semantic attestor rendered prompt changed")
    try:
        response = json.loads(response_content)
        decisions = response["decisions"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CanonicalEvidenceError("semantic attestor response is invalid") from exc
    if (
        type(decisions) is not list
        or _sha256_hex(canonical_json_bytes(decisions))
        != ai_log_binding["response_decision_sha256"]
    ):
        raise CanonicalEvidenceError("semantic attestor decisions changed")
    decision_by_id = {
        item.get("candidate_id"): item for item in decisions if isinstance(item, dict)
    }
    if len(decision_by_id) != len(decisions) or set(decision_by_id) != set(candidates_by_id):
        raise CanonicalEvidenceError("semantic attestor decision set changed")

    source_sha = _sha256_hex(canonical_json_bytes(source_manifest))
    for mapping in mappings:
        if mapping.get("verification_policy") != SEMANTIC_ATTESTATION_POLICY:
            continue
        attestation = mapping.get("attestation") or {}
        candidate_id = attestation.get("candidate_id")
        bound = candidates_by_id.get(candidate_id)
        if bound is None:
            raise CanonicalEvidenceError("semantic attestation candidate is missing")
        manifest, candidate = bound
        decision = decision_by_id[candidate_id]
        per_log = attestation.get("ai_log") or {}
        common_log = {key: value for key, value in per_log.items() if key != "response_decision_sha256"}
        expected_common = {key: value for key, value in ai_log_binding.items() if key != "response_decision_sha256"}
        if (
            attestation.get("batch_id") != commit["batch_id"]
            or attestation.get("job_id") != job_id
            or attestation.get("note_type") != note_type
            or attestation.get("note_sha256") != _sha256_hex(note_data)
            or attestation.get("source_manifest_sha256") != source_sha
            or manifest["note_type"] != note_type
            or candidate["anchor"] != mapping.get("anchor")
            or candidate["prefix"] != mapping.get("prefix")
            or candidate["suffix"] != mapping.get("suffix")
            or candidate["section"] != mapping.get("section")
            or candidate["source_segment_id"] not in mapping.get("source_segment_ids", [])
            or candidate["transform_kind"] != attestation.get("transform_kind")
            or candidate["producer_component"] != attestation.get("producer_component")
            or candidate["producer_invocation_id"] != attestation.get("producer_invocation_id")
            or common_log != expected_common
            or per_log.get("response_decision_sha256")
            != _sha256_hex(canonical_json_bytes(decision))
            or decision.get("decision") != "supported"
            or decision.get("confidence_ppm") != attestation.get("confidence_ppm")
            or decision.get("reason_codes") != attestation.get("reason_codes")
            or set(decision) != {
                "candidate_id", "decision", "confidence_ppm", "reason_codes",
            }
        ):
            raise CanonicalEvidenceError("semantic attestation binding changed")


async def build_canonical_evidence_records_with_reader(
    *,
    job_id: str,
    pipeline: str,
    note_type: str,
    note_path: str,
    note_data: bytes,
    normalized_body: str,
    chunks: list[dict[str, Any]],
    source_manifest_data: bytes,
    source_manifest_path: str,
    provenance_path: str,
    provenance_data: bytes,
    read_file: Callable[[str, int], Awaitable[bytes | None]],
    sha256_file: Callable[[str], Awaitable[str | None]],
) -> list[dict[str, Any]]:
    """重验显式 provenance 并投影到 scheduler 的唯一 chunk 边界。"""
    _normalized_string(job_id, field="job_id")
    _normalized_string(pipeline, field="pipeline")
    _normalized_string(note_type, field="note_type")
    note_path = _safe_job_relative(note_path)
    provenance_path = _safe_job_relative(provenance_path)
    if (
        not isinstance(note_data, bytes)
        or not note_data
        or len(note_data) > MAX_CANONICAL_SIDECAR_BYTES
    ):
        raise CanonicalEvidenceError("note artifact is empty or exceeds size limit")
    if type(normalized_body) is not str or not normalized_body:
        raise CanonicalEvidenceError("normalized note body is empty")

    source_manifest_path = _safe_job_relative(source_manifest_path)
    source_manifest = _load_canonical_sidecar(
        source_manifest_data, name="source_segments"
    )
    try:
        source_manifest = validate_source_manifest(source_manifest)
    except ValueError as exc:
        raise CanonicalEvidenceError(str(exc)) from exc
    if source_manifest.get("job_id") != job_id or source_manifest.get("pipeline") != pipeline:
        raise CanonicalEvidenceError("source_segments identity does not match job")
    if source_manifest_data != canonical_json_bytes(source_manifest):
        raise CanonicalEvidenceError("source_segments is not canonical JSON")
    artifacts = {
        str(item["source_id"]): item for item in source_manifest["source_artifacts"]
    }
    source_segments = {
        str(item["segment_id"]): item for item in source_manifest["segments"]
    }

    text_source_ids = {
        str(segment["source_id"])
        for segment in source_segments.values()
        if segment["locator"]["kind"] == "text"
    }
    source_payloads: dict[str, bytes] = {}
    for source_id, artifact in artifacts.items():
        actual_sha256 = await sha256_file(artifact["path"])
        if actual_sha256 is None:
            raise CanonicalEvidenceError(f"source artifact is missing: {source_id}")
        if actual_sha256 != artifact["sha256"]:
            raise CanonicalEvidenceError(f"source artifact sha256 changed: {source_id}")
        if source_id in text_source_ids:
            payload = await read_file(artifact["path"], MAX_CANONICAL_SIDECAR_BYTES)
            if (
                not isinstance(payload, bytes)
                or len(payload) > MAX_CANONICAL_SIDECAR_BYTES
            ):
                raise CanonicalEvidenceError(
                    f"text source is missing or exceeds size limit: {source_id}"
                )
            source_payloads[source_id] = payload

    for segment_id, segment in source_segments.items():
        if segment["locator"]["kind"] != "text":
            continue
        source_id = str(segment["source_id"])
        try:
            source_text = source_payloads[source_id].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalEvidenceError(
                f"text source is not UTF-8: {segment_id}"
            ) from exc
        start, end = int(segment["start"]), int(segment["end"])
        locator = segment["locator"]
        if end > len(source_text) or source_text[start:end] != locator["exact"]:
            raise CanonicalEvidenceError(f"text locator range changed: {segment_id}")
        if locator["prefix"] and not source_text[:start].endswith(locator["prefix"]):
            raise CanonicalEvidenceError(f"text locator prefix changed: {segment_id}")
        if locator["suffix"] and not source_text[end:].startswith(locator["suffix"]):
            raise CanonicalEvidenceError(f"text locator suffix changed: {segment_id}")

    provenance = _load_canonical_sidecar(provenance_data, name="note provenance")
    try:
        provenance = validate_provenance_manifest(
            provenance,
            source_manifest=source_manifest,
            note_bytes=note_data,
            normalized_body=normalized_body,
        )
    except ValueError as exc:
        raise CanonicalEvidenceError(str(exc)) from exc
    if (
        provenance.get("job_id") != job_id
        or provenance.get("note_type") != note_type
        or provenance.get("note_artifact") != note_path
        or provenance.get("source_manifest") != source_manifest_path
        or provenance_data != canonical_json_bytes(provenance)
    ):
        raise CanonicalEvidenceError("note provenance identity is invalid")
    raw_mappings = provenance["segments"]
    if any(
        mapping.get("verification_policy") == SEMANTIC_ATTESTATION_POLICY
        for mapping in raw_mappings
        if isinstance(mapping, dict)
    ):
        await _verify_semantic_attestation_batch(
            job_id=job_id,
            pipeline=pipeline,
            note_type=note_type,
            note_path=note_path,
            note_data=note_data,
            normalized_body=normalized_body,
            provenance_path=provenance_path,
            provenance_data=provenance_data,
            source_manifest=source_manifest,
            mappings=raw_mappings,
            read_file=read_file,
        )

    support_payloads: dict[tuple[str, str], bytes] = {}
    referenced_segment_ids = {
        str(segment_id)
        for mapping in raw_mappings
        for segment_id in mapping["source_segment_ids"]
    }
    if source_manifest["schema_version"] >= 2:
        for segment_id in sorted(referenced_segment_ids):
            segment = source_segments[segment_id]
            support_artifact = segment.get("support_artifact")
            if support_artifact is None:
                continue
            path = str(support_artifact["path"])
            expected_sha256 = str(support_artifact["sha256"])
            key = (path, expected_sha256)
            payload = support_payloads.get(key)
            if payload is None:
                actual_sha256 = await sha256_file(path)
                if actual_sha256 is None:
                    raise CanonicalEvidenceError(
                        f"support artifact is missing: {segment_id}"
                    )
                if actual_sha256 != expected_sha256:
                    raise CanonicalEvidenceError(
                        f"support artifact sha256 changed: {segment_id}"
                    )
                payload = await read_file(path, MAX_SUPPORT_ARTIFACT_BYTES)
                if (
                    not isinstance(payload, bytes)
                    or len(payload) > MAX_SUPPORT_ARTIFACT_BYTES
                ):
                    raise CanonicalEvidenceError(
                        f"support artifact is unreadable: {segment_id}"
                    )
                support_payloads[key] = payload
            artifact = artifacts[str(segment["source_id"])]
            try:
                expected_support = support_text_from_artifact(
                    payload, support_artifact, segment, artifact,
                )
            except ValueError as exc:
                raise CanonicalEvidenceError(str(exc)) from exc
            if expected_support != segment.get("support_text"):
                raise CanonicalEvidenceError(
                    f"support text does not match artifact: {segment_id}"
                )

    prepared_chunks: list[dict[str, Any]] = []
    chunk_fields = {"chunk_id", "body", "section", "char_start", "char_end"}
    seen_chunk_ids: set[str] = set()
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != chunk_fields:
            raise CanonicalEvidenceError("scheduler chunk fields are invalid")
        chunk_id = _normalized_string(chunk.get("chunk_id"), field="chunk_id")
        if chunk_id in seen_chunk_ids:
            raise CanonicalEvidenceError("scheduler chunk_id is duplicated")
        seen_chunk_ids.add(str(chunk_id))
        start, end = chunk.get("char_start"), chunk.get("char_end")
        if (
            type(chunk.get("body")) is not str or not chunk["body"]
            or type(chunk.get("section")) is not str
            or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(normalized_body)
        ):
            raise CanonicalEvidenceError("scheduler chunk values are invalid")
        prepared_chunks.append(chunk)

    note_sha256 = _sha256_hex(note_data)
    provenance_sha256 = _sha256_hex(provenance_data)
    records: dict[str, dict[str, Any]] = {}
    mapping_fields = {"anchor", "prefix", "suffix", "section", "source_segment_ids"}
    if provenance["schema_version"] >= 2:
        mapping_fields.add("verification_policy")
    for mapping in raw_mappings:
        expected_mapping_fields = set(mapping_fields)
        if (
            provenance["schema_version"] >= 3
            and isinstance(mapping, dict)
            and mapping.get("verification_policy") == SEMANTIC_ATTESTATION_POLICY
        ):
            expected_mapping_fields.add("attestation")
        if not isinstance(mapping, dict) or set(mapping) != expected_mapping_fields:
            raise CanonicalEvidenceError("note provenance segment fields are invalid")
        anchor = _normalized_string(mapping.get("anchor"), field="note anchor")
        prefix, suffix = mapping.get("prefix"), mapping.get("suffix")
        if type(prefix) is not str or type(suffix) is not str:
            raise CanonicalEvidenceError("note anchor context is invalid")
        section = mapping.get("section")
        if section is not None:
            _normalized_string(section, field="note provenance section")
        refs = mapping.get("source_segment_ids")
        if (
            not isinstance(refs, list) or not refs
            or any(type(ref) is not str or not ref or ref != ref.strip() for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise CanonicalEvidenceError("source_segment_ids are invalid")
        verification_policy = mapping.get(
            "verification_policy", DIRECT_LOCATOR_POLICY,
        )
        if verification_policy not in {
            DIRECT_LOCATOR_POLICY,
            EXACT_QUOTE_POLICY,
            SEMANTIC_ATTESTATION_POLICY,
        }:
            raise CanonicalEvidenceError("verification_policy is invalid")
        if verification_policy == EXACT_QUOTE_POLICY:
            try:
                validate_exact_quote_mapping(
                    mapping, source_manifest, field="canonical provenance segment",
                )
            except ValueError as exc:
                raise CanonicalEvidenceError(str(exc)) from exc
        anchor_start, anchor_end = locate_provenance_anchor(
            normalized_body, anchor=str(anchor), prefix=prefix, suffix=suffix,
        )
        overlapping = [
            chunk for chunk in prepared_chunks
            if int(chunk["char_start"]) < anchor_end and int(chunk["char_end"]) > anchor_start
        ]
        if not overlapping:
            raise CanonicalEvidenceError("note provenance anchor does not overlap a chunk")
        for segment_id in refs:
            source_segment = source_segments.get(segment_id)
            if source_segment is None:
                raise CanonicalEvidenceError("note provenance references unknown segment")
            artifact = artifacts[str(source_segment["source_id"])]
            locator = source_segment["locator"]
            if locator["kind"] == "image":
                asset_sha256 = await sha256_file(locator["asset_path"])
                if asset_sha256 != locator["asset_sha256"]:
                    raise CanonicalEvidenceError("image locator asset changed")
            source_ref = str(source_segment["source_id"])
            source_identity = {
                "source_ref": source_ref,
                "source_segment_id": segment_id,
                "path": artifact["path"],
                "sha256": artifact["sha256"],
                "revision": artifact["revision"],
                "start": source_segment["start"],
                "end": source_segment["end"],
                "section": source_segment["section"],
                "locator": locator,
            }
            if source_manifest["schema_version"] >= 2:
                source_identity["support_text"] = source_segment.get("support_text")
                source_identity["support_artifact"] = source_segment.get(
                    "support_artifact"
                )
            source_fingerprint = canonical_source_fingerprint(source_identity)
            for chunk in overlapping:
                body_sha256 = _sha256_hex(chunk["body"].encode("utf-8"))
                evidence_identity = canonical_evidence_content_identity(
                    job_id=job_id,
                    note_type=note_type,
                    note_path=note_path,
                    note_sha256=note_sha256,
                    provenance_sha256=provenance_sha256,
                    chunk_id=chunk["chunk_id"],
                    chunk_body_sha256=body_sha256,
                    chunk_char_start=chunk["char_start"],
                    chunk_char_end=chunk["char_end"],
                    anchor_start=anchor_start,
                    anchor_end=anchor_end,
                    source_fingerprint=source_fingerprint,
                    provenance_schema_version=provenance["schema_version"],
                    verification_policy=verification_policy,
                )
                evidence_fingerprint = canonical_evidence_fingerprint(evidence_identity)
                identity = {
                    "schema_version": CANONICAL_EVIDENCE_SCHEMA_VERSION,
                    "job_id": job_id,
                    "note_type": note_type,
                    "chunk_id": chunk["chunk_id"],
                    "source_ref": source_ref,
                    "source_segment_id": segment_id,
                    "evidence_fingerprint": evidence_fingerprint,
                }
                evidence_id = canonical_evidence_id(identity)
                record = {
                    "evidence_id": evidence_id,
                    "schema_version": CANONICAL_EVIDENCE_SCHEMA_VERSION,
                    "job_id": job_id,
                    "note_type": note_type,
                    "chunk_id": chunk["chunk_id"],
                    "section": chunk["section"],
                    "source_ref": source_ref,
                    "source_segment_id": segment_id,
                    "source_path": artifact["path"],
                    "source_sha256": artifact["sha256"],
                    "source_revision": artifact["revision"],
                    "note_path": note_path,
                    "note_sha256": note_sha256,
                    "provenance_path": provenance_path,
                    "provenance_sha256": provenance_sha256,
                    "chunk_body_sha256": body_sha256,
                    "chunk_char_start": chunk["char_start"],
                    "chunk_char_end": chunk["char_end"],
                    "locator_kind": locator["kind"],
                    "locator_json": _canonical_json(locator),
                    "evidence_fingerprint": evidence_fingerprint,
                    "source_fingerprint": source_fingerprint,
                }
                previous = records.get(evidence_id)
                if previous is not None and previous != record:
                    raise CanonicalEvidenceError("canonical evidence id collision")
                records[evidence_id] = record
    return [records[key] for key in sorted(records)]
