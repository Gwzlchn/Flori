"""验证论文HTML静态快照并把上游CSS投影成无外联样式。"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlparse

import tinycss2


SCHOLARLY_HTML_SNAPSHOT_FORMAT = "flori-scholarly-html-snapshot"
SCHOLARLY_HTML_SNAPSHOT_VERSION = 1
SCHOLARLY_HTML_SNAPSHOT_PATH = "input/html_snapshot.json"
SCHOLARLY_HTML_ASSET_PREFIX = "input/html_assets/"
SCHOLARLY_HTML_SOURCE_MAX_BYTES = 32 * 1024 * 1024
SCHOLARLY_HTML_SNAPSHOT_MAX_BYTES = 1024 * 1024
SCHOLARLY_HTML_CSS_MAX_BYTES = 2 * 1024 * 1024
SCHOLARLY_HTML_RESOURCE_MAX_BYTES = 8 * 1024 * 1024
SCHOLARLY_HTML_RESOURCE_TOTAL_MAX_BYTES = 32 * 1024 * 1024
SCHOLARLY_HTML_MAX_RESOURCES = 256
SCHOLARLY_HTML_MAX_STYLESHEETS = 32
SCHOLARLY_HTML_MAX_REFERENCE_EVENTS = 4096
SCHOLARLY_HTML_MAX_STYLE_CHUNKS = 10_000
SCHOLARLY_HTML_SANITIZED_CSS_MAX_BYTES = 4 * 1024 * 1024
SCHOLARLY_HTML_CSS_MAX_RULES = 10_000
SCHOLARLY_HTML_CSS_MAX_DECLARATIONS = 100_000
SCHOLARLY_HTML_CSS_MAX_TOKENS = 200_000
SCHOLARLY_HTML_CSS_MAX_DEPTH = 32

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
_SAFE_RESOURCE_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}\Z")
_SAFE_CSS_NAME_RE = re.compile(r"(?:--|-)?[A-Za-z_][A-Za-z0-9_-]{0,127}\Z")
_RESOURCE_MIME_TYPES = frozenset({
    "text/css",
    "font/woff",
    "font/woff2",
    "font/ttf",
    "font/otf",
    "application/font-woff",
    "application/x-font-ttf",
    "application/x-font-opentype",
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})
_RESOURCE_KINDS = frozenset({"stylesheet", "font", "image"})
_PROVIDER_DOCUMENT_HOSTS = {
    "arxiv": frozenset({"arxiv.org", "www.arxiv.org"}),
    "ar5iv": frozenset({"ar5iv.org", "www.ar5iv.org", "ar5iv.labs.arxiv.org"}),
}
_SAFE_NESTED_AT_RULES = frozenset({"media", "supports", "layer", "keyframes"})
_CSS_STRING_IMAGE_FUNCTIONS = frozenset({
    "cross-fade",
    "-webkit-cross-fade",
    "image",
    "image-set",
    "-webkit-image-set",
})
_CSS_DYNAMIC_VALUE_FUNCTIONS = frozenset({"attr", "env", "var"})
_CSS_RESOURCE_VALUE_PROPERTIES = frozenset({
    "background", "background-image", "border-image", "border-image-source",
    "clip-path", "content", "cursor", "filter", "list-style",
    "list-style-image", "mask", "mask-image", "offset-path", "shape-outside",
    "src",
})
_DROP_PROPERTIES = frozenset({
    "behavior",
    "-moz-binding",
    "-webkit-user-modify",
})


class ScholarlyHtmlSnapshotError(ValueError):
    """表示论文HTML静态快照或CSS不满足离线渲染契约。"""


class ScholarlyHtmlSnapshotLimitError(ScholarlyHtmlSnapshotError):
    """表示论文HTML快照越过确定性资源预算。"""


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_snapshot_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ScholarlyHtmlSnapshotError(
            f"{field}: keys mismatch missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _bounded_string(value: object, field: str, *, max_chars: int = 4096) -> str:
    if type(value) is not str or not value or len(value) > max_chars:
        raise ScholarlyHtmlSnapshotError(f"{field}: invalid string")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise ScholarlyHtmlSnapshotError(f"{field}: control character")
    return value


def _digest(value: object, field: str) -> str:
    text = _bounded_string(value, field, max_chars=71)
    if _DIGEST_RE.fullmatch(text) is None:
        raise ScholarlyHtmlSnapshotError(f"{field}: invalid digest")
    return text


def _bounded_int(value: object, field: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ScholarlyHtmlSnapshotError(f"{field}: invalid integer")
    return value


def _https_url(value: object, field: str, *, allow_fragment: bool = False) -> str:
    text = _bounded_string(value, field, max_chars=8192)
    parsed = urlparse(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.fragment and not allow_fragment)
    ):
        raise ScholarlyHtmlSnapshotError(f"{field}: invalid HTTPS URL")
    return text


def scholarly_provider_hosts(provider: str) -> frozenset[str]:
    """返回论文HTML来源允许的主机集合;未知来源拒绝而不是退成任意公网。"""
    hosts = _PROVIDER_DOCUMENT_HOSTS.get(provider)
    if hosts is None:
        raise ScholarlyHtmlSnapshotError("snapshot.provider: unsupported")
    return hosts


def validate_scholarly_document_url(provider: str, value: object) -> str:
    """绑定来源与最终文档URL;资源闭包只能从受信论文入口开始。"""
    document_url = _https_url(value, "snapshot.document_url")
    if (urlparse(document_url).hostname or "").casefold() not in scholarly_provider_hosts(provider):
        raise ScholarlyHtmlSnapshotError("snapshot.document_url: provider host mismatch")
    return document_url


def _resource_path(value: object, field: str, *, kind: str) -> str:
    text = _bounded_string(value, field, max_chars=512)
    normalized = posixpath.normpath(text)
    if (
        text != normalized
        or _SAFE_RESOURCE_PATH_RE.fullmatch(text) is None
        or text.startswith(("/", "../"))
        or "/../" in text
    ):
        raise ScholarlyHtmlSnapshotError(f"{field}: invalid path")
    if kind in {"stylesheet", "font"} and not text.startswith(SCHOLARLY_HTML_ASSET_PREFIX):
        raise ScholarlyHtmlSnapshotError(f"{field}: CSS/font must use snapshot prefix")
    if kind == "image" and not (
        text.startswith("assets/") or text.startswith(SCHOLARLY_HTML_ASSET_PREFIX)
    ):
        raise ScholarlyHtmlSnapshotError(f"{field}: image path is outside snapshot")
    return text


def validate_scholarly_html_snapshot(
    value: object,
    *,
    expected_job_id: str,
    source_html: bytes | None = None,
) -> dict:
    """严格验证快照身份与资源清单;传入原文时再校验实际字节。"""
    if type(value) is not dict:
        raise ScholarlyHtmlSnapshotError("snapshot: must be an object")
    _exact_keys(
        value,
        frozenset({
            "format", "format_version", "job_id", "provider", "document_url",
            "html", "stylesheets", "resources",
        }),
        "snapshot",
    )
    if value["format"] != SCHOLARLY_HTML_SNAPSHOT_FORMAT:
        raise ScholarlyHtmlSnapshotError("snapshot.format: unsupported")
    if value["format_version"] != SCHOLARLY_HTML_SNAPSHOT_VERSION:
        raise ScholarlyHtmlSnapshotError("snapshot.format_version: unsupported")
    job_id = _bounded_string(value["job_id"], "snapshot.job_id", max_chars=200)
    if _JOB_ID_RE.fullmatch(job_id) is None or job_id != expected_job_id:
        raise ScholarlyHtmlSnapshotError("snapshot.job_id: identity mismatch")
    provider = _bounded_string(value["provider"], "snapshot.provider", max_chars=16)
    if provider not in {"arxiv", "ar5iv"}:
        raise ScholarlyHtmlSnapshotError("snapshot.provider: unsupported")
    document_url = validate_scholarly_document_url(provider, value["document_url"])

    html_record = value["html"]
    if type(html_record) is not dict:
        raise ScholarlyHtmlSnapshotError("snapshot.html: must be an object")
    _exact_keys(
        html_record,
        frozenset({"path", "sha256", "size_bytes", "media_type"}),
        "snapshot.html",
    )
    if html_record["path"] != "input/source.html" or html_record["media_type"] != "text/html":
        raise ScholarlyHtmlSnapshotError("snapshot.html: invalid identity")
    html_size = _bounded_int(
        html_record["size_bytes"], "snapshot.html.size_bytes",
        maximum=SCHOLARLY_HTML_SOURCE_MAX_BYTES,
    )
    html_digest = _digest(html_record["sha256"], "snapshot.html.sha256")
    if source_html is not None and (
        html_size != len(source_html) or html_digest != sha256_digest(source_html)
    ):
        raise ScholarlyHtmlSnapshotError("snapshot.html: source bytes mismatch")

    resources = value["resources"]
    if type(resources) is not list or len(resources) > SCHOLARLY_HTML_MAX_RESOURCES:
        raise ScholarlyHtmlSnapshotError("snapshot.resources: invalid array")
    normalized_resources: list[dict] = []
    seen_paths: set[str] = set()
    seen_alias_urls: set[str] = set()
    total_bytes = 0
    for index, record in enumerate(resources):
        field = f"snapshot.resources[{index}]"
        if type(record) is not dict:
            raise ScholarlyHtmlSnapshotError(f"{field}: must be an object")
        _exact_keys(
            record,
            frozenset({
                "kind", "path", "request_url", "source_url", "sha256",
                "size_bytes", "media_type",
            }),
            field,
        )
        kind = _bounded_string(record["kind"], f"{field}.kind", max_chars=16)
        if kind not in _RESOURCE_KINDS:
            raise ScholarlyHtmlSnapshotError(f"{field}.kind: unsupported")
        path = _resource_path(record["path"], f"{field}.path", kind=kind)
        request_url = _https_url(
            record["request_url"], f"{field}.request_url",
            allow_fragment=kind == "stylesheet",
        )
        source_url = _https_url(
            record["source_url"], f"{field}.source_url",
            allow_fragment=kind == "stylesheet",
        )
        digest = _digest(record["sha256"], f"{field}.sha256")
        size = _bounded_int(
            record["size_bytes"], f"{field}.size_bytes",
            maximum=SCHOLARLY_HTML_RESOURCE_MAX_BYTES,
        )
        media_type = _bounded_string(record["media_type"], f"{field}.media_type", max_chars=100)
        if media_type not in _RESOURCE_MIME_TYPES:
            raise ScholarlyHtmlSnapshotError(f"{field}.media_type: unsupported")
        if kind == "stylesheet" and media_type != "text/css":
            raise ScholarlyHtmlSnapshotError(f"{field}: stylesheet MIME mismatch")
        if kind == "font" and "font" not in media_type:
            raise ScholarlyHtmlSnapshotError(f"{field}: font MIME mismatch")
        if kind == "image" and not media_type.startswith("image/"):
            raise ScholarlyHtmlSnapshotError(f"{field}: image MIME mismatch")
        record_aliases = {request_url, source_url}
        if path in seen_paths or seen_alias_urls.intersection(record_aliases):
            raise ScholarlyHtmlSnapshotError(f"{field}: duplicate identity")
        seen_paths.add(path)
        seen_alias_urls.update(record_aliases)
        total_bytes += size
        if total_bytes > SCHOLARLY_HTML_RESOURCE_TOTAL_MAX_BYTES:
            raise ScholarlyHtmlSnapshotLimitError("snapshot.resources: total bytes exceed limit")
        normalized_resources.append({
            "kind": kind,
            "path": path,
            "request_url": request_url,
            "source_url": source_url,
            "sha256": digest,
            "size_bytes": size,
            "media_type": media_type,
        })
    if [item["path"] for item in normalized_resources] != sorted(
        seen_paths, key=lambda item: item.encode("utf-8"),
    ):
        raise ScholarlyHtmlSnapshotError("snapshot.resources: paths must be sorted")

    stylesheets = value["stylesheets"]
    if (
        type(stylesheets) is not list
        or len(stylesheets) > SCHOLARLY_HTML_MAX_STYLESHEETS
        or not all(type(path) is str for path in stylesheets)
        or len(set(stylesheets)) != len(stylesheets)
    ):
        raise ScholarlyHtmlSnapshotError("snapshot.stylesheets: invalid array")
    by_path = {item["path"]: item for item in normalized_resources}
    if any(path not in by_path or by_path[path]["kind"] != "stylesheet" for path in stylesheets):
        raise ScholarlyHtmlSnapshotError("snapshot.stylesheets: unknown stylesheet")
    return {
        "format": SCHOLARLY_HTML_SNAPSHOT_FORMAT,
        "format_version": SCHOLARLY_HTML_SNAPSHOT_VERSION,
        "job_id": job_id,
        "provider": provider,
        "document_url": document_url,
        "html": {
            "path": "input/source.html",
            "sha256": html_digest,
            "size_bytes": html_size,
            "media_type": "text/html",
        },
        "stylesheets": list(stylesheets),
        "resources": normalized_resources,
    }


def decode_scholarly_html_snapshot(
    raw: bytes,
    *,
    expected_job_id: str,
    source_html: bytes | None = None,
) -> dict:
    if len(raw) > SCHOLARLY_HTML_SNAPSHOT_MAX_BYTES:
        raise ScholarlyHtmlSnapshotLimitError("snapshot JSON exceeds limit")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ScholarlyHtmlSnapshotError("snapshot JSON is invalid") from exc
    return validate_scholarly_html_snapshot(
        value, expected_job_id=expected_job_id, source_html=source_html,
    )


def _css_url_value(token: object) -> str | None:
    token_type = getattr(token, "type", "")
    if token_type == "url":
        return str(getattr(token, "value", ""))
    if token_type != "function" or getattr(token, "lower_name", "") != "url":
        return None
    arguments = list(getattr(token, "arguments", ()))
    meaningful = [item for item in arguments if getattr(item, "type", "") != "whitespace"]
    if len(meaningful) != 1 or getattr(meaningful[0], "type", "") not in {"string", "url", "ident"}:
        return None
    return str(getattr(meaningful[0], "value", ""))


def iter_css_urls(css: bytes) -> tuple[list[str], list[str]]:
    """返回(import URLs,其它资源URLs);畸形CSS直接拒绝而不猜测。"""
    if len(css) > SCHOLARLY_HTML_CSS_MAX_BYTES:
        raise ScholarlyHtmlSnapshotLimitError("stylesheet exceeds limit")
    try:
        rules = tinycss2.parse_stylesheet_bytes(
            css, skip_comments=True, skip_whitespace=True,
        )[0]
    except Exception as exc:
        raise ScholarlyHtmlSnapshotError("stylesheet parse failed") from exc
    imports: list[str] = []
    resources: list[str] = []

    token_count = 0

    def walk(tokens: Iterable[object], *, depth: int = 0) -> None:
        nonlocal token_count
        if depth > SCHOLARLY_HTML_CSS_MAX_DEPTH:
            raise ScholarlyHtmlSnapshotLimitError("stylesheet token nesting exceeds limit")
        for token in tokens:
            token_count += 1
            if token_count > SCHOLARLY_HTML_CSS_MAX_TOKENS:
                raise ScholarlyHtmlSnapshotLimitError("stylesheet token count exceeds limit")
            url = _css_url_value(token)
            if url is not None:
                resources.append(url)
                continue
            if (
                getattr(token, "type", "") == "function"
                and getattr(token, "lower_name", "") in _CSS_STRING_IMAGE_FUNCTIONS
            ):
                resources.extend(
                    str(getattr(argument, "value", ""))
                    for argument in getattr(token, "arguments", ())
                    if getattr(argument, "type", "") == "string"
                )
            content = getattr(token, "content", None)
            if content is not None:
                walk(content, depth=depth + 1)
            arguments = getattr(token, "arguments", None)
            if arguments is not None:
                walk(arguments, depth=depth + 1)

    try:
        for rule in rules:
            if getattr(rule, "type", "") == "error":
                raise ScholarlyHtmlSnapshotError("stylesheet contains parse error")
            if getattr(rule, "type", "") == "at-rule" and getattr(rule, "lower_at_keyword", "") == "import":
                meaningful = [
                    token for token in rule.prelude
                    if getattr(token, "type", "") != "whitespace"
                ]
                if not meaningful:
                    raise ScholarlyHtmlSnapshotError("stylesheet import is empty")
                url = _css_url_value(meaningful[0])
                if url is None and getattr(meaningful[0], "type", "") == "string":
                    url = str(getattr(meaningful[0], "value", ""))
                if not url:
                    raise ScholarlyHtmlSnapshotError("stylesheet import URL is invalid")
                imports.append(url)
                continue
            prelude = getattr(rule, "prelude", None)
            content = getattr(rule, "content", None)
            if prelude is not None:
                walk(prelude)
            if content is not None:
                walk(content)
    except RecursionError as exc:
        raise ScholarlyHtmlSnapshotLimitError(
            "stylesheet token nesting exceeds limit"
        ) from exc
    return list(dict.fromkeys(imports)), list(dict.fromkeys(resources))


class SnapshotCssBudget:
    def __init__(self) -> None:
        self.rules = 0
        self.declarations = 0
        self.tokens = 0

    def add_rule(self) -> None:
        self.rules += 1
        if self.rules > SCHOLARLY_HTML_CSS_MAX_RULES:
            raise ScholarlyHtmlSnapshotLimitError("stylesheet rule count exceeds limit")

    def add_declaration(self) -> None:
        self.declarations += 1
        if self.declarations > SCHOLARLY_HTML_CSS_MAX_DECLARATIONS:
            raise ScholarlyHtmlSnapshotLimitError("stylesheet declaration count exceeds limit")

    def add_token(self) -> None:
        self.tokens += 1
        if self.tokens > SCHOLARLY_HTML_CSS_MAX_TOKENS:
            raise ScholarlyHtmlSnapshotLimitError("stylesheet token count exceeds limit")


def _consume_css_token_tree(
    tokens: Iterable[object],
    *,
    budget: SnapshotCssBudget,
    depth: int = 0,
) -> None:
    """在 tinycss2.serialize 前验证嵌套与节点预算,避免深 selector 绕过 value 门。"""
    if depth > SCHOLARLY_HTML_CSS_MAX_DEPTH:
        raise ScholarlyHtmlSnapshotLimitError("stylesheet token nesting exceeds limit")
    for token in tokens:
        budget.add_token()
        content = getattr(token, "content", None)
        if content is not None:
            _consume_css_token_tree(content, budget=budget, depth=depth + 1)
        arguments = getattr(token, "arguments", None)
        if arguments is not None:
            _consume_css_token_tree(arguments, budget=budget, depth=depth + 1)


def _safe_css_text(value: str, field: str, *, max_chars: int) -> str:
    if len(value) > max_chars or "\x00" in value or "</style" in value.casefold():
        raise ScholarlyHtmlSnapshotError(f"{field}: unsafe text")
    return value.replace("<", r"\3c ")


def _serialize_css_tokens(
    tokens: Iterable[object],
    *,
    base_url: str,
    resolve_resource: Callable[[str], str | None],
    string_urls: bool = False,
    resource_context: bool = False,
    reject_resource_strings: bool = False,
    budget: SnapshotCssBudget,
    depth: int = 0,
) -> str | None:
    if depth > SCHOLARLY_HTML_CSS_MAX_DEPTH:
        raise ScholarlyHtmlSnapshotLimitError("stylesheet token nesting exceeds limit")
    parts: list[str] = []
    for token in tokens:
        budget.add_token()
        token_type = getattr(token, "type", "")
        if reject_resource_strings and token_type == "string":
            raw_string = str(getattr(token, "value", "")).strip().casefold()
            if raw_string.startswith(("/api/", "//", "http://", "https://", "data:")):
                return None
        if string_urls and token_type == "string":
            absolute = urljoin(base_url, str(getattr(token, "value", "")))
            local = resolve_resource(absolute)
            if local is None:
                return None
            escaped = local.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'url("{escaped}")')
            continue
        url = _css_url_value(token)
        if url is not None:
            if url.startswith("#"):
                parts.append(f'url("{url}")')
                continue
            absolute = urljoin(base_url, url)
            local = resolve_resource(absolute)
            if local is None:
                return None
            parts.append(f'url("{local}")')
            continue
        if token_type == "function":
            name = str(getattr(token, "name", ""))
            lower_name = str(getattr(token, "lower_name", ""))
            if (
                _SAFE_CSS_NAME_RE.fullmatch(name) is None
                or (
                    (string_urls or resource_context)
                    and lower_name in _CSS_DYNAMIC_VALUE_FUNCTIONS
                )
            ):
                return None
            body = _serialize_css_tokens(
                getattr(token, "arguments", ()),
                base_url=base_url,
                resolve_resource=resolve_resource,
                string_urls=(
                    lower_name in _CSS_STRING_IMAGE_FUNCTIONS
                ),
                resource_context=(
                    resource_context or lower_name in _CSS_STRING_IMAGE_FUNCTIONS
                ),
                reject_resource_strings=reject_resource_strings,
                budget=budget,
                depth=depth + 1,
            )
            if body is None:
                return None
            parts.append(f"{name}({body})")
            continue
        if token_type in {"() block", "[] block", "{} block"}:
            delimiters = {"() block": ("(", ")"), "[] block": ("[", "]"), "{} block": ("{", "}")}
            left, right = delimiters[token_type]
            body = _serialize_css_tokens(
                getattr(token, "content", ()),
                base_url=base_url,
                resolve_resource=resolve_resource,
                resource_context=resource_context,
                reject_resource_strings=reject_resource_strings,
                budget=budget,
                depth=depth + 1,
            )
            if body is None:
                return None
            parts.append(left + body + right)
            continue
        parts.append(tinycss2.serialize([token]))
    return _safe_css_text("".join(parts), "stylesheet value", max_chars=512 * 1024)


def _sanitize_declarations(
    content: Iterable[object],
    *,
    base_url: str,
    resolve_resource: Callable[[str], str | None],
    budget: SnapshotCssBudget,
) -> str:
    result: list[str] = []
    raw_content = tuple(content)
    declarations = tinycss2.parse_declaration_list(
        raw_content, skip_comments=True, skip_whitespace=True,
    )
    if any(getattr(item, "type", "") == "error" for item in declarations):
        # ParseError不保留精确原token区间。先审计整段保证被丢弃的
        # nesting也消耗token与深度预算;后续有效值重复计数是有意的保守上界。
        _consume_css_token_tree(raw_content, budget=budget)
    for declaration in declarations:
        declaration_type = getattr(declaration, "type", "")
        if declaration_type in {"declaration", "error"}:
            budget.add_declaration()
        # CSS nesting等上游扩展会被tinycss2投影为error。本地不解释该项;
        # 安全降级为丢弃,同规则的其他声明仅能经下方安全门输出。
        if declaration_type == "error":
            continue
        if declaration_type != "declaration":
            continue
        name = str(getattr(declaration, "lower_name", ""))
        if _SAFE_CSS_NAME_RE.fullmatch(name) is None or name in _DROP_PROPERTIES:
            continue
        value = _serialize_css_tokens(
            declaration.value,
            base_url=base_url,
            resolve_resource=resolve_resource,
            resource_context=name in _CSS_RESOURCE_VALUE_PROPERTIES,
            reject_resource_strings=name.startswith("--"),
            budget=budget,
        )
        if value is None:
            continue
        if name == "position" and value.strip().casefold() in {"fixed", "sticky"}:
            continue
        important = "!important" if declaration.important else ""
        result.append(f"{name}:{value}{important};")
    return "".join(result)


def _sanitize_rules(
    rules: Iterable[object],
    *,
    base_url: str,
    resolve_resource: Callable[[str], str | None],
    budget: SnapshotCssBudget,
    depth: int,
) -> str:
    if depth > SCHOLARLY_HTML_CSS_MAX_DEPTH:
        raise ScholarlyHtmlSnapshotLimitError("stylesheet nesting exceeds limit")
    result: list[str] = []
    for rule in rules:
        rule_type = getattr(rule, "type", "")
        if rule_type == "error":
            raise ScholarlyHtmlSnapshotError("stylesheet contains parse error")
        if rule_type in {"whitespace", "comment"}:
            continue
        budget.add_rule()
        if rule_type == "qualified-rule":
            _consume_css_token_tree(rule.prelude, budget=budget)
            selector = _safe_css_text(
                tinycss2.serialize(rule.prelude).strip(),
                "stylesheet selector",
                max_chars=16 * 1024,
            )
            if not selector:
                continue
            declarations = _sanitize_declarations(
                rule.content, base_url=base_url,
                resolve_resource=resolve_resource, budget=budget,
            )
            if declarations:
                result.append(f"{selector}{{{declarations}}}")
            continue
        if rule_type != "at-rule":
            continue
        keyword = str(getattr(rule, "lower_at_keyword", ""))
        if keyword == "import":
            continue
        if keyword == "font-face":
            declarations = _sanitize_declarations(
                rule.content or (), base_url=base_url,
                resolve_resource=resolve_resource, budget=budget,
            )
            if declarations:
                result.append(f"@font-face{{{declarations}}}")
            continue
        if keyword not in _SAFE_NESTED_AT_RULES or rule.content is None:
            continue
        _consume_css_token_tree(rule.prelude, budget=budget)
        prelude = _safe_css_text(
            tinycss2.serialize(rule.prelude).strip(),
            "stylesheet at-rule prelude",
            max_chars=16 * 1024,
        )
        nested = tinycss2.parse_rule_list(
            rule.content, skip_comments=True, skip_whitespace=True,
        )
        body = _sanitize_rules(
            nested, base_url=base_url, resolve_resource=resolve_resource,
            budget=budget, depth=depth + 1,
        )
        if body:
            suffix = f" {prelude}" if prelude else ""
            result.append(f"@{keyword}{suffix}{{{body}}}")
    rendered = "".join(result)
    if len(rendered.encode("utf-8")) > SCHOLARLY_HTML_SANITIZED_CSS_MAX_BYTES:
        raise ScholarlyHtmlSnapshotLimitError("sanitized stylesheet exceeds limit")
    return rendered


def sanitize_snapshot_stylesheet(
    css: bytes,
    *,
    base_url: str,
    resolve_resource: Callable[[str], str | None],
    budget: SnapshotCssBudget | None = None,
) -> str:
    """净化单份CSS并把所有资源URL改写为已验证同Job端点。"""
    if len(css) > SCHOLARLY_HTML_CSS_MAX_BYTES:
        raise ScholarlyHtmlSnapshotLimitError("stylesheet exceeds limit")
    try:
        rules = tinycss2.parse_stylesheet_bytes(
            css, skip_comments=True, skip_whitespace=True,
        )[0]
    except Exception as exc:
        raise ScholarlyHtmlSnapshotError("stylesheet parse failed") from exc
    try:
        return _sanitize_rules(
            rules,
            base_url=base_url,
            resolve_resource=resolve_resource,
            budget=budget or SnapshotCssBudget(),
            depth=0,
        )
    except RecursionError as exc:
        raise ScholarlyHtmlSnapshotLimitError(
            "stylesheet token nesting exceeds limit"
        ) from exc
