"""search 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    Job,
    _MAX_NOTE_EVIDENCE_PROJECTION,
    _canonical_ids_from_evidence_json,
    _chunk_note_body,
    _clean_search_query,
    _fts_match_query,
    _normalized_body_sha256,
    _sha256_text,
    _substring_snippet,
    _two_cjk_query,
    json,
)

class SearchRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def list_unindexed_done_jobs(self, limit: int = 100) -> list[Job]:
        """返回尚无任何全文索引的当前已完成 job,供 scheduler 幂等补账。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM jobs
                   WHERE status='done' AND is_current=1
                     AND NOT EXISTS (
                       SELECT 1 FROM notes_fts5 WHERE notes_fts5.job_id=jobs.id
                     )
                   ORDER BY created_at ASC LIMIT ?""",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_unreconciled_concept_occurrence_jobs(
        self, limit: int = 100,
    ) -> list[Job]:
        """返回已建索引但 occurrence 投影尚未确认完成的当前 Job。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM jobs
                   WHERE status='done' AND is_current=1
                     AND EXISTS (
                       SELECT 1 FROM notes_fts5 WHERE notes_fts5.job_id=jobs.id
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM concept_occurrence_projection p
                       WHERE p.job_id=jobs.id
                     )
                   ORDER BY created_at ASC LIMIT ?""",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_concept_occurrence_projection_source(self, job_id: str) -> str | None:
        """返回当前投影绑定的源摘要;缺失时由scheduler重放。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT source_digest FROM concept_occurrence_projection WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return str(row["source_digest"]) if row is not None else None

    def canonical_evidence_database_states(
        self,
        evidence_ids: list[str],
    ) -> dict[str, dict]:
        """批量重算 DB 侧有效性；文件 SHA 由 resolver 在同一批次继续验证。"""
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= 100
            or any(type(item) is not str or not item for item in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            raise ValueError("evidence_ids must contain 1..100 unique strings")
        placeholders = ",".join("?" for _ in evidence_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM canonical_evidence WHERE evidence_id IN ({placeholders})",
                evidence_ids,
            ).fetchall()
            result: dict[str, dict] = {}
            for row in rows:
                item = self._row_to_canonical_evidence(row)
                job = self._conn.execute(
                    "SELECT status, is_current FROM jobs WHERE id=?",
                    (row["job_id"],),
                ).fetchone()
                if job is None:
                    status, reason = "missing", "job_deleted"
                elif job["status"] != "done":
                    status, reason = "stale", "job_not_done"
                elif int(job["is_current"]) != 1:
                    status, reason = "stale", "job_superseded"
                else:
                    chunk = self._conn.execute(
                        """SELECT job_id, note_type, section, char_start, char_end, body
                           FROM note_chunks WHERE chunk_id=?""",
                        (row["chunk_id"],),
                    ).fetchone()
                    if chunk is None:
                        status, reason = "missing", "chunk_removed"
                    elif (
                        chunk["job_id"] != row["job_id"]
                        or chunk["note_type"] != row["note_type"]
                        or chunk["section"] != row["section"]
                        or int(chunk["char_start"]) != int(row["chunk_char_start"])
                        or int(chunk["char_end"]) != int(row["chunk_char_end"])
                        or _sha256_text(str(chunk["body"])) != row["chunk_body_sha256"]
                    ):
                        status, reason = "stale", "chunk_changed"
                    else:
                        status, reason = "valid", None
                item["database_status"] = status
                item["database_reason"] = reason
                result[str(row["evidence_id"])] = item
        return result

    def canonical_evidence_ids_for_job(
        self,
        job_id: str,
        note_type: str | None = None,
    ) -> list[str]:
        """从当前 chunk 快照返回稳定 ID；失效 ID 仍交 resolver 显式投影。"""
        where = "job_id=?"
        params: list[object] = [job_id]
        if note_type is not None:
            where += " AND note_type=?"
            params.append(note_type)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT evidence_json FROM note_chunks
                    WHERE {where}
                    ORDER BY note_type, chunk_id""",
                params,
            ).fetchall()
        return list(dict.fromkeys(
            evidence_id
            for row in rows
            for evidence_id in _canonical_ids_from_evidence_json(row["evidence_json"])
        ))

    def canonical_evidence_ids_for_source_segments(
        self,
        *,
        job_id: str,
        note_type: str,
        source_segment_ids: list[str],
    ) -> dict[str, list[str]]:
        """把 source segment 映到当前 note snapshot 的 canonical IDs。"""
        if (
            not isinstance(source_segment_ids, list)
            or len(source_segment_ids) > 500
            or any(
                not isinstance(item, str) or not item.strip()
                for item in source_segment_ids
            )
        ):
            raise ValueError("source_segment_ids 必须是至多 500 个非空字符串")
        normalized_segments = list(dict.fromkeys(source_segment_ids))
        if not normalized_segments:
            return {}
        result = {segment_id: [] for segment_id in normalized_segments}
        with self._lock:
            chunk_rows = self._conn.execute(
                """SELECT evidence_json FROM note_chunks
                   WHERE job_id=? AND note_type=? ORDER BY chunk_id""",
                (job_id, note_type),
            ).fetchall()
            current_ids = list(dict.fromkeys(
                evidence_id
                for row in chunk_rows
                for evidence_id in _canonical_ids_from_evidence_json(
                    row["evidence_json"]
                )
            ))
            if not current_ids:
                return result
            id_placeholders = ",".join("?" for _ in current_ids)
            segment_placeholders = ",".join("?" for _ in normalized_segments)
            rows = self._conn.execute(
                f"""SELECT evidence_id, source_segment_id
                    FROM canonical_evidence
                    WHERE job_id=? AND note_type=? AND status='valid'
                      AND evidence_id IN ({id_placeholders})
                      AND source_segment_id IN ({segment_placeholders})
                    ORDER BY source_segment_id, evidence_id""",
                (job_id, note_type, *current_ids, *normalized_segments),
            ).fetchall()
        for row in rows:
            result[str(row["source_segment_id"])].append(str(row["evidence_id"]))
        return result

    def canonical_evidence_ids_for_notes(
        self, refs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], list[str]]:
        """批量返回检索笔记的当前证据 ID，避免按结果逐条查询。"""
        normalized = list(dict.fromkeys(refs))
        result = {ref: [] for ref in normalized}
        if not normalized:
            return result
        where = " OR ".join("(job_id=? AND note_type=?)" for _ in normalized)
        params = [value for ref in normalized for value in ref]
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT job_id,note_type,evidence_json FROM note_chunks
                    WHERE ({where})
                    ORDER BY job_id COLLATE BINARY,note_type COLLATE BINARY,
                             chunk_id COLLATE BINARY""",
                params,
            ).fetchall()
        for row in rows:
            key = (str(row["job_id"]), str(row["note_type"]))
            for evidence_id in _canonical_ids_from_evidence_json(row["evidence_json"]):
                if (
                    len(result[key]) < _MAX_NOTE_EVIDENCE_PROJECTION
                    and evidence_id not in result[key]
                ):
                    result[key].append(evidence_id)
        return result

    def search_notes(
        self,
        q: str,
        collection_id: str | None = None,
        domain: str | None = None,
        content_type: str | None = None,
        document_kind: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """全文检索笔记;2 字 CJK 用参数化 instr,3+ 字符用 FTS5。"""
        cleaned = _clean_search_query(q)
        if not cleaned or len(cleaned) == 1:
            return 0, []

        short_query = _two_cjk_query(cleaned)
        if short_query:
            where_parts = ["(instr(title, ?) > 0 OR instr(body, ?) > 0)"]
            params: list = [short_query, short_query]
        else:
            match = _fts_match_query(cleaned)
            if not match:
                return 0, []
            where_parts = ["notes_fts5 MATCH ?"]
            params = [match]
        if collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if content_type:
            where_parts.append("content_type=?")
            params.append(content_type)
        if document_kind:
            where_parts.append(
                "EXISTS (SELECT 1 FROM jobs AS j "
                "WHERE j.id=notes_fts5.job_id AND j.document_kind=?)"
            )
            params.append(document_kind)
        where = " AND ".join(where_parts)

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM notes_fts5 WHERE {where}", params
        ).fetchone()[0]

        if short_query:
            rows = self._conn.execute(
                f"""SELECT job_id, note_type, title, content_type, domain,
                           collection_id, body,
                           (SELECT j.document_kind FROM jobs AS j
                            WHERE j.id=notes_fts5.job_id) AS document_kind
                    FROM notes_fts5 WHERE {where}
                    ORDER BY job_id COLLATE BINARY, note_type COLLATE BINARY
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        else:
            # snippet(表, 列号 6=body, 高亮包裹, 省略号, 单片最多 12 token)。
            rows = self._conn.execute(
                f"""SELECT job_id, note_type, title, content_type, domain,
                           collection_id,
                           (SELECT j.document_kind FROM jobs AS j
                            WHERE j.id=notes_fts5.job_id) AS document_kind,
                           snippet(notes_fts5, 6, '<mark>', '</mark>', '…', 12) AS snippet
                    FROM notes_fts5 WHERE {where}
                    ORDER BY rank, job_id COLLATE BINARY, note_type COLLATE BINARY
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        items = [
            {
                "job_id": r["job_id"],
                "note_type": r["note_type"],
                "title": r["title"],
                "snippet": (
                    _substring_snippet(r["body"], r["title"], short_query)
                    if short_query else r["snippet"]
                ),
                "content_type": r["content_type"],
                "document_kind": r["document_kind"] or None,
                "domain": r["domain"],
                "collection_id": r["collection_id"] or None,
            }
            for r in rows
        ]
        evidence_ids = self.canonical_evidence_ids_for_notes([
            (str(item["job_id"]), str(item["note_type"])) for item in items
        ])
        for item in items:
            item["canonical_evidence_ids"] = evidence_ids.get(
                (str(item["job_id"]), str(item["note_type"])), []
            )
        return total, items

    def search_note_chunks(
        self,
        q: str,
        collection_id: str | None = None,
        domain: str | None = None,
        content_type: str | None = None,
        document_kind: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """全文检索问答证据块;2 字 CJK 兼容路径与公开 filter 语义一致。"""
        cleaned = _clean_search_query(q)
        if not cleaned or len(cleaned) == 1:
            return 0, []

        short_query = _two_cjk_query(cleaned)
        if short_query:
            where_parts = [
                "(instr(title, ?) > 0 OR instr(section, ?) > 0 OR instr(body, ?) > 0)"
            ]
            params: list = [short_query, short_query, short_query]
        else:
            match = _fts_match_query(cleaned)
            if not match:
                return 0, []
            where_parts = ["note_chunks_fts5 MATCH ?"]
            params = [match]
        if collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if content_type:
            where_parts.append("content_type=?")
            params.append(content_type)
        if document_kind:
            where_parts.append(
                "EXISTS (SELECT 1 FROM jobs AS j "
                "WHERE j.id=note_chunks_fts5.job_id AND j.document_kind=?)"
            )
            params.append(document_kind)
        where = " AND ".join(where_parts)

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM note_chunks_fts5 WHERE {where}", params
        ).fetchone()[0]
        if short_query:
            rows = self._conn.execute(
                f"""SELECT chunk_id, job_id, note_type, title, content_type, domain,
                           collection_id, section, body, evidence_json,
                           (SELECT j.document_kind FROM jobs AS j
                            WHERE j.id=note_chunks_fts5.job_id) AS document_kind
                    FROM note_chunks_fts5 WHERE {where}
                    ORDER BY job_id COLLATE BINARY, note_type COLLATE BINARY,
                             chunk_id COLLATE BINARY
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"""SELECT chunk_id, job_id, note_type, title, content_type, domain,
                           collection_id, section, body, evidence_json,
                           (SELECT j.document_kind FROM jobs AS j
                            WHERE j.id=note_chunks_fts5.job_id) AS document_kind,
                           snippet(note_chunks_fts5, 8, '<mark>', '</mark>', '…', 12) AS snippet
                    FROM note_chunks_fts5 WHERE {where}
                    ORDER BY rank, job_id COLLATE BINARY, note_type COLLATE BINARY,
                             chunk_id COLLATE BINARY
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        items = []
        artifact_hashes: dict[tuple[str, str], str | None] = {}
        for r in rows:
            try:
                evidence = json.loads(r["evidence_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                evidence = {}
            evidence.setdefault("note_type", r["note_type"])
            evidence.setdefault("body_sha256", _normalized_body_sha256(r["body"]))
            if not evidence.get("artifact_sha256"):
                artifact_key = (r["job_id"], r["note_type"])
                if artifact_key not in artifact_hashes:
                    note_row = self._conn.execute(
                        """SELECT body FROM notes_fts5
                           WHERE job_id=? AND note_type=? LIMIT 1""",
                        artifact_key,
                    ).fetchone()
                    artifact_hashes[artifact_key] = (
                        _sha256_text(note_row["body"]) if note_row else None
                    )
                artifact_sha256 = artifact_hashes[artifact_key]
                if artifact_sha256:
                    evidence["artifact_sha256"] = artifact_sha256
            items.append({
                "chunk_id": r["chunk_id"],
                "job_id": r["job_id"],
                "note_type": r["note_type"],
                "title": r["title"],
                "snippet": (
                    _substring_snippet(r["body"], r["title"], short_query)
                    if short_query else r["snippet"]
                ),
                "body": r["body"],
                "content_type": r["content_type"],
                "document_kind": r["document_kind"] or None,
                "domain": r["domain"],
                "collection_id": r["collection_id"] or None,
                "section": r["section"] or "",
                "evidence": evidence,
            })
        return total, items

    def _replace_note_chunks_locked_in_tx(
        self,
        connection,
        *,
        job_id: str,
        note_type: str,
        title: str,
        body: str,
        content_type: str = "",
        domain: str = "",
        collection_id: str = "",
    ) -> None:
        """重建某 job/note_type 的证据块索引。调用方须已持锁,并负责 commit。"""
        connection.execute(
            "DELETE FROM note_chunks WHERE job_id=? AND note_type=?", (job_id, note_type)
        )
        connection.execute(
            "DELETE FROM note_chunks_fts5 WHERE job_id=? AND note_type=?",
            (job_id, note_type),
        )
        now = _db._now_iso()
        artifact_sha256 = _sha256_text(body)
        for idx, chunk in enumerate(_chunk_note_body(body)):
            chunk_id = f"{job_id}:{note_type}:{idx}"
            evidence = {
                "chunk_id": chunk_id,
                "note_type": note_type,
                "section": chunk["section"],
                "chunk_index": idx,
                "char_start": chunk["char_start"],
                "char_end": chunk["char_end"],
                "timestamp_sec": None,
                "page": None,
                "frame_path": None,
                "image_path": None,
                "artifact_sha256": artifact_sha256,
                "body_sha256": _normalized_body_sha256(chunk["body"]),
            }
            evidence_json = json.dumps(evidence, ensure_ascii=False)
            values = (
                chunk_id, job_id, note_type, content_type or "", collection_id or "",
                domain or "", title or "", chunk["section"], idx,
                chunk["char_start"], chunk["char_end"], chunk["body"], evidence_json,
                now, now,
            )
            connection.execute(
                """INSERT INTO note_chunks
                   (chunk_id, job_id, note_type, content_type, collection_id, domain,
                    title, section, chunk_index, char_start, char_end, body,
                    evidence_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            connection.execute(
                """INSERT INTO note_chunks_fts5
                   (chunk_id, job_id, note_type, content_type, collection_id, domain,
                    title, section, body, evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    chunk_id, job_id, note_type, content_type or "", collection_id or "",
                    domain or "", title or "", chunk["section"], chunk["body"], evidence_json,
                ),
            )
        self._revalidate_study_suggestion_evidence_locked(
            job_id=job_id, note_type=note_type
        )

    def _replace_canonical_evidence_locked_in_tx(
        self,
        connection,
        *,
        job_id: str,
        note_type: str,
        records: list[dict],
    ) -> None:
        """原子替换当前证据集合；旧 ID 留存并失效，不随 chunk 删除。"""
        if not isinstance(records, list):
            raise ValueError("canonical evidence records must be a list")
        now = _db._now_iso()
        connection.execute(
            """UPDATE canonical_evidence
               SET status='stale', invalid_reason='note_reindexed',
                   validated_at=?, updated_at=?
               WHERE job_id=? AND note_type=?""",
            (now, now, job_id, note_type),
        )
        expected_fields = {
            "evidence_id", "schema_version", "job_id", "note_type", "chunk_id",
            "section", "source_ref", "source_segment_id", "source_path", "source_sha256",
            "source_revision", "note_path", "note_sha256", "provenance_path",
            "provenance_sha256", "chunk_body_sha256", "chunk_char_start",
            "chunk_char_end", "locator_kind", "locator_json",
            "evidence_fingerprint", "source_fingerprint",
        }
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != expected_fields:
                raise ValueError("canonical evidence record fields are invalid")
            if record["job_id"] != job_id or record["note_type"] != note_type:
                raise ValueError("canonical evidence crosses job or note")
            evidence_id = str(record["evidence_id"])
            if evidence_id in seen:
                raise ValueError("canonical evidence id is duplicated")
            seen.add(evidence_id)
            chunk = connection.execute(
                """SELECT job_id, note_type, section, char_start, char_end, body
                   FROM note_chunks WHERE chunk_id=?""",
                (record["chunk_id"],),
            ).fetchone()
            if (
                chunk is None
                or chunk["job_id"] != job_id
                or chunk["note_type"] != note_type
                or chunk["section"] != record["section"]
                or int(chunk["char_start"]) != record["chunk_char_start"]
                or int(chunk["char_end"]) != record["chunk_char_end"]
                or _sha256_text(str(chunk["body"])) != record["chunk_body_sha256"]
            ):
                raise ValueError("canonical evidence does not match current chunk")
            connection.execute(
                """INSERT INTO canonical_evidence
                   (evidence_id, schema_version, job_id, note_type, chunk_id, section,
                    source_ref, source_segment_id, source_path, source_sha256, source_revision,
                    note_path, note_sha256, provenance_path, provenance_sha256,
                    chunk_body_sha256, chunk_char_start, chunk_char_end,
                    locator_kind, locator_json, evidence_fingerprint,
                    source_fingerprint, status, invalid_reason, validated_at,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                           'valid',NULL,?,?,?)
                   ON CONFLICT(evidence_id) DO UPDATE SET
                       status='valid', invalid_reason=NULL, validated_at=excluded.validated_at,
                       updated_at=excluded.updated_at""",
                (
                    record["evidence_id"], record["schema_version"], record["job_id"],
                    record["note_type"], record["chunk_id"], record["section"],
                    record["source_ref"], record["source_segment_id"],
                    record["source_path"], record["source_sha256"],
                    record["source_revision"], record["note_path"], record["note_sha256"],
                    record["provenance_path"], record["provenance_sha256"],
                    record["chunk_body_sha256"], record["chunk_char_start"],
                    record["chunk_char_end"], record["locator_kind"],
                    record["locator_json"], record["evidence_fingerprint"],
                    record["source_fingerprint"], now, now, now,
                ),
            )
        rows = connection.execute(
            """SELECT chunk_id, evidence_id FROM canonical_evidence
               WHERE job_id=? AND note_type=? AND status='valid'
               ORDER BY chunk_id, evidence_id""",
            (job_id, note_type),
        ).fetchall()
        ids_by_chunk: dict[str, list[str]] = {}
        for row in rows:
            ids_by_chunk.setdefault(str(row["chunk_id"]), []).append(
                str(row["evidence_id"])
            )
        chunks = connection.execute(
            """SELECT chunk_id, evidence_json FROM note_chunks
               WHERE job_id=? AND note_type=? ORDER BY chunk_id""",
            (job_id, note_type),
        ).fetchall()
        for chunk in chunks:
            try:
                projection = json.loads(str(chunk["evidence_json"]))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError("note chunk evidence projection is invalid") from exc
            projection["canonical_evidence_ids"] = ids_by_chunk.get(
                str(chunk["chunk_id"]), []
            )
            encoded = json.dumps(
                projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                "UPDATE note_chunks SET evidence_json=?, updated_at=? WHERE chunk_id=?",
                (encoded, now, chunk["chunk_id"]),
            )
            connection.execute(
                "UPDATE note_chunks_fts5 SET evidence_json=? WHERE chunk_id=?",
                (encoded, chunk["chunk_id"]),
            )

    def set_canonical_evidence_states_in_tx(self, connection, states: list[dict]) -> None:
        """原子落下 resolver 结论；状态不参与 ID，可随当前文件事实变化。"""
        if not isinstance(states, list) or len(states) > 100:
            raise ValueError("canonical evidence states count is invalid")
        expected = {"evidence_id", "status", "reason"}
        now = _db._now_iso()
        for state in states:
            if not isinstance(state, dict) or set(state) != expected:
                raise ValueError("canonical evidence state fields are invalid")
            status, reason = state.get("status"), state.get("reason")
            if status not in {"valid", "stale", "missing"}:
                raise ValueError("canonical evidence status is invalid")
            if (
                (status == "valid" and reason is not None)
                or (
                    status != "valid"
                    and (type(reason) is not str or not reason.strip())
                )
            ):
                raise ValueError("canonical evidence reason is invalid")
            changed = connection.execute(
                """UPDATE canonical_evidence
                   SET status=?, invalid_reason=?, validated_at=?, updated_at=?
                   WHERE evidence_id=?""",
                (status, reason, now, now, state["evidence_id"]),
            )
            if changed.rowcount != 1:
                raise KeyError(f"canonical evidence not found: {state['evidence_id']}")
