"""构造只读 HTML 树并提供稳定 DOM 定位。"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterator


_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


@dataclass(eq=False)
class HtmlNode:
    tag: str
    attrs: dict[str, str]
    parent: HtmlNode | None = None
    children: list[HtmlNode | str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def descendants(self, predicate: Callable[[HtmlNode], bool] | None = None) -> Iterator[HtmlNode]:
        for child in self.children:
            if not isinstance(child, HtmlNode):
                continue
            if predicate is None or predicate(child):
                yield child
            yield from child.descendants(predicate)

    def text(self, *, exclude: set[str] | None = None) -> str:
        parts: list[str] = []
        excluded = exclude or set()

        def walk(node: HtmlNode) -> None:
            if node.tag in excluded:
                return
            if node.tag == "math":
                alttext = node.attrs.get("alttext", "").strip()
                if alttext:
                    parts.append(alttext)
                    return
            if node.tag in {"annotation", "annotation-xml"}:
                return
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    walk(child)

        walk(self)
        return " ".join("".join(parts).split())


class HtmlTreeParser(HTMLParser):
    """容忍不完整网页的最小树构造器，不改写来源字节。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(
            tag.lower(),
            {key.lower(): value or "" for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        target = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == target:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def parse_html_tree(text: str) -> HtmlNode:
    parser = HtmlTreeParser()
    parser.feed(text)
    parser.close()
    return parser.root


def dom_path(node: HtmlNode) -> str:
    parts: list[str] = []
    current: HtmlNode | None = node
    while current is not None and current.parent is not None:
        siblings = [
            child for child in current.parent.children
            if isinstance(child, HtmlNode) and child.tag == current.tag
        ]
        index = siblings.index(current) + 1
        parts.append(f"{current.tag}[{index}]")
        current = current.parent
    return "/" + "/".join(reversed(parts))


def closest(node: HtmlNode, predicate: Callable[[HtmlNode], bool]) -> HtmlNode | None:
    current: HtmlNode | None = node
    while current is not None:
        if predicate(current):
            return current
        current = current.parent
    return None


def first_node(root: HtmlNode, predicate: Callable[[HtmlNode], bool]) -> HtmlNode | None:
    if predicate(root):
        return root
    return next(root.descendants(predicate), None)
