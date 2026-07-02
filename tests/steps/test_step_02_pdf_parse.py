"""steps/paper/step_02_pdf_parse.py 的测试,pymupdf 全 mock。"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from steps.paper.step_02_pdf_parse import PdfParseStep
from tests.steps.conftest import make_step_config


class TestPdfParseStep:
    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        config = make_step_config(tmp_path, step_name="02_pdf_parse")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        assert step.validate_inputs() == ["input/source.pdf"]

    def test_execute_mock(self, tmp_path, monkeypatch):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4 fake")

        mock_page = MagicMock()
        mock_page.get_text.side_effect = lambda fmt=None: (
            {"blocks": [{"lines": [{"spans": [
                {"text": "Test Title", "size": 18, "flags": 16}
            ]}]}]}
            if fmt == "dict" else "Abstract\nSome text\n\nIntroduction\nIntro text"
        )
        mock_page.get_images.return_value = []

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 1
        mock_doc.__getitem__ = lambda self, i: mock_page
        mock_doc.__iter__ = lambda self: iter([mock_page])
        mock_doc.__enter__ = lambda self: self
        mock_doc.__exit__ = lambda self, *a: None
        mock_doc.metadata = {"title": "Test Paper", "author": "Author A, Author B"}

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc
        monkeypatch.setitem(sys.modules, "fitz", mock_fitz)

        config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        result = step.execute()

        parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
        assert parsed["title"] == "Test Paper"
        assert parsed["pages"] == 1
        assert len(parsed["authors"]) == 2

    def test_input_hashes(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF test")
        config = make_step_config(tmp_path, step_name="02_pdf_parse")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        hashes = step.input_hashes()
        assert "pdf" in hashes
        assert hashes["pdf"].startswith("sha256:")


# 标题跨 span 拼接 + 摘要终止符兜底(轻量 fake doc)

class _FakePage:
    def __init__(self, page_dict, page_text):
        self._dict = page_dict
        self._text = page_text

    def get_text(self, kind=None):
        return self._dict if kind == "dict" else self._text


class _FakeDoc:
    def __init__(self, metadata, page_dict=None, page_text=""):
        self.metadata = metadata
        self._page = _FakePage(page_dict or {"blocks": []}, page_text)

    def __len__(self):
        return 1

    def __getitem__(self, i):
        return self._page


def _mk_step(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["input", "intermediate"]:
        (job_dir / d).mkdir()
    config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
    return PdfParseStep("02_pdf_parse", job_dir, config)


def _blocks(*lines_of_spans):
    return {"blocks": [{"lines": [{"spans": list(spans)} for spans in lines_of_spans]}]}


class TestExtractTitle:
    def test_prefers_metadata_title(self, tmp_path):
        step = _mk_step(tmp_path)
        assert step._extract_title(_FakeDoc({"title": "Meta Title"})) == "Meta Title"

    def test_skips_arxiv_stamp_pdf_title(self, tmp_path):
        # PDF 内置 title 是 arXiv 戳时不可信 → 跳过,走字号启发拿真标题。
        step = _mk_step(tmp_path)
        doc = _FakeDoc(
            {"title": "arXiv:1810.04805v2  [cs.CL]  24 May 2019"},
            _blocks([{"size": 18.0, "text": "Real Paper Title"}]),
        )
        assert step._extract_title(doc) == "Real Paper Title"

    def test_load_source_meta(self, tmp_path):
        # 读 01_download 写的 input/metadata.json(arxiv API 权威);缺/坏 → {}。
        step = _mk_step(tmp_path)
        (step.job_dir / "input" / "metadata.json").write_text(
            json.dumps({"title": "BERT", "authors": ["Jacob Devlin"]}))
        assert step._load_source_meta()["authors"] == ["Jacob Devlin"]
        (step.job_dir / "input" / "metadata.json").write_text("{bad json")
        assert step._load_source_meta() == {}

    def test_joins_spans_at_max_size(self, tmp_path):
        # 标题跨多个 span(同最大字号)应拼接,而非只取第一个。
        step = _mk_step(tmp_path)
        doc = _FakeDoc(
            {"title": ""},
            _blocks(
                [{"size": 20.0, "text": "Attention Is All"}],
                [{"size": 20.0, "text": "You Need"}],
                [{"size": 10.0, "text": "small body text"}],
            ),
        )
        assert step._extract_title(doc) == "Attention Is All You Need"

    def test_overlong_falls_back_to_first_span(self, tmp_path):
        step = _mk_step(tmp_path)
        big = "x" * 300
        doc = _FakeDoc(
            {"title": ""},
            _blocks([{"size": 18.0, "text": big}], [{"size": 18.0, "text": "y" * 50}]),
        )
        assert step._extract_title(doc) == big  # 拼接 >250 → 退回首个


class TestExtractAbstract:
    def test_terminates_at_blank_line(self, tmp_path):
        step = _mk_step(tmp_path)
        doc = _FakeDoc({"title": "t"}, page_text="Abstract\nThe body here.\n\nIntroduction\nmore")
        assert step._extract_abstract(doc) == "The body here."

    def test_falls_back_to_end_without_terminator(self, tmp_path):
        # 首页无空行、无 introduction:\Z 兜底应仍抽到摘要而非返回空。
        step = _mk_step(tmp_path)
        doc = _FakeDoc({"title": "t"}, page_text="Abstract\nlone abstract with no blank line")
        assert "lone abstract with no blank line" in step._extract_abstract(doc)

    def test_caps_overlong_abstract(self, tmp_path):
        step = _mk_step(tmp_path)
        doc = _FakeDoc({"title": "t"}, page_text="Abstract\n" + "w" * 5000)
        assert len(step._extract_abstract(doc)) <= 3000

    def test_scans_later_pages_when_cover_page(self, tmp_path):
        # 会议 PDF(USENIX/OSDI)首页是封面/版权页,没有 Abstract;真正摘要在第 2 页,应扫到。
        step = _mk_step(tmp_path)

        class _MultiDoc:
            metadata = {"title": "t"}
            _pages = [
                _FakePage({"blocks": []}, "This paper is included in the Proceedings of OSDI."),
                _FakePage({"blocks": []}, "Abstract\nThe real abstract on page two.\n\nIntroduction"),
            ]

            def __len__(self):
                return len(self._pages)

            def __getitem__(self, i):
                return self._pages[i]

        assert step._extract_abstract(_MultiDoc()) == "The real abstract on page two."


def _doc1(text):
    class _Doc:
        metadata = {}
        _pages = [_FakePage({"blocks": []}, text)]
        def __len__(self): return 1
        def __getitem__(self, i): return self._pages[i]
    return _Doc()


class TestExtractVenue:
    def test_osdi_cover_to_acronym(self, tmp_path):
        # USENIX 封面 venue 跨行(真实 PDF 如此)→ 归一化后命中 configs/venues.yaml 映射 "OSDI 2023"。
        step = _mk_step(tmp_path)
        doc = _doc1("This paper is included in the Proceedings of the\n17th USENIX Symposium on Operating Systems\n"
                    "Design and Implementation.\nJuly 10-12, 2023 - Boston, MA")
        assert step._extract_venue(doc) == "OSDI 2023"

    def test_arxiv(self, tmp_path):
        step = _mk_step(tmp_path)
        assert step._extract_venue(_doc1("arXiv:2310.12345v1 [cs.LG] 5 Oct 2023")) == "arXiv"

    def test_unknown_venue_keeps_full_name(self, tmp_path):
        step = _mk_step(tmp_path)
        assert step._extract_venue(_doc1("Proceedings of the Foo Bar Workshop. 2021")) == "Foo Bar Workshop 2021"

    def test_no_venue_returns_empty(self, tmp_path):
        step = _mk_step(tmp_path)
        assert step._extract_venue(_doc1("Just some body text with no venue line.")) == ""


class TestExtractFigureRefs:
    def test_dedup_keeps_longest_caption(self, tmp_path):
        # 同图号去重:正文 inline 引用("Figure 1. ...")caption 短,真图注最长 → 留最长。
        step = _mk_step(tmp_path)
        doc = _FakeDoc({"title": "t"}, page_text=(
            "Figure 1: Architecture of the model.\n"
            "As shown in Figure 1. it works\n"
            "Figure 2: Results table.\n"))
        figs = step._extract_figure_refs(doc)
        assert [f["id"] for f in figs] == ["fig1", "fig2"]
        assert figs[0]["caption"] == "Architecture of the model."
