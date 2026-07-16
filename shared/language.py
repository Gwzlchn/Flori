"""正文主语言检测,输出可展示的 ISO 639-1 语言代码."""

from __future__ import annotations

from langdetect import DetectorFactory, LangDetectException, detect


DetectorFactory.seed = 0


def detect_language(text: str) -> str:
    """检测正文主语言;文字不足或检测失败时返回 unknown."""
    sample = " ".join(text.split())[:50000]
    if sum(char.isalpha() for char in sample) < 20:
        return "unknown"
    try:
        language = detect(sample).lower()
    except LangDetectException:
        return "unknown"
    if language in {"zh-cn", "zh-tw"}:
        return "zh"
    return language
