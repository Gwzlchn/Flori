"""tests for shared/notes_versions.py（智能笔记版本文件名解析 + 配对评审路径）。

此前该模块零直接单测;配对错位/排序倒置/解析异常都不会被发现(审计 #8)。"""

from __future__ import annotations

from shared.notes_versions import (
    latest_smart,
    parse_smart_version,
    review_path_for_note,
)

V1 = "output/versions/notes_smart_claude-cli_claude-opus-4-8_20260101-000000.md"
V2 = "output/versions/notes_smart_anthropic_claude-sonnet-4-6_20260102-120000.md"
V3 = "output/versions/notes_smart_deepseek_deepseek-chat_20260101-235959.md"


class TestParseSmartVersion:
    def test_valid(self):
        v = parse_smart_version(V1)
        assert v == {
            "provider": "claude-cli",
            "model": "claude-opus-4-8",
            "version": "20260101-000000",
            "file": V1,
        }

    def test_provider_model_with_dots_and_hyphens(self):
        # provider/model 段可含 . 和 -,但不含 _(写入时已归一)
        assert parse_smart_version(V2)["model"] == "claude-sonnet-4-6"

    def test_non_matching_returns_none(self):
        assert parse_smart_version("output/notes_mechanical.md") is None
        assert parse_smart_version("output/versions/review_x_y_20260101-000000.json") is None
        assert parse_smart_version("") is None


class TestLatestSmart:
    def test_picks_max_version(self):
        # V2(0102) > V3(0101-235959) > V1(0101-000000)
        assert latest_smart([V1, V2, V3]) == V2

    def test_ignores_non_versions(self):
        assert latest_smart(["output/notes_mechanical.md", V1]) == V1

    def test_empty_returns_none(self):
        assert latest_smart([]) is None
        assert latest_smart(["output/review.json"]) is None


class TestReviewPathForNote:
    def test_pairs_review_to_note(self):
        assert review_path_for_note(V1) == (
            "output/versions/review_claude-cli_claude-opus-4-8_20260101-000000.json"
        )

    def test_roundtrip_consistency(self):
        # 笔记→评审路径,二者 provider/model/version 段一致(1:1 配对)
        note = parse_smart_version(V2)
        rpath = review_path_for_note(V2)
        assert note["version"] in rpath and note["provider"] in rpath and note["model"] in rpath

    def test_non_matching_returns_none(self):
        assert review_path_for_note("output/notes_mechanical.md") is None
