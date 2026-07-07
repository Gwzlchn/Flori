"""shared/concepts.py 实体归一和 resolve-then-merge 测试。"""

from __future__ import annotations

from shared.concepts import (
    candidate_keys, norm_key, primary_fields, resolve, split_annotation,
)


class TestNormKey:
    def test_case_and_whitespace(self):
        assert norm_key("Multi-Head  Attention") == norm_key("multi-head attention")

    def test_fullwidth_paren_annotation(self):
        # 全半角括号和空格差异不影响注音变体归一。
        k = norm_key("量化")
        assert norm_key("量化 (Quantization)") == k
        assert norm_key("量化（Quantization）") == k
        assert norm_key("量化(quantization)") == k

    def test_mid_paren_not_stripped(self):
        # 括号在中间不是注音形态,不剥。
        assert norm_key("P(X) given Y") == "p(x) given y"

    def test_chinese_conservative(self):
        assert norm_key("多头注意力") == "多头注意力"

    def test_empty(self):
        assert norm_key("") == ""


class TestSplitAnnotation:
    def test_split(self):
        assert split_annotation("量化 (Quantization)") == ("量化", "Quantization")
        assert split_annotation("量化（Quantization）") == ("量化", "Quantization")

    def test_no_annotation(self):
        assert split_annotation("量化") == ("量化", None)

    def test_paren_only_not_split(self):
        assert split_annotation("(x)") == ("(x)", None)


class TestCandidateKeys:
    def test_order_main_note_zh(self):
        ks = candidate_keys("量化 (Quantization)", "量化技术")
        assert ks == ["量化", "quantization", "量化技术"]

    def test_dedup(self):
        assert candidate_keys("量化 (量化)") == ["量化"]


class TestResolve:
    ROWS = [
        {"term": "Multi-Head Attention", "zh_name": "多头注意力", "aliases": []},
        {"term": "量化", "zh_name": "", "aliases": ["量化 (Quantization)"]},
    ]

    def test_exact_norm_hit(self):
        assert resolve(self.ROWS, "multi-head attention") == "Multi-Head Attention"

    def test_zh_name_hit(self):
        # 中文说法经 zh_name 归到英文实体。
        assert resolve(self.ROWS, "多头注意力") == "Multi-Head Attention"

    def test_alias_hit(self):
        assert resolve(self.ROWS, "量化（quantization）") == "量化"

    def test_incoming_zh_name_hit(self):
        assert resolve(self.ROWS, "MHA", "多头注意力") == "Multi-Head Attention"

    def test_miss(self):
        assert resolve(self.ROWS, "Transformer") is None


class TestPrimaryFields:
    def test_zh_with_en_note(self):
        term, zh, aliases = primary_fields("量化 (Quantization)")
        assert term == "Quantization" and zh == "量化"
        assert aliases == ["量化 (Quantization)"]

    def test_en_with_zh_note(self):
        term, zh, aliases = primary_fields("Kelly criterion（凯利准则）")
        assert term == "Kelly criterion" and zh == "凯利准则"

    def test_pure_chinese(self):
        assert primary_fields("坐庄") == ("坐庄", "", [])

    def test_pure_english_keeps_case(self):
        assert primary_fields("Transformer", "变换器") == ("Transformer", "变换器", [])


class TestSuggestionResolveMerge:
    def test_variant_merges_into_existing(self, db):
        db.add_glossary_suggestion("ml", "量化 (Quantization)", "j1", "article",
                                   definition="压缩权重精度", zh_name="量化")
        db.add_glossary_suggestion("ml", "量化(quantization)", "j2", "paper")
        terms = db.list_glossary("ml")
        assert len(terms) == 1
        t = terms[0]
        assert len(t["occurrences"]) == 2
        assert {o["job_id"] for o in t["occurrences"]} == {"j1", "j2"}
        assert "量化(quantization)" in t["aliases"]

    def test_zh_variant_merges_via_zh_name(self, db):
        db.add_glossary_suggestion("ml", "Multi-Head Attention", "j1", "paper",
                                   zh_name="多头注意力")
        db.add_glossary_suggestion("ml", "多头注意力", "j2", "video")
        terms = db.list_glossary("ml")
        assert len(terms) == 1
        assert terms[0]["term"] == "Multi-Head Attention"
        assert len(terms[0]["occurrences"]) == 2

    def test_new_entity_primary_naming(self, db):
        # 「中文 (English)」组合形态拆开:英文做主名,中文进 zh_name,原始串入 aliases。
        db.add_glossary_suggestion("ml", "鞅 (Martingale)", "j1", "article")
        t = db.list_glossary("ml")[0]
        assert t["term"] == "Martingale" and t["zh_name"] == "鞅"
        assert "鞅 (Martingale)" in t["aliases"]

    def test_same_job_occurrence_deduped(self, db):
        db.add_glossary_suggestion("ml", "A-term", "j1")
        db.add_glossary_suggestion("ml", "A-term", "j1")
        assert len(db.list_glossary("ml")[0]["occurrences"]) == 1

    def test_definition_fill_only_empty(self, db):
        db.add_glossary_suggestion("ml", "B-term", "j1", definition="第一版")
        db.add_glossary_suggestion("ml", "B-term", "j2", definition="第二版")
        assert db.list_glossary("ml")[0]["definition"] == "第一版"


