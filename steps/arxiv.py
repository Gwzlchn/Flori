"""arxiv-fetch and arxiv-convert steps (contract 1 in docs/contracts.md).

Usage: python3 -I arxiv.py fetch|convert <job_dir> <out_dir>

fetch stores the paper HTML as page.html with its images under res/, img src rewritten to
those local paths. convert reads page.html by its logical name and writes original.md and
figures.json, the figures as the page groups them.
Requests go out directly: the contract gives arXiv steps no proxy.
"""

import http.client
import json
import pathlib
import posixpath
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    import lxml  # noqa: F401  BeautifulSoup needs it for the "lxml" parser
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag
except ImportError:
    BeautifulSoup = None

RETRY, PERMANENT = 10, 20
UA = "Mozilla/5.0 (X11; Linux x86_64) flori-arxiv/1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
HOSTS = {"arxiv": "arxiv.org", "ar5iv": "ar5iv.labs.arxiv.org"}
# The author's comment and journal reference are free text; categories are short terms. Real ones
# stay well within these bounds.
NOTE_CHARS, CATEGORY_CHARS, MAX_CATEGORIES = 1000, 64, 32
# A figure caption in figures.json; the note's image list shows less of it.
FIGURE_CAPTION_CHARS = 1000


class StepError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def http_get(url: str) -> tuple[int, str, bytes]:
    """Returns (status, final_url, body) for any status below 500.

    5xx, 429, timeouts and broken connections raise StepError(RETRY).
    """
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with OPENER.open(request, timeout=60) as response:
            return response.status, response.geturl(), response.read()
    except urllib.error.HTTPError as err:
        if err.code >= 500 or err.code == 429:
            raise StepError(RETRY, f"{url}: HTTP {err.code}") from None
        return err.code, err.geturl(), err.read()
    except (OSError, http.client.HTTPException) as err:
        raise StepError(RETRY, f"{url}: {err}") from None


def page_state(source: str, status: int, final_url: str, html: str) -> str:
    """ok, missing (this source has no HTML for the paper) or truncated."""
    if status != 200 or urllib.parse.urlsplit(final_url).hostname != HOSTS[source]:
        return "missing"  # ar5iv sends papers it lacks to arxiv.org/abs
    if not re.search(r"</body\s*>\s*</html\s*>\s*$", html, re.I):
        return "truncated"
    article = BeautifulSoup(html, "lxml").select_one("article.ltx_document")
    if article is None:
        return "missing"
    for error in article.select(".ltx_ERROR"):
        error.decompose()
    # ar5iv answers 200 with "No content available" plus a LaTeXML fatal error for papers it lacks.
    body = " ".join(p.get_text(" ", strip=True) for p in article.select(".ltx_para"))
    return "ok" if len(body) >= 200 else "missing"


def find_html(arxiv_id: str, get) -> tuple[str, str, str] | None:
    """Official HTML first, then ar5iv. Returns (source, final_url, html) or None if neither has it."""
    transient = None
    for source, host in HOSTS.items():
        url = f"https://{host}/html/{arxiv_id}"
        try:
            status, final_url, body = get(url)
        except StepError as err:
            print(f"{source}: {err}", flush=True)
            transient = err
            continue
        html = body.decode("utf-8", "replace")
        state = page_state(source, status, final_url, html)
        print(f"{source}: HTTP {status} {final_url} -> {state}", flush=True)
        if state == "ok":
            return source, final_url, html
        if state == "truncated":
            transient = StepError(RETRY, f"{url}: HTML 被截断")
    if transient is not None:
        raise transient
    return None


