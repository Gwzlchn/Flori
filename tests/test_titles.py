"""shared/titles.py:垃圾标题判定 + PDF 首页标题启发式(02 与 scheduler 共用同一套)。"""

import pytest

from shared.titles import is_suspicious_title, title_from_first_page


class TestIsSuspiciousTitle:
    @pytest.mark.parametrize("t", [
        "", "  ",
        "10things",                       # 无空格短 token(线上实证)
        "paper.dvi", "main.tex", "report.pdf", "draft.docx",
        "NBER WORKING PAPER SERIES",      # 系列名页眉
        "Working Paper 20592",
        "Microsoft Word - final.doc",
        "Untitled",
    ])
    def test_junk(self, t):
        assert is_suspicious_title(t)

    @pytest.mark.parametrize("t", [
        "In Search of an Understandable Consensus Algorithm",
        "In-Datacenter Performance Analysis of a Tensor Processing Unit",
        "Attention Is All You Need",
        "MapReduce: Simplified Data Processing on Large Clusters",
        "深度双向 Transformer 预训练",       # 中文含空格与否不重要,长且非单 token
    ])
    def test_real(self, t):
        assert not is_suspicious_title(t)


class TestTitleFromFirstPage:
    def test_picks_first_meaningful_line(self):
        text = "\n".join([
            "", "USENIX ATC 2014",                       # 页眉短行(≤3 词含数字)跳过
            "In Search of an Understandable Consensus Algorithm (Extended Version)",
            "Diego Ongaro and John Ousterhout",
        ])
        assert title_from_first_page(text) == (
            "In Search of an Understandable Consensus Algorithm (Extended Version)")

    def test_skips_series_banner_and_arxiv_line(self):
        text = "\n".join([
            "NBER WORKING PAPER SERIES",
            "arXiv:1704.04760v1 [cs.AR] 16 Apr 2017",
            "Market Liquidity and Funding Liquidity",
        ])
        assert title_from_first_page(text) == "Market Liquidity and Funding Liquidity"

    def test_joins_hyphen_broken_line(self):
        text = "Ten Simple Rules for Reproducible Computational Re-\nsearch\nAuthors here"
        assert title_from_first_page(text) == (
            "Ten Simple Rules for Reproducible Computational Research")

    def test_gives_up_on_abstract_or_paragraph(self):
        assert title_from_first_page("Abstract\nThis paper ...") is None
        assert title_from_first_page("x" * 200) is None
        assert title_from_first_page("") is None
