"""arxiv.py fetch and convert against real pages saved under tests/fixtures/arxiv (no network).

2305.18290: official HTML, trimmed. 1406.2661: official page cut mid-stream plus the ar5iv page.
2411.00010 and hep-th/9711200 have no HTML anywhere: ar5iv redirects to /abs or answers
200 "No content available".
"""

import contextlib
import io
import json
import pathlib
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
            "\n## References\n\n- [1] Y. Bai, A. Jones",
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
