"""学习闭环 / SRS 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.services.study import schedule_next_review


def test_schedule_good_first_review_sets_one_day():
    card = {"review": {"interval_days": 0, "ease": 2.5, "repetitions": 0, "lapses": 0}}
    now = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    res = schedule_next_review(card, "good", reviewed_at=now)
    assert res["interval_days"] == 1
    assert res["ease"] == 2.5
    assert res["repetitions"] == 1
    assert res["lapses"] == 0
    assert res["next_due_at"].startswith("2026-07-10T00:00:00")


def test_schedule_again_resets_repetition_and_adds_lapse():
    card = {"review": {"interval_days": 6, "ease": 2.5, "repetitions": 3, "lapses": 1}}
    now = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    res = schedule_next_review(card, "again", reviewed_at=now)
    assert res["interval_days"] == round(10 / 1440, 4)
    assert res["ease"] == 2.3
    assert res["repetitions"] == 0
    assert res["lapses"] == 2
    assert res["next_due_at"].startswith("2026-07-09T00:10:00")


class TestStudyDb:
    def test_create_active_card_is_due_immediately(self, db):
        card = db.create_study_card(
            card_id="sc_1",
            domain="ml",
            front="反向传播解决什么问题?",
            back="高效计算梯度。",
            evidence=[{"chunk_id": "j:smart:0", "snippet": "梯度"}],
        )
        assert card["status"] == "active"
        assert card["review"]["due_at"]
        total, due = db.list_due_study_cards(domain="ml", limit=10)
        assert total == 1
        assert due[0]["card_id"] == "sc_1"
        assert due[0]["evidence"][0]["chunk_id"] == "j:smart:0"

    def test_review_updates_due_and_writes_log(self, db):
        db.create_study_card(
            card_id="sc_2", domain="ml", front="Q", back="A",
        )
        updated = db.record_study_review(
            card_id="sc_2",
            grade="good",
            next_due_at="2026-07-10T00:00:00+00:00",
            interval_days=1,
            ease=2.5,
            repetitions=1,
            lapses=0,
            response_ms=1200,
            reviewed_at="2026-07-09T00:00:00+00:00",
        )
        assert updated["review"]["interval_days"] == 1
        assert updated["review"]["repetitions"] == 1
        logs = db._conn.execute(
            "SELECT grade, response_ms, next_due_at FROM study_review_logs WHERE card_id=?",
            ("sc_2",),
        ).fetchall()
        assert len(logs) == 1
        assert logs[0]["grade"] == "good"
        assert logs[0]["response_ms"] == 1200

    def test_suspended_card_not_due(self, db):
        db.create_study_card(card_id="sc_3", domain="ml", front="Q", back="A")
        db.set_study_card_status("sc_3", "suspended")
        total, due = db.list_due_study_cards(domain="ml", limit=10)
        assert total == 0
        assert due == []


class TestStudyApi:
    @pytest.mark.asyncio
    async def test_create_due_and_review(self, client):
        resp = await client.post(
            "/api/study/cards",
            json={
                "domain": "ml",
                "front": "Transformer 的注意力机制解决什么问题?",
                "back": "让序列位置之间直接建模依赖。",
                "explanation": "最小卡片",
                "evidence": [{"chunk_id": "j_attn:smart:0", "snippet": "注意力"}],
            },
        )
        assert resp.status_code == 201
        card = resp.json()
        assert card["card_id"].startswith("sc_")
        assert card["review"]["repetitions"] == 0

        due = (await client.get("/api/study/due?domain=ml")).json()
        assert due["total"] == 1
        assert due["items"][0]["card_id"] == card["card_id"]

        reviewed = await client.post(
            "/api/study/reviews",
            json={"card_id": card["card_id"], "grade": "good", "response_ms": 500},
        )
        assert reviewed.status_code == 200
        data = reviewed.json()
        assert data["review"]["last_grade"] == "good"
        assert data["review"]["repetitions"] == 1

    @pytest.mark.asyncio
    async def test_status_and_delete(self, client):
        card = (await client.post(
            "/api/study/cards",
            json={"domain": "ml", "front": "Q", "back": "A"},
        )).json()
        resp = await client.post(
            f"/api/study/cards/{card['card_id']}/status",
            json={"status": "suspended"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "suspended"
        due = (await client.get("/api/study/due?domain=ml")).json()
        assert due["total"] == 0
        assert (await client.delete(f"/api/study/cards/{card['card_id']}")).status_code == 204
        assert (await client.delete(f"/api/study/cards/{card['card_id']}")).status_code == 404
