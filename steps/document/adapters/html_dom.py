"""为通用 HTML adapter 提供保序 DOM 和稳定路径。"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterator


_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


@dataclass(eq=False)
class HtmlNode:
    """保留文本与子节点相对顺序的最小 DOM 节点。"""

    tag: str
    attrs: dict[str, str]
    parent: HtmlNode | None
    sibling_index: int
    content: list[str | HtmlNode] = field(default_factory=list)

    @property
    def children(self) -> list[HtmlNode]:
        return [item for item in self.content if isinstance(item, HtmlNode)]

    @property
    def dom_path(self) -> str:
        if self.parent is None or self.parent.tag == "#document":
            return f"/{self.tag}[{self.sibling_index}]"
        return f"{self.parent.dom_path}/{self.tag}[{self.sibling_index}]"

    def descendants(self, tag: str | None = None) -> Iterator[HtmlNode]:
        for child in self.children:
            if tag is None or child.tag == tag:
                yield child
            yield from child.descendants(tag)

    def first_descendant(self, *tags: str) -> HtmlNode | None:
        wanted = set(tags)
        return next((node for node in self.descendants() if node.tag in wanted), None)

    def raw_text(self) -> str:
        parts: list[str] = []
        for item in self.content:
            parts.append(item if isinstance(item, str) else item.raw_text())
        return "".join(parts)

    def has_class_token(self, *tokens: str) -> bool:
        classes = set(self.attrs.get("class", "").lower().split())
        return bool(classes & {token.lower() for token in tokens})


class OrderedHtmlParser(HTMLParser):
    """把不可信 HTML 转成不执行脚本的保序树；畸形闭合按最近同名祖先恢复。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("#document", {}, None, 1)
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_node(tag, attrs, push=tag.lower() not in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_node(tag, attrs, push=False)

    def _append_node(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        push: bool,
    ) -> None:
        parent = self._stack[-1]
        normalized_tag = tag.lower()
        index = 1 + sum(child.tag == normalized_tag for child in parent.children)
        node = HtmlNode(
            normalized_tag,
            {str(key).lower(): str(value or "") for key, value in attrs},
            parent,
            index,
        )
        parent.content.append(node)
        if push:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].content.append(data)


def parse_html(source: str) -> HtmlNode:
    parser = OrderedHtmlParser()
    parser.feed(source)
    parser.close()
    return parser.root
