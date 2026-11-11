"""arxiv.py fetch and convert against real pages saved under tests/fixtures/arxiv (no network).

2305.18290: official HTML, trimmed. 1406.2661: official page cut mid-stream plus the ar5iv page.
2411.00010 and hep-th/9711200 have no HTML anywhere: ar5iv redirects to /abs or answers
200 "No content available".
"""

import contextlib
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "arxiv"
sys.path.insert(0, str(ROOT / "steps"))

import arxiv  # noqa: E402

API = "https://export.arxiv.org/api/query?id_list="


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeWeb:
    """URL -> (status, final_url, body); images under the paper's host answer with stub bytes."""

    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.seen: list[str] = []

    def __call__(self, url: str):
        self.seen.append(url)
        page = self.pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is not None:
            return page
        if url.endswith((".png", ".svg", ".jpeg")):
            return 200, url, b"image " + url.encode()
        return 404, url, fixture("arxiv-404.html")


def ok(url: str, name: str):
    return 200, url, fixture(name)


def make_job(root: pathlib.Path, arxiv_id: str) -> pathlib.Path:
    job = {
        "job_id": 7, "pipeline": "arxiv",
        "source": {"platform": "arxiv", "id": arxiv_id, "url": f"https://arxiv.org/abs/{arxiv_id}"},
        "domain": "ml", "collection": None, "translate": "auto",
        "ai": {"tool": "claude", "model_note": "m", "model_translate": "m", "cli_version": "0"},
        "created": "2026-11-08T10:00:00+08:00", "public_url": "http://nas.lan:8780", "vault_name": "flori",
    }
    (root / "job.json").write_text(json.dumps(job))
    for name in ("01-arxiv-fetch", "02-arxiv-convert"):
        (root / "steps" / name).mkdir(parents=True)
    return root


def manifest(step: pathlib.Path) -> dict:
    data = json.loads((step / "manifest.json").read_text())
    assert set(data) == {"title", "files", "assets", "meta"}, data
    for value in data["meta"].values():
        scalar = isinstance(value, (str, int, float, bool))
        assert scalar or (isinstance(value, list) and all(isinstance(v, str) for v in value)), data["meta"]
    return data


