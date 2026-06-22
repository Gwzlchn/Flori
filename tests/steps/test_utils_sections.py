"""tests for steps/utils/sections.py — 章节树 → markdown 渲染。"""

from __future__ import annotations

from steps.utils.sections import build_section_tree, render_section_tree


class TestRenderSectionTree:
    def test_single_section_with_text(self):
        parts: list = []
        render_section_tree({"title": "Intro", "text": "hello"}, parts, level=1)
        out = "".join(parts)
        assert "# Intro" in out
        assert "hello" in out

    def test_heading_level_controls_hashes(self):
        parts: list = []
        render_section_tree({"title": "Deep"}, parts, level=3)
        assert "### Deep" in "".join(parts)

    def test_no_text_key_omits_body(self):
        # 缺 text 键:只渲染标题,不追加正文片段。
        parts: list = []
        render_section_tree({"title": "Empty"}, parts, level=1)
        out = "".join(parts)
        assert "# Empty" in out
        # 标题片段固定为 "\n# Empty\n\n",无额外正文行。
        assert out == "\n# Empty\n\n"

    def test_empty_text_string_omits_body(self):
        # text 为空串属 falsy → section.get("text") 为假,不追加正文。
        parts: list = []
        render_section_tree({"title": "Blank", "text": ""}, parts, level=2)
        assert "".join(parts) == "\n## Blank\n\n"

    def test_text_truncated_to_max_chars(self):
        parts: list = []
        long_text = "x" * 5000
        render_section_tree({"title": "Long", "text": long_text}, parts, level=1, max_chars=100)
        out = "".join(parts)
        # 正文恰好 100 个 x(被截断),不是 5000。
        assert "x" * 100 + "\n" in out
        assert "x" * 101 not in out

    def test_text_shorter_than_max_chars_kept_whole(self):
        parts: list = []
        render_section_tree({"title": "Short", "text": "abc"}, parts, level=1, max_chars=2000)
        assert "abc\n" in "".join(parts)

    def test_nested_children_increment_level(self):
        section = {
            "title": "Root",
            "text": "root body",
            "children": [
                {
                    "title": "Child",
                    "text": "child body",
                    "children": [
                        {"title": "Grandchild", "text": "gc body"},
                    ],
                }
            ],
        }
        parts: list = []
        render_section_tree(section, parts, level=1)
        out = "".join(parts)
        assert "# Root" in out
        assert "## Child" in out
        assert "### Grandchild" in out
        # 渲染保持深度优先顺序。
        assert out.index("# Root") < out.index("## Child") < out.index("### Grandchild")
        assert "root body" in out and "child body" in out and "gc body" in out

    def test_multiple_siblings_same_level(self):
        section = {
            "title": "Parent",
            "children": [
                {"title": "A", "text": "a"},
                {"title": "B", "text": "b"},
            ],
        }
        parts: list = []
        render_section_tree(section, parts, level=2)
        out = "".join(parts)
        # 两个兄弟都在 level 3。
        assert out.count("### A") == 1
        assert out.count("### B") == 1
        assert out.index("### A") < out.index("### B")

    def test_empty_children_list(self):
        parts: list = []
        render_section_tree({"title": "Leaf", "text": "t", "children": []}, parts, level=1)
        out = "".join(parts)
        assert "# Leaf" in out and "t\n" in out

    def test_appends_to_existing_parts(self):
        # 函数追加到既有 list,不重置已有内容。
        parts: list = ["PRE"]
        render_section_tree({"title": "X"}, parts, level=1)
        assert parts[0] == "PRE"
        assert "".join(parts[1:]) == "\n# X\n\n"


class TestBuildSectionTree:
    def test_flat_to_nested(self):
        flat = [
            {"level": 1, "title": "A", "page": 1, "text": "a"},
            {"level": 2, "title": "A1", "page": 1, "text": "a1"},
            {"level": 1, "title": "B", "page": 2, "text": "b"},
        ]
        tree = build_section_tree(flat)
        assert [n["title"] for n in tree] == ["A", "B"]
        assert [c["title"] for c in tree[0]["children"]] == ["A1"]
        assert tree[1]["children"] == []

    def test_defensive_missing_keys(self):
        # 缺 level/title/page/text 不应 KeyError(I-L18:article 旧版直接索引会崩)。
        tree = build_section_tree([{}])
        assert tree == [{"level": 1, "title": "", "page": 1, "text": "", "children": []}]

    def test_empty(self):
        assert build_section_tree([]) == []
