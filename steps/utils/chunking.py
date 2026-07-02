"""Markdown 分块:长文按段落贪心打包成 ≤max_chars 的 chunk,供翻译等逐块 AI 调用。

背景:整篇单调用翻译大论文(如 GPT-3 75 页)必撞步超时 600s + CLI 超时 600s(线上实证死循环重试)。
按块调用后单次调用规模可控,且每块都有自己的审计记录 + transcript sidecar(call_index 自增)。

切法:按段落("\n\n")贪心打包——段落边界不破坏 Markdown 结构(标题/列表/表格/代码块整体成段);
单段超预算再按行切(极端长段兜底)。fits 时原样单块返回,小文行为与整篇单调用完全一致。
"""

from __future__ import annotations


def split_markdown_chunks(text: str, max_chars: int) -> list[str]:
    """把 Markdown 文本切成若干 ≤max_chars 的块(段落边界优先,超长段按行兜底)。"""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0

    for para in text.split("\n\n"):
        pieces = [para] if len(para) <= max_chars else _split_long_paragraph(para, max_chars)
        for piece in pieces:
            # +2 计入段落分隔符 "\n\n"
            if current and current_len + len(piece) + 2 > max_chars:
                _flush()
            current.append(piece)
            current_len += len(piece) + 2
    _flush()
    return chunks


def _split_long_paragraph(para: str, max_chars: int) -> list[str]:
    """单段超预算(如超长表格/代码块)按行贪心再切;单行仍超长则硬切(极端兜底,不无限递归)。"""
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in para.split("\n"):
        while len(line) > max_chars:            # 单行硬切
            if current:
                parts.append("\n".join(current))
                current, current_len = [], 0
            parts.append(line[:max_chars])
            line = line[max_chars:]
        if current and current_len + len(line) + 1 > max_chars:
            parts.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts
