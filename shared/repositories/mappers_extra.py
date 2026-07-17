"""mappers_extra 领域的显式数据库边界。"""

from __future__ import annotations

from ..db import (
    Collection,
    Step,
    StepStatus,
    Worker,
    _norm_related,
    _parse_dt,
    json,
    sqlite3,
)

class DatabaseRowMappersExtra:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def _row_to_collection(self, r: sqlite3.Row) -> Collection:
        return Collection(
            id=r["id"],
            name=r["name"],
            domain=r["domain"],
            description=r["description"],
            tags=json.loads(r["tags"]),
            job_count=r["job_count"],
            source_type=r["source_type"],
            source_id=r["source_id"],
            sync_enabled=bool(r["sync_enabled"]),
            last_synced_at=_parse_dt(r["last_synced_at"]),
            last_sync_status=r["last_sync_status"],
            last_sync_error=r["last_sync_error"],
            created_at=_parse_dt(r["created_at"]),
            updated_at=_parse_dt(r["updated_at"]),
        )

    @staticmethod
    def _row_to_canonical_evidence(row: sqlite3.Row) -> dict:
        try:
            locator = json.loads(str(row["locator_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            locator = None
        return {
            "evidence_id": row["evidence_id"],
            "schema_version": row["schema_version"],
            "job_id": row["job_id"],
            "note_type": row["note_type"],
            "chunk_id": row["chunk_id"],
            "section": row["section"],
            "source_ref": row["source_ref"],
            "source_segment_id": row["source_segment_id"],
            "source_path": row["source_path"],
            "source_sha256": row["source_sha256"],
            "source_revision": row["source_revision"],
            "note_path": row["note_path"],
            "note_sha256": row["note_sha256"],
            "provenance_path": row["provenance_path"],
            "provenance_sha256": row["provenance_sha256"],
            "chunk_body_sha256": row["chunk_body_sha256"],
            "chunk_char_start": row["chunk_char_start"],
            "chunk_char_end": row["chunk_char_end"],
            "locator_kind": row["locator_kind"],
            "locator": locator,
            "evidence_fingerprint": row["evidence_fingerprint"],
            "source_fingerprint": row["source_fingerprint"],
            "status": row["status"],
            "reason": row["invalid_reason"],
            "validated_at": row["validated_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_study_suggestion_batch(row: sqlite3.Row) -> dict:
        try:
            llm_request = json.loads(str(row["llm_request_json"]))
        except (json.JSONDecodeError, TypeError):
            llm_request = {}
        try:
            result = json.loads(str(row["result_json"])) if row["result_json"] else None
        except (json.JSONDecodeError, TypeError):
            result = None
        return {
            "batch_id": row["batch_id"],
            "domain": row["domain"],
            "status": row["status"],
            "revision": row["revision"],
            "attempt": row["attempt"],
            "generator_fingerprint": row["generator_fingerprint"],
            "input_fingerprint": row["input_fingerprint"],
            "task_id": row["task_id"],
            "provider": row["provider"],
            "model": row["model"],
            "max_cards": row["max_cards"],
            "llm_request": llm_request,
            "result": result,
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "deadline_at": row["deadline_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_study_suggestion_locked(self, row: sqlite3.Row) -> dict:
        evidence_rows = self._conn.execute(
            """SELECT l.evidence_id, l.ordinal, l.quote_snapshot, l.quote_sha256,
                      e.job_id, e.chunk_id, e.note_type, e.source_domain_snapshot,
                      e.current_domain, e.title_snapshot, e.section_snapshot,
                      e.body_sha256, e.locator_json, e.status, e.invalid_reason
               FROM study_suggestion_evidence_links l
               JOIN study_suggestion_evidence e ON e.evidence_id=l.evidence_id
               WHERE l.suggestion_id=? ORDER BY l.ordinal""",
            (row["suggestion_id"],),
        ).fetchall()
        evidence = []
        for entry in evidence_rows:
            try:
                locator = json.loads(str(entry["locator_json"]))
            except (json.JSONDecodeError, TypeError):
                locator = {}
            evidence.append(
                {
                    "evidence_id": entry["evidence_id"],
                    "job_id": entry["job_id"],
                    "chunk_id": entry["chunk_id"],
                    "note_type": entry["note_type"],
                    "source_domain": entry["source_domain_snapshot"],
                    "current_domain": entry["current_domain"],
                    "title": entry["title_snapshot"],
                    "section": entry["section_snapshot"],
                    "quote": entry["quote_snapshot"],
                    "quote_sha256": entry["quote_sha256"],
                    "body_sha256": entry["body_sha256"],
                    "locator": locator,
                    "status": entry["status"],
                    "invalid_reason": entry["invalid_reason"],
                }
            )
        return {
            "suggestion_id": row["suggestion_id"],
            "batch_id": row["batch_id"],
            "ordinal": row["ordinal"],
            "status": row["status"],
            "revision": row["revision"],
            "domain": row["domain"],
            "concept_term": row["concept_term"],
            "knowledge_key": row["knowledge_key"],
            "card_type": row["card_type"],
            "front": row["front"],
            "back": row["back"],
            "explanation": row["explanation"],
            "knowledge_fingerprint": row["knowledge_fingerprint"],
            "content_fingerprint": row["content_fingerprint"],
            "accepted_card_id": row["accepted_card_id"],
            "rejection_reason": row["rejection_reason"],
            "evidence": evidence,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_study_card(self, row: sqlite3.Row) -> dict:
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            evidence = []
        review = None
        if row["review_due_at"] is not None:
            review = {
                "due_at": row["review_due_at"],
                "interval_days": row["interval_days"],
                "ease": row["ease"],
                "repetitions": row["repetitions"],
                "lapses": row["lapses"],
                "last_grade": row["last_grade"],
                "last_reviewed_at": row["last_reviewed_at"],
                "updated_at": row["review_updated_at"],
            }
        return {
            "card_id": row["card_id"],
            "domain": row["domain"],
            "job_id": row["job_id"],
            "concept_term": row["concept_term"],
            "card_type": row["card_type"],
            "front": row["front"],
            "back": row["back"],
            "explanation": row["explanation"],
            "evidence": evidence,
            "status": row["status"],
            "source": row["source"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "review": review,
        }

    def _row_to_glossary(self, row: sqlite3.Row) -> dict:
        return {
            "domain": row["domain"],
            "term": row["term"],
            "definition": row["definition"],
            "zh_name": (row["zh_name"] if "zh_name" in row.keys() else "") or "",
            "aliases": json.loads(
                (row["aliases"] if "aliases" in row.keys() else "") or "[]"
            ),
            "occurrences": json.loads(row["occurrences"] or "[]"),
            # 规范形态 [{term, rel}];存量字符串元素在读出时归一(rel='related')。
            "related": _norm_related(json.loads(row["related"] or "[]")),
            "status": row["status"],
            "watched": bool(row["watched"] if "watched" in row.keys() else 0),
            "is_topic": bool(row["is_topic"]),
            "definition_locked": bool(row["definition_locked"]),
            "current_definition_version_id": (
                row["current_definition_version_id"]
                if "current_definition_version_id" in row.keys()
                else None
            ),
            "lock_revision": int(
                row["lock_revision"] if "lock_revision" in row.keys() else 0
            ),
            "created_at": _parse_dt(row["created_at"]),
            "updated_at": _parse_dt(row["updated_at"]),
        }

    @staticmethod
    def _row_to_concept_definition_version(row: sqlite3.Row) -> dict:
        return {
            "definition_version_id": row["definition_version_id"],
            "domain": row["domain"],
            "term": row["term"],
            "version": int(row["version"]),
            "definition": row["definition"],
            "source_evidence_ids": json.loads(row["source_evidence_ids_json"]),
            "source_set_fingerprint": row["source_set_fingerprint"],
            "strategy": row["strategy"],
            "provider": row["provider"],
            "model": row["model"],
            "prompt_hash": row["prompt_hash"],
            "input_hash": row["input_hash"],
            "supersedes_version_id": row["supersedes_version_id"],
            "actor": row["actor"],
            "created_at": _parse_dt(row["created_at"]),
        }

    def _row_to_step(self, row: sqlite3.Row) -> Step:
        return Step(
            job_id=row["job_id"],
            name=row["step"],
            scope_key=row["scope_key"],
            status=StepStatus(row["status"]),
            pool=row["pool"],
            input_hash=row["input_hash"],
            worker_id=row["worker_id"],
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            duration_sec=row["duration_sec"],
            meta=json.loads(row["meta"]) if row["meta"] else {},
            error=row["error"],
            retries=row["retries"],
        )

    def _row_to_worker(self, row: sqlite3.Row) -> Worker:
        return Worker(
            id=row["id"],
            type=row["type"],
            pools=json.loads(row["pools"]),
            tags=set(json.loads(row["tags"])),
            reject_tags=set(json.loads(row["reject_tags"])),
            hostname=row["hostname"],
            gpu_name=row["gpu_name"],
            gpu_memory_mb=row["gpu_memory_mb"],
            concurrency=row["concurrency"] if "concurrency" in row.keys() else 1,
            remote_addr=row["remote_addr"] if "remote_addr" in row.keys() else None,
            status=row["status"],
            admin_status=row["admin_status"] if "admin_status" in row.keys() else "",
            current_job=row["current_job"],
            current_step=row["current_step"],
            tasks_completed=row["tasks_completed"],
            tasks_failed=row["tasks_failed"],
            total_duration_sec=row["total_duration_sec"],
            first_seen=_parse_dt(row["first_seen"]),
            started_at=_parse_dt(row["started_at"]),
            last_heartbeat=_parse_dt(row["last_heartbeat"]),
            admin_note=row["admin_note"],
            desired_config=(
                json.loads(row["desired_config"])
                if "desired_config" in row.keys() and row["desired_config"] else None
            ),
            cfg_rev=(row["cfg_rev"] or 0) if "cfg_rev" in row.keys() else 0,
        )