def api_meta(arxiv_id: str, get) -> dict:
    url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv_id, safe='/')}"
    status, _, body = get(url)
    if status != 200:
        raise StepError(PERMANENT, f"arXiv API HTTP {status}")
    try:
        entry = ET.fromstring(body).find("a:entry", ATOM)
    except ET.ParseError as err:
        raise StepError(RETRY, f"arXiv API 返回无法解析: {err}") from None
    if entry is None:
        raise StepError(RETRY, "arXiv API 返回空结果")
    entry_id = entry.findtext("a:id", "", ATOM)
    if "/api/errors" in entry_id:
        raise StepError(PERMANENT, f"arXiv API 不认识 {arxiv_id}")
    version = re.search(r"v(\d+)$", entry_id)
    # published is the first version's date; the category is the one arXiv lists the paper under.
    # [0-9], not \d, which also takes other scripts' digits.
    published = re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", entry.findtext("a:published", "", ATOM).strip())
    category = entry.find("arxiv:primary_category", ATOM)
    category = (category.get("term") or "").strip() if category is not None else ""
    categories: list[str] = []
    for node in entry.findall("a:category", ATOM):
        term = plain(node.get("term") or "", CATEGORY_CHARS)
        if term and term not in categories and len(categories) < MAX_CATEGORIES:
            categories.append(term)
    return {
        "title": " ".join(entry.findtext("a:title", "", ATOM).split()),
        "authors": [n.strip() for n in (a.findtext("a:name", "", ATOM) for a in entry.findall("a:author", ATOM)) if n.strip()],
        "abstract": " ".join(entry.findtext("a:summary", "", ATOM).split()),
        "version": f"v{version.group(1)}" if version else None,
        "published": published.group() if published else None,
        "primary_category": category or None,
        "comment": plain(entry.findtext("arxiv:comment", "", ATOM), NOTE_CHARS) or None,
        "journal_ref": plain(entry.findtext("arxiv:journal_ref", "", ATOM), NOTE_CHARS) or None,
        "categories": categories or None,
    }


def plain(text: str, limit: int) -> str:
    """One line of text without control characters, cut to at most limit characters."""
    text = " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def local_path(url: str, used: set[str]) -> str:
    """res/<url path> with only safe characters; a numeric suffix keeps names unique."""
    parts = [re.sub(r"[^A-Za-z0-9._-]", "_", p) for p in urllib.parse.urlsplit(url).path.split("/")]
    base = "/".join(["res"] + ([p for p in parts if p.strip(".")] or ["image"]))
    candidate, n = base, 1
    while candidate in used:
        n += 1
        root, ext = posixpath.splitext(base)
        candidate = f"{root}-{n}{ext}"
    used.add(candidate)
    return candidate


def save_images(article, page_url: str, out_dir: pathlib.Path, get) -> int:
    """Downloads every img/object image of the article from the page's host and points it at the copy."""
    host = urllib.parse.urlsplit(page_url).hostname
    saved: dict[str, str | None] = {}
    used: set[str] = set()
    for node in article.find_all(["img", "object"]):
        attr = "src" if node.name == "img" else "data"
        if not node.get(attr):
            continue
        url = urllib.parse.urljoin(page_url, node[attr])
        if urllib.parse.urlsplit(url).hostname != host:
            continue  # data: URIs and other sites stay as they are
        if url not in saved:
            status, _, body = get(url)
            if status != 200:
                print(f"skip image {url}: HTTP {status}", flush=True)
                saved[url] = None
                continue
            rel = local_path(url, used)
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / rel).write_bytes(body)
            saved[url] = rel
        if saved[url]:
            node[attr] = saved[url]
    return sum(1 for rel in saved.values() if rel)


