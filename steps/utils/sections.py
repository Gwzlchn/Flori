"""章节树 → markdown 渲染,供 paper/article smart 步的 prompt 构造共用(消逐字重复副本)。"""

from __future__ import annotations


def render_section_tree(section: dict, parts: list, level: int, max_chars: int = 2000) -> None:
    """把章节树渲染成 markdown 片段:标题按 level 加 #,正文截断 max_chars,递归子节点。"""
    prefix = "#" * level
    parts.append(f"\n{prefix} {section['title']}\n\n")
    if section.get("text"):
        parts.append(f"{section['text'][:max_chars]}\n")
    for child in section.get("children", []):
        render_section_tree(child, parts, level + 1, max_chars)
