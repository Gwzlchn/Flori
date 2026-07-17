"""论文全文候选解析和已验证替代源注册表."""

from __future__ import annotations

import os
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import yaml


_DEFAULT_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "document_source_alternatives.yaml"
)


@dataclass(frozen=True)
class DocumentSourceAlternative:
    """一个经人工核验的跨站全文替代源."""

    original_url: str
    resolved_url: str
    document_kind: str
    reason: str
    min_pages: int


def canonical_source_url(url: str) -> str:
    """把来源 URL 归一化为精确匹配键,不改变路径语义."""
    parsed = urlsplit((url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source alternative URL must be absolute http(s)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source alternative URL must not contain credentials")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source alternative URL has invalid port") from exc
    default_port = 443 if scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, netloc, parsed.path or "/", query, ""))


def alternatives_path() -> Path:
    """返回运行配置路径,外部配置缺失时回退镜像内置表."""
    config_dir = os.environ.get("CONFIG_DIR")
    configured = (
        Path(config_dir) / "document_source_alternatives.yaml"
        if config_dir else None
    )
    if configured is not None and configured.is_file():
        return configured
    return _DEFAULT_PATH


def load_document_source_alternatives(
    path: str | Path | None = None,
) -> dict[str, DocumentSourceAlternative]:
    """读取替代源并按 canonical original URL 建索引,错配置直接失败."""
    source = Path(path) if path is not None else alternatives_path()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load document source alternatives: {source}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("document source alternatives schema_version must be 1")
    entries = raw.get("alternatives")
    if not isinstance(entries, list):
        raise ValueError("document source alternatives must be a list")

    resolved: dict[str, DocumentSourceAlternative] = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"document source alternative {index} must be a mapping")
        original_url = canonical_source_url(str(item.get("original_url") or ""))
        resolved_url = canonical_source_url(str(item.get("resolved_url") or ""))
        document_kind = str(item.get("document_kind") or "").strip()
        reason = str(item.get("reason") or "").strip()
        min_pages = item.get("min_pages", 2)
        if not document_kind or not reason:
            raise ValueError(f"document source alternative {index} misses kind or reason")
        if type(min_pages) is not int or min_pages < 2 or min_pages > 10_000:
            raise ValueError(f"document source alternative {index} has invalid min_pages")
        if original_url in resolved:
            raise ValueError(f"duplicate canonical original_url: {original_url}")
        resolved[original_url] = DocumentSourceAlternative(
            original_url=original_url,
            resolved_url=resolved_url,
            document_kind=document_kind,
            reason=reason,
            min_pages=min_pages,
        )
    return resolved


def resolve_document_source_alternative(
    original_url: str,
    *,
    document_kind: str = "research_paper",
    path: str | Path | None = None,
) -> DocumentSourceAlternative | None:
    """只有 URL 和文档体裁都精确匹配时才返回替代源."""
    alternative = load_document_source_alternatives(path).get(
        canonical_source_url(original_url)
    )
    if alternative is None or alternative.document_kind != document_kind:
        return None
    return alternative


class _PdfLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.citation: str | None = None
        self.typed_link: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta" and values.get("name", "").lower() == "citation_pdf_url":
            self.citation = values.get("content") or self.citation
            return
        if tag.lower() not in {"a", "link"}:
            return
        content_type = values.get("type", "").split(";", 1)[0].strip().lower()
        href = values.get("href", "").strip()
        if content_type == "application/pdf" and href and self.typed_link is None:
            self.typed_link = href


def extract_research_pdf_url(html: str, base_url: str) -> str | None:
    """优先读 citation PDF,其次读明确标注 application/pdf 的链接."""
    parser = _PdfLinkParser()
    try:
        parser.feed(html)
    except (ValueError, TypeError):
        return None
    candidate = parser.citation or parser.typed_link
    return urljoin(base_url, candidate) if candidate else None


def detect_access_challenge(body: bytes) -> str | None:
    """识别确定性访问挑战,只返回低基数诊断标签."""
    text = body[:512_000].decode("utf-8", errors="ignore").lower()
    cloudflare_markers = (
        "cf-chl-", "cf-ray", "just a moment", "attention required! | cloudflare",
    )
    if any(marker in text for marker in cloudflare_markers):
        return "cloudflare"
    human_markers = (
        "g-recaptcha", "hcaptcha", "cf-turnstile", "verify you are human",
    )
    if any(marker in text for marker in human_markers):
        return "human_verification"
    return None