def fetch(job_dir: pathlib.Path, out_dir: pathlib.Path, get=http_get) -> int:
    arxiv_id = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["source"]["id"]
    found = find_html(arxiv_id, get)
    if found is None:
        raise StepError(PERMANENT, f"{arxiv_id} 没有官方 HTML,ar5iv 也没有;不回退 PDF")
    source, page_url, html = found
    meta = api_meta(arxiv_id, get)
    soup = BeautifulSoup(html, "lxml")
    images = save_images(soup.select_one("article.ltx_document"), page_url, out_dir, get)
    (out_dir / "page.html").write_text(str(soup), encoding="utf-8")
    shown = re.search(re.escape(arxiv_id) + r"v(\d+)", page_url + html)
    meta = {
        "arxiv_id": arxiv_id,
        "version": f"v{shown.group(1)}" if shown else meta["version"],
        "title": meta["title"],
        "authors": meta["authors"],
        "abstract": meta["abstract"],
        "html_source": source,
        "published": meta["published"],
        "primary_category": meta["primary_category"],
        "categories": meta["categories"],
        "comment": meta["comment"],
        "journal_ref": meta["journal_ref"],
    }
    # manifest meta values cannot be null (MetaValue in src/pipeline.rs).
    meta = {key: value for key, value in meta.items() if value is not None}
    write_manifest(out_dir, meta["title"] or None, {"page": "page.html"}, [], meta)
    print(f"saved {page_url} with {images} images", flush=True)
    return 0


def write_manifest(out_dir: pathlib.Path, title, files: dict, assets: list, meta: dict) -> None:
    manifest = {"title": title, "files": files, "assets": assets, "meta": meta}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