class TestMergeGlossaryTerms:
    def test_merge_semantics(self, db):
        db.add_glossary_suggestion("ml", "Attention", "j1", definition="短")
        db.add_glossary_suggestion("ml", "AttentionMechanism", "j2",
                                   definition="更长的注意力机制定义", zh_name="注意力机制")
        db.accept_glossary_term("ml", "AttentionMechanism")
        merged = db.merge_glossary_terms("ml", "AttentionMechanism", "Attention")
        assert merged["term"] == "Attention"
        assert merged["status"] == "accepted"                       # 取更高档
        assert merged["definition"] == "更长的注意力机制定义"        # 取更长者
        assert merged["zh_name"] == "注意力机制"                    # 补空
        assert "AttentionMechanism" in merged["aliases"]            # src 名留痕
        assert {o["job_id"] for o in merged["occurrences"]} == {"j1", "j2"}
        assert db.get_glossary_term("ml", "AttentionMechanism") is None   # src 已删

    def test_merge_occurrence_dedup(self, db):
        db.add_glossary_suggestion("ml", "X1", "j1")
        db.add_glossary_suggestion("ml", "X2-completely-different", "j1")
        merged = db.merge_glossary_terms("ml", "X2-completely-different", "X1")
        assert len(merged["occurrences"]) == 1

    def test_merge_missing_raises(self, db):
        db.add_glossary_suggestion("ml", "X1", "j1")
        import pytest
        with pytest.raises(ValueError):
            db.merge_glossary_terms("ml", "X1", "nope")
        with pytest.raises(ValueError):
            db.merge_glossary_terms("ml", "X1", "X1")


class TestListGlossaryQ:
    def test_q_matches_term_zh_aliases(self, db):
        db.add_glossary_suggestion("ml", "Kelly criterion", "j1", zh_name="凯利准则")
        db.add_glossary_suggestion("ml", "Sharpe ratio", "j2", zh_name="夏普比率")
        assert [t["term"] for t in db.list_glossary("ml", q="凯利")] == ["Kelly criterion"]
        assert [t["term"] for t in db.list_glossary("ml", q="sharpe")] == ["Sharpe ratio"]
        assert db.list_glossary("ml", q="没有的") == []


class TestGetJobTitles:
    def test_titles(self, db):
        from shared.models import Job
        db.create_job(Job(id="jt1", content_type="article", pipeline="article_v2",
                          title="标题一"))
        assert db.get_job_titles(["jt1", "missing"]) == {"jt1": "标题一"}


class TestNormRelated:
    def test_strings_and_dicts(self):
        from shared.concepts import norm_related
        assert norm_related(["A", {"term": "B", "rel": "is_a"}, {"term": "A"}, 42]) == [
            {"term": "A", "rel": "related"},
            {"term": "B", "rel": "is_a"},
        ]

    def test_unknown_rel_degraded(self):
        from shared.concepts import norm_related
        assert norm_related([{"term": "X", "rel": "weird"}]) == [
            {"term": "X", "rel": "related"}
        ]


class TestAddGlossaryRelations:
    def test_add_and_dedup(self, db):
        db.add_glossary_suggestion("ml", "Transformer", "j1")
        n = db.add_glossary_relations("ml", "Transformer",
                                      [{"term": "Attention", "rel": "part_of"}])
        assert n == 1
        # 同目标再写不覆盖已有 rel(先到先得);自指跳过。
        n2 = db.add_glossary_relations("ml", "Transformer",
                                       [{"term": "Attention", "rel": "related"},
                                        {"term": "Transformer", "rel": "related"}])
        assert n2 == 0
        t = db.get_glossary_term("ml", "Transformer")
        assert t["related"] == [{"term": "Attention", "rel": "part_of"}]

    def test_missing_row_returns_zero(self, db):
        assert db.add_glossary_relations("ml", "nope", [{"term": "A", "rel": "related"}]) == 0

    def test_legacy_string_related_normalized_on_read(self, db):
        db.upsert_glossary_term("ml", "老词", related=["旧关联"])
        t = db.get_glossary_term("ml", "老词")
        assert t["related"] == [{"term": "旧关联", "rel": "related"}]
