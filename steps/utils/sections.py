"""章节树 → markdown 渲染,供 paper/article smart 步的 prompt 构造共用。"""

from __future__ import annotations


def render_section_tree(section: dict, parts: list, level: int, max_chars: int = 2000) -> None:
    """把章节树渲染成 markdown 片段:标题按 level 加 #,正文截断 max_chars,递归子节点。"""
    prefix = "#" * level
    parts.append(f"\n{prefix} {section['title']}\n\n")
    if section.get("text"):
        parts.append(f"{section['text'][:max_chars]}\n")
    for child in section.get("children", []):
        render_section_tree(child, parts, level + 1, max_chars)


def build_section_tree(flat: list[dict]) -> list[dict]:
    """扁平章节列表 → 树形(按 level 嵌套)。paper/article 共用,勿各自另写。

    容错:缺 level/title/page/text 时用默认值,不因畸形输入(如手改 parsed.json
    或上游 schema 变化)KeyError。
    """
    tree: list[dict] = []
    stack: list[dict] = []

    for section in flat:
        node = {
            "level": section.get("level", 1),
            "title": section.get("title", ""),
            "page": section.get("page", 1),
            "text": section.get("text", ""),
            "children": [],
        }

        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()

        if stack:
            stack[-1]["children"].append(node)
        else:
            tree.append(node)

        stack.append(node)

    return tree
