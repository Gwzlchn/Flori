"""study 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    MAX_SQLITE_INTEGER,
    STUDY_STATUSES,
    StudyConflictError,
    StudyNotFoundError,
    StudySuggestionConflictError,
    StudySuggestionNotFoundError,
    _now_iso,
    canonical_json,
    canonical_utc_iso,
    content_fingerprint,
    datetime,
    datetime_to_epoch_us,
    json,
    payload_fingerprint,
    require_external_request_id,
    require_identifier,
    require_plain_int,
    require_revision,
    sha256_text,
    sqlite3,
    timedelta,
    timezone,
    utc_now,
    uuid,
)

class StudyRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def get_study_suggestion_batch(self, batch_id: str) -> dict | None:
        normalized = require_identifier(batch_id, "batch_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM study_suggestion_batches WHERE batch_id=?", (normalized,)
            ).fetchone()
            if row is None:
                return None
            result = self._row_to_study_suggestion_batch(row)
            result["evidence_count"] = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM study_suggestion_evidence WHERE batch_id=?",
                    (normalized,),
                ).fetchone()[0]
            )
            result["suggestion_count"] = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM study_suggestions WHERE batch_id=?",
                    (normalized,),
                ).fetchone()[0]
            )
            return result

    def list_study_suggestion_batches_for_reconcile(
        self,
        *,
        statuses: tuple[str, ...] = ("pending_enqueue", "queued"),
        limit: int = 200,
    ) -> list[dict]:
        """按持久状态列出待投递/收割批次,供任意 Scheduler 副本幂等对账."""
        allowed = {"pending_enqueue", "queued"}
        if (
            not isinstance(statuses, tuple)
            or not statuses
            or any(status not in allowed for status in statuses)
        ):
            raise ValueError("statuses 只允许 pending_enqueue/queued")
        normalized_limit = require_plain_int(limit, "limit", minimum=1, maximum=1_000)
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM study_suggestion_batches
                    WHERE status IN ({placeholders})
                    ORDER BY deadline_at_epoch_us, batch_id LIMIT ?""",
                [*statuses, normalized_limit],
            ).fetchall()
        return [self._row_to_study_suggestion_batch(row) for row in rows]

    def list_study_suggestions(
        self,
        *,
        batch_id: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        normalized_batch = (
            require_identifier(batch_id, "batch_id") if batch_id is not None else None
        )
        normalized_domain = (
            require_identifier(domain, "domain") if domain is not None else None
        )
        if status is not None and status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status 必须是 suggested/accepted/rejected")
        normalized_limit = require_plain_int(limit, "limit", minimum=1, maximum=200)
        normalized_offset = require_plain_int(
            offset, "offset", minimum=0, maximum=2_147_483_647
        )
        with self._lock:
            return self._list_study_suggestions_locked(
                batch_id=normalized_batch,
                domain=normalized_domain,
                status=status,
                limit=normalized_limit,
                offset=normalized_offset,
            )

    def get_study_suggestion(self, suggestion_id: str) -> dict | None:
        normalized = require_identifier(suggestion_id, "suggestion_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM study_suggestions WHERE suggestion_id=?", (normalized,)
            ).fetchone()
            return self._row_to_study_suggestion_locked(row) if row else None

    def get_study_mastery(self, *, domain: str | None = None) -> dict:
        """按每卡最后一次真实评分聚合 canonical concept 掌握度."""
        normalized_domain = (
            require_identifier(domain, "domain") if domain is not None else None
        )
        with self._lock:
            rows = self._conn.execute(
                """WITH eligible AS (
                     SELECT c.card_id, c.domain, c.concept_term
                     FROM study_cards c
                     WHERE c.status IN ('active','suspended')
                       AND c.concept_term IS NOT NULL
                       AND length(trim(c.concept_term)) > 0
                       AND (? IS NULL OR c.domain=?)
                   ),
                   ranked AS (
                     SELECT e.card_id, e.domain, e.concept_term, l.id, l.grade,
                            l.reviewed_at, l.reviewed_at_epoch_us,
                            ROW_NUMBER() OVER (
                              PARTITION BY e.card_id
                              ORDER BY l.reviewed_at_epoch_us DESC, l.id DESC
                            ) AS rank
                     FROM eligible e
                     JOIN study_review_logs l ON l.card_id=e.card_id
                   ),
                   per_card AS (
                     SELECT card_id, domain, concept_term, reviewed_at,
                            CASE grade
                              WHEN 'again' THEN 0
                              WHEN 'hard' THEN 50
                              WHEN 'good' THEN 80
                              WHEN 'easy' THEN 100
                            END AS score
                     FROM ranked WHERE rank=1
                   ),
                   review_counts AS (
                     SELECT e.card_id, COUNT(l.id) AS reviews_total
                     FROM eligible e
                     JOIN study_review_logs l ON l.card_id=e.card_id
                     GROUP BY e.card_id
                   )
                   SELECT p.domain, p.concept_term,
                          CAST(ROUND(AVG(p.score), 0) AS INTEGER) AS score,
                          COUNT(*) AS reviewed_cards,
                          SUM(rc.reviews_total) AS reviews_total,
                          MAX(p.reviewed_at) AS last_reviewed_at
                   FROM per_card p
                   JOIN review_counts rc ON rc.card_id=p.card_id
                   GROUP BY p.domain, p.concept_term
                   ORDER BY score ASC, p.concept_term ASC""",
                (normalized_domain, normalized_domain),
            ).fetchall()
        items = []
        for row in rows:
            score = int(row["score"])
            level = "mastered" if score >= 85 else "learning" if score >= 60 else "fragile"
            items.append(
                {
                    "domain": row["domain"],
                    "concept_term": row["concept_term"],
                    "score": score,
                    "level": level,
                    "reviewed_cards": int(row["reviewed_cards"]),
                    "reviews_total": int(row["reviews_total"]),
                    "last_reviewed_at": row["last_reviewed_at"],
                }
            )
        return {"total": len(items), "items": items}

    def get_study_card(self, card_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                          c.front, c.back, c.explanation, c.evidence_json, c.status,
                          c.source, c.revision, c.created_at, c.updated_at,
                          r.due_at AS review_due_at, r.interval_days, r.ease,
                          r.repetitions, r.lapses, r.last_grade, r.last_reviewed_at,
                          r.updated_at AS review_updated_at
                   FROM study_cards c
                   LEFT JOIN study_reviews r ON r.card_id = c.card_id
                   WHERE c.card_id=?""",
                (card_id,),
            ).fetchone()
        return self._row_to_study_card(row) if row else None

    def list_study_cards(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        where_parts = ["1=1"]
        params: list = []
        if domain:
            where_parts.append("c.domain=?")
            params.append(domain)
        if status:
            where_parts.append("c.status=?")
            params.append(status)
        if q:
            like = f"%{q}%"
            where_parts.append(
                "(c.front LIKE ? OR c.back LIKE ? OR c.explanation LIKE ? OR c.concept_term LIKE ?)"
            )
            params.extend([like, like, like, like])
        where = " AND ".join(where_parts)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM study_cards c WHERE {where}", params,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                           c.front, c.back, c.explanation, c.evidence_json, c.status,
                           c.source, c.revision, c.created_at, c.updated_at,
                           r.due_at AS review_due_at, r.interval_days, r.ease,
                           r.repetitions, r.lapses, r.last_grade, r.last_reviewed_at,
                           r.updated_at AS review_updated_at
                    FROM study_cards c
                    LEFT JOIN study_reviews r ON r.card_id = c.card_id
                    WHERE {where}
                    ORDER BY c.updated_at DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        return total, [self._row_to_study_card(r) for r in rows]

    def list_due_study_cards(
        self,
        *,
        domain: str | None = None,
        now: datetime | str | None = None,
        now_iso: str | None = None,
        limit: int = 50,
    ) -> tuple[int, list[dict]]:
        if now is not None and now_iso is not None:
            raise ValueError("now 与 now_iso 不能同时传入")
        current = now if now is not None else now_iso if now_iso is not None else _db.utc_now()
        current_epoch = datetime_to_epoch_us(current, "now")
        where_parts = ["c.status='active'", "r.due_at_epoch_us<=?"]
        params: list = [current_epoch]
        if domain:
            where_parts.append("c.domain=?")
            params.append(domain)
        where = " AND ".join(where_parts)
        with self._lock:
            total = self._conn.execute(
                f"""SELECT COUNT(*) FROM study_cards c
                    JOIN study_reviews r ON r.card_id = c.card_id
                    WHERE {where}""",
                params,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                           c.front, c.back, c.explanation, c.evidence_json, c.status,
                           c.source, c.revision, c.created_at, c.updated_at,
                           r.due_at AS review_due_at, r.interval_days, r.ease,
                           r.repetitions, r.lapses, r.last_grade, r.last_reviewed_at,
                           r.updated_at AS review_updated_at
                    FROM study_cards c
                    JOIN study_reviews r ON r.card_id = c.card_id
                    WHERE {where}
                    ORDER BY r.due_at_epoch_us ASC, c.created_at ASC
                    LIMIT ?""",
                params + [limit],
            ).fetchall()
        return total, [self._row_to_study_card(r) for r in rows]

    def get_study_stats(
        self,
        *,
        domain: str | None = None,
        now: datetime | str | None = None,
    ) -> dict:
        """单次 CTE 从已提交事实聚合卡片,到期,评分和留存统计."""
        now_epoch = datetime_to_epoch_us(now or _db.utc_now(), "now")
        with self._lock:
            row = self._conn.execute(
                """WITH filtered_cards AS (
                   SELECT card_id, status FROM study_cards
                   WHERE (? IS NULL OR domain=?)
                 ),
                 card_totals AS (
                   SELECT COUNT(*) AS total,
                          COALESCE(SUM(status='suggested'),0) AS suggested,
                          COALESCE(SUM(status='active'),0) AS active,
                          COALESCE(SUM(status='suspended'),0) AS suspended,
                          COALESCE(SUM(status='rejected'),0) AS rejected
                   FROM filtered_cards
                 ),
                 due_totals AS (
                   SELECT COUNT(*) AS due
                   FROM filtered_cards c JOIN study_reviews r USING(card_id)
                   WHERE c.status='active' AND r.due_at_epoch_us<=?
                 ),
                 log_totals AS (
                   SELECT COUNT(l.id) AS reviews_total,
                          COUNT(DISTINCT l.card_id) AS reviewed_cards,
                          COALESCE(SUM(l.grade='again'),0) AS again_count,
                          COALESCE(SUM(l.grade='hard'),0) AS hard_count,
                          COALESCE(SUM(l.grade='good'),0) AS good_count,
                          COALESCE(SUM(l.grade='easy'),0) AS easy_count
                   FROM filtered_cards c
                   LEFT JOIN study_review_logs l USING(card_id)
                 )
                     SELECT * FROM card_totals CROSS JOIN due_totals CROSS JOIN log_totals""",
                (domain, domain, now_epoch),
            ).fetchone()
        reviews_total = int(row["reviews_total"])
        retained = int(row["hard_count"]) + int(row["good_count"]) + int(row["easy_count"])
        return {
            "total": int(row["total"]),
            "statuses": {
                "suggested": int(row["suggested"]),
                "active": int(row["active"]),
                "suspended": int(row["suspended"]),
                "rejected": int(row["rejected"]),
            },
            "due": int(row["due"]),
            "reviewed_cards": int(row["reviewed_cards"]),
            "reviews_total": reviews_total,
            "grades": {
                "again": int(row["again_count"]),
                "hard": int(row["hard_count"]),
                "good": int(row["good_count"]),
                "easy": int(row["easy_count"]),
            },
            "retained_reviews": retained,
            "retention_rate": round(retained / reviews_total, 4) if reviews_total else 0.0,
        }

    def _study_suggestion_monotonic_now_locked_in_tx(
        self,
        connection,
        batch_ids: list[str],
        wall_time: datetime | str,
    ) -> datetime:
        """在持有写事务时把墙钟钳制到整本建议账本的全局前态之后."""
        candidate = (
            datetime.fromisoformat(wall_time)
            if isinstance(wall_time, str)
            else wall_time
        )
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise ValueError("study suggestion wall time 必须带 UTC 时区")
        # batch_ids 保留在签名中,避免调用方误以为可在事务外预取时间.
        # 下界必须覆盖整本账本,否则另一批次的后提交事实可被墙钟回拨越过.
        del batch_ids
        tail = connection.execute(
            """SELECT created_at AS value FROM study_suggestion_operations
               ORDER BY ledger_seq DESC LIMIT 1"""
        ).fetchone()
        lower_bound = candidate.astimezone(timezone.utc)
        if tail is not None:
            value = datetime.fromisoformat(str(tail["value"]))
            if value.tzinfo is None or value.utcoffset() is None:
                raise RuntimeError("study suggestion 时间前态缺少时区")
            lower_bound = max(lower_bound, value.astimezone(timezone.utc))
        return lower_bound

    @staticmethod
    def _study_suggestion_lifecycle_operation_payload(
        *,
        operation_kind: str,
        batch_id: str,
        task_id: str,
        attempt: int,
        expected_revision: int,
        details: dict[str, object] | None = None,
    ) -> tuple[str, str, str]:
        """为一次 batch 状态迁移生成稳定幂等键和 canonical request."""
        identity = {
            "operation_kind": operation_kind,
            "batch_id": batch_id,
            "task_id": task_id,
            "attempt": attempt,
            "expected_revision": expected_revision,
        }
        request_id = (
            f"study-lifecycle:{operation_kind}:"
            f"{payload_fingerprint(identity)}"
        )
        request = {**identity, "request_id": request_id, **(details or {})}
        request_json = canonical_json(request)
        return request_id, request_json, sha256_text(request_json)

    def _study_suggestion_lifecycle_replay_matches_current_in_tx(
        self,
        connection,
        *,
        request_id: str,
        batch_id: str,
        replay: dict | None,
        current: dict,
    ) -> bool:
        """从 lifecycle outcome 继续重放 identity 变化后核对 current row."""
        if replay is None or set(replay) != set(current):
            return False
        lifecycle = connection.execute(
            """SELECT ledger_seq FROM study_suggestion_operations
               WHERE request_id=? AND batch_id=?""",
            (request_id, batch_id),
        ).fetchone()
        if lifecycle is None:
            return False
        expected = dict(replay)
        identity_rows = connection.execute(
            """SELECT request_json, created_at
               FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind='identity_transition'
                 AND ledger_seq>?
               ORDER BY ledger_seq""",
            (batch_id, lifecycle["ledger_seq"]),
        ).fetchall()
        for row in identity_rows:
            try:
                request = json.loads(str(row["request_json"]))
            except (json.JSONDecodeError, TypeError):
                return False
            if (
                request.get("batch_id") != batch_id
                or request.get("source_domain") != expected["domain"]
            ):
                return False
            if request.get("transition_kind") == "domain_rename":
                expected["domain"] = request.get("target_domain")
                expected["updated_at"] = row["created_at"]
            elif (
                request.get("transition_kind") != "concept_merge"
                or request.get("target_domain") != expected["domain"]
            ):
                return False
        return expected == current

    def _study_suggestion_operation_replay_locked_in_tx(
        self,
        connection,
        request_id: str,
        request_fingerprint: str,
    ) -> dict | None:
        row = connection.execute(
            """SELECT request_fingerprint, outcome_json
               FROM study_suggestion_operations WHERE request_id=?""",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != request_fingerprint:
            raise StudySuggestionConflictError(
                "study_suggestion_request_id_conflict",
                "request_id was already used with a different payload",
            )
        return json.loads(str(row["outcome_json"]))

    def _insert_study_suggestion_operation_locked_in_tx(
        self,
        connection,
        *,
        request_id: str,
        request_fingerprint: str,
        operation_kind: str,
        batch_id: str,
        request_json: str,
        outcome: dict,
        created_at: str,
    ) -> None:
        previous = connection.execute(
            """SELECT ledger_seq, ledger_sha256
               FROM study_suggestion_operations ORDER BY ledger_seq DESC LIMIT 1"""
        ).fetchone()
        if previous is None:
            ledger_seq = 1
            previous_ledger_sha256 = "0" * 64
        else:
            previous_seq = int(previous["ledger_seq"])
            if previous_seq == MAX_SQLITE_INTEGER:
                raise StudySuggestionConflictError(
                    "study_suggestion_ledger_exhausted",
                    "study suggestion operation ledger is exhausted",
                )
            ledger_seq = previous_seq + 1
            previous_ledger_sha256 = str(previous["ledger_sha256"])
        outcome_json = canonical_json(outcome)
        ledger_sha256 = payload_fingerprint(
            {
                "ledger_seq": ledger_seq,
                "previous_ledger_sha256": previous_ledger_sha256,
                "request_id": request_id,
                "request_fingerprint": request_fingerprint,
                "operation_kind": operation_kind,
                "batch_id": batch_id,
                "request_json": request_json,
                "outcome_json": outcome_json,
                "created_at": created_at,
            }
        )
        connection.execute(
            """INSERT INTO study_suggestion_operations
               (request_id, ledger_seq, previous_ledger_sha256, ledger_sha256,
                request_fingerprint, operation_kind, batch_id, request_json,
                outcome_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id, ledger_seq, previous_ledger_sha256, ledger_sha256,
                request_fingerprint, operation_kind, batch_id, request_json,
                outcome_json, created_at,
            ),
        )

    def _record_study_identity_transition_locked(
        self,
        *,
        batch_ids: list[str],
        transition_kind: str,
        source_domain: str,
        target_domain: str,
        source_concept: str | None,
        target_concept: str | None,
        created_at: str,
        impacts: dict[str, dict[str, list[str]]],
    ) -> None:
        """把 canonical identity 变化写入既有不可变操作账本."""
        for batch_id in batch_ids:
            request_id = f"identity-transition:{uuid.uuid4().hex}"
            payload = {
                "operation_kind": "identity_transition",
                "request_id": request_id,
                "batch_id": batch_id,
                "transition_kind": transition_kind,
                "source_domain": source_domain,
                "target_domain": target_domain,
                "source_concept": source_concept,
                "target_concept": target_concept,
            }
            request_json = canonical_json(payload)
            self._insert_study_suggestion_operation_locked(
                request_id=request_id,
                request_fingerprint=sha256_text(request_json),
                operation_kind="identity_transition",
                batch_id=batch_id,
                request_json=request_json,
                outcome={
                    "batch_id": batch_id,
                    "input_ids": impacts[batch_id]["input_ids"],
                    "suggestion_ids": impacts[batch_id]["suggestion_ids"],
                },
                created_at=created_at,
            )

    def _study_identity_transition_impacts_locked_in_tx(
        self,
        connection,
        *,
        batch_ids: list[str],
        transition_kind: str,
        source_concept: str | None,
    ) -> dict[str, dict[str, list[str]]]:
        """在 identity 写入前冻结实际受影响的输入和已物化候选集合."""
        impacts: dict[str, dict[str, list[str]]] = {}
        for batch_id in batch_ids:
            if transition_kind == "concept_merge":
                input_ids = [
                    str(row["input_id"])
                    for row in connection.execute(
                        """SELECT input_id FROM study_suggestion_inputs
                           WHERE batch_id=? AND kind='concept'
                             AND current_concept_term=? ORDER BY input_id""",
                        (batch_id, source_concept),
                    ).fetchall()
                ]
                suggestion_ids = [
                    str(row["suggestion_id"])
                    for row in connection.execute(
                        """SELECT suggestion_id FROM study_suggestions
                           WHERE batch_id=? AND concept_term=? ORDER BY suggestion_id""",
                        (batch_id, source_concept),
                    ).fetchall()
                ]
            else:
                input_ids = []
                suggestion_ids = [
                    str(row["suggestion_id"])
                    for row in connection.execute(
                        """SELECT suggestion_id FROM study_suggestions
                           WHERE batch_id=? ORDER BY suggestion_id""",
                        (batch_id,),
                    ).fetchall()
                ]
            impacts[batch_id] = {
                "input_ids": input_ids,
                "suggestion_ids": suggestion_ids,
            }
        return impacts

    def _list_study_suggestions_locked_in_tx(
        self,
        connection,
        *,
        batch_id: str | None,
        domain: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict]]:
        where = ["1=1"]
        params: list[object] = []
        if batch_id is not None:
            where.append("batch_id=?")
            params.append(batch_id)
        if domain is not None:
            where.append("domain=?")
            params.append(domain)
        if status is not None:
            where.append("status=?")
            params.append(status)
        clause = " AND ".join(where)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM study_suggestions WHERE {clause}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT * FROM study_suggestions WHERE {clause}
                ORDER BY created_at DESC, ordinal ASC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        return total, [self._row_to_study_suggestion_locked(row) for row in rows]

    def _assert_study_suggestion_evidence_current_locked_in_tx(
        self,
        connection,
        suggestion: sqlite3.Row,
    ) -> list[dict]:
        rows = connection.execute(
            """SELECT l.evidence_id, l.quote_snapshot, l.quote_sha256,
                      e.job_id, e.chunk_id, e.note_type, e.current_domain,
                      e.title_snapshot, e.section_snapshot, e.body_snapshot,
                      e.body_sha256, e.locator_json, e.status, e.invalid_reason
               FROM study_suggestion_evidence_links l
               JOIN study_suggestion_evidence e ON e.evidence_id=l.evidence_id
               WHERE l.suggestion_id=? ORDER BY l.ordinal""",
            (suggestion["suggestion_id"],),
        ).fetchall()
        if not rows:
            raise StudySuggestionConflictError(
                "study_suggestion_evidence_missing", "suggestion has no evidence"
            )
        output = []
        for row in rows:
            self._assert_study_suggestion_evidence_row_current_locked(
                row,
                expected_domain=str(suggestion["domain"]),
            )
            if (
                row["quote_snapshot"] not in str(row["body_snapshot"])
                or sha256_text(str(row["quote_snapshot"])) != row["quote_sha256"]
            ):
                raise StudySuggestionConflictError(
                    "study_suggestion_evidence_stale", "evidence no longer matches current chunk"
                )
            try:
                locator = json.loads(str(row["locator_json"]))
            except (json.JSONDecodeError, TypeError):
                locator = {}
            output.append(
                {
                    "evidence_id": row["evidence_id"],
                    "job_id": row["job_id"],
                    "chunk_id": row["chunk_id"],
                    "note_type": row["note_type"],
                    "title": row["title_snapshot"],
                    "section": row["section_snapshot"],
                    "quote": row["quote_snapshot"],
                    "body_sha256": row["body_sha256"],
                    "locator": locator,
                }
            )
        return output

    def _study_suggestion_evidence_state_locked_in_tx(
        self,
        connection,
        evidence: sqlite3.Row,
        *,
        expected_domain: str,
    ) -> tuple[str, str | None, str]:
        """从当前 job 和 chunk 重算证据状态,不信任缓存的 status."""
        job = connection.execute(
            "SELECT domain, status, is_current FROM jobs WHERE id=?",
            (evidence["job_id"],),
        ).fetchone()
        if job is None:
            return "unavailable", "job_deleted", str(evidence["current_domain"])
        current_domain = str(job["domain"] or "")
        if current_domain != expected_domain:
            return "stale", "job_domain_changed", current_domain
        if job["status"] != "done":
            return "stale", "job_not_done", current_domain
        if int(job["is_current"]) != 1:
            return "stale", "job_superseded", current_domain
        current = connection.execute(
            """SELECT job_id, note_type, domain, body FROM note_chunks
               WHERE chunk_id=?""",
            (evidence["chunk_id"],),
        ).fetchone()
        if current is None:
            return "unavailable", "chunk_removed", current_domain
        current_hash = sha256_text(str(current["body"]))
        if (
            current["job_id"] != evidence["job_id"]
            or current["note_type"] != evidence["note_type"]
            or current["domain"] != expected_domain
            or current_hash != evidence["body_sha256"]
            or current_hash != sha256_text(str(evidence["body_snapshot"]))
        ):
            return "stale", "chunk_changed", current_domain
        return "valid", None, current_domain

    def _assert_study_suggestion_evidence_row_current_locked(
        self,
        evidence: sqlite3.Row,
        *,
        expected_domain: str,
    ) -> None:
        state, reason, current_domain = self._study_suggestion_evidence_state_locked(
            evidence,
            expected_domain=expected_domain,
        )
        if (
            evidence["status"] != "valid"
            or state != "valid"
            or evidence["current_domain"] != current_domain
        ):
            raise StudySuggestionConflictError(
                "study_suggestion_evidence_unavailable"
                if evidence["status"] != "valid" else "study_suggestion_evidence_stale",
                f"evidence is not current: {evidence['evidence_id']} ({reason or state})",
            )

    def _study_card_content_duplicate_locked_in_tx(
        self,
        connection,
        *,
        domain: str,
        card_type: str,
        front: str,
        back: str,
        explanation: str,
    ) -> bool:
        expected = content_fingerprint(
            domain=domain,
            card_type=card_type,
            front=front,
            back=back,
            explanation=explanation,
        )
        rows = connection.execute(
            """SELECT card_type, front, back, explanation FROM study_cards
               WHERE domain=?""",
            (domain,),
        ).fetchall()
        return any(
            content_fingerprint(
                domain=domain,
                card_type=str(row["card_type"]),
                front=str(row["front"]),
                back=str(row["back"]),
                explanation=str(row["explanation"] or ""),
            )
            == expected
            for row in rows
        )

    def _revalidate_study_suggestion_evidence_locked_in_tx(
        self,
        connection,
        *,
        job_id: str,
        note_type: str | None = None,
    ) -> None:
        """job 或 chunk 变化后更新可变有效性,快照始终不改."""
        note_filter = " AND e.note_type=?" if note_type is not None else ""
        params: list[object] = [job_id]
        if note_type is not None:
            params.append(note_type)
        rows = connection.execute(
            f"""SELECT e.*, b.domain AS expected_domain
               FROM study_suggestion_evidence e
               JOIN study_suggestion_batches b ON b.batch_id=e.batch_id
               WHERE e.job_id=?{note_filter}""",
            params,
        ).fetchall()
        now = _db._now_iso()
        for row in rows:
            if row["status"] == "unavailable":
                continue
            status, reason, current_domain = self._study_suggestion_evidence_state_locked(
                row,
                expected_domain=str(row["expected_domain"]),
            )
            connection.execute(
                """UPDATE study_suggestion_evidence
                   SET current_domain=?, status=?, invalid_reason=?, validated_at=?
                   WHERE evidence_id=?""",
                (current_domain, status, reason, now, row["evidence_id"]),
            )