SKIP_TAGS = {"script", "style", "nav", "button", "noscript", "form", "header", "footer", "svg"}
SKIP_CLASSES = {
    "ltx_authors", "ltx_pubnotes", "ltx_page_logo", "ltx_page_header", "ltx_page_footer",
    "ltx_ERROR", "ltx_pagination", "ltx_dates", "ltx_TOC", "ltx_listing_data",
    "ltx_tag_listingline", "ltx_tag_item", "ltx_rdf",
}
BLOCK_TAGS = {
    "p", "div", "section", "article", "figure", "figcaption", "table", "ul", "ol", "dl", "dt", "dd",
    "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}
# A span holding one of these is laid out as a block (LaTeXML wraps tabulars in inline-block spans).
BLOCK_INSIDE_SPAN = sorted(BLOCK_TAGS - {"li", "dt", "dd"})
# Every bracket in paper text is escaped, so text can never open a link, an image or an Obsidian
# [[link]]; only this converter writes those. "!" is escaped as well: a text "!" right before
# a footnote mark or link written here would turn it into an image.
MD_SPECIAL = re.compile(r"([\\`*_$<!\[\]])")
# Obsidian reads #word as a tag, %% as a comment and == as a highlight.
OBSIDIAN_SPECIAL = re.compile(r"#(?=\S)|%{2,}|={2,}")
# List labels LaTeXML writes for plain itemize and enumerate; anything else is the author's \item[...] text.
PLAIN_LABEL = re.compile(r"[•·∙◦▪‣⋅∗–—*-]|\d+\.")
# Blocks that must start their own line: code, tables, lists, quotes, display math, images, headings.
NOT_PROSE = re.compile(r"```|~~~|\||- |\d+\. |>|\$\$|!\[|#")
# How a line of text would open a block (heading, quote, list item, fence, table row) instead of a paragraph.
BLOCK_START = re.compile(r"[#>+|-]|~~~|([0-9]{1,9})([.)])(?=\s|$)")
# A $ after an even run of backslashes, which TeX reads as a math shift.
UNESCAPED_DOLLAR = re.compile(r"(?<!\\)((?:\\\\)*)\$")


def escape(text: str) -> str:
    text = MD_SPECIAL.sub(r"\\\1", text)
    return OBSIDIAN_SPECIAL.sub(lambda m: "".join("\\" + c for c in m.group()), text)


def link_target(href: str) -> str:
    """The URL with everything but plain URL characters percent-encoded. Spaces, parentheses,
    brackets and backslashes would end the link early or split it into text plus a second link.
    Existing escapes stay; a % that starts none is encoded too, so %% (an Obsidian comment) never
    appears."""
    return re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", urllib.parse.quote(href, safe=":/?#&=+,;@%"))


def literal_start(line: str) -> str:
    """Escapes a block marker at the start of a prose line so it stays paragraph text."""
    start = BLOCK_START.match(line)
    if start is None:
        return line
    if start.group(1):
        return f"{start.group(1)}\\{start.group(2)}{line[start.end():]}"
    return "\\" + line


def fence(code: str) -> str:
    """A code block whose fence is longer than any backtick run inside it."""
    mark = "`" * max([3] + [len(run) + 1 for run in re.findall(r"`+", code)])
    return f"{mark}\n{code}\n{mark}"


def span(cell, attr: str, limit: int) -> int:
    """A cell's colspan or rowspan: 1 when missing or malformed, never above limit, so a forged
    span cannot make the grid huge (HTML itself caps colspan at 1000). str.isdigit would pass "²",
    which int() rejects."""
    value = cell.get(attr) or ""
    return min(int(value), limit) if re.fullmatch(r"[0-9]{1,9}", value) and int(value) > 1 else 1


def tex(math) -> str:
    """LaTeX source of a MathML element on one line, with % comments dropped, ready to sit
    between Markdown $ delimiters.

    A $ of the source (math inside \\text{...}) would end the Markdown math right there and turn
    the rest of the formula, links and images included, into Markdown. Unescaped ones become \\(
    and \\) in turn, which MathJax reads the same way. A trailing backslash, left over from a
    control space, would escape the closing delimiter, so it is dropped."""
    source = math.get("alttext") or ""
    if not source:
        annotation = math.find("annotation", attrs={"encoding": "application/x-tex"})
        source = annotation.get_text() if annotation else math.get_text()
    lines = (re.sub(r"(?<!\\)%.*", "", line) for line in source.splitlines())
    source = " ".join(" ".join(lines).split())
    opened = False

    def delimiter(m: re.Match) -> str:
        nonlocal opened
        opened = not opened
        return m.group(1) + ("\\(" if opened else "\\)")

    source = UNESCAPED_DOLLAR.sub(delimiter, source)
    while (len(source) - len(source.rstrip("\\"))) % 2:
        source = source[:-1].rstrip()
    return source


class Converter:
    def __init__(self, step_dir: pathlib.Path, out_dir: pathlib.Path, authors: list[str]) -> None:
        self.step_dir = step_dir.resolve()
        self.out_dir = out_dir
        self.authors = authors
        self.assets: dict[pathlib.Path, str] = {}
        self.notes: list[str] = []
        self.notes_written = 0
        self.plain = False

    def document(self, article) -> str:
        return "\n\n".join(self.blocks(article) + self.note_definitions()) + "\n"

    def note_definitions(self) -> list[str]:
        """Definitions not written yet. They go before References: translate keeps that section verbatim."""
        first, self.notes_written = self.notes_written, len(self.notes)
        return [f"[^{n}]: {text}" for n, text in enumerate(self.notes[first:], first + 1)]

    def plain_text(self, node) -> str:
        """Inline text without emphasis or links, for headings and list labels."""
        self.plain = True
        text = " ".join(self.inline(node).split())
        self.plain = False
        return text

    def skipped(self, node) -> bool:
        return node.name in SKIP_TAGS or not SKIP_CLASSES.isdisjoint(node.get("class") or ())

    def is_block(self, node) -> bool:
        if "ltx_note" in (node.get("class") or ()):
            return False
        if node.name == "span":
            return any(not self.skipped(inner) for inner in node.find_all(BLOCK_INSIDE_SPAN))
        return node.name in BLOCK_TAGS

    def blocks(self, node) -> list[str]:
        out: list[str] = []
        pending: list[str] = []

        def flush() -> None:
            text = " ".join("".join(pending).split())
            if text:
                out.append(literal_start(text))
            pending.clear()

        for child in node.children:
            if isinstance(child, Tag):
                if self.skipped(child):
                    continue
                if self.is_block(child):
                    flush()
                    out.extend(self.block(child))
                else:
                    pending.append(self.inline_tag(child))
            elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                pending.append(escape(str(child)))
        flush()
        return out

    def block(self, node) -> list[str]:
        name, classes = node.name, set(node.get("class") or ())
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = self.plain_text(node)
            if not text:
                return []
            if "ltx_title_abstract" in classes:
                return [f"## {text}"]
            if name == "h6":
                return [f"**{text}**"]
            out = ["#" * int(name[1]) + " " + text]
            if "ltx_title_document" in classes and self.authors:
                out.append(", ".join(escape(a) for a in self.authors))
            return out
        if name == "table":
            if classes & {"ltx_equation", "ltx_equationgroup", "ltx_eqn_table"}:
                return self.equations(node)
            return self.table(node)
        if name in {"ul", "ol"}:
            return self.bullet_list(node)
        if name == "pre":
            return [fence(node.get_text().strip("\n"))]
        if "ltx_listing" in classes:
            return self.listing(node)
        if name == "blockquote":
            return ["\n".join("> " + line for line in "\n\n".join(self.blocks(node)).split("\n"))]
        if name == "dl":
            return self.description(node)
        if "ltx_bibliography" in classes:
            return self.note_definitions() + self.blocks(node)
        if name == "hr":
            return ["---"]
        return self.blocks(node)

    def equations(self, table) -> list[str]:
        out = []
        for row in table.find_all("tr"):
            parts = [tex(m) for m in row.find_all("math") if m.find_parent("tr") is row]
            parts = [p for p in parts if p]
            if not parts:
                continue
            number = row.select_one(".ltx_tag_equation, .ltx_tag_equationgroup")
            label = number.get_text(strip=True).strip("()") if number and number.find_parent("tr") is row else ""
            # The number is plain text; a $ in it would end the display math early.
            label = label.replace("$", "").strip()
            body = " ".join(parts) + (f" \\tag{{{label}}}" if label else "")
            out.append(f"$${body}$$")
        return out

    def table(self, table) -> list[str]:
        """A Markdown grid of the table. A cell spanning several columns or rows keeps its text in its
        first slot and leaves the others empty, so the cells after it and the rows below stay in their
        columns; ablation tables group rows under one rowspan label."""
        rows = []
        trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        occupied: set[tuple[int, int]] = set()  # slots of later rows taken by a rowspan above
        for r, tr in enumerate(trs):
            cells: list[str] = []
            for cell in tr.find_all(["td", "th"], recursive=False):
                while (r, len(cells)) in occupied:
                    cells.append("")
                width, height = span(cell, "colspan", 1000), span(cell, "rowspan", len(trs) - r)
                occupied.update((r + dr, len(cells) + dc) for dr in range(1, height) for dc in range(width))
                cells.append(" ".join(self.inline(cell).split()).replace("|", "\\|"))
                cells.extend([""] * (width - 1))
            if any(cells):
                rows.append(cells)
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(r) + " |" for r in rows]
        lines.insert(1, "|" + " --- |" * width)
        return ["\n".join(lines)]

    def bullet_list(self, node) -> list[str]:
        items = []
        for n, li in enumerate(node.find_all("li", recursive=False), 1):
            tag = li.find(class_="ltx_tag_item", recursive=False)
            label = self.plain_text(tag) if tag else ""
            custom = label and not PLAIN_LABEL.fullmatch(label)
            label = literal_start(label)
            marker = f"{n}. " if node.name == "ol" and not custom else "- "
            parts = self.blocks(li)
            if custom:
                parts = self.lead(label, parts)
            pad = " " * len(marker)
            # A code fence, table or other block must start its own line: the marker stands alone
            # and the block follows directly (a blank line here would end the item).
            start = marker if not parts or not NOT_PROSE.match(parts[0]) else marker.rstrip() + "\n" + pad
            items.append(start + "\n\n".join(parts).replace("\n", "\n" + pad))
        return ["\n".join(items)] if items else []

    def description(self, node) -> list[str]:
        """dt terms (LaTeXML puts them in an ltx_tag_item span) in bold, leading their dd."""
        out, term = [], ""
        for child in node.find_all(["dt", "dd"], recursive=False):
            if child.name == "dt":
                if term:
                    out.append(f"**{term}**")
                tag = child.find(class_="ltx_tag_item")
                term = self.plain_text(tag or child)
                continue
            out.extend(self.lead(f"**{term}**", self.blocks(child)) if term else self.blocks(child))
            term = ""
        return out + ([f"**{term}**"] if term else [])

    @staticmethod
    def lead(label: str, parts: list[str]) -> list[str]:
        """Puts a term or list label in front of a paragraph; other blocks get it on its own line."""
        if parts and not NOT_PROSE.match(parts[0]):
            return [f"{label} {parts[0]}"] + parts[1:]
        return [label] + parts

    def listing(self, node) -> list[str]:
        for noise in node.select(".ltx_tag_listingline, .ltx_listing_data"):
            noise.decompose()
        lines = node.select(".ltx_listingline") or [node]
        if node.find("math") is None:
            return [fence("\n".join(line.get_text().strip("\n") for line in lines))]
        texts = [literal_start(" ".join(self.inline(line).split())) for line in lines]
        return ["  \n".join(t for t in texts if t)]

    def inline(self, node) -> str:
        parts = []
        for child in node.children:
            if isinstance(child, Tag):
                if not self.skipped(child):
                    parts.append(self.inline_tag(child))
            elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                parts.append(escape(str(child)))
        return "".join(parts)

    def inline_tag(self, node) -> str:
        name, classes = node.name, set(node.get("class") or ())
        if name == "math":
            source = tex(node)
            if not source:
                return ""
            return f"$${source}$$" if node.get("display") == "block" else f"${source}$"
        if name in {"img", "object"}:
            return self.image(node)
        if name == "br":
            return " "
        if "ltx_note" in classes:
            return self.footnote(node)
        href = node.get("href", "") if name == "a" else ""
        if href.startswith(("http://", "https://")) and not self.plain:
            text = node.get_text(" ", strip=True)
            target = link_target(href)
            return target if text in ("", href) else f"[{' '.join(self.inline(node).split())}]({target})"
        if name == "code" or ("ltx_font_typewriter" in classes and node.find("math") is None):
            code = " ".join(node.get_text().split())
            if not code:
                return ""
            mark = "`" * max([1] + [len(run) + 1 for run in re.findall(r"`+", code)])
            pad = " " if code.startswith("`") or code.endswith("`") else ""
            return f"{mark}{pad}{code}{pad}{mark}"
        inner = self.inline(node)
        if name in {"em", "i"} or "ltx_font_italic" in classes:
            return self.wrap(inner, "*")
        if name in {"b", "strong"} or "ltx_font_bold" in classes:
            return self.wrap(inner, "**")
        return inner

    def wrap(self, text: str, mark: str) -> str:
        core = text.strip()
        if not core or self.plain:
            return text
        return text[: len(text) - len(text.lstrip())] + mark + core + mark + text[len(text.rstrip()):]

    def footnote(self, note) -> str:
        content = note.select_one(".ltx_note_content")
        if content is None:
            return ""
        # The type prefix ("footnotetext: ", "footnotemark: ") is LaTeXML bookkeeping; a bare
        # \footnotemark leaves nothing else and is dropped by the empty check below.
        for mark in content.select(".ltx_note_mark, .ltx_tag_note, .ltx_note_type"):
            mark.decompose()
        text = " ".join(self.inline(content).split())
        if not text:
            return ""
        self.notes.append(text)
        return f"[^{len(self.notes)}]"

    def image_path(self, node) -> pathlib.Path | None:
        """The fetched file an img or object shows; None for other sites, data: URIs and paths
        outside the fetch step."""
        ref = node.get("src") if node.name == "img" else node.get("data")
        if not ref or urllib.parse.urlsplit(ref).scheme:
            return None
        path = (self.step_dir / ref).resolve()
        if not path.is_relative_to(self.step_dir) or not path.is_file():
            return None
        return path

    def image(self, node) -> str:
        path = self.image_path(node)
        if path is None:
            return ""
        if path not in self.assets:
            stem, suffix = path.stem, path.suffix
            name, n = path.name, 1
            while name in self.assets.values():
                n += 1
                name = f"{stem}-{n}{suffix}"
            (self.out_dir / "assets").mkdir(exist_ok=True)
            shutil.copyfile(path, self.out_dir / "assets" / name)
            self.assets[path] = name
        return f"![](assets/{self.assets[path]})"

    def figures(self, article) -> list[dict]:
        """Every outermost figure element that shows an image of original.md, in document order:
        its label ("Figure 2"), its own caption as plain text with formulas as $TeX$, and the
        asset names of all its images, those of nested panels included. A table without images
        is no figure. Runs after document(), which names the assets."""
        out = []
        for figure in article.find_all("figure"):
            if any(isinstance(p, Tag) and (p.name == "figure" or self.skipped(p)) for p in figure.parents):
                continue
            images: list[str] = []
            for node in figure.find_all(["img", "object"]):
                name = self.assets.get(self.image_path(node))
                if name and name not in images:
                    images.append(name)
            if not images:
                continue
            caption = next((c for c in figure.find_all("figcaption") if c.find_parent("figure") is figure), None)
            tag = caption.find(class_="ltx_tag") if caption else None
            label = plain(tag.get_text(), FIGURE_CAPTION_CHARS).rstrip(":：. ") if tag else ""
            text = plain(self.caption_text(caption), FIGURE_CAPTION_CHARS) if caption else ""
            out.append({"label": label, "caption": text, "images": images})
        return out

    def caption_text(self, node) -> str:
        """Text of a caption without its "Figure n:" tag or footnotes; formulas as $TeX$."""
        parts = []
        for child in node.children:
            if isinstance(child, Tag):
                classes = set(child.get("class") or ())
                if child.name == "math":
                    source = tex(child)
                    parts.append(f"${source}$" if source else "")
                elif not (self.skipped(child) or classes & {"ltx_tag", "ltx_note"}):
                    parts.append(self.caption_text(child))
            elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                parts.append(str(child))
        return "".join(parts)


