"""论文标题启发式:识别垃圾标题 + 从 PDF 首页文本提真标题。

pdf-only 论文的标题只有 PDF 内嵌 metadata 一个来源,而它经常是垃圾(编译产物文件名
"10things"/"paper.dvi"、系列名 "NBER WORKING PAPER SERIES"、"Microsoft Word - x.doc")。
02 步用 is_suspicious_title 判定后从 pdftotext 首页提真标题;scheduler metadata_sync 用同款
判定决定"已入库的非空标题是否允许被更好的候选覆盖"——两处必须同一套标准,故收敛于此。
"""

from __future__ import annotations

import re

# 常见垃圾标题(小写比对):期刊/系列页眉、编辑器默认名。相等或前缀命中都算垃圾。
_JUNK_PREFIXES = (
    "nber working paper",
    "working paper",
    "microsoft word",
    "untitled",
    "draft",
)


def is_suspicious_title(title: str) -> bool:
    """判定标题是否为垃圾(可用更好的候选覆盖)。规则宁窄勿宽:误判正常标题为垃圾
    会让它被启发式候选覆盖,比留着垃圾更糟。"""
    t = (title or "").strip()
    if not t:
        return True
    low = t.lower()
    if re.search(r"\.(dvi|tex|pdf|docx?)$", low):
        return True
    if any(low == p or low.startswith(p) for p in _JUNK_PREFIXES):
        return True
    # 无空格短 token("10things"/"paper2"):真标题几乎不可能是单个短词。
    if " " not in t and len(t) <= 24:
        return True
    return False


def title_from_first_page(text: str, max_lines: int = 40) -> str | None:
    """从 pdftotext 第 1 页输出提标题:跳过页眉/会议横幅/arXiv 行,取首个实义行;
    行尾连字符拼接下一行。提不出返 None(调用方保留原值)。"""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    picked: str | None = None
    for i, ln in enumerate(lines[:max_lines]):
        if not ln:
            continue
        low = ln.lower()
        if low.startswith(("arxiv:", "doi:", "issn", "isbn", "http")):
            continue
        if any(low == p or low.startswith(p) for p in _JUNK_PREFIXES):
            continue
        # 页眉短行:≤4 词且含数字/日期(如 "USENIX ATC 2014" / "PLOS Computational Biology 2013")。
        words = ln.split()
        if len(words) <= 4 and re.search(r"\d", ln):
            continue
        if low.startswith("abstract"):     # 已到摘要还没实义行 → 放弃
            return None
        if 6 <= len(ln) <= 160:
            picked = ln
            # 行尾连字符 = 标题跨行断词,拼下一非空行。
            if ln.endswith("-"):
                for nxt in lines[i + 1:i + 4]:
                    if nxt.strip():
                        picked = ln[:-1] + nxt.strip()
                        break
            return " ".join(picked.split())
        if len(ln) > 160:                  # 首个长行已是正文段落 → 放弃
            return None
    return None