@unittest.skipIf(arxiv.BeautifulSoup is None, "beautifulsoup4/lxml missing; install steps/requirements.txt")
class FetchConvertTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_both(self, arxiv_id: str, web: FakeWeb) -> tuple[pathlib.Path, pathlib.Path]:
        job = make_job(pathlib.Path(tempfile.mkdtemp(dir=self.root)), arxiv_id)
        fetched, converted = job / "steps/01-arxiv-fetch", job / "steps/02-arxiv-convert"
        self.assertEqual(arxiv.fetch(job, fetched, web), 0)
        self.assertEqual(arxiv.convert(job, converted), 0)
        return fetched, converted

    def test_new_paper_uses_official_html(self) -> None:
        page = "https://arxiv.org/html/2305.18290"
        web = FakeWeb({page: ok(page, "2305.18290.html"), API + "2305.18290": ok("", "2305.18290.atom.xml")})
        fetched, converted = self.run_both("2305.18290", web)

        meta = manifest(fetched)
        self.assertEqual(meta["files"], {"page": "page.html"})
        self.assertEqual(meta["assets"], [])
        self.assertEqual(meta["title"], meta["meta"]["title"])
        self.assertEqual(meta["meta"]["html_source"], "arxiv")
        self.assertEqual(meta["meta"]["version"], "v3")
        self.assertEqual(meta["meta"]["arxiv_id"], "2305.18290")
        self.assertEqual(meta["meta"]["authors"][0], "Rafael Rafailov")
        self.assertTrue(meta["meta"]["abstract"].startswith("While large-scale"))
        self.assertEqual(meta["meta"]["published"], "2023-05-29")
        self.assertEqual(meta["meta"]["primary_category"], "cs.LG")
        image = fetched / "res/html/2305.18290v3/figures/diagrams/teaser.png"
        self.assertTrue(image.is_file())
        self.assertIn('src="res/html/2305.18290v3/figures/diagrams/teaser.png"', (fetched / "page.html").read_text())
        self.assertNotIn("https://arxiv.org/static", " ".join(web.seen))

        out = manifest(converted)
        self.assertEqual(out["files"], {"original": "original.md"})
        self.assertEqual(out["assets"], ["frontier.svg", "teaser.png", "tldr_winrate_vs_temp.svg"])
        for name in out["assets"]:
            self.assertTrue((converted / "assets" / name).is_file())
        md = (converted / "original.md").read_text()
        for expected in (
            "# Direct Preference Optimization: Your Language Model is Secretly a Reward Model\n",
            "\n## Abstract\n",
            "\n## 1 Introduction\n",
            "\n### 6.3 Generalization to a new input distribution\n",
            "parametrized policy $\\pi_{\\theta}$.",
            "\\right)\\right]. \\tag{7}$$\n",
            "\n**Lemma 1.**\n",
            "\n![](assets/teaser.png)\n",
            "\n| DPO | 0.36 | 0.31 |\n",
            "\n## References\n\n- \\[1\\] Y. Bai, A. Jones",
            "```\nimport torch.nn.functional as F\n",
            "summaries[^1] with",
            "\n[^1]: https://huggingface.co/CarperAI/openai_summarize_tldr_sft\n",
        ):
            self.assertIn(expected, md)
        for noise in ("Back to arXiv", "Report Issue", "License: CC BY", "thanks:"):
            self.assertNotIn(noise, md)

    def test_truncated_official_page_falls_back_to_ar5iv(self) -> None:
        official, ar5iv = "https://arxiv.org/html/1406.2661", "https://ar5iv.labs.arxiv.org/html/1406.2661"
        for first in (ok(official, "1406.2661.arxiv-truncated.html"), (404, official, fixture("arxiv-404.html"))):
            with self.subTest(official=first[0]):
                web = FakeWeb({official: first, ar5iv: ok(ar5iv, "1406.2661.ar5iv.html"), API + "1406.2661": ok("", "1406.2661.atom.xml")})
                fetched, converted = self.run_both("1406.2661", web)
                meta = manifest(fetched)["meta"]
                self.assertEqual(meta["html_source"], "ar5iv")
                self.assertEqual(meta["version"], "v1")
                self.assertIn("https://ar5iv.labs.arxiv.org/html/1406.2661/assets/mnist_samples.png", web.seen)
                self.assertNotIn("https://ar5iv.labs.arxiv.org/assets/ar5iv.png", web.seen)
                self.assertEqual(len(manifest(converted)["assets"]), 6)
                md = (converted / "original.md").read_text()
                self.assertIn("# Generative Adversarial Nets\n", md)
                self.assertIn("[\\log(1-D(G(\\bm{z})))]. \\tag{1}$$", md)
                self.assertIn("\n1. A *conditional generative* model $p(\\bm{x}\\mid\\bm{c})$", md)
                self.assertIn("**for** number of training iterations **do**  \n", md)
                self.assertIn("| ![](assets/mnist_samples.png) | ![](assets/tfd_samples.png) |", md)
                self.assertLess(md.index("\n[^1]: All code and hyperparameters"), md.index("\n## References\n"))

    def test_no_html_anywhere_is_permanent(self) -> None:
        cases = {
            "2411.00010": (200, "https://arxiv.org/abs/2411.00010", b"<html><body>abs page</body></html>"),
            "hep-th/9711200": ok("https://ar5iv.labs.arxiv.org/html/hep-th/9711200", "ar5iv-no-content.html"),
        }
        for arxiv_id, ar5iv_answer in cases.items():
            with self.subTest(arxiv_id=arxiv_id):
                job = make_job(pathlib.Path(tempfile.mkdtemp(dir=self.root)), arxiv_id)
                web = FakeWeb({f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}": ar5iv_answer})
                with self.assertRaises(arxiv.StepError) as caught:
                    arxiv.fetch(job, job / "steps/01-arxiv-fetch", web)
                self.assertEqual(caught.exception.code, arxiv.PERMANENT)
                self.assertEqual(web.seen, [f"https://arxiv.org/html/{arxiv_id}", f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"])
                self.assertEqual(list((job / "steps/01-arxiv-fetch").iterdir()), [])

    def test_network_errors_and_truncation_are_retryable(self) -> None:
        official, ar5iv = "https://arxiv.org/html/1406.2661", "https://ar5iv.labs.arxiv.org/html/1406.2661"
        down = arxiv.StepError(arxiv.RETRY, "timed out")
        for pages in (
            {official: down, ar5iv: (302, "https://arxiv.org/abs/1406.2661", b"")},
            {official: ok(official, "1406.2661.arxiv-truncated.html"), ar5iv: (200, "https://arxiv.org/abs/1406.2661", b"")},
            {official: ok(official, "1406.2661.arxiv-truncated.html"), ar5iv: ok(ar5iv, "1406.2661.ar5iv.html"), API + "1406.2661": down},
        ):
            with self.subTest(pages=list(pages)):
                job = make_job(pathlib.Path(tempfile.mkdtemp(dir=self.root)), "1406.2661")
                with self.assertRaises(arxiv.StepError) as caught:
                    arxiv.fetch(job, job / "steps/01-arxiv-fetch", FakeWeb(pages))
                self.assertEqual(caught.exception.code, arxiv.RETRY)

    def test_api_answers_and_version_source(self) -> None:
        page = "https://arxiv.org/html/2305.18290"
        html = fixture("2305.18290.html").decode()
        atom = fixture("2305.18290.atom.xml").decode()
        feed_of = '<feed xmlns="http://www.w3.org/2005/Atom">{}</feed>'.format
        error_entry = feed_of("<entry><id>http://arxiv.org/api/errors#incorrect_id_format_for_2305.18290</id><title>Error</title></entry>")
        for name, answer, code in (
            ("error entry", (200, "", error_entry.encode()), arxiv.PERMANENT),
            ("http 400", (400, "", b"bad id"), arxiv.PERMANENT),
            ("empty feed", (200, "", feed_of("").encode()), arxiv.RETRY),
            ("not xml", (200, "", b"<html>busy"), arxiv.RETRY),
        ):
            with self.subTest(name):
                job = make_job(pathlib.Path(tempfile.mkdtemp(dir=self.root)), "2305.18290")
                web = FakeWeb({page: ok(page, "2305.18290.html"), API + "2305.18290": answer})
                with self.assertRaises(arxiv.StepError) as caught:
                    arxiv.fetch(job, job / "steps/01-arxiv-fetch", web)
                self.assertEqual(caught.exception.code, code)

        for name, page_version, api_version, expected in (
            ("page wins", "v2", "v3", "v2"),
            ("api fills in", "", "v3", "v3"),
            ("neither", "", "", None),
        ):
            with self.subTest(name):
                body = html.replace("2305.18290v3", "2305.18290" + page_version).encode()
                api = atom.replace("2305.18290v3</id>", f"2305.18290{api_version}</id>").encode()
                web = FakeWeb({page: (200, page, body), API + "2305.18290": (200, "", api)})
                job = make_job(pathlib.Path(tempfile.mkdtemp(dir=self.root)), "2305.18290")
                self.assertEqual(arxiv.fetch(job, job / "steps/01-arxiv-fetch", web), 0)
                self.assertEqual(manifest(job / "steps/01-arxiv-fetch")["meta"].get("version"), expected)

        with self.subTest("no date or category"):
            api = re.sub(r"<published>.*</published>|<arxiv:primary_category [^>]*/>", "", atom)
            api = api.replace("<author>", "<published>soon</published><author>", 1).encode()
            web = FakeWeb({page: ok(page, "2305.18290.html"), API + "2305.18290": (200, "", api)})
            job = make_job(pathlib.Path(tempfile.mkdtemp(dir=self.root)), "2305.18290")
            self.assertEqual(arxiv.fetch(job, job / "steps/01-arxiv-fetch", web), 0)
            meta = manifest(job / "steps/01-arxiv-fetch")["meta"]
            self.assertNotIn("published", meta)
            self.assertNotIn("primary_category", meta)

    def test_http_get_maps_server_errors_to_retry(self) -> None:
        error = urllib.error.HTTPError("https://arxiv.org/html/x", 503, "busy", {}, None)
        with mock.patch.object(arxiv.OPENER, "open", side_effect=error):
            with self.assertRaises(arxiv.StepError) as caught:
                arxiv.http_get("https://arxiv.org/html/x")
        self.assertEqual(caught.exception.code, arxiv.RETRY)
        with mock.patch.object(arxiv.OPENER, "open", side_effect=TimeoutError("slow")):
            with self.assertRaises(arxiv.StepError):
                arxiv.http_get("https://arxiv.org/html/x")

    def test_convert_names_assets_uniquely_and_ignores_outside_paths(self) -> None:
        job = make_job(self.root, "2401.00001")
        fetched = job / "steps/01-arxiv-fetch"
        for rel in ("res/a/fig.png", "res/b/fig.png"):
            (fetched / rel).parent.mkdir(parents=True, exist_ok=True)
            (fetched / rel).write_bytes(rel.encode())
        (job / "secret.png").write_bytes(b"outside")
        (fetched / "page.html").write_text(
            '<html><body><article class="ltx_document"><h1 class="ltx_title ltx_title_document">T</h1>'
            '<div class="ltx_para"><p class="ltx_p">Cost $5 and a_b *x* &lt;eos&gt;.</p></div>'
            '<ul class="ltx_biblist"><li class="ltx_bibitem"><span class="ltx_tag ltx_tag_bibitem">(1)</span>\n'
            '<span class="ltx_bibblock">A. Author.\n</span>\n<span class="ltx_bibblock"><span class="ltx_font_italic">Venue</span>, 2023.\n'
            '<div class="ltx_pagination ltx_role_newpage"></div>\n</span></li></ul>'
            '<ul class="ltx_itemize"><li class="ltx_item"><span class="ltx_tag ltx_tag_item">•</span>'
            '<div class="ltx_para"><p class="ltx_p">first</p></div></li>'
            '<li class="ltx_item"><span class="ltx_tag ltx_tag_item">•</span><div class="ltx_para"><p class="ltx_p">second</p></div></li></ul>'
            '<img src="res/a/fig.png"/><img src="res/b/fig.png"/><img src="res/a/fig.png"/>'
            '<img src="../../secret.png"/><img src="https://example.org/x.png"/></article></body></html>'
        )
        arxiv.write_manifest(fetched, "T", {"page": "page.html"}, [], {"authors": ["A. Author"]})
        converted = job / "steps/02-arxiv-convert"
        self.assertEqual(arxiv.convert(job, converted), 0)
        self.assertEqual(manifest(converted)["assets"], ["fig-2.png", "fig.png"])
        self.assertEqual((converted / "assets/fig-2.png").read_bytes(), b"res/b/fig.png")
        md = (converted / "original.md").read_text()
        self.assertEqual(
            md,
            "# T\n\nA. Author\n\nCost \\$5 and a\\_b \\*x\\* \\<eos>.\n\n- (1) A. Author. *Venue*, 2023.\n\n- first\n- second\n\n"
            "![](assets/fig.png)![](assets/fig-2.png)![](assets/fig.png)\n",
        )


def raw_brackets(md: str) -> list[int]:
    """Offsets of [ and ] that no backslash escapes (an even run of backslashes before them)."""
    return [m.start(2) for m in re.finditer(r"(\\*)([\[\]])", md) if len(m.group(1)) % 2 == 0]


def fenced_lines(md: str) -> list[bool]:
    """For each line, whether it sits inside a fenced code block (CommonMark backtick fences).

    Leading spaces are allowed because list items indent their fences.
    """
    inside, opener, flags = False, "", []
    for line in md.split("\n"):
        line = line.lstrip(" ")
        run = len(line) - len(line.lstrip("`"))
        if not inside and run >= 3 and "`" not in line[run:]:
            inside, opener = True, "`" * run
            flags.append(True)
        elif inside and line.strip().startswith(opener) and not line.strip().strip("`"):
            inside = False
            flags.append(True)
        else:
            flags.append(inside)
    return flags


@unittest.skipIf(arxiv.BeautifulSoup is None, "beautifulsoup4/lxml missing; install steps/requirements.txt")
class ConvertMarkupTest(unittest.TestCase):
    def convert(self, body: str) -> str:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            job = make_job(pathlib.Path(tmp), "2401.00001")
            fetched = job / "steps/01-arxiv-fetch"
            (fetched / "page.html").write_text(f'<html><body><article class="ltx_document">{body}</article></body></html>')
            (fetched / "res").mkdir()
            (fetched / "res/x.png").write_bytes(b"png")
            arxiv.write_manifest(fetched, "T", {"page": "page.html"}, [], {})
            self.assertEqual(arxiv.convert(job, job / "steps/02-arxiv-convert"), 0)
            return (job / "steps/02-arxiv-convert/original.md").read_text()

    def test_code_with_backticks_keeps_its_fence(self) -> None:
        lines = ["```python", "print(1)", "```", '&lt;iframe src="https://example.org"&gt;']
        md = self.convert(
            '<div class="ltx_listing">'
            + "".join(f'<div class="ltx_listingline"><span class="ltx_tag ltx_tag_listingline">{n}</span>{text}</div>' for n, text in enumerate(lines, 1))
            + '</div><pre class="ltx_verbatim ltx_font_typewriter">Reply as ```json\n{"a": 1}\n```\n&lt;img src=x&gt;</pre>'
            '<div class="ltx_para"><p class="ltx_p">Use <code>a``b</code> and <code>`x</code> here.</p></div>'
            '<div class="ltx_para"><p class="ltx_p">After the code.</p></div>'
        )
        rows = md.split("\n")
        flags = fenced_lines(md)
        for marker in ("<iframe", "<img", "print(1)", '{"a": 1}'):
            hits = [i for i, row in enumerate(rows) if marker in row]
            self.assertTrue(hits and all(flags[i] for i in hits), (marker, md))
        self.assertFalse(flags[rows.index("After the code.")], md)
        self.assertIn("Use ```a``b``` and `` `x `` here.", md)
        self.assertNotIn("\n1```python", md)

    def test_description_terms_and_custom_item_labels(self) -> None:
        md = self.convert(
            '<dl class="ltx_description">'
            '<dt class="ltx_item"><span class="ltx_tag ltx_tag_item"><span class="ltx_text ltx_font_bold">Offline RL:</span></span></dt>'
            '<dd class="ltx_item"><div class="ltx_para"><p class="ltx_p">learns from a fixed dataset.</p></div></dd>'
            '<dt class="ltx_item"><span class="ltx_tag ltx_tag_item"><span class="ltx_text ltx_font_bold">Online RL:</span></span></dt>'
            '<dd class="ltx_item"><div class="ltx_para"><p class="ltx_p">interacts.</p></div></dd></dl>'
            '<ul class="ltx_itemize"><li class="ltx_item"><span class="ltx_tag ltx_tag_item">Q1:</span>'
            '<div class="ltx_para"><p class="ltx_p">Does it work?</p></div></li>'
            '<li class="ltx_item"><span class="ltx_tag ltx_tag_item">•</span><div class="ltx_para"><p class="ltx_p">plain</p></div></li>'
            '<li class="ltx_item"><span class="ltx_tag ltx_tag_item">·</span><div class="ltx_para"><p class="ltx_p">fourth level</p></div></li></ul>'
            '<ol class="ltx_enumerate"><li class="ltx_item"><span class="ltx_tag ltx_tag_item">1.</span>'
            '<div class="ltx_para"><p class="ltx_p">numbered</p></div></li></ol>'
        )
        self.assertIn("**Offline RL:** learns from a fixed dataset.\n\n**Online RL:** interacts.", md)
        self.assertIn("- Q1: Does it work?\n- plain\n- fourth level\n", md)
        self.assertIn("\n1. numbered\n", md)

    def test_labels_never_share_a_line_with_a_block(self) -> None:
        def dd(first: str) -> str:
            return f'<dd class="ltx_item">{first}<div class="ltx_para"><p class="ltx_p">Tail.</p></div></dd>'

        def dt(term: str) -> str:
            return f'<dt class="ltx_item"><span class="ltx_tag ltx_tag_item"><span class="ltx_font_bold">{term}</span></span></dt>'

        listing = ('<div class="ltx_listing"><div class="ltx_listingline">```html</div>'
                   '<div class="ltx_listingline">&lt;iframe src="https://example.org"&gt;</div></div>')
        md = self.convert(
            '<dl class="ltx_description">'
            + dt("Code:") + dd('<pre class="ltx_verbatim">&lt;img src=x onerror=alert(1)&gt;\n```</pre>')
            + dt("Listing:") + dd(listing)
            + dt("Steps:") + dd('<ul class="ltx_itemize"><li class="ltx_item"><span class="ltx_tag ltx_tag_item">•</span>'
                                '<div class="ltx_para"><p class="ltx_p">one</p></div></li></ul>')
            + dt("Table:") + dd('<table class="ltx_tabular"><tr class="ltx_tr"><td class="ltx_td">a</td><td class="ltx_td">b</td></tr></table>')
            + '</dl><ul class="ltx_itemize"><li class="ltx_item"><span class="ltx_tag ltx_tag_item">Prompt:</span>'
            + listing.replace("iframe", "img") + '</li></ul>'
            '<section class="ltx_section"><h2 class="ltx_title">Next</h2><div class="ltx_para"><p class="ltx_p">After.</p></div></section>'
        )
        rows, flags = md.split("\n"), fenced_lines(md)
        for marker in ("<img", "<iframe", "```html"):
            hits = [i for i, row in enumerate(rows) if marker in row]
            self.assertTrue(hits and all(flags[i] for i in hits), (marker, md))
        for prose in ("## Next", "After.", "Tail.", "**Code:**", "**Listing:**", "**Steps:**", "**Table:**", "- Prompt:"):
            hits = [i for i, row in enumerate(rows) if row.strip() == prose]
            self.assertTrue(hits and not any(flags[i] for i in hits), (prose, md))
        self.assertIn("**Steps:**\n\n- one\n", md)
        self.assertIn("**Table:**\n\n| a | b |\n", md)
        self.assertIn("- Prompt:\n  \n  ````\n  ```html\n  <img", md)

    def test_list_item_starting_with_a_block_puts_the_marker_alone(self) -> None:
        bullet = '<span class="ltx_tag ltx_tag_item">•</span>'
        listing = ('<div class="ltx_listing"><div class="ltx_listingline">&lt;img src=x onerror=alert(1)&gt;</div>'
                   '<div class="ltx_listingline">```</div><div class="ltx_listingline">print(1)</div></div>')
        md = self.convert(
            f'<ul class="ltx_itemize"><li class="ltx_item">{bullet}{listing}</li>'
            f'<li class="ltx_item">{bullet}<div class="ltx_para"><p class="ltx_p">prose item</p></div></li></ul>'
            '<ol class="ltx_enumerate"><li class="ltx_item"><span class="ltx_tag ltx_tag_item">1.</span>'
            '<table class="ltx_tabular"><tr class="ltx_tr"><td class="ltx_td">a</td><td class="ltx_td">b</td></tr></table></li></ol>'
            '<div class="ltx_para"><p class="ltx_p">After.</p></div>'
        )
        self.assertIn("\n-\n  ````\n  <img src=x onerror=alert(1)>\n  ```\n  print(1)\n  ````\n- prose item\n", md)
        self.assertIn("\n1.\n   | a | b |\n   | --- | --- |\n", md)
        rows, flags = md.split("\n"), fenced_lines(md)
        for marker in ("<img", "print(1)"):
            hits = [i for i, row in enumerate(rows) if marker in row]
            self.assertTrue(hits and all(flags[i] for i in hits), (marker, md))
        for prose in ("- prose item", "After.", "| a | b |"):
            hits = [i for i, row in enumerate(rows) if row.strip() == prose]
            self.assertTrue(hits and not any(flags[i] for i in hits), (prose, md))

    def test_prose_starting_like_a_block_stays_prose(self) -> None:
        starts = ["# of samples is 10k.", "> 0 always.", "- 1 is negative.", "+ 1 more.", "2010. The first release.",
                  "3) third.", "~~~ waves", "| a | b"]
        literal = ["\\# of samples is 10k.", "\\> 0 always.", "\\- 1 is negative.", "\\+ 1 more.",
                   "2010\\. The first release.", "3\\) third.", "\\~~~ waves", "\\| a | b"]
        paragraphs = "".join(f'<div class="ltx_para"><p class="ltx_p">{text}</p></div>' for text in starts)
        md = self.convert(
            '<section class="ltx_section"><h2 class="ltx_title">1 Intro</h2>' + paragraphs + "</section>"
            '<dl class="ltx_description"><dt class="ltx_item"><span class="ltx_tag ltx_tag_item">Data:</span></dt>'
            '<dd class="ltx_item"><div class="ltx_para"><p class="ltx_p"># of samples is 10k.</p></div></dd></dl>'
            '<ul class="ltx_itemize">'
            '<li class="ltx_item"><span class="ltx_tag ltx_tag_item">Q:</span><div class="ltx_para"><p class="ltx_p">2010. A year.</p></div></li>'
            '<li class="ltx_item"><span class="ltx_tag ltx_tag_item">+</span><div class="ltx_para"><p class="ltx_p">Fast.</p></div></li>'
            '<li class="ltx_item"><span class="ltx_tag ltx_tag_item">•</span><div class="ltx_para"><p class="ltx_p">&gt; plain</p></div></li></ul>'
            '<div class="ltx_listing"><div class="ltx_listingline">- update <math alttext="x"></math></div></div>'
            '<table class="ltx_tabular"><tr class="ltx_tr"><td class="ltx_td">- a</td><td class="ltx_td">b</td></tr></table>'
        )
        rows = md.split("\n")
        for line in literal:
            self.assertIn(line, rows)
        self.assertIn("## 1 Intro", rows)
        self.assertIn("**Data:** \\# of samples is 10k.", rows)
        self.assertIn("- Q: 2010\\. A year.", rows)
        self.assertIn("- \\+ Fast.", rows)
        self.assertIn("- \\> plain", rows)
        self.assertIn("\\- update $x$", rows)
        self.assertIn("| - a | b |", rows)

    def test_terms_stand_alone_before_math_images_headings_and_quotes(self) -> None:
        def item(term: str, first: str) -> str:
            return (f'<dt class="ltx_item"><span class="ltx_tag ltx_tag_item">{term}</span></dt>'
                    f'<dd class="ltx_item">{first}<div class="ltx_para"><p class="ltx_p">Tail.</p></div></dd>')

        md = self.convert(
            '<dl class="ltx_description">'
            + item("Eq:", '<table class="ltx_equation"><tr><td><math alttext="E=mc^2" display="block"></math></td></tr></table>')
            + item("Fig:", '<figure class="ltx_figure"><img src="res/x.png"/></figure>')
            + item("Sub:", '<section class="ltx_subsection"><h3 class="ltx_title">Inner</h3></section>')
            + item("Quote:", '<blockquote class="ltx_quote"><p class="ltx_p">Said.</p></blockquote>')
            + "</dl>"
        )
        for block in ("**Eq:**\n\n$$E=mc^2$$", "**Fig:**\n\n![](assets/x.png)", "**Sub:**\n\n### Inner", "**Quote:**\n\n> Said."):
            self.assertIn(block, md)

    def test_footnote_type_prefixes_and_bare_marks_are_dropped(self) -> None:
        md = self.convert(
            '<div class="ltx_para"><p class="ltx_p">Text'
            '<span class="ltx_note ltx_role_footnotetext"><sup class="ltx_note_mark">0</sup><span class="ltx_note_outer">'
            '<span class="ltx_note_content"><sup class="ltx_note_mark">0</sup><span class="ltx_note_type">footnotetext: </span>'
            'Compared to V1, this draft adds baselines.</span></span></span> and'
            '<span class="ltx_note ltx_role_footnotemark"><sup class="ltx_note_mark">1</sup><span class="ltx_note_outer">'
            '<span class="ltx_note_content"><sup class="ltx_note_mark">1</sup><span class="ltx_note_type">footnotemark: </span>'
            '<span class="ltx_tag ltx_tag_note">1</span></span></span></span> more.</p></div>'
        )
        self.assertIn("[^1]: Compared to V1, this draft adds baselines.", md)
        for noise in ("footnotetext:", "footnotemark:", "[^2]"):
            self.assertNotIn(noise, md)

    def test_obsidian_syntax_in_text_is_escaped(self) -> None:
        md = self.convert(
            '<table class="ltx_tabular"><tr class="ltx_tr"><th class="ltx_td">#Params</th><th class="ltx_td">Acc</th></tr>'
            '<tr class="ltx_tr"><td class="ltx_td">7B</td><td class="ltx_td">1</td></tr></table>'
            '<div class="ltx_para"><p class="ltx_p">We count #tokens and 50%% here, see [[x]] ==hl== and C# 1.</p></div>'
        )
        self.assertIn("| \\#Params | Acc |", md)
        self.assertIn("We count \\#tokens and 50\\%\\% here, see \\[\\[x\\]\\] \\=\\=hl\\=\\= and C# 1.", md)

    def test_rowspan_cells_keep_the_rows_below_in_their_columns(self) -> None:
        # A fragment of Table 3 in 1706.03762: header cells and the (A) group label span two rows.
        def row(*cells: str) -> str:
            return '<tr class="ltx_tr">' + "".join(cells) + "</tr>"

        def td(text: str = "", rowspan: int = 1, colspan: int = 1) -> str:
            spans = (f' rowspan="{rowspan}"' if rowspan > 1 else "") + (f' colspan="{colspan}"' if colspan > 1 else "")
            return f'<td class="ltx_td"{spans}>{text}</td>'

        md = self.convert(
            '<table class="ltx_tabular">'
            + row(td(), td("N", rowspan=2), td("h", rowspan=2), td("train"), td("PPL"), td("BLEU"))
            + row(td(), td("steps"), td("(dev)"), td("(dev)"))
            + row(td("base"), td("6"), td("8"), td("100K"), td("4.92"), td("25.8"))
            + row(td("(A)", rowspan=2), td(), td("1"), td(), td("5.29"), td("24.9"))
            + row(td(), td("4"), td(), td("5.00"), td("25.5"))
            + row(td("(E)"), td("positional embedding", colspan=2), td(), td("4.92"), td("25.7"))
            + row(td("big"), td("6"), td("16"), td("300K"), td("4.33"), td("26.4"))
            + "</table>"
        )
        self.assertIn(
            "\n".join([
                "|  | N | h | train | PPL | BLEU |",
                "| --- | --- | --- | --- | --- | --- |",
                "|  |  |  | steps | (dev) | (dev) |",
                "| base | 6 | 8 | 100K | 4.92 | 25.8 |",
                "| (A) |  | 1 |  | 5.29 | 24.9 |",
                "|  |  | 4 |  | 5.00 | 25.5 |",
                "| (E) | positional embedding |  |  | 4.92 | 25.7 |",
                "| big | 6 | 16 | 300K | 4.33 | 26.4 |",
            ]),
            md,
        )

    def test_forged_spans_stay_small(self) -> None:
        md = self.convert(
            '<table class="ltx_tabular"><tr class="ltx_tr"><td class="ltx_td" rowspan="99999999">a</td>'
            '<td class="ltx_td" colspan="²">b</td><td class="ltx_td" colspan="99999999">c</td></tr>'
            '<tr class="ltx_tr"><td class="ltx_td">d</td></tr></table>'
        )
        self.assertIn("| a | b | c |", md)
        self.assertIn("|  | d |", md)
        self.assertLess(len(md), 100_000)

    def test_links_and_images_written_in_text_stay_text(self) -> None:
        text = 'See ![x](https://a/p.png), [t](obsidian://x) and C:\\[w](https://a/w) now.'
        md = self.convert(f'<div class="ltx_para"><p class="ltx_p">{text}</p></div>'
                          f'<table class="ltx_tabular"><tr class="ltx_tr"><td class="ltx_td">{text}</td></tr></table>')
        self.assertIn("See \\!\\[x\\](https://a/p.png), \\[t\\](obsidian://x) and C:\\\\\\[w\\](https://a/w) now.", md)
        # CommonMark and Obsidian open a link or an image only at an unescaped "[".
        self.assertEqual(raw_brackets(md), [], md)

    def test_own_markup_survives_escaping(self) -> None:
        md = self.convert(
            '<div class="ltx_para"><p class="ltx_p">Wow!<span class="ltx_note ltx_role_footnote"><span class="ltx_note_outer">'
            '<span class="ltx_note_content">A [note]!</span></span></span> '
            '<a href="https://a/b">site [1]</a>! <math alttext="x_{[i]}!"></math><img src="res/x.png"/></p></div>'
        )
        self.assertIn("Wow\\![^1] [site \\[1\\]](https://a/b)\\! $x_{[i]}!$![](assets/x.png)", md)
        self.assertIn("[^1]: A \\[note\\]\\!", md)

    def test_math_keeps_its_content_inside_the_delimiters(self) -> None:
        # Markdown math ends at the first unescaped $ (or $$), so none may remain inside it.
        md = self.convert(
            '<div class="ltx_para"><p class="ltx_p">'
            '<math alttext="1\\text{ if $x&gt;0$}"></math>, '
            '<math alttext="\\text{$ ![x](https://e/p.png) [l](obsidian://new) $}"></math>, '
            '<math alttext="\\$5+\\\\$y$ a\\ "></math> then '
            '<math display="block" alttext="a\\text{$$} ![y](https://e/q.png) \\text{$$}b"></math></p></div>'
            '<table class="ltx_equation"><tr><td><math alttext="x=1"></math></td>'
            '<td><span class="ltx_tag ltx_tag_equation">($$ ![t](https://e/t.png) $$)</span></td></tr></table>'
        )
        self.assertIn(
            "$1\\text{ if \\(x>0\\)}$, $\\text{\\( ![x](https://e/p.png) [l](obsidian://new) \\)}$, "
            "$\\$5+\\\\\\(y\\) a$ then $$a\\text{\\(\\)} ![y](https://e/q.png) \\text{\\(\\)}b$$",
            md,
        )
        self.assertIn("\n$$x=1 \\tag{![t](https://e/t.png)}$$\n", md)

    def test_link_targets_with_spaces_and_parentheses_stay_one_link(self) -> None:
        md = self.convert(
            '<div class="ltx_para"><p class="ltx_p">'
            '<a href="https://en.wikipedia.org/wiki/Foo_(bar) baz?q=a b#s">Foo</a> then '
            '<a href="https://a/x)[t](obsidian://y">trap</a> then '
            '<a href="https://a/b (c)">https://a/b (c)</a> end.</p></div>'
            '<div class="ltx_para"><p class="ltx_p"><a href="https://a/50%%off?q=%41%2">sale</a> and '
            '<a href="https://a/%">https://a/%</a>%</p></div>'
        )
        self.assertIn("[Foo](https://en.wikipedia.org/wiki/Foo_%28bar%29%20baz?q=a%20b#s) then "
                      "[trap](https://a/x%29%5Bt%5D%28obsidian://y) then https://a/b%20%28c%29 end.", md)
        self.assertIn("[sale](https://a/50%25%25off?q=%41%252) and https://a/%25%", md)


class CliTest(unittest.TestCase):
    def test_missing_dependency_gives_clear_error(self) -> None:
        code = (
            "import sys; sys.modules['bs4'] = None; sys.argv = ['arxiv.py', 'fetch', 'job', 'out'];"
            f"exec(compile(open({str(ROOT / 'steps/arxiv.py')!r}).read(), 'arxiv.py', 'exec'))"
        )
        run = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, arxiv.PERMANENT)
        self.assertIn("steps/requirements.txt", run.stderr)

    @unittest.skipIf(arxiv.BeautifulSoup is None, "beautifulsoup4/lxml missing")
    def test_convert_runs_as_isolated_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = make_job(pathlib.Path(tmp), "2305.18290")
            fetched = job / "steps/01-arxiv-fetch"
            (fetched / "page.html").write_bytes(fixture("2305.18290.html"))
            arxiv.write_manifest(fetched, "DPO", {"page": "page.html"}, [], {})
            out = job / "steps/02-arxiv-convert"
            run = subprocess.run(
                [sys.executable, "-I", str(ROOT / "steps/arxiv.py"), "convert", str(job), str(out)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue((out / "original.md").read_text().startswith("# Direct Preference Optimization"))


if __name__ == "__main__":
    unittest.main()