def find_step(job_dir: pathlib.Path, name: str) -> pathlib.Path:
    found = sorted(job_dir.glob(f"steps/*-{name}/manifest.json"))
    if not found:
        raise StepError(PERMANENT, f"找不到 {name} 的产物")
    return found[-1].parent


def convert(job_dir: pathlib.Path, out_dir: pathlib.Path) -> int:
    step_dir = find_step(job_dir, "arxiv-fetch")
    manifest = json.loads((step_dir / "manifest.json").read_text(encoding="utf-8"))
    soup = BeautifulSoup((step_dir / manifest["files"]["page"]).read_bytes(), "lxml")
    article = soup.select_one("article.ltx_document")
    if article is None:
        raise StepError(PERMANENT, "page.html 里没有 LaTeXML 正文")
    meta = manifest["meta"]
    converter = Converter(step_dir, out_dir, meta.get("authors", []))
    text = converter.document(article)
    if article.select_one(".ltx_title_document") is None and manifest["title"]:
        text = f"# {manifest['title']}\n\n{text}"
    (out_dir / "original.md").write_text(text, encoding="utf-8")
    figures = converter.figures(article)
    (out_dir / "figures.json").write_text(json.dumps(figures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = {"original": "original.md", "figures": "figures.json"}
    write_manifest(out_dir, None, files, sorted(converter.assets.values()), {})
    print(f"original.md {len(text)} chars, {len(converter.assets)} images, {len(figures)} figures", flush=True)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("fetch", "convert"):
        print("usage: arxiv.py fetch|convert <job_dir> <out_dir>", file=sys.stderr)
        return PERMANENT
    if BeautifulSoup is None:
        print("缺少 Python 包 beautifulsoup4 或 lxml,按 steps/requirements.txt 安装", file=sys.stderr)
        return PERMANENT
    job_dir, out_dir = pathlib.Path(argv[2]), pathlib.Path(argv[3])
    try:
        return fetch(job_dir, out_dir) if argv[1] == "fetch" else convert(job_dir, out_dir)
    except StepError as err:
        print(err, file=sys.stderr)
        return err.code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
