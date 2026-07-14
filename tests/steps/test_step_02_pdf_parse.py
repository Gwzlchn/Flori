"""steps/paper/step_02_pdf_parse.py 的测试,pymupdf 全 mock。"""

import json
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from steps.paper.step_02_pdf_parse import PdfParseStep
from steps.article.provenance import source_reference_block
from shared.provenance import MAX_PROVENANCE_BYTES
from tests.steps.conftest import make_step_config


class TestPdfParseStep:
    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        config = make_step_config(tmp_path, step_name="02_pdf_parse")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        assert step.validate_inputs() == ["input/source.html|input/source.pdf"]

    def test_execute_pdf_only_poppler(self, tmp_path, monkeypatch):
        # 无 HTML 源:pdfinfo 取页数 → 页区间伪章节 + source_kind=pdf-only + 恒写翻译标记(直喂交 AI 步)。
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4 fake")
        (job_dir / "input" / "metadata.json").write_text(
            json.dumps({
                "title": "MapReduce Processing at Scale",
                "authors": ["Author A", "Author B"],
            }),
        )

        config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        calls = []

        def fake_subprocess(cmd, timeout=0):
            if cmd[0] == "pdfinfo":
                return SimpleNamespace(stdout="Title: x\nPages:          9\n")
            Path(cmd[-1]).write_text(
                "\f".join(
                    f"Page {page} contains bounded source evidence."
                    for page in range(1, 10)
                ) + "\f",
                encoding="utf-8",
            )
            calls.append(cmd)
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(step.commands, "run", fake_subprocess)
        result = step.execute()

        parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
        assert parsed["source_kind"] == "pdf-only"
        assert parsed["pages"] == 9
        assert parsed["title"] == "MapReduce Processing at Scale"
        assert len(parsed["authors"]) == 2
        assert parsed["sections"][0]["title"] == "Pages 1-4"   # 每 4 页一伪章节
        assert parsed["sections"][-1]["title"] == "Pages 9-9"
        assert not (job_dir / "output" / "original.md").exists()  # 不产原文 MD(原文=内嵌 PDF)
        assert (job_dir / "intermediate" / "needs_translation.json").exists()
        source_manifest = json.loads(
            (job_dir / "intermediate/source_segments.json").read_text()
        )
        assert source_manifest["source_artifacts"][0]["page_count"] == 9
        assert [item["locator"]["page"] for item in source_manifest["segments"]] == list(
            range(1, 10)
        )
        assert all(item["locator"]["bbox"] is None for item in source_manifest["segments"])
        assert source_manifest["segments"][0]["support_text"] == (
            "Page 1 contains bounded source evidence."
        )
        assert "PDF 第" not in source_reference_block(source_manifest)
        assert "Page 1 contains bounded source evidence." in source_reference_block(
            source_manifest
        )
        assert len(calls) == 1

    def test_pdfinfo_unreadable_fails_loud(self, tmp_path, monkeypatch):
        # 页数是直喂分块地基,pdfinfo 读不出 → InputInvalidError(不静默 0 页)。
        from shared.errors import InputInvalidError
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"broken")
        config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        from types import SimpleNamespace
        monkeypatch.setattr(step.commands, "run",
                            lambda cmd, timeout=None: SimpleNamespace(stdout="garbage"))
        with pytest.raises(InputInvalidError):
            step.execute()


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



def _make_job(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["input", "intermediate"]:
        (job_dir / d).mkdir()
    (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4 fake")
    return job_dir


def test_pdf_only_suspicious_title_extracted_from_first_page(tmp_path, monkeypatch):
    # 内嵌 metadata 垃圾标题("10things")→ pdftotext 首页启发式提真标题写 parsed.json。
    job_dir = _make_job(tmp_path)
    (job_dir / "input" / "metadata.json").write_text(json.dumps({"title": "10things"}))
    config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
    step = PdfParseStep("02_pdf_parse", job_dir, config)

    def fake_subprocess(cmd, timeout=0):
        if cmd[0] == "pdfinfo":
            return SimpleNamespace(stdout="Pages:          8\n")
        assert cmd[0] == "pdftotext"
        first = (
            "PLOS Computational Biology 2013\n"
            "Ten Simple Rules for Reproducible Computational Research\nAuthors\n"
        )
        Path(cmd[-1]).write_text(
            "\f".join([first, *(f"Page {page} source text" for page in range(2, 9))])
            + "\f",
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(step.commands, "run", fake_subprocess)
    step.execute()
    parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
    assert parsed["title"] == "Ten Simple Rules for Reproducible Computational Research"


def test_pdf_only_good_title_untouched(tmp_path, monkeypatch):
    job_dir = _make_job(tmp_path)
    good = "In Search of an Understandable Consensus Algorithm"
    (job_dir / "input" / "metadata.json").write_text(json.dumps({"title": good}))
    config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
    step = PdfParseStep("02_pdf_parse", job_dir, config)

    def fake_subprocess(cmd, timeout=0):
        if cmd[0] == "pdfinfo":
            return SimpleNamespace(stdout="Pages:          8\n")
        Path(cmd[-1]).write_text(
            "\f".join(f"Page {page} source text" for page in range(1, 9)) + "\f",
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(step.commands, "run", fake_subprocess)
    step.execute()
    parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
    assert parsed["title"] == good


@pytest.mark.parametrize(
    "mode", ["failure", "empty", "oversized", "misaligned", "global-oversized"],
)
def test_pdf_page_support_failure_modes_publish_null(tmp_path, monkeypatch, mode):
    job_dir = _make_job(tmp_path)
    (job_dir / "input/metadata.json").write_text(
        json.dumps({"title": "A Trustworthy Paper Title"}), encoding="utf-8",
    )
    config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
    step = PdfParseStep("02_pdf_parse", job_dir, config)

    def fake_subprocess(cmd, timeout=0):
        if cmd[0] == "pdfinfo":
            return SimpleNamespace(stdout="Pages:          1\n")
        if mode == "failure":
            raise RuntimeError("pdftotext failed")
        if mode == "empty":
            text = "\f"
        elif mode == "misaligned":
            text = "first\fsecond\f"
        elif mode == "global-oversized":
            text = "x" * (MAX_PROVENANCE_BYTES + 1)
        else:
            text = "x" * 4097 + "\f"
        Path(cmd[-1]).write_text(text, encoding="utf-8")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(step.commands, "run", fake_subprocess)
    step.execute()

    manifest = json.loads(
        (job_dir / "intermediate/source_segments.json").read_text(encoding="utf-8")
    )
    assert manifest["segments"][0]["support_text"] is None
    assert source_reference_block(manifest) == ""
