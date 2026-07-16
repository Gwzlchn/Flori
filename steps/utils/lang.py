"""正文主语言检测兼容入口."""

from __future__ import annotations

from shared.language import detect_language


def detect_lang(text: str) -> str:
    """返回 ISO 639-1 代码;无足够正文时返回 unknown."""
    return detect_language(text)
