"""提供笔记索引与溯源共同使用的 Markdown 正文归一化。"""

from __future__ import annotations

import re


def markdown_to_index_text(markdown: str) -> str:
    """将 Markdown 转成稳定索引正文,保留标题、段落和 fenced code 正文。"""
    text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    in_fence = False
    fence_marker = ""
    for raw_line in text.split("\n"):
        stripped = raw_line.lstrip()
        fence = re.match(r"(`{3,}|~{3,})", stripped)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
                continue
            if marker == fence_marker:
                in_fence = False
                fence_marker = ""
                continue

        line = re.sub(r"<[^>]+>", " ", raw_line)
        if not in_fence:
            line = re.sub(r"`([^`]*)`", r"\1", line)
            line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"^(\s{0,3})[-*+]\s+", r"\1", line)
            line = re.sub(r"^(\s{0,3})>\s?", r"\1", line)
            line = re.sub(r"[*_~]+", "", line)
        lines.append(line.rstrip())

    compact: list[str] = []
    for line in lines:
        if line.strip():
            compact.append(line)
        elif compact and compact[-1] != "":
            compact.append("")
    while compact and compact[-1] == "":
        compact.pop()
    return "\n".join(compact)
