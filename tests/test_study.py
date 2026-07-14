"""学习闭环的 SRS、事务、统计和 API 契约。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from api.services.study import schedule_next_review
from shared.study import (
    AGAIN_DELAY_SECONDS,
    MAX_INTERVAL_DAYS,
    MAX_SQLITE_INTEGER,
    StudyConflictError,
    datetime_to_epoch_us,
)


UTC = timezone.utc
REVIEWED_AT = datetime(2026, 7, 9, tzinfo=UTC)


@pytest.mark.parametrize("repetitions", [0, 1, 2])
@pytest.mark.parametrize("grade", ["again", "hard", "good", "easy"])
def test_schedule_covers_four_grades_at_every_repetition_boundary(
    grade: str, repetitions: int
) -> None:
    card = {
        "review": {
            "interval_days": 6,
            "ease": 2.5,
            "repetitions": repetitions,
            "lapses": 1,
        }
    }
    result = schedule_next_review(card, grade, reviewed_at=REVIEWED_AT)

    assert result["reviewed_at"] == REVIEWED_AT.isoformat()
    assert result["next_due_at_epoch_us"] == datetime_to_epoch_us(result["next_due_at"])
    if grade == "again":
        assert result["repetitions"] == 0
        assert result["lapses"] == 2
        assert datetime.fromisoformat(result["next_due_at"]) - REVIEWED_AT == timedelta(
            seconds=AGAIN_DELAY_SECONDS
        )
    else:
        assert result["repetitions"] == repetitions + 1
        assert result["lapses"] == 1
    if grade == "good":
        assert result["interval_days"] == {0: 1, 1: 3, 2: 15}[repetitions]
    if grade == "easy":
        assert result["interval_days"] == {0: 3, 1: 6, 2: 19.5}[repetitions]


def test_schedule_clamps_ease_interval_and_datetime_overflow() -> None:
    hard = schedule_next_review(
        {"review": {"interval_days": MAX_INTERVAL_DAYS, "ease": 1.3, "repetitions": 2}},
        "hard",
        reviewed_at=REVIEWED_AT,
    )
    easy = schedule_next_review(
        {"review": {"interval_days": MAX_INTERVAL_DAYS, "ease": 3.0, "repetitions": 2}},
        "easy",
        reviewed_at=REVIEWED_AT,
    )
    near_max = datetime.max.replace(tzinfo=UTC) - timedelta(microseconds=1)
    overflow = schedule_next_review(
        {"review": {"interval_days": MAX_INTERVAL_DAYS, "ease": 3.0, "repetitions": 2}},
        "easy",
        reviewed_at=near_max,
    )

    assert hard["ease"] == 1.3
    assert hard["interval_days"] == MAX_INTERVAL_DAYS
    assert easy["ease"] == 3.0
    assert easy["interval_days"] == MAX_INTERVAL_DAYS
    assert overflow["next_due_at"] == datetime.max.replace(tzinfo=UTC).isoformat()
    assert 0 < overflow["interval_days"] < 1 / 86_400


def test_schedule_rejects_naive_review_time() -> None:
    with pytest.raises(ValueError, match="带时区"):
        schedule_next_review({}, "good", reviewed_at=datetime(2026, 7, 9))


def _create(db, card_id: str = "sc_1", *, status: str = "active", due_at=None):
    return db.create_study_card(
        card_id=card_id,
        domain="ml",
        front="Q",
        back="A",
        status=status,
        due_at=due_at,
    )


def _review(db, card_id: str, request_id: str, *, grade="good", revision=1, fault=None):
    return db.record_study_review(
        request_id=request_id,
        card_id=card_id,
        grade=grade,
        expected_revision=revision,
        response_ms=1200,
        reviewed_at=REVIEWED_AT,
        fault_injector=fault,
    )


class TestStudyDb:
    def test_create_active_card_is_due_at_exact_boundary(self, db):
        instant = datetime(2026, 7, 9, 1, 2, 3, 4, tzinfo=UTC)
        card = db.create_study_card(
            card_id="sc_due",
            domain="ml",
            front="反向传播解决什么问题?",
            back="高效计算梯度。",
            evidence=[{"chunk_id": "j:smart:0", "snippet": "梯度"}],
            due_at=instant,
        )
        assert card["revision"] == 1
        assert card["review"]["due_at"] == instant.isoformat()
        assert db.list_due_study_cards(domain="ml", now=instant, limit=10)[0] == 1
        assert db.list_due_study_cards(
            domain="ml", now=instant - timedelta(microseconds=1), limit=10
        )[0] == 0

    def test_offset_equivalent_instants_use_epoch_not_iso_lexical_order(self, db):
        zulu = "2026-07-09T00:00:00Z"
        plus_eight = "2026-07-09T08:00:00+08:00"
        minus_five = "2026-07-08T19:00:00-05:00"
        assert {datetime_to_epoch_us(value) for value in (zulu, plus_eight, minus_five)} == {
            datetime_to_epoch_us(zulu)
        }
        card = _create(db, "sc_offset", due_at=plus_eight)
        assert card["review"]["due_at"] == "2026-07-09T00:00:00+00:00"
        assert db.list_due_study_cards(now=minus_five)[0] == 1

    def test_review_is_immutable_replay_and_cas(self, db):
        _create(db, "sc_review")
        first = _review(db, "sc_review", "request-one")
        replay = _review(db, "sc_review", "request-one")
        assert replay == first
        assert first["revision"] == 2
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_review_logs WHERE card_id='sc_review'"
        ).fetchone()[0] == 1

        with pytest.raises(StudyConflictError) as reused:
            _review(db, "sc_review", "request-one", grade="easy")
        assert reused.value.code == "study_request_id_conflict"
        with pytest.raises(StudyConflictError) as stale:
            _review(db, "sc_review", "request-two", revision=1)
        assert stale.value.code == "study_revision_stale"

    @pytest.mark.parametrize(
        "fault_stage", ["after_card_cas", "after_review", "after_log", "before_commit"]
    )
    def test_review_fault_injection_rolls_back_every_write(self, db, fault_stage):
        before = _create(db, f"sc_fault_{fault_stage}")

        def fail(stage: str) -> None:
            if stage == fault_stage:
                raise RuntimeError(f"fault:{stage}")

        with pytest.raises(RuntimeError, match=fault_stage):
            _review(db, before["card_id"], f"request-{fault_stage}", fault=fail)

        after = db.get_study_card(before["card_id"])
        assert after == before
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_review_logs WHERE card_id=?", (before["card_id"],)
        ).fetchone()[0] == 0
        assert not db._conn.in_transaction

    def test_reader_cannot_observe_partial_review_transaction(self, db):
        before = _create(db, "sc_reader_atomic")
        transaction_paused = threading.Event()
        release_transaction = threading.Event()
        reader_started = threading.Event()
        reader_finished = threading.Event()
        results: dict[str, object] = {}

        def pause_after_cas(stage: str) -> None:
            if stage == "after_card_cas":
                transaction_paused.set()
                if not release_transaction.wait(timeout=5):
                    raise TimeoutError("reader atomicity test did not release transaction")

        def write_review() -> None:
            results["write"] = _review(
                db,
                before["card_id"],
                "request-reader-atomic",
                fault=pause_after_cas,
            )

        def read_card() -> None:
            reader_started.set()
            results["read"] = db.get_study_card(before["card_id"])
            reader_finished.set()

        writer = threading.Thread(target=write_review)
        reader = threading.Thread(target=read_card)
        writer.start()
        assert transaction_paused.wait(timeout=5)
        reader.start()
        assert reader_started.wait(timeout=5)
        assert not reader_finished.wait(timeout=0.1)
        release_transaction.set()
        writer.join(timeout=5)
        reader.join(timeout=5)

        assert not writer.is_alive()
        assert not reader.is_alive()
        assert results["read"] == results["write"]

    def test_active_only_scoring_and_status_state_machine(self, db):
        active = _create(db, "sc_active")
        suspended = db.set_study_card_status(
            active["card_id"], "suspended", expected_revision=active["revision"]
        )
        assert suspended["revision"] == 2
        with pytest.raises(StudyConflictError) as blocked:
            _review(db, active["card_id"], "request-suspended", revision=2)
        assert blocked.value.code == "study_card_not_active"
        same = db.set_study_card_status(
            active["card_id"], "suspended", expected_revision=1
        )
        assert same["revision"] == 2
        restored = db.set_study_card_status(
            active["card_id"], "active", expected_revision=2
        )
        assert restored["revision"] == 3

        suggested = _create(db, "sc_suggested", status="suggested")
        with pytest.raises(StudyConflictError):
            db.set_study_card_status(
                suggested["card_id"], "active", expected_revision=suggested["revision"]
            )
        rejected = db.set_study_card_status(
            suggested["card_id"], "rejected", expected_revision=suggested["revision"]
        )
        with pytest.raises(StudyConflictError):
            db.set_study_card_status(
                rejected["card_id"], "active", expected_revision=rejected["revision"]
            )

    def test_revision_exhaustion_is_a_conflict_not_sqlite_overflow(self, db):
        status_card = _create(db, "sc_revision_max_status")
        review_card = _create(db, "sc_revision_max_review")
        db._conn.execute(
            "UPDATE study_cards SET revision=? WHERE card_id IN (?,?)",
            (
                MAX_SQLITE_INTEGER,
                status_card["card_id"],
                review_card["card_id"],
            ),
        )
        db._conn.commit()

        with pytest.raises(StudyConflictError) as status_error:
            db.set_study_card_status(
                status_card["card_id"],
                "suspended",
                expected_revision=MAX_SQLITE_INTEGER,
            )
        assert status_error.value.code == "study_revision_exhausted"

        with pytest.raises(StudyConflictError) as review_error:
            _review(
                db,
                review_card["card_id"],
                "request-revision-max",
                revision=MAX_SQLITE_INTEGER,
            )
        assert review_error.value.code == "study_revision_exhausted"
        assert db.get_study_card(status_card["card_id"])["revision"] == MAX_SQLITE_INTEGER
        assert db.get_study_card(review_card["card_id"])["revision"] == MAX_SQLITE_INTEGER
        assert not db._conn.in_transaction

    def test_full_stats_are_not_limited_by_list_page(self, db):
        past = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(251):
            _create(
                db,
                f"sc_stats_{index:03d}",
                status="active" if index < 203 else "suspended",
                due_at=past,
            )
        for index, grade in enumerate(("again", "hard", "good", "easy")):
            _review(db, f"sc_stats_{index:03d}", f"stats-request-{grade}", grade=grade)

        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            stats = db.get_study_stats(domain="ml", now=datetime(2030, 1, 1, tzinfo=UTC))
        finally:
            db._conn.set_trace_callback(None)
        assert stats == {
            "total": 251,
            "statuses": {"suggested": 0, "active": 203, "suspended": 48, "rejected": 0},
            "due": 203,
            "reviewed_cards": 4,
            "reviews_total": 4,
            "grades": {"again": 1, "hard": 1, "good": 1, "easy": 1},
            "retained_reviews": 3,
            "retention_rate": 0.75,
        }
        assert len([sql for sql in statements if sql.lstrip().upper().startswith("WITH")]) == 1


class TestStudyApi:
    @pytest.mark.asyncio
    async def test_create_due_review_replay_and_stats(self, client):
        created = await client.post(
            "/api/study/cards",
            json={
                "domain": "ml",
                "front": "Transformer 的注意力机制解决什么问题?",
                "back": "让序列位置之间直接建模依赖。",
                "evidence": [{"chunk_id": "j_attn:smart:0", "snippet": "注意力"}],
            },
        )
        assert created.status_code == 201
        card = created.json()
        assert card["revision"] == 1
        assert (await client.get("/api/study/due?domain=ml")).json()["total"] == 1

        payload = {
            "request_id": "api-request-one",
            "card_id": card["card_id"],
            "expected_revision": card["revision"],
            "grade": "good",
            "response_ms": 500,
        }
        reviewed = await client.post("/api/study/reviews", json=payload)
        replay = await client.post("/api/study/reviews", json=payload)
        assert reviewed.status_code == replay.status_code == 200
        assert replay.json() == reviewed.json()
        assert reviewed.json()["revision"] == 2
        stats = (await client.get("/api/study/stats?domain=ml")).json()
        assert stats["reviews_total"] == 1
        assert stats["grades"]["good"] == 1

    @pytest.mark.asyncio
    async def test_structured_conflicts_missing_and_active_only(self, client):
        missing = await client.post(
            "/api/study/reviews",
            json={
                "request_id": "missing-request",
                "card_id": "sc_missing",
                "expected_revision": 1,
                "grade": "good",
            },
        )
        assert missing.status_code == 404
        assert missing.json()["message"]["code"] == "study_card_not_found"

        card = (
            await client.post(
                "/api/study/cards", json={"domain": "ml", "front": "Q", "back": "A"}
            )
        ).json()
        suspended = await client.post(
            f"/api/study/cards/{card['card_id']}/status",
            json={"status": "suspended", "expected_revision": 1},
        )
        assert suspended.status_code == 200
        blocked = await client.post(
            "/api/study/reviews",
            json={
                "request_id": "inactive-request",
                "card_id": card["card_id"],
                "expected_revision": 2,
                "grade": "good",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["message"]["code"] == "study_card_not_active"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("expected_revision", True),
            ("expected_revision", 0),
            ("expected_revision", -1),
            ("expected_revision", 1 << 63),
            ("response_ms", True),
            ("response_ms", -1),
            ("response_ms", 1 << 63),
            ("request_id", "   "),
        ],
    )
    async def test_review_rejects_invalid_integer_and_key_boundaries(self, client, field, value):
        payload = {
            "request_id": "valid-request",
            "card_id": "sc_valid",
            "expected_revision": 1,
            "grade": "good",
            field: value,
        }
        response = await client.post("/api/study/reviews", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_blank_card_fields_and_illegal_manual_status_are_422(self, client):
        for payload in (
            {"domain": "ml", "front": "   ", "back": "A"},
            {"domain": "ml", "front": "Q", "back": "   "},
            {"domain": "ml", "front": "Q", "back": "A", "status": "suggested"},
        ):
            assert (await client.post("/api/study/cards", json=payload)).status_code == 422

    @pytest.mark.asyncio
    async def test_openapi_exposes_revision_request_id_and_stats(self, client):
        schema = (await client.get("/openapi.json")).json()
        review = schema["components"]["schemas"]["StudyReviewRequest"]
        card = schema["components"]["schemas"]["StudyCardResponse"]
        assert {"request_id", "expected_revision"} <= set(review["required"])
        assert "revision" in card["required"]
        assert "/api/study/stats" in schema["paths"]
