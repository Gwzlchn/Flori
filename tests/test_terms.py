"""shared/terms.py 术语命中、注入、回收和提炼纯函数测试。"""

from __future__ import annotations

import pytest

from shared.terms import (
    TERM_LIMIT,
    extract_pairs,
    hit_terms,
    render_term_block,
    zh_name_from_glossary_row,
)


class TestHitTerms:
    MAP = {"Kelly criterion": "凯利准则", "martingale": "鞅", "alpha": "阿尔法"}

    def test_word_boundary_and_case(self):
        text = "The Kelly Criterion beats naive sizing; martingales diverge."
        hits = dict(hit_terms(text, self.MAP))
        assert hits["Kelly criterion"] == "凯利准则"
        assert hits["martingale"] == "鞅"          # 复数 martingales 归一命中

    def test_no_substring_false_positive(self):
        # "alphabet" 不得命中 "alpha"(词边界)。
        assert hit_terms("alphabet soup", self.MAP) == []

    def test_possessive(self):
        assert hit_terms("Kelly criterion's edge", self.MAP)[0][0] == "Kelly criterion"

    def test_frequency_order_and_limit(self):
        m = {f"term{i:02d}": f"译{i}" for i in range(50)}
        text = " ".join(f"term{i:02d}" for i in range(50)) + " term07 term07"
        hits = hit_terms(text, m)
        assert len(hits) == TERM_LIMIT
        assert hits[0][0] == "term07"              # 频次最高排首位

    def test_empty(self):
        assert hit_terms("", self.MAP) == []
        assert hit_terms("text", {}) == []


class TestRenderTermBlock:
    def test_block_format(self):
        block = render_term_block([("martingale", "鞅")])
        assert "术语对照表" in block and "- martingale → 鞅" in block
        assert "表外专有名词" in block

    def test_empty_hits_no_trace(self):
        assert render_term_block([]) == ""


class TestExtractPairs:
    def test_fullwidth_pair(self):
        # 译名需在对照之外复现(复用的术语才有一致性诉求)。
        assert extract_pairs("其一是:鞅（martingale）。该鞅性质…") == {"martingale": "鞅"}

    def test_single_occurrence_not_collected(self):
        # 只出现一次的术语无一致性问题,漏收无害(换来对左界误捕的免疫)。
        assert extract_pairs("其一是:鞅（martingale）的概念") == {}

    def test_ambiguous_left_boundary_rejected(self):
        # 「引入鞅(…)」译名左界不可判(鞅 vs 入鞅)→ 保守放弃(错收会教坏后续 chunk)。
        assert extract_pairs("引入鞅（martingale）的概念") == {}

    def test_halfwidth_not_matched(self):
        # 半角括号(代码/公式常见)不回收——保守。
        assert extract_pairs("鞅(martingale)") == {}

    def test_first_occurrence_wins(self):
        md = "凯利准则（Kelly criterion）……凯利公式（Kelly criterion）……凯利准则与凯利公式之争"
        assert extract_pairs(md) == {"Kelly criterion": "凯利准则"}

    def test_code_fence_skipped(self):
        md = "```\n注释:假（fake term）\n```\n正文说:鞅（martingale）,鞅即公平赌局"
        assert extract_pairs(md) == {"martingale": "鞅"}

    def test_chinese_in_paren_rejected(self):
        assert extract_pairs("某某（中文Eng）") == {}


class TestZhNameFromGlossaryRow:
    def test_zh_name_column_priority(self):
        assert zh_name_from_glossary_row("Kelly criterion", "凯利准则", "任意解释") == (
            "Kelly criterion", "凯利准则")

    def test_bilingual_term(self):
        assert zh_name_from_glossary_row("凯利准则（Kelly criterion）", None, "") == (
            "Kelly criterion", "凯利准则")

    def test_definition_leading_short_name(self):
        assert zh_name_from_glossary_row(
            "recency bias", None, "近因偏差,过分强调近期事件。") == ("recency bias", "近因偏差")

    @pytest.mark.parametrize("term,definition", [
        ("坐庄", "庄家控盘流程"),                     # 纯中文词条:无英文名可导出
        ("recency bias", "一种过分强调近期事件的认知偏差"),  # definition 无短名前缀
        ("", ""),
    ])
    def test_unresolvable_returns_none(self, term, definition):
        assert zh_name_from_glossary_row(term, None, definition) is None


class TestConflictGrouping:
    """术语一致性审查脚本的核心归组逻辑。"""

    def test_conflicts_and_clean_terms(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from term_consistency_check import collect_conflicts
        by_job = {
            "j1": {"martingale": "鞅", "alpha": "阿尔法"},
            "j2": {"martingale": "马丁格尔", "alpha": "阿尔法"},
            "j3": {"martingale": "鞅"},
        }
        c = collect_conflicts(by_job)
        assert set(c) == {"martingale"}                      # alpha 一致,不算冲突
        assert c["martingale"]["鞅"] == ["j1", "j3"]
        assert c["martingale"]["马丁格尔"] == ["j2"]
