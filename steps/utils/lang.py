"""正文主语言粗判(中/非中),供文章与论文的翻译触发用:非中文即需翻译。"""

from __future__ import annotations

import re


def detect_lang(text: str) -> str:
    """CJK 汉字占(汉字+拉丁字母)比 ≥15% 判 'zh',否则 'non-zh'(英文等→需翻译);无文字 → 'unknown'。
    中文文章夹少量英文术语仍 zh;纯英文 CJK≈0 → non-zh。"""
    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk + latin == 0:
        return "unknown"
    return "zh" if cjk / (cjk + latin) >= 0.15 else "non-zh"
