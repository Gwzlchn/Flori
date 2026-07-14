"""aggregates 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    ConceptConflictError,
    ConceptEvidenceError,
    ConceptNotFoundError,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_PROVIDER,
    Job,
    JobStatus,
    MAX_GENERATED_CARDS,
    MAX_SQLITE_INTEGER,
    StudyConflictError,
    StudyFaultInjector,
    StudyNotFoundError,
    StudySuggestionConflictError,
    StudySuggestionFaultInjector,
    StudySuggestionNotFoundError,
    _JOB_UPDATABLE,
    _concept_source_set,
    _lineage_key_of,
    _norm_related,
    _now_iso,
    canonical_json,
    content_fingerprint,
    datetime,
    datetime_to_epoch_us,
    hashlib,
    json,
    knowledge_fingerprint,
    operation_payload,
    parse_ai_suggestions,
    payload_fingerprint,
    require_aware_utc,
    require_external_request_id,
    require_identifier,
    require_plain_int,
    resolve_study_suggestion_prompt,
    review_request_fingerprint,
    schedule_next_review,
    sha256_text,
    sqlite3,
    study_suggestion_generator_fingerprint,
    timedelta,
    utc_now,
    uuid,
    validate_card_content,
    validate_operation_items,
    validate_review_request,
    validate_study_suggestion_prompt_snapshot,
)

from ..db import (
    AIUsage,
    Collection,
    ConceptConflictError,
    ConceptEvidenceError,
    ConceptNotFoundError,
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    Job,
    MAX_SQLITE_INTEGER,
    PROMPT_VERSION_MAX,
    PromptVersionExhaustedError,
    STALE,
    STUDY_STATUSES,
    Step,
    StepStatus,
    StudyConflictError,
    StudyNotFoundError,
    StudySuggestionConflictError,
    StudySuggestionNotFoundError,
    Worker,
    _MAX_NOTE_EVIDENCE_PROJECTION,
    _STEP_UPDATABLE,
    _canonical_ids_from_evidence_json,
    _chunk_note_body,
    _clean_search_query,
    _concept_definition_version_id,
    _concept_source_set,
    _fernet,
    _fts_match_query,
    _norm_related,
    _normalized_body_sha256,
    _now_iso,
    _optional_sha256,
    _parse_dt,
    _sha256_text,
    _substring_snippet,
    _two_cjk_query,
    _valid_prompt_version,
    _warn_plaintext_credentials_once,
    canonical_json,
    canonical_utc_iso,
    compute_worker_status,
    content_fingerprint,
    datetime,
    datetime_to_epoch_us,
    hashlib,
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

class DatabaseAggregates:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def create_job(self, job: Job) -> None:
        # lineage_key 缺省由 id 反推(去时间戳),保证同源快照归一组。
        lineage = job.lineage_key or _lineage_key_of(job.id)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """INSERT INTO jobs
                       (id, content_type, pipeline, collection_id, url, title,
                        domain, source, style_tags, status, progress_pct, meta,
                        published_at, created_at, updated_at, error,
                        lineage_key, is_current, source_digest, pipeline_digest, parent_job_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job.id,
                        job.content_type,
                        job.pipeline,
                        job.collection_id,
                        job.url,
                        job.title,
                        job.domain,
                        job.source,
                        json.dumps(job.style_tags, ensure_ascii=False),
                        job.status.value if isinstance(job.status, JobStatus) else job.status,
                        job.progress_pct,
                        json.dumps(job.meta, ensure_ascii=False),
                        job.published_at.isoformat() if job.published_at else None,
                        job.created_at.isoformat(),
                        job.updated_at.isoformat(),
                        job.error,
                        lineage,
                        1 if job.is_current else 0,
                        job.source_digest,
                        job.pipeline_digest,
                        job.parent_job_id,
                    ),
                )
                # 降级旧快照和证据失效必须同事务提交。
                # 否则新 current 可见时旧证据仍会被接受。
                if job.is_current and lineage:
                    superseded = self._conn.execute(
                        "SELECT id FROM jobs WHERE lineage_key=? AND id!=? AND is_current=1",
                        (lineage, job.id),
                    ).fetchall()
                    self._conn.execute(
                        "UPDATE jobs SET is_current=0 WHERE lineage_key=? AND id!=?",
                        (lineage, job.id),
                    )
                    for row in superseded:
                        self._revalidate_study_suggestion_evidence_locked(
                            job_id=str(row["id"])
                        )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def promote_lineage_current(self, lineage_key: str) -> None:
        """若某 lineage 当前无 current(如 current 被删),把剩余最新 created_at 的一版提为 current。
        幂等:已有 current 则不动。"""
        if not lineage_key:
            return
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                has = self._conn.execute(
                    "SELECT 1 FROM jobs WHERE lineage_key=? AND is_current=1 LIMIT 1",
                    (lineage_key,),
                ).fetchone()
                if has:
                    self._conn.commit()
                    return
                latest = self._conn.execute(
                    "SELECT id FROM jobs WHERE lineage_key=? ORDER BY created_at DESC LIMIT 1",
                    (lineage_key,),
                ).fetchone()
                if latest:
                    self._conn.execute(
                        "UPDATE jobs SET is_current=1 WHERE id=?", (latest["id"],)
                    )
                    self._revalidate_study_suggestion_evidence_locked(
                        job_id=str(latest["id"])
                    )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        invalid = set(fields.keys()) - _JOB_UPDATABLE
        if invalid:
            raise ValueError(f"Invalid job columns: {invalid}")
        if "is_current" in fields:
            raw_current = fields["is_current"]
            if type(raw_current) not in (bool, int) or raw_current not in (0, 1):
                raise ValueError("is_current 必须是 bool/0/1")
            fields["is_current"] = 1 if raw_current else 0
        fields["updated_at"] = _db._now_iso()
        if "style_tags" in fields:
            fields["style_tags"] = json.dumps(fields["style_tags"], ensure_ascii=False)
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        if "status" in fields and isinstance(fields["status"], JobStatus):
            fields["status"] = fields["status"].value
        if "published_at" in fields and isinstance(fields["published_at"], datetime):
            fields["published_at"] = fields["published_at"].isoformat()

        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [job_id]
        # FTS 行冗余存 title/domain/collection_id,这几项变更要同步,否则检索元数据漂移。
        fts_sync = {k: fields[k] for k in ("title", "domain", "collection_id") if k in fields}
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                before = self._conn.execute(
                    "SELECT lineage_key, is_current FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if (
                    before is not None
                    and "lineage_key" in fields
                    and fields["lineage_key"] != before["lineage_key"]
                    and int(fields.get("is_current", before["is_current"])) == 1
                ):
                    raise ValueError("current job 不允许单独变更 lineage_key")
                self._conn.execute(
                    f"UPDATE jobs SET {set_clause} WHERE id=?", values
                )
                if fts_sync:
                    fts_clause = ", ".join(f"{k}=?" for k in fts_sync)
                    self._conn.execute(
                        f"UPDATE notes_fts5 SET {fts_clause} WHERE job_id=?",
                        [("" if v is None else v) for v in fts_sync.values()] + [job_id],
                    )
                    self._conn.execute(
                        f"UPDATE note_chunks SET {fts_clause} WHERE job_id=?",
                        [("" if v is None else v) for v in fts_sync.values()] + [job_id],
                    )
                    self._conn.execute(
                        f"UPDATE note_chunks_fts5 SET {fts_clause} WHERE job_id=?",
                        [("" if v is None else v) for v in fts_sync.values()] + [job_id],
                    )
                if fields.get("is_current"):
                    current = self._conn.execute(
                        "SELECT lineage_key FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if current is not None and current["lineage_key"]:
                        superseded = self._conn.execute(
                            """SELECT id FROM jobs
                               WHERE lineage_key=? AND id!=? AND is_current=1""",
                            (current["lineage_key"], job_id),
                        ).fetchall()
                        self._conn.execute(
                            "UPDATE jobs SET is_current=0 WHERE lineage_key=? AND id!=?",
                            (current["lineage_key"], job_id),
                        )
                        for row in superseded:
                            self._revalidate_study_suggestion_evidence_locked(
                                job_id=str(row["id"])
                            )
                if {"status", "domain", "is_current", "lineage_key"} & fields.keys():
                    self._revalidate_study_suggestion_evidence_locked(
                        job_id=job_id,
                    )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def delete_job_cascade(
        self, job_id: str, collection_id: str | None = None, item_id: str | None = None
    ) -> None:
        """原子删 job:jobs 行 + FTS 索引 + ai_usage 行 + 集合计数 -1 + 摘除 glossary.occurrences 里的 job_id
        +(订阅 job)清 ingested_items 该条。全部单事务,避免两次 commit 间崩溃留孤儿。
        job_steps 经 FK ON DELETE CASCADE 连带删除。
        item_id:订阅来源 job 的去重键(从 job.meta['source_item_id'] 取);传了才清 ingested_items
        → 该条下轮订阅枚举可重新入库(彻底删除)。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._detach_study_sources_locked([job_id])
                now = _db._now_iso()
                self._conn.execute(
                    """UPDATE canonical_evidence
                       SET status='missing', invalid_reason='job_deleted',
                           validated_at=?, updated_at=?
                       WHERE job_id=?""",
                    (now, now, job_id),
                )
                self._conn.execute("DELETE FROM notes_fts5 WHERE job_id=?", (job_id,))
                self._conn.execute("DELETE FROM note_chunks WHERE job_id=?", (job_id,))
                self._conn.execute("DELETE FROM note_chunks_fts5 WHERE job_id=?", (job_id,))
                # ai_usage 无外键,不会随 jobs 行 CASCADE,须显式删,否则 token/费用行成永久悬挂孤儿。
                self._conn.execute("DELETE FROM ai_usage WHERE job_id=?", (job_id,))
                self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                if collection_id:
                    self._conn.execute(
                        "UPDATE collections SET job_count = MAX(0, job_count - 1) WHERE id=?",
                        (collection_id,),
                    )
                    if item_id:
                        self._conn.execute(
                            "DELETE FROM ingested_items WHERE collection_id=? AND item_id=?",
                            (collection_id, item_id),
                        )
                self._strip_occurrences_for_jobs([job_id])
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def delete_collection(self, collection_id: str, purge: bool = False) -> None:
        """删集合两模式。默认解绑:名下 job 的 collection_id 置 NULL(保留 job)。
        purge=True:连名下 job 一起删(jobs 行 + FTS 行 + 摘除各 job 的 glossary.occurrences;
        注:产物/MinIO 清理走既有 job 删除路径)。
        两种都清该集合 ingested_items(便于重订阅重新入库)。FTS 索引行同步处理,避免悬空行。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if purge:
                    job_rows = self._conn.execute(
                        "SELECT id FROM jobs WHERE collection_id=?", (collection_id,)
                    ).fetchall()
                    job_ids = [str(row["id"]) for row in job_rows]
                    self._detach_study_sources_locked(job_ids)
                    self._strip_occurrences_for_jobs(job_ids)
                    self._conn.execute(
                        "DELETE FROM notes_fts5 WHERE collection_id=?", (collection_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM note_chunks WHERE collection_id=?", (collection_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM note_chunks_fts5 WHERE collection_id=?", (collection_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM ai_usage WHERE job_id IN "
                        "(SELECT id FROM jobs WHERE collection_id=?)",
                        (collection_id,),
                    )
                    self._conn.execute(
                        "DELETE FROM jobs WHERE collection_id=?", (collection_id,)
                    )
                else:
                    self._conn.execute(
                        "UPDATE jobs SET collection_id=NULL WHERE collection_id=?",
                        (collection_id,),
                    )
                    self._conn.execute(
                        "UPDATE notes_fts5 SET collection_id='' WHERE collection_id=?",
                        (collection_id,),
                    )
                    self._conn.execute(
                        "UPDATE note_chunks SET collection_id='' WHERE collection_id=?",
                        (collection_id,),
                    )
                    self._conn.execute(
                        "UPDATE note_chunks_fts5 SET collection_id='' WHERE collection_id=?",
                        (collection_id,),
                    )
                self._conn.execute(
                    "DELETE FROM ingested_items WHERE collection_id=?", (collection_id,)
                )
                self._conn.execute(
                    "DELETE FROM collections WHERE id=?", (collection_id,)
                )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def rename_domain(self, old: str, new: str) -> dict[str, int]:
        """把领域键 old 原子改成 new(领域是派生键,散在 jobs/collections/glossary + notes_fts5 冗余列)。
        一个事务内迁移所有引用,任一失败回滚。返回各表迁移行数。调用方须先校验 new 合法且不冲突。"""
        if old == new:
            raise ValueError("new domain 不得与 old 相同")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for table in (
                    "jobs",
                    "collections",
                    "glossary",
                    "study_cards",
                    "study_suggestion_batches",
                    "study_suggestions",
                ):
                    if self._conn.execute(
                        f"SELECT 1 FROM {table} WHERE domain=? LIMIT 1", (new,)
                    ).fetchone():
                        raise ValueError(f"目标 domain 已存在: {new}")
                if self._conn.execute(
                    "SELECT 1 FROM study_suggestion_evidence WHERE current_domain=? LIMIT 1",
                    (new,),
                ).fetchone():
                    raise ValueError(f"目标 domain 已存在: {new}")
                affected_batches = [
                    str(row["batch_id"])
                    for row in self._conn.execute(
                        "SELECT batch_id FROM study_suggestion_batches "
                        "WHERE domain=? ORDER BY batch_id",
                        (old,),
                    ).fetchall()
                ]
                identity_impacts = self._study_identity_transition_impacts_locked(
                    batch_ids=affected_batches,
                    transition_kind="domain_rename",
                    source_concept=None,
                )
                now = self._study_suggestion_monotonic_now_locked(
                    affected_batches, _db._now_iso()
                ).isoformat()
                renamed_versions: dict[str, str] = {}
                concept_rows = self._conn.execute(
                    """SELECT term, current_definition_version_id
                       FROM glossary WHERE domain=? ORDER BY term""",
                    (old,),
                ).fetchall()
                for concept_row in concept_rows:
                    previous = self._definition_row_locked(
                        str(concept_row["current_definition_version_id"])
                    )
                    if previous is None:
                        raise ConceptConflictError("domain rename current version 不存在")
                    input_hash = hashlib.sha256(
                        json.dumps(
                            {
                                "old_domain": old,
                                "new_domain": new,
                                "term": concept_row["term"],
                                "source_version": previous["definition_version_id"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    renamed = self._insert_definition_version_locked(
                        domain=new,
                        term=str(concept_row["term"]),
                        definition=str(previous["definition"] or ""),
                        source_evidence_ids_json=previous["source_evidence_ids_json"],
                        source_set_fingerprint=previous["source_set_fingerprint"],
                        strategy="domain_rename",
                        provider=previous["provider"],
                        model=previous["model"],
                        prompt_hash=previous["prompt_hash"],
                        input_hash=input_hash,
                        supersedes_version_id=str(previous["definition_version_id"]),
                        actor="database:domain_rename",
                        created_at=now,
                    )
                    renamed_versions[str(concept_row["term"])] = str(
                        renamed["definition_version_id"]
                    )
                n_jobs = self._conn.execute(
                    "UPDATE jobs SET domain=? WHERE domain=?", (new, old)
                ).rowcount
                n_coll = self._conn.execute(
                    "UPDATE collections SET domain=? WHERE domain=?", (new, old)
                ).rowcount
                n_gloss = 0
                for term, version_id in renamed_versions.items():
                    n_gloss += self._conn.execute(
                        """UPDATE glossary
                           SET domain=?, current_definition_version_id=?,
                               lock_revision=lock_revision+1, updated_at=?
                           WHERE domain=? AND term=?""",
                        (new, version_id, now, old, term),
                    ).rowcount
                n_cards = self._conn.execute(
                    "UPDATE study_cards SET domain=?, updated_at=? WHERE domain=?",
                    (new, now, old),
                ).rowcount
                n_batches = self._conn.execute(
                    "UPDATE study_suggestion_batches SET domain=?, updated_at=? WHERE domain=?",
                    (new, now, old),
                ).rowcount
                suggestion_rows = self._conn.execute(
                    """SELECT suggestion_id, knowledge_key, card_type, front, back,
                              explanation FROM study_suggestions WHERE domain=?
                       ORDER BY suggestion_id""",
                    (old,),
                ).fetchall()
                for row in suggestion_rows:
                    self._conn.execute(
                        """UPDATE study_suggestions
                           SET domain=?, knowledge_fingerprint=?, content_fingerprint=?,
                               updated_at=? WHERE suggestion_id=?""",
                        (
                            new,
                            knowledge_fingerprint(new, str(row["knowledge_key"])),
                            content_fingerprint(
                                domain=new,
                                card_type=str(row["card_type"]),
                                front=str(row["front"]),
                                back=str(row["back"]),
                                explanation=str(row["explanation"] or ""),
                            ),
                            now,
                            row["suggestion_id"],
                        ),
                    )
                n_suggestions = len(suggestion_rows)
                n_evidence = self._conn.execute(
                    """UPDATE study_suggestion_evidence
                       SET current_domain=?, validated_at=? WHERE current_domain=?""",
                    (new, now, old),
                ).rowcount
                self._conn.execute(
                    "UPDATE notes_fts5 SET domain=? WHERE domain=?", (new, old)
                )
                self._conn.execute(
                    "UPDATE note_chunks SET domain=? WHERE domain=?", (new, old)
                )
                self._conn.execute(
                    "UPDATE note_chunks_fts5 SET domain=? WHERE domain=?", (new, old)
                )
                self._record_study_identity_transition_locked(
                    batch_ids=affected_batches,
                    transition_kind="domain_rename",
                    source_domain=old,
                    target_domain=new,
                    source_concept=None,
                    target_concept=None,
                    created_at=now,
                    impacts=identity_impacts,
                )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        return {
            "jobs": n_jobs,
            "collections": n_coll,
            "glossary": n_gloss,
            "study_cards": n_cards,
            "study_suggestion_batches": n_batches,
            "study_suggestions": n_suggestions,
            "study_suggestion_evidence": n_evidence,
            "concept_definition_versions": len(renamed_versions),
        }

    def replace_concept_occurrences_for_job(
        self,
        *,
        domain: str,
        term: str,
        job_id: str,
        evidence_ids: list[str],
    ) -> bool:
        """原子替换单 concept/job 的证据集合；完全相同返回 False。"""
        source_json, _ = _concept_source_set(evidence_ids)
        normalized_ids: list[str] = json.loads(source_json)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                concept = self._conn.execute(
                    "SELECT status FROM glossary WHERE domain=? AND term=?",
                    (domain, term),
                ).fetchone()
                if concept is None:
                    raise ConceptNotFoundError(f"concept not found: {domain}/{term}")
                if concept["status"] == "rejected":
                    raise ConceptConflictError("rejected concept 不接受 occurrence")
                job = self._conn.execute(
                    "SELECT domain FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if job is None or job["domain"] != domain:
                    raise ConceptEvidenceError("job 不存在或 domain 不属于请求 concept")
                if normalized_ids:
                    placeholders = ",".join("?" for _ in normalized_ids)
                    rows = self._conn.execute(
                        f"""SELECT evidence_id, job_id, status
                            FROM canonical_evidence
                            WHERE evidence_id IN ({placeholders})""",
                        normalized_ids,
                    ).fetchall()
                    by_id = {str(row["evidence_id"]): row for row in rows}
                    for evidence_id in normalized_ids:
                        evidence = by_id.get(evidence_id)
                        if evidence is None:
                            raise ConceptEvidenceError(
                                f"canonical evidence 不存在: {evidence_id}"
                            )
                        if evidence["job_id"] != job_id:
                            raise ConceptEvidenceError(
                                f"canonical evidence 跨 job: {evidence_id}"
                            )
                        if evidence["status"] != "valid":
                            raise ConceptEvidenceError(
                                f"canonical evidence 当前无效: {evidence_id}"
                            )
                existing_ids = [
                    str(row["evidence_id"])
                    for row in self._conn.execute(
                        """SELECT evidence_id FROM concept_occurrences
                           WHERE domain=? AND term=? AND job_id=?
                           ORDER BY evidence_id""",
                        (domain, term, job_id),
                    ).fetchall()
                ]
                if existing_ids == normalized_ids:
                    self._conn.commit()
                    return False
                if normalized_ids:
                    placeholders = ",".join("?" for _ in normalized_ids)
                    self._conn.execute(
                        f"""DELETE FROM concept_occurrences
                            WHERE domain=? AND term=? AND job_id=?
                              AND evidence_id NOT IN ({placeholders})""",
                        (domain, term, job_id, *normalized_ids),
                    )
                else:
                    self._conn.execute(
                        """DELETE FROM concept_occurrences
                           WHERE domain=? AND term=? AND job_id=?""",
                        (domain, term, job_id),
                    )
                now = _db._now_iso()
                self._conn.executemany(
                    """INSERT OR IGNORE INTO concept_occurrences
                       (domain, term, job_id, evidence_id, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        (domain, term, job_id, evidence_id, now)
                        for evidence_id in normalized_ids
                    ],
                )
                self._conn.commit()
                return True
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def replace_job_concept_occurrences(
        self,
        *,
        domain: str,
        job_id: str,
        mapping: dict[str, list[str]],
    ) -> bool:
        """原子对账一个 job 的全部 concept/evidence 映射，移除消失概念。"""
        if not isinstance(mapping, dict) or any(
            not isinstance(term, str) or not term.strip()
            for term in mapping
        ):
            raise ValueError("mapping 必须是 term 到 canonical evidence IDs 的对象")
        normalized: dict[str, list[str]] = {}
        for term, evidence_ids in mapping.items():
            source_json, _ = _concept_source_set(evidence_ids)
            normalized[term] = json.loads(source_json)
        expected = sorted(
            (term, evidence_id)
            for term, evidence_ids in normalized.items()
            for evidence_id in evidence_ids
        )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                job = self._conn.execute(
                    "SELECT domain FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if job is None or job["domain"] != domain:
                    raise ConceptEvidenceError("job 不存在或 domain 不属于请求 concept")
                if normalized:
                    terms = sorted(normalized)
                    placeholders = ",".join("?" for _ in terms)
                    rows = self._conn.execute(
                        f"""SELECT term, status FROM glossary
                            WHERE domain=? AND term IN ({placeholders})""",
                        (domain, *terms),
                    ).fetchall()
                    found_terms = {str(row["term"]) for row in rows}
                    missing = [term for term in terms if term not in found_terms]
                    if missing:
                        raise ConceptNotFoundError(
                            f"concept not found: {domain}/{missing[0]}"
                        )
                    rejected = [
                        str(row["term"]) for row in rows if row["status"] == "rejected"
                    ]
                    if rejected:
                        raise ConceptConflictError(
                            f"rejected concept 不接受 occurrence: {rejected[0]}"
                        )
                evidence_ids = sorted({item[1] for item in expected})
                if evidence_ids:
                    placeholders = ",".join("?" for _ in evidence_ids)
                    rows = self._conn.execute(
                        f"""SELECT evidence_id, job_id, status
                            FROM canonical_evidence
                            WHERE evidence_id IN ({placeholders})""",
                        evidence_ids,
                    ).fetchall()
                    by_id = {str(row["evidence_id"]): row for row in rows}
                    for evidence_id in evidence_ids:
                        evidence = by_id.get(evidence_id)
                        if evidence is None:
                            raise ConceptEvidenceError(
                                f"canonical evidence 不存在: {evidence_id}"
                            )
                        if evidence["job_id"] != job_id:
                            raise ConceptEvidenceError(
                                f"canonical evidence 跨 job: {evidence_id}"
                            )
                        if evidence["status"] != "valid":
                            raise ConceptEvidenceError(
                                f"canonical evidence 当前无效: {evidence_id}"
                            )
                existing = [
                    (str(row["term"]), str(row["evidence_id"]))
                    for row in self._conn.execute(
                        """SELECT term, evidence_id FROM concept_occurrences
                           WHERE domain=? AND job_id=?
                           ORDER BY term, evidence_id""",
                        (domain, job_id),
                    ).fetchall()
                ]
                if existing == expected:
                    self._conn.commit()
                    return False
                self._conn.execute(
                    "DELETE FROM concept_occurrences WHERE domain=? AND job_id=?",
                    (domain, job_id),
                )
                now = _db._now_iso()
                self._conn.executemany(
                    """INSERT INTO concept_occurrences
                       (domain, term, job_id, evidence_id, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        (domain, term, job_id, evidence_id, now)
                        for term, evidence_id in expected
                    ],
                )
                self._conn.commit()
                return True
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def append_concept_definition_version(
        self,
        *,
        domain: str,
        term: str,
        definition: str,
        evidence_ids: list[str],
        strategy: str,
        actor: str,
        expected_current_version_id: str,
        expected_lock_revision: int,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        input_hash: str | None = None,
        allow_locked: bool = False,
        allow_same_source_set: bool = False,
    ) -> dict:
        """append + current pointer CAS；source set 未变时默认幂等 no-op。"""
        if type(expected_lock_revision) is not int or expected_lock_revision < 0:
            raise ValueError("expected_lock_revision 必须是非负整数")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                glossary = self._conn.execute(
                    """SELECT current_definition_version_id, lock_revision,
                              definition_locked, status
                       FROM glossary WHERE domain=? AND term=?""",
                    (domain, term),
                ).fetchone()
                if glossary is None:
                    raise ConceptNotFoundError(f"concept not found: {domain}/{term}")
                if glossary["status"] == "rejected":
                    raise ConceptConflictError("rejected concept 不接受定义版本")
                if (
                    glossary["current_definition_version_id"]
                    != expected_current_version_id
                    or int(glossary["lock_revision"]) != expected_lock_revision
                ):
                    raise ConceptConflictError("concept current version 或 lock revision 已变化")
                if glossary["definition_locked"] and not allow_locked:
                    raise ConceptConflictError("concept definition 已锁定")
                source_json, fingerprint = self._validate_concept_source_evidence_locked(
                    domain=domain,
                    term=term,
                    evidence_ids=evidence_ids,
                )
                current = self._definition_row_locked(expected_current_version_id)
                if current is None:
                    raise ConceptConflictError("concept current version 不存在")
                if (
                    not allow_same_source_set
                    and current["source_set_fingerprint"] == fingerprint
                ):
                    self._conn.commit()
                    result = self._row_to_concept_definition_version(current)
                    result["created"] = False
                    return result
                now = _db._now_iso()
                if input_hash is None:
                    input_hash = hashlib.sha256(
                        json.dumps(
                            {
                                "definition": definition,
                                "evidence_ids": json.loads(source_json),
                                "strategy": strategy,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                inserted = self._insert_definition_version_locked(
                    domain=domain,
                    term=term,
                    definition=definition,
                    source_evidence_ids_json=source_json,
                    source_set_fingerprint=fingerprint,
                    strategy=strategy,
                    provider=provider,
                    model=model,
                    prompt_hash=prompt_hash,
                    input_hash=input_hash,
                    supersedes_version_id=expected_current_version_id,
                    actor=actor,
                    created_at=now,
                )
                cursor = self._conn.execute(
                    """UPDATE glossary
                       SET definition=?, current_definition_version_id=?, updated_at=?
                       WHERE domain=? AND term=?
                         AND current_definition_version_id=? AND lock_revision=?""",
                    (
                        definition,
                        inserted["definition_version_id"],
                        now,
                        domain,
                        term,
                        expected_current_version_id,
                        expected_lock_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConceptConflictError("concept current pointer CAS 失败")
                self._conn.commit()
                result = self._row_to_concept_definition_version(inserted)
                result["created"] = True
                return result
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def merge_glossary_terms(self, domain: str, src_term: str, dst_term: str) -> dict:
        """把 src 实体并入 dst,供存量清洗与前端"合并到已有词条"共用:
        occurrences 并集按 job_id 去重(dst 先)、definition 取更长者、zh_name 补空、
        src 的 term/zh_name/aliases 全部入 dst.aliases(可逆留痕)、status 取更高档
        (accepted > suggested > rejected)、is_topic/definition_locked 取或、related 并集。
        然后删 src 行。任一行不存在或 src==dst 抛 ValueError。返回合并后的行 dict。"""
        if src_term == dst_term:
            raise ValueError("src and dst are the same term")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows = {
                    r["term"]: r for r in self._conn.execute(
                        "SELECT * FROM glossary WHERE domain=? AND term IN (?,?)",
                        (domain, src_term, dst_term),
                    ).fetchall()
                }
                if src_term not in rows or dst_term not in rows:
                    missing = src_term if src_term not in rows else dst_term
                    raise ValueError(f"term not found: {missing}")
                s, d = rows[src_term], rows[dst_term]

                occs = json.loads(d["occurrences"] or "[]")
                seen_jobs = {o.get("job_id") for o in occs}
                for occurrence in json.loads(s["occurrences"] or "[]"):
                    if occurrence.get("job_id") not in seen_jobs:
                        occs.append(occurrence)
                        seen_jobs.add(occurrence.get("job_id"))

                d_def = (d["definition"] or "").strip()
                s_def = (s["definition"] or "").strip()
                definition = d_def if len(d_def) >= len(s_def) else s_def
                zh_name = (d["zh_name"] or "").strip() or (s["zh_name"] or "").strip()

                aliases = json.loads(d["aliases"] or "[]")
                candidates = json.loads(s["aliases"] or "[]") + [
                    s["term"],
                    (s["zh_name"] or "").strip(),
                ]
                for candidate in candidates:
                    if (
                        candidate
                        and candidate != dst_term
                        and candidate != zh_name
                        and candidate not in aliases
                    ):
                        aliases.append(candidate)

                related = _norm_related(json.loads(d["related"] or "[]"))
                related_terms = {relation["term"] for relation in related}
                for relation in _norm_related(json.loads(s["related"] or "[]")):
                    if relation["term"] not in related_terms:
                        related.append(relation)
                        related_terms.add(relation["term"])

                rank = self._STATUS_RANK
                status = max(
                    (d["status"], s["status"]), key=lambda value: rank.get(value, 1)
                )
                affected_batches = [
                    str(row["batch_id"])
                    for row in self._conn.execute(
                        """SELECT b.batch_id
                           FROM study_suggestion_batches b
                           WHERE b.domain=? AND (
                             EXISTS (
                               SELECT 1 FROM study_suggestion_inputs i
                               WHERE i.batch_id=b.batch_id
                                 AND i.current_concept_term=?
                             )
                             OR EXISTS (
                               SELECT 1 FROM study_suggestions s
                               WHERE s.batch_id=b.batch_id AND s.concept_term=?
                             )
                           )
                           ORDER BY b.batch_id""",
                        (domain, src_term, src_term),
                    ).fetchall()
                ]
                identity_impacts = self._study_identity_transition_impacts_locked(
                    batch_ids=affected_batches,
                    transition_kind="concept_merge",
                    source_concept=src_term,
                )
                now = self._study_suggestion_monotonic_now_locked(
                    affected_batches, _db._now_iso()
                ).isoformat()
                src_version = self._definition_row_locked(
                    str(s["current_definition_version_id"])
                )
                dst_version = self._definition_row_locked(
                    str(d["current_definition_version_id"])
                )
                if src_version is None or dst_version is None:
                    raise ConceptConflictError("merge concept current version 不存在")
                self._conn.execute(
                    """INSERT OR IGNORE INTO concept_occurrences
                       (domain, term, job_id, evidence_id, created_at)
                       SELECT domain, ?, job_id, evidence_id, created_at
                       FROM concept_occurrences
                       WHERE domain=? AND term=?""",
                    (dst_term, domain, src_term),
                )
                merge_evidence_ids = [
                    str(row["evidence_id"])
                    for row in self._conn.execute(
                        """SELECT o.evidence_id
                           FROM concept_occurrences o
                           JOIN canonical_evidence c ON c.evidence_id=o.evidence_id
                           WHERE o.domain=? AND o.term=? AND c.status='valid'
                           ORDER BY o.evidence_id""",
                        (domain, dst_term),
                    ).fetchall()
                ]
                source_json, source_fingerprint = _concept_source_set(
                    merge_evidence_ids
                )
                merge_input_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "source_version": src_version["definition_version_id"],
                            "target_version": dst_version["definition_version_id"],
                            "definition": definition,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                merged_version = self._insert_definition_version_locked(
                    domain=domain,
                    term=dst_term,
                    definition=definition,
                    source_evidence_ids_json=source_json,
                    source_set_fingerprint=source_fingerprint,
                    strategy="concept_merge",
                    provider=None,
                    model=None,
                    prompt_hash=None,
                    input_hash=merge_input_hash,
                    supersedes_version_id=str(dst_version["definition_version_id"]),
                    actor="database:concept_merge",
                    created_at=now,
                )
                self._conn.execute(
                    """UPDATE glossary SET definition=?, zh_name=?, aliases=?, occurrences=?,
                       related=?, status=?, is_topic=?, definition_locked=?,
                       current_definition_version_id=?, lock_revision=lock_revision+1,
                       updated_at=?
                       WHERE domain=? AND term=?""",
                    (
                        definition,
                        zh_name,
                        json.dumps(aliases, ensure_ascii=False),
                        json.dumps(occs, ensure_ascii=False),
                        json.dumps(related, ensure_ascii=False),
                        status,
                        1 if (d["is_topic"] or s["is_topic"]) else 0,
                        1 if (d["definition_locked"] or s["definition_locked"]) else 0,
                        merged_version["definition_version_id"],
                        now,
                        domain,
                        dst_term,
                    ),
                )
                # 指纹故意不包含展示 concept,合并只迁移可变 canonical pointer.
                self._conn.execute(
                    """UPDATE study_suggestion_inputs
                       SET current_concept_term=?
                       WHERE current_concept_term=? AND batch_id IN (
                         SELECT batch_id FROM study_suggestion_batches WHERE domain=?
                       )""",
                    (dst_term, src_term, domain),
                )
                self._conn.execute(
                    """UPDATE study_suggestions SET concept_term=?, updated_at=?
                       WHERE domain=? AND concept_term=?""",
                    (dst_term, now, domain, src_term),
                )
                self._conn.execute(
                    """UPDATE study_cards SET concept_term=?, updated_at=?
                       WHERE domain=? AND concept_term=?""",
                    (dst_term, now, domain, src_term),
                )
                self._conn.execute(
                    "DELETE FROM glossary WHERE domain=? AND term=?", (domain, src_term)
                )
                self._record_study_identity_transition_locked(
                    batch_ids=affected_batches,
                    transition_kind="concept_merge",
                    source_domain=domain,
                    target_domain=domain,
                    source_concept=src_term,
                    target_concept=dst_term,
                    created_at=now,
                    impacts=identity_impacts,
                )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        merged = self.get_glossary_term(domain, dst_term)
        assert merged is not None
        return merged

    def index_job_notes(
        self,
        job_id: str,
        note_type: str,
        title: str,
        body: str,
        content_type: str = "",
        domain: str = "",
        collection_id: str = "",
        supersede_note_types: list[str] | None = None,
        canonical_evidence: list[dict] | None = None,
    ) -> None:
        """原子替换某 job/note_type 的全文与证据块索引,失败时保留旧版本。"""
        with self._lock:
            # notes_fts5 与两张 chunk 表是一个可见版本。任一写入失败都回滚删除和
            # 已插入行,避免后续无关 commit 固化半成品;同一输入可安全重试。
            with self._conn:
                for stale_type in set(supersede_note_types or []) - {note_type}:
                    self._conn.execute(
                        "DELETE FROM notes_fts5 WHERE job_id=? AND note_type=?",
                        (job_id, stale_type),
                    )
                    self._conn.execute(
                        "DELETE FROM note_chunks WHERE job_id=? AND note_type=?",
                        (job_id, stale_type),
                    )
                    self._conn.execute(
                        "DELETE FROM note_chunks_fts5 WHERE job_id=? AND note_type=?",
                        (job_id, stale_type),
                    )
                    self._revalidate_study_suggestion_evidence_locked(
                        job_id=job_id, note_type=stale_type
                    )
                    now = _db._now_iso()
                    self._conn.execute(
                        """UPDATE canonical_evidence
                           SET status='missing', invalid_reason='note_superseded',
                               validated_at=?, updated_at=?
                           WHERE job_id=? AND note_type=?""",
                        (now, now, job_id, stale_type),
                    )
                self._conn.execute(
                    "DELETE FROM notes_fts5 WHERE job_id=? AND note_type=?",
                    (job_id, note_type),
                )
                self._conn.execute(
                    """INSERT INTO notes_fts5
                       (job_id, content_type, note_type, collection_id, domain,
                        title, body)
                       VALUES (?,?,?,?,?,?,?)""",
                    (job_id, content_type, note_type, collection_id or "",
                     domain or "", title or "", body or ""),
                )
                self._replace_note_chunks_locked(
                    job_id=job_id,
                    note_type=note_type,
                    title=title,
                    body=body,
                    content_type=content_type,
                    domain=domain,
                    collection_id=collection_id,
                )
                if canonical_evidence is not None:
                    self._replace_canonical_evidence_locked(
                        job_id=job_id,
                        note_type=note_type,
                        records=canonical_evidence,
                    )

    def create_study_suggestion_batch(
        self,
        *,
        request_id: str,
        domain: str,
        job_ids: list[str] | None = None,
        concept_terms: list[str] | None = None,
        max_cards: int = 10,
        provider: str = DEFAULT_AI_PROVIDER,
        model: str = DEFAULT_AI_MODEL,
        prompt_snapshot: dict[str, object] | None = None,
        deadline_seconds: int = 1_800,
    ) -> dict:
        """在一个快照事务中固化候选的 chunk 和 concept 输入."""
        normalized_request_id = require_external_request_id(request_id)
        normalized_domain = require_identifier(domain, "domain", max_length=256)
        normalized_provider = require_identifier(provider, "provider", max_length=128)
        normalized_model = require_identifier(model, "model", max_length=256)
        normalized_max = require_plain_int(
            max_cards,
            "max_cards",
            minimum=1,
            maximum=MAX_GENERATED_CARDS,
        )
        normalized_deadline = require_plain_int(
            deadline_seconds,
            "deadline_seconds",
            minimum=60,
            maximum=86_400,
        )

        def normalize_values(
            values: list[str] | None,
            field: str,
            *,
            limit: int = 100,
        ) -> list[str]:
            if values is None:
                return []
            if not isinstance(values, list) or len(values) > limit:
                raise ValueError(f"{field} 最多 {limit} 项")
            normalized = [require_identifier(value, field) for value in values]
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"{field} 不得重复")
            return sorted(normalized)

        normalized_jobs = normalize_values(job_ids, "job_ids")
        normalized_concepts = normalize_values(concept_terms, "concept_terms")
        prompt = prompt_snapshot or resolve_study_suggestion_prompt()
        validate_study_suggestion_prompt_snapshot(prompt)
        prompt = dict(prompt)
        generator = study_suggestion_generator_fingerprint(prompt)
        if (
            not isinstance(generator, str)
            or len(generator) != 71
            or not generator.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in generator[7:])
        ):
            raise ValueError("generator_fingerprint 必须是 sha256:<小写64hex>")
        request_payload = {
            "operation_kind": "batch_create",
            "request_id": normalized_request_id,
            "domain": normalized_domain,
            "job_ids": normalized_jobs,
            "concept_terms": normalized_concepts,
            "max_cards": normalized_max,
            "provider": normalized_provider,
            "model": normalized_model,
            "generator_fingerprint": generator,
            "prompt_snapshot": prompt,
            "deadline_seconds": normalized_deadline,
        }
        request_json = canonical_json(request_payload)
        request_fingerprint = sha256_text(request_json)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                replay = self._study_suggestion_operation_replay_locked(
                    normalized_request_id, request_fingerprint
                )
                if replay is not None:
                    self._conn.commit()
                    return replay
                now_dt = self._study_suggestion_monotonic_now_locked([], _db.utc_now())
                now = now_dt.isoformat()
                deadline = now_dt + timedelta(seconds=normalized_deadline)

                if normalized_jobs:
                    placeholders = ",".join("?" for _ in normalized_jobs)
                    job_rows = self._conn.execute(
                        f"""SELECT id, domain, status, is_current FROM jobs
                            WHERE id IN ({placeholders}) ORDER BY id""",
                        normalized_jobs,
                    ).fetchall()
                    found_jobs = {str(row["id"]): row for row in job_rows}
                    missing = [job_id for job_id in normalized_jobs if job_id not in found_jobs]
                    if missing:
                        raise StudySuggestionNotFoundError(
                            "study_suggestion_job_not_found",
                            f"job not found: {missing[0]}",
                        )
                    invalid = [
                        job_id
                        for job_id, row in found_jobs.items()
                        if row["domain"] != normalized_domain
                        or row["status"] != "done"
                        or int(row["is_current"]) != 1
                    ]
                    if invalid:
                        raise StudySuggestionConflictError(
                            "study_suggestion_job_ineligible",
                            f"job is not current/done in domain: {invalid[0]}",
                        )
                    chunk_rows = self._conn.execute(
                        f"""SELECT n.* FROM note_chunks n
                            WHERE n.job_id IN ({placeholders})
                            ORDER BY n.job_id, n.note_type, n.chunk_index LIMIT 101""",
                        normalized_jobs,
                    ).fetchall()
                else:
                    job_rows = self._conn.execute(
                        """SELECT id, domain, status, is_current FROM jobs
                           WHERE domain=? AND status='done' AND is_current=1
                           ORDER BY id""",
                        (normalized_domain,),
                    ).fetchall()
                    chunk_rows = self._conn.execute(
                        """SELECT n.* FROM note_chunks n
                           JOIN jobs j ON j.id=n.job_id
                           WHERE j.domain=? AND j.status='done' AND j.is_current=1
                             AND n.domain=?
                           ORDER BY n.job_id, n.note_type, n.chunk_index LIMIT 101""",
                        (normalized_domain, normalized_domain),
                    ).fetchall()
                if not job_rows:
                    raise ValueError("指定领域没有可用的 current/done job")
                invalid_chunk = next(
                    (
                        str(row["chunk_id"])
                        for row in chunk_rows
                        if str(row["domain"]) != normalized_domain
                    ),
                    None,
                )
                if invalid_chunk is not None:
                    raise StudySuggestionConflictError(
                        "study_suggestion_chunk_domain_mismatch",
                        f"note chunk is outside requested domain: {invalid_chunk}",
                    )

                selected_chunks: list[sqlite3.Row] = []
                selected_bytes = 0
                for row in chunk_rows:
                    body = str(row["body"] or "")
                    if not body:
                        continue
                    size = len(body.encode("utf-8"))
                    if len(selected_chunks) >= 100 or selected_bytes + size > 512 * 1024:
                        break
                    selected_chunks.append(row)
                    selected_bytes += size
                if not selected_chunks:
                    raise ValueError("指定输入没有可用的 note chunk")

                if normalized_concepts:
                    placeholders = ",".join("?" for _ in normalized_concepts)
                    concept_rows = self._conn.execute(
                        f"""SELECT term, status FROM glossary
                            WHERE domain=? AND term IN ({placeholders}) ORDER BY term""",
                        [normalized_domain, *normalized_concepts],
                    ).fetchall()
                    found_concepts = {str(row["term"]): str(row["status"]) for row in concept_rows}
                    missing = [term for term in normalized_concepts if term not in found_concepts]
                    if missing:
                        raise StudySuggestionNotFoundError(
                            "study_suggestion_concept_not_found",
                            f"concept not found: {missing[0]}",
                        )
                    rejected = [term for term, status in found_concepts.items() if status != "accepted"]
                    if rejected:
                        raise StudySuggestionConflictError(
                            "study_suggestion_concept_unavailable",
                            f"concept is not accepted: {rejected[0]}",
                        )
                    selected_concepts = sorted(found_concepts)
                else:
                    selected_concepts = [
                        str(row[0])
                        for row in self._conn.execute(
                            """SELECT term FROM glossary
                               WHERE domain=? AND status='accepted'
                               ORDER BY term LIMIT 100""",
                            (normalized_domain,),
                        ).fetchall()
                    ]

                chunk_facts = []
                for row in selected_chunks:
                    try:
                        locator = json.loads(str(row["evidence_json"] or "{}"))
                    except (json.JSONDecodeError, TypeError):
                        locator = {}
                    chunk_facts.append(
                        {
                            "chunk_id": str(row["chunk_id"]),
                            "job_id": str(row["job_id"]),
                            "note_type": str(row["note_type"]),
                            "domain": str(row["domain"]),
                            "title": str(row["title"] or ""),
                            "section": str(row["section"] or ""),
                            "body_sha256": sha256_text(str(row["body"])),
                            "locator": locator,
                        }
                    )
                input_fingerprint = payload_fingerprint(
                    {
                        "domain": normalized_domain,
                        "chunks": chunk_facts,
                        "concept_terms": selected_concepts,
                        "max_cards": normalized_max,
                        "provider": normalized_provider,
                        "model": normalized_model,
                        "generator_fingerprint": generator,
                        "prompt_snapshot": prompt,
                    }
                )
                existing = self._conn.execute(
                    """SELECT * FROM study_suggestion_batches
                       WHERE domain=? AND input_fingerprint=?""",
                    (normalized_domain, input_fingerprint),
                ).fetchone()
                if existing is not None:
                    outcome = self._row_to_study_suggestion_batch(existing)
                    operation_now = self._study_suggestion_monotonic_now_locked(
                        [str(existing["batch_id"])], now_dt
                    ).isoformat()
                    self._insert_study_suggestion_operation_locked(
                        request_id=normalized_request_id,
                        request_fingerprint=request_fingerprint,
                        operation_kind="batch_create",
                        batch_id=str(existing["batch_id"]),
                        request_json=request_json,
                        outcome=outcome,
                        created_at=operation_now,
                    )
                    self._conn.commit()
                    return outcome

                batch_id = f"ssb_{uuid.uuid4().hex}"
                task_id = f"study-suggestions:{uuid.uuid4().hex}"
                evidence_payloads: list[dict] = []
                input_rows: list[tuple] = []
                evidence_rows: list[tuple] = []
                ordinal = 0
                for row in selected_chunks:
                    input_id = f"ssi_{uuid.uuid4().hex}"
                    evidence_id = f"sse_{uuid.uuid4().hex}"
                    body = str(row["body"])
                    body_hash = sha256_text(body)
                    input_hash = payload_fingerprint(
                        {
                            "kind": "evidence",
                            "job_id": row["job_id"],
                            "chunk_id": row["chunk_id"],
                            "body_sha256": body_hash,
                        }
                    )
                    try:
                        locator = json.loads(str(row["evidence_json"] or "{}"))
                    except (json.JSONDecodeError, TypeError):
                        locator = {}
                    locator_json = canonical_json(locator)
                    input_rows.append(
                        (input_id, batch_id, ordinal, "evidence", None, None, input_hash, now)
                    )
                    evidence_rows.append(
                        (
                            evidence_id, batch_id, input_id, str(row["job_id"]),
                            str(row["chunk_id"]), str(row["note_type"]), normalized_domain,
                            normalized_domain, str(row["title"] or ""),
                            str(row["section"] or ""), body, body_hash, locator_json,
                            "valid", None, now, now,
                        )
                    )
                    evidence_payloads.append(
                        {
                            "evidence_id": evidence_id,
                            "title": str(row["title"] or ""),
                            "section": str(row["section"] or ""),
                            "untrusted_body": body,
                        }
                    )
                    ordinal += 1
                concept_payloads: list[dict] = []
                for term in selected_concepts:
                    input_id = f"ssi_{uuid.uuid4().hex}"
                    input_hash = payload_fingerprint({"kind": "concept", "term": term})
                    input_rows.append(
                        (input_id, batch_id, ordinal, "concept", term, term, input_hash, now)
                    )
                    concept_payloads.append({"input_id": input_id, "term": term})
                    ordinal += 1
                llm_request = {
                    "schema_version": 1,
                    "batch_id": batch_id,
                    "max_cards": normalized_max,
                    "domain": normalized_domain,
                    "concepts": concept_payloads,
                    "evidence": evidence_payloads,
                    "prompt_snapshot": prompt,
                }
                self._conn.execute(
                    """INSERT INTO study_suggestion_batches
                       (batch_id, domain, status, revision, attempt,
                        generator_fingerprint, input_fingerprint, task_id, provider,
                        model, max_cards, llm_request_json, result_json, error_code,
                        error_message, deadline_at, deadline_at_epoch_us,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id, normalized_domain, "pending_enqueue", 1, 1,
                        generator, input_fingerprint, task_id, normalized_provider,
                        normalized_model, normalized_max, canonical_json(llm_request),
                        None, None, None, deadline.isoformat(),
                        datetime_to_epoch_us(deadline, "deadline_at"), now, now,
                    ),
                )
                self._conn.executemany(
                    """INSERT INTO study_suggestion_inputs
                       (input_id, batch_id, ordinal, kind, concept_term_snapshot,
                        current_concept_term, input_fingerprint, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    input_rows,
                )
                self._conn.executemany(
                    """INSERT INTO study_suggestion_evidence
                       (evidence_id, batch_id, input_id, job_id, chunk_id, note_type,
                        source_domain_snapshot, current_domain, title_snapshot,
                        section_snapshot, body_snapshot, body_sha256, locator_json,
                        status, invalid_reason, validated_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    evidence_rows,
                )
                created_row = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if created_row is None:
                    raise RuntimeError("study suggestion batch disappeared inside transaction")
                outcome = self._row_to_study_suggestion_batch(created_row)
                self._insert_study_suggestion_operation_locked(
                    request_id=normalized_request_id,
                    request_fingerprint=request_fingerprint,
                    operation_kind="batch_create",
                    batch_id=batch_id,
                    request_json=request_json,
                    outcome=outcome,
                    created_at=now,
                )
                self._conn.commit()
                return outcome
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def materialize_study_suggestions(
        self,
        batch_id: str,
        *,
        task_id: str,
        result: object,
    ) -> list[dict]:
        """严格校验 AI 输出并原子物化整批候选."""
        normalized_batch = require_identifier(batch_id, "batch_id")
        normalized_task = require_identifier(task_id, "task_id")
        if isinstance(result, str):
            try:
                parsed_result: object = json.loads(result)
            except json.JSONDecodeError as exc:
                raise ValueError("AI 输出不是有效 JSON") from exc
        else:
            parsed_result = result
        canonical_result = canonical_json(parsed_result)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                batch = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                if batch is None:
                    raise StudySuggestionNotFoundError(
                        "study_suggestion_batch_not_found", "batch not found"
                    )
                if batch["task_id"] != normalized_task:
                    raise StudySuggestionConflictError(
                        "study_suggestion_task_stale", "task result no longer belongs to batch"
                    )
                if batch["status"] == "ready":
                    if batch["result_json"] != canonical_result:
                        raise StudySuggestionConflictError(
                            "study_suggestion_result_conflict",
                            "ready batch received a different result",
                        )
                    expected_revision = int(batch["revision"]) - 1
                    request_id, request_json, fingerprint = (
                        self._study_suggestion_lifecycle_operation_payload(
                            operation_kind="batch_ready",
                            batch_id=normalized_batch,
                            task_id=normalized_task,
                            attempt=int(batch["attempt"]),
                            expected_revision=expected_revision,
                            details={"result_sha256": sha256_text(canonical_result)},
                        )
                    )
                    replay = self._study_suggestion_operation_replay_locked(
                        request_id, fingerprint
                    )
                    current = self._row_to_study_suggestion_batch(batch)
                    if not self._study_suggestion_lifecycle_replay_matches_current(
                        request_id=request_id,
                        batch_id=normalized_batch,
                        replay=replay,
                        current=current,
                    ):
                        raise StudySuggestionConflictError(
                            "study_suggestion_lifecycle_conflict",
                            "ready lifecycle operation is missing or inconsistent",
                        )
                    items = self._list_study_suggestions_locked(
                        batch_id=normalized_batch, domain=None, status=None, limit=200, offset=0
                    )[1]
                    self._conn.commit()
                    return items
                if batch["status"] != "queued":
                    raise StudySuggestionConflictError(
                        "study_suggestion_batch_not_queued", "batch is not queued"
                    )
                revision = int(batch["revision"])
                if revision == MAX_SQLITE_INTEGER:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_exhausted", "batch revision is exhausted"
                    )
                request_id, request_json, fingerprint = (
                    self._study_suggestion_lifecycle_operation_payload(
                        operation_kind="batch_ready",
                        batch_id=normalized_batch,
                        task_id=normalized_task,
                        attempt=int(batch["attempt"]),
                        expected_revision=revision,
                        details={"result_sha256": sha256_text(canonical_result)},
                    )
                )
                if self._study_suggestion_operation_replay_locked(
                    request_id, fingerprint
                ) is not None:
                    raise StudySuggestionConflictError(
                        "study_suggestion_lifecycle_conflict",
                        "ready operation exists before batch transition",
                    )
                now = self._study_suggestion_monotonic_now_locked(
                    [normalized_batch], _db._now_iso()
                ).isoformat()
                evidence_rows = self._conn.execute(
                    "SELECT * FROM study_suggestion_evidence WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchall()
                for evidence_row in evidence_rows:
                    self._assert_study_suggestion_evidence_row_current_locked(
                        evidence_row,
                        expected_domain=str(batch["domain"]),
                    )
                evidence = {str(row["evidence_id"]): row for row in evidence_rows}
                concept_rows = self._conn.execute(
                    """SELECT input_id, current_concept_term FROM study_suggestion_inputs
                       WHERE batch_id=? AND kind='concept'""",
                    (normalized_batch,),
                ).fetchall()
                concepts = {str(row["input_id"]): row["current_concept_term"] for row in concept_rows}
                parsed = parse_ai_suggestions(
                    parsed_result,
                    max_cards=int(batch["max_cards"]),
                    evidence_ids=set(evidence),
                    concept_input_ids=set(concepts),
                )
                staged: list[dict] = []
                seen_knowledge: set[str] = set()
                seen_content: set[str] = set()
                for ordinal, item in enumerate(parsed):
                    for ref in item["evidence"]:
                        evidence_row = evidence[ref["evidence_id"]]
                        if evidence_row["status"] != "valid":
                            raise StudySuggestionConflictError(
                                "study_suggestion_evidence_unavailable",
                                "AI result references non-current evidence",
                            )
                        if ref["quote"] not in str(evidence_row["body_snapshot"]):
                            raise ValueError("AI quote 不是证据快照的原文子串")
                    concept_term = (
                        concepts.get(item["concept_input_id"])
                        if item["concept_input_id"] is not None
                        else None
                    )
                    if concept_term is not None:
                        concept = self._conn.execute(
                            "SELECT status FROM glossary WHERE domain=? AND term=?",
                            (batch["domain"], concept_term),
                        ).fetchone()
                        if concept is None or concept["status"] != "accepted":
                            raise StudySuggestionConflictError(
                                "study_suggestion_concept_unavailable",
                                f"concept is not accepted: {concept_term}",
                            )
                    knowledge_hash = knowledge_fingerprint(
                        str(batch["domain"]), item["knowledge_key"]
                    )
                    content_hash = content_fingerprint(
                        domain=str(batch["domain"]),
                        card_type=item["card_type"],
                        front=item["front"],
                        back=item["back"],
                        explanation=item["explanation"],
                    )
                    if knowledge_hash in seen_knowledge or content_hash in seen_content:
                        raise ValueError("AI 输出包含重复知识或卡片内容")
                    seen_knowledge.add(knowledge_hash)
                    seen_content.add(content_hash)
                    staged.append(
                        {
                            **item,
                            "suggestion_id": f"ss_{uuid.uuid4().hex}",
                            "ordinal": ordinal,
                            "concept_term": concept_term,
                            "knowledge_fingerprint": knowledge_hash,
                            "content_fingerprint": content_hash,
                        }
                    )
                for item in staged:
                    self._conn.execute(
                        """INSERT INTO study_suggestions
                           (suggestion_id, batch_id, ordinal, status, revision, domain,
                            concept_term, knowledge_key, card_type, front, back,
                            explanation, knowledge_fingerprint, content_fingerprint,
                            accepted_card_id, rejection_reason, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            item["suggestion_id"], normalized_batch, item["ordinal"],
                            "suggested", 1, batch["domain"], item["concept_term"],
                            item["knowledge_key"], item["card_type"], item["front"],
                            item["back"], item["explanation"],
                            item["knowledge_fingerprint"], item["content_fingerprint"],
                            None, None, now, now,
                        ),
                    )
                    for ref_ordinal, ref in enumerate(item["evidence"]):
                        self._conn.execute(
                            """INSERT INTO study_suggestion_evidence_links
                               (batch_id, suggestion_id, evidence_id, ordinal,
                                quote_snapshot, quote_sha256, created_at)
                               VALUES (?,?,?,?,?,?,?)""",
                            (
                                normalized_batch, item["suggestion_id"], ref["evidence_id"],
                                ref_ordinal, ref["quote"], sha256_text(ref["quote"]), now,
                            ),
                        )
                changed = self._conn.execute(
                    """UPDATE study_suggestion_batches
                       SET status='ready', revision=revision+1, result_json=?,
                           error_code=NULL, error_message=NULL, updated_at=?
                       WHERE batch_id=? AND status='queued' AND task_id=? AND revision=?""",
                    (canonical_result, now, normalized_batch, normalized_task, revision),
                )
                if changed.rowcount != 1:
                    raise StudySuggestionConflictError(
                        "study_suggestion_batch_state_conflict",
                        "batch task/status/revision no longer matches",
                    )
                updated = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                outcome = self._row_to_study_suggestion_batch(updated)
                self._insert_study_suggestion_operation_locked(
                    request_id=request_id,
                    request_fingerprint=fingerprint,
                    operation_kind="batch_ready",
                    batch_id=normalized_batch,
                    request_json=request_json,
                    outcome=outcome,
                    created_at=now,
                )
                items = self._list_study_suggestions_locked(
                    batch_id=normalized_batch, domain=None, status=None, limit=200, offset=0
                )[1]
                self._conn.commit()
                return items
            except sqlite3.IntegrityError as exc:
                if self._conn.in_transaction:
                    self._conn.rollback()
                detail = str(exc)
                duplicate_constraints = (
                    "study_suggestions.domain, study_suggestions.knowledge_fingerprint",
                    "study_suggestions.domain, study_suggestions.content_fingerprint",
                )
                if any(constraint in detail for constraint in duplicate_constraints):
                    raise StudySuggestionConflictError(
                        "study_suggestion_duplicate",
                        "suggestion knowledge/content fingerprint already exists",
                    ) from exc
                raise StudySuggestionConflictError(
                    "study_suggestion_constraint_conflict",
                    "suggestion materialization violated a committed invariant",
                ) from exc
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def apply_study_suggestion_operations(
        self,
        *,
        request_id: str,
        batch_id: str,
        items: object,
        fault_injector: StudySuggestionFaultInjector | None = None,
    ) -> dict:
        """在一个 IMMEDIATE 事务中编辑,接受或拒绝最多 100 项."""
        normalized_request = require_external_request_id(request_id)
        normalized_batch = require_identifier(batch_id, "batch_id")
        normalized_items = validate_operation_items(items)
        payload = operation_payload(
            request_id=normalized_request,
            batch_id=normalized_batch,
            items=normalized_items,
        )
        payload["operation_kind"] = "suggestion_review"
        request_json = canonical_json(payload)
        request_fingerprint = sha256_text(request_json)

        def inject(stage: str) -> None:
            if fault_injector is not None:
                fault_injector(stage)

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                replay = self._study_suggestion_operation_replay_locked(
                    normalized_request, request_fingerprint
                )
                if replay is not None:
                    self._conn.commit()
                    return replay
                batch = self._conn.execute(
                    "SELECT status, domain FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                if batch is None:
                    raise StudySuggestionNotFoundError(
                        "study_suggestion_batch_not_found", "batch not found"
                    )
                if batch["status"] != "ready":
                    raise StudySuggestionConflictError(
                        "study_suggestion_batch_not_ready", "batch is not ready"
                    )
                now_dt = self._study_suggestion_monotonic_now_locked(
                    [normalized_batch], _db.utc_now()
                )
                now = now_dt.isoformat()
                now_epoch = datetime_to_epoch_us(now_dt)
                created_cards: list[dict] = []
                touched_ids: list[str] = []
                for item in normalized_items:
                    suggestion_id = item["suggestion_id"]
                    row = self._conn.execute(
                        "SELECT * FROM study_suggestions WHERE suggestion_id=?",
                        (suggestion_id,),
                    ).fetchone()
                    if row is None or row["batch_id"] != normalized_batch:
                        raise StudySuggestionNotFoundError(
                            "study_suggestion_not_found",
                            f"suggestion not found in batch: {suggestion_id}",
                        )
                    if row["status"] != "suggested":
                        raise StudySuggestionConflictError(
                            "study_suggestion_terminal",
                            f"suggestion is already {row['status']}: {suggestion_id}",
                        )
                    expected_revision = int(item["expected_revision"])
                    if int(row["revision"]) != expected_revision:
                        raise StudySuggestionConflictError(
                            "study_suggestion_revision_stale",
                            f"suggestion revision is stale: {suggestion_id}",
                        )
                    if expected_revision == MAX_SQLITE_INTEGER:
                        raise StudySuggestionConflictError(
                            "study_suggestion_revision_exhausted",
                            f"suggestion revision is exhausted: {suggestion_id}",
                        )
                    action = str(item["action"])
                    patch = dict(item["patch"])
                    concept_term = row["concept_term"]
                    if "concept_term" in patch:
                        raw_concept = patch["concept_term"]
                        if raw_concept is None or (
                            isinstance(raw_concept, str) and not raw_concept.strip()
                        ):
                            concept_term = None
                        else:
                            concept_term = require_identifier(
                                raw_concept, "concept_term", max_length=256
                            )
                    if action != "reject" and concept_term is not None:
                        concept = self._conn.execute(
                            """SELECT status FROM glossary
                               WHERE domain=? AND term=?""",
                            (row["domain"], concept_term),
                        ).fetchone()
                        if concept is None or concept["status"] != "accepted":
                            raise StudySuggestionConflictError(
                                "study_suggestion_concept_unavailable",
                                f"concept is not accepted: {concept_term}",
                            )

                    if action == "reject":
                        reason = item["reason"] or "user_rejected"
                        changed = self._conn.execute(
                            """UPDATE study_suggestions
                               SET status='rejected', revision=revision+1,
                                   rejection_reason=?, updated_at=?
                               WHERE suggestion_id=? AND status='suggested' AND revision=?""",
                            (reason, now, suggestion_id, expected_revision),
                        )
                    else:
                        card_type, front, back, explanation = validate_card_content(
                            card_type=patch.get("card_type", row["card_type"]),
                            front=patch.get("front", row["front"]),
                            back=patch.get("back", row["back"]),
                            explanation=patch.get("explanation", row["explanation"]),
                        )
                        content_hash = content_fingerprint(
                            domain=str(row["domain"]),
                            card_type=card_type,
                            front=front,
                            back=back,
                            explanation=explanation,
                        )
                        duplicate = self._conn.execute(
                            """SELECT suggestion_id FROM study_suggestions
                               WHERE domain=? AND content_fingerprint=?
                                 AND suggestion_id<>? LIMIT 1""",
                            (row["domain"], content_hash, suggestion_id),
                        ).fetchone()
                        if duplicate is not None:
                            raise StudySuggestionConflictError(
                                "study_suggestion_duplicate",
                                "edited card content duplicates an existing suggestion",
                            )
                        if action == "edit":
                            changed = self._conn.execute(
                                """UPDATE study_suggestions
                                   SET revision=revision+1, concept_term=?, card_type=?,
                                       front=?, back=?, explanation=?, content_fingerprint=?,
                                       updated_at=?
                                   WHERE suggestion_id=? AND status='suggested' AND revision=?""",
                                (
                                    concept_term, card_type, front, back, explanation,
                                    content_hash, now, suggestion_id, expected_revision,
                                ),
                            )
                        else:
                            evidence = self._assert_study_suggestion_evidence_current_locked(
                                row
                            )
                            if self._study_card_content_duplicate_locked(
                                domain=str(row["domain"]),
                                card_type=card_type,
                                front=front,
                                back=back,
                                explanation=explanation,
                            ):
                                raise StudySuggestionConflictError(
                                    "study_suggestion_card_duplicate",
                                    "an equivalent study card already exists",
                                )
                            card_id = f"sc_{uuid.uuid4().hex}"
                            job_ids = {entry["job_id"] for entry in evidence}
                            card_job_id = next(iter(job_ids)) if len(job_ids) == 1 else None
                            self._conn.execute(
                                """INSERT INTO study_cards
                                   (card_id, domain, job_id, concept_term, card_type,
                                    front, back, explanation, evidence_json, status,
                                    source, revision, created_at, updated_at)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    card_id, row["domain"], card_job_id, concept_term,
                                    card_type, front, back, explanation,
                                    canonical_json(evidence), "active",
                                    f"suggestion:{suggestion_id}", 1, now, now,
                                ),
                            )
                            inject(f"after_card:{suggestion_id}")
                            self._conn.execute(
                                """INSERT INTO study_reviews
                                   (card_id, due_at, due_at_epoch_us, interval_days,
                                    ease, repetitions, lapses, updated_at)
                                   VALUES (?,?,?,?,?,?,?,?)""",
                                (card_id, now, now_epoch, 0, 2.5, 0, 0, now),
                            )
                            inject(f"after_due:{suggestion_id}")
                            changed = self._conn.execute(
                                """UPDATE study_suggestions
                                   SET status='accepted', revision=revision+1,
                                       concept_term=?, card_type=?, front=?, back=?,
                                       explanation=?, content_fingerprint=?,
                                       accepted_card_id=?, updated_at=?
                                   WHERE suggestion_id=? AND status='suggested' AND revision=?""",
                                (
                                    concept_term, card_type, front, back, explanation,
                                    content_hash, card_id, now, suggestion_id,
                                    expected_revision,
                                ),
                            )
                            card = self.get_study_card(card_id)
                            if card is None:
                                raise RuntimeError("accepted study card disappeared in transaction")
                            created_cards.append(card)
                    if changed.rowcount != 1:
                        raise StudySuggestionConflictError(
                            "study_suggestion_revision_stale",
                            f"suggestion revision changed: {suggestion_id}",
                        )
                    inject(f"after_suggestion:{suggestion_id}")
                    touched_ids.append(suggestion_id)

                updated_items = []
                for suggestion_id in touched_ids:
                    updated = self._conn.execute(
                        "SELECT * FROM study_suggestions WHERE suggestion_id=?",
                        (suggestion_id,),
                    ).fetchone()
                    if updated is None:
                        raise RuntimeError("study suggestion disappeared in transaction")
                    updated_items.append(self._row_to_study_suggestion_locked(updated))
                outcome = {
                    "batch_id": normalized_batch,
                    "items": updated_items,
                    "cards": created_cards,
                }
                self._insert_study_suggestion_operation_locked(
                    request_id=normalized_request,
                    request_fingerprint=request_fingerprint,
                    operation_kind="suggestion_review",
                    batch_id=normalized_batch,
                    request_json=request_json,
                    outcome=outcome,
                    created_at=now,
                )
                inject("after_operation")
                inject("before_commit")
                self._conn.commit()
                return outcome
            except sqlite3.IntegrityError as exc:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise StudySuggestionConflictError(
                    "study_suggestion_constraint_conflict",
                    "suggestion operation conflicts with a committed fact",
                ) from exc
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def record_study_review(
        self,
        *,
        request_id: str,
        card_id: str,
        grade: str,
        expected_revision: int,
        response_ms: int | None = None,
        reviewed_at: datetime | str | None = None,
        fault_injector: StudyFaultInjector | None = None,
    ) -> dict:
        """在一个 IMMEDIATE 事务内完成幂等检查,CAS,调度和日志."""
        normalized_request_id, normalized_grade = validate_review_request(
            request_id=request_id,
            card_id=card_id,
            grade=grade,
            response_ms=response_ms,
            expected_revision=expected_revision,
        )
        fingerprint = review_request_fingerprint(
            card_id=card_id,
            grade=normalized_grade,
            response_ms=response_ms,
            expected_revision=expected_revision,
        )
        reviewed_dt = _db.utc_now() if reviewed_at is None else require_aware_utc(
            reviewed_at, "reviewed_at"
        )
        reviewed_iso = reviewed_dt.isoformat()
        reviewed_epoch = datetime_to_epoch_us(reviewed_dt, "reviewed_at")

        def inject(stage: str) -> None:
            if fault_injector is not None:
                fault_injector(stage)

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    """SELECT request_fingerprint, outcome_json
                       FROM study_review_logs WHERE request_id=?""",
                    (normalized_request_id,),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != fingerprint:
                        raise StudyConflictError(
                            "study_request_id_conflict",
                            "request_id was already used with a different payload",
                        )
                    outcome = json.loads(existing["outcome_json"])
                    self._conn.commit()
                    return outcome

                row = self._conn.execute(
                    """SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                              c.front, c.back, c.explanation, c.evidence_json, c.status,
                              c.source, c.revision, c.created_at, c.updated_at,
                              r.due_at AS review_due_at, r.due_at_epoch_us,
                              r.interval_days, r.ease, r.repetitions, r.lapses,
                              r.last_grade, r.last_reviewed_at,
                              r.updated_at AS review_updated_at
                       FROM study_cards c
                       LEFT JOIN study_reviews r ON r.card_id=c.card_id
                       WHERE c.card_id=?""",
                    (card_id,),
                ).fetchone()
                if row is None:
                    raise StudyNotFoundError("card not found")
                if row["status"] != "active":
                    raise StudyConflictError(
                        "study_card_not_active", "only active study cards can be reviewed"
                    )
                if int(row["revision"]) != expected_revision:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                if expected_revision == MAX_SQLITE_INTEGER:
                    raise StudyConflictError(
                        "study_revision_exhausted",
                        "study card revision exhausted SQLite integer range",
                    )
                card = self._row_to_study_card(row)
                schedule = schedule_next_review(card, normalized_grade, reviewed_dt)
                scheduled_due_at = row["review_due_at"]
                scheduled_due_epoch = row["due_at_epoch_us"]
                changed = self._conn.execute(
                    """UPDATE study_cards SET revision=revision+1, updated_at=?
                       WHERE card_id=? AND status='active' AND revision=?""",
                    (reviewed_iso, card_id, expected_revision),
                )
                if changed.rowcount != 1:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                inject("after_card_cas")
                self._conn.execute(
                    """INSERT INTO study_reviews
                       (card_id, due_at, due_at_epoch_us, interval_days, ease,
                        repetitions, lapses, last_grade, last_reviewed_at,
                        last_reviewed_at_epoch_us, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(card_id) DO UPDATE SET
                         due_at=excluded.due_at,
                         due_at_epoch_us=excluded.due_at_epoch_us,
                         interval_days=excluded.interval_days,
                         ease=excluded.ease,
                         repetitions=excluded.repetitions,
                         lapses=excluded.lapses,
                         last_grade=excluded.last_grade,
                         last_reviewed_at=excluded.last_reviewed_at,
                         last_reviewed_at_epoch_us=excluded.last_reviewed_at_epoch_us,
                         updated_at=excluded.updated_at""",
                    (
                        card_id,
                        schedule["next_due_at"],
                        schedule["next_due_at_epoch_us"],
                        schedule["interval_days"],
                        schedule["ease"],
                        schedule["repetitions"],
                        schedule["lapses"],
                        normalized_grade,
                        reviewed_iso,
                        reviewed_epoch,
                        reviewed_iso,
                    ),
                )
                inject("after_review")
                updated_row = self._conn.execute(
                    """SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                              c.front, c.back, c.explanation, c.evidence_json, c.status,
                              c.source, c.revision, c.created_at, c.updated_at,
                              r.due_at AS review_due_at, r.due_at_epoch_us,
                              r.interval_days, r.ease, r.repetitions, r.lapses,
                              r.last_grade, r.last_reviewed_at,
                              r.updated_at AS review_updated_at
                       FROM study_cards c JOIN study_reviews r ON r.card_id=c.card_id
                       WHERE c.card_id=?""",
                    (card_id,),
                ).fetchone()
                if updated_row is None:
                    raise RuntimeError("study review update disappeared inside transaction")
                outcome = self._row_to_study_card(updated_row)
                outcome_json = json.dumps(
                    outcome, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                self._conn.execute(
                    """INSERT INTO study_review_logs
                       (id, card_id, request_id, request_fingerprint, grade, reviewed_at,
                        reviewed_at_epoch_us, response_ms, scheduled_due_at,
                        scheduled_due_at_epoch_us, next_due_at, next_due_at_epoch_us,
                        interval_days, ease, repetitions, lapses, revision_before,
                        revision_after, outcome_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"srl_{uuid.uuid4().hex}", card_id, normalized_request_id,
                        fingerprint, normalized_grade, reviewed_iso, reviewed_epoch,
                        response_ms, scheduled_due_at, scheduled_due_epoch,
                        schedule["next_due_at"], schedule["next_due_at_epoch_us"],
                        schedule["interval_days"], schedule["ease"],
                        schedule["repetitions"], schedule["lapses"],
                        expected_revision, expected_revision + 1, outcome_json,
                    ),
                )
                inject("after_log")
                inject("before_commit")
                self._conn.commit()
                return outcome
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise






































    def upsert_concept_occurrence(
        self,
        *,
        domain: str,
        term: str,
        job_id: str,
        evidence_id: str,
    ) -> bool:
        """精确绑定 concept/job/evidence；重复 completion 返回 False。"""
        now = _db._now_iso()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                concept = self._conn.execute(
                    "SELECT status FROM glossary WHERE domain=? AND term=?",
                    (domain, term),
                ).fetchone()
                if concept is None:
                    raise ConceptNotFoundError(f"concept not found: {domain}/{term}")
                if concept["status"] == "rejected":
                    raise ConceptConflictError("rejected concept 不接受 occurrence")
                evidence = self._conn.execute(
                    """SELECT c.job_id, c.status, j.domain
                       FROM canonical_evidence c
                       LEFT JOIN jobs j ON j.id=c.job_id
                       WHERE c.evidence_id=?""",
                    (evidence_id,),
                ).fetchone()
                if evidence is None:
                    raise ConceptEvidenceError(f"canonical evidence 不存在: {evidence_id}")
                if evidence["job_id"] != job_id:
                    raise ConceptEvidenceError("canonical evidence 不属于请求 job")
                if evidence["domain"] != domain:
                    raise ConceptEvidenceError("job domain 不属于请求 concept")
                if evidence["status"] != "valid":
                    raise ConceptEvidenceError("canonical evidence 当前不是 valid")
                cursor = self._conn.execute(
                    """INSERT OR IGNORE INTO concept_occurrences
                       (domain, term, job_id, evidence_id, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (domain, term, job_id, evidence_id, now),
                )
                self._conn.commit()
                return cursor.rowcount > 0
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise


    def set_concept_definition_lock(
        self,
        *,
        domain: str,
        term: str,
        locked: bool,
        expected_current_version_id: str,
        expected_lock_revision: int,
    ) -> dict:
        """以 current version + lock revision 做 lock/unlock CAS。"""
        if type(locked) is not bool:
            raise ValueError("locked 必须是 bool")
        if type(expected_lock_revision) is not int or expected_lock_revision < 0:
            raise ValueError("expected_lock_revision 必须是非负整数")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    """SELECT current_definition_version_id, lock_revision,
                              definition_locked
                       FROM glossary WHERE domain=? AND term=?""",
                    (domain, term),
                ).fetchone()
                if row is None:
                    raise ConceptNotFoundError(f"concept not found: {domain}/{term}")
                if (
                    row["current_definition_version_id"] != expected_current_version_id
                    or int(row["lock_revision"]) != expected_lock_revision
                ):
                    raise ConceptConflictError("concept current version 或 lock revision 已变化")
                if bool(row["definition_locked"]) == locked:
                    self._conn.commit()
                    return {
                        "current_definition_version_id": expected_current_version_id,
                        "lock_revision": expected_lock_revision,
                        "locked": locked,
                        "changed": False,
                    }
                cursor = self._conn.execute(
                    """UPDATE glossary
                       SET definition_locked=?, lock_revision=lock_revision+1,
                           updated_at=?
                       WHERE domain=? AND term=?
                         AND current_definition_version_id=? AND lock_revision=?""",
                    (
                        1 if locked else 0,
                        _db._now_iso(),
                        domain,
                        term,
                        expected_current_version_id,
                        expected_lock_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConceptConflictError("concept lock CAS 失败")
                self._conn.commit()
                return {
                    "current_definition_version_id": expected_current_version_id,
                    "lock_revision": expected_lock_revision + 1,
                    "locked": locked,
                    "changed": True,
                }
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def update_glossary_definition_cas(
        self,
        *,
        domain: str,
        term: str,
        definition: str | None,
        related: list | None,
        expected_current_version_id: str | None,
        expected_lock_revision: int | None,
        actor: str,
    ) -> dict:
        """人工定义与 related 在同一事务追加版本并 CAS 切换。"""
        if not actor.strip():
            raise ValueError("actor 不能为空")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                glossary = self._conn.execute(
                    """SELECT definition, related, current_definition_version_id,
                              lock_revision, definition_locked, status
                       FROM glossary WHERE domain=? AND term=?""",
                    (domain, term),
                ).fetchone()
                if glossary is None:
                    raise ConceptNotFoundError(f"concept not found: {domain}/{term}")
                related_json = (
                    glossary["related"]
                    if related is None
                    else json.dumps(_norm_related(related), ensure_ascii=False)
                )
                if definition is None or str(glossary["definition"] or "") == definition:
                    self._conn.execute(
                        """UPDATE glossary SET related=?, updated_at=?
                           WHERE domain=? AND term=?""",
                        (related_json, _db._now_iso(), domain, term),
                    )
                    self._conn.commit()
                    current = self._definition_row_locked(
                        str(glossary["current_definition_version_id"])
                    )
                    if current is None:
                        raise ConceptConflictError("concept current version 不存在")
                    result = self._row_to_concept_definition_version(current)
                    result["created"] = False
                    return result
                if glossary["status"] == "rejected":
                    raise ConceptConflictError("rejected concept 不接受定义版本")
                if (
                    not isinstance(expected_current_version_id, str)
                    or not expected_current_version_id
                    or type(expected_lock_revision) is not int
                    or expected_lock_revision < 0
                ):
                    raise ConceptConflictError(
                        "definition 变更必须携带 current version 与 lock revision"
                    )
                if (
                    glossary["current_definition_version_id"]
                    != expected_current_version_id
                    or int(glossary["lock_revision"]) != expected_lock_revision
                ):
                    raise ConceptConflictError("concept current version 或 lock revision 已变化")
                if glossary["definition_locked"]:
                    raise ConceptConflictError("concept definition 已锁定")
                previous = self._definition_row_locked(expected_current_version_id)
                if previous is None:
                    raise ConceptConflictError("concept current version 不存在")
                source_json, source_fingerprint = _concept_source_set([])
                input_hash = hashlib.sha256(
                    json.dumps(
                        {"definition": definition, "strategy": "manual_edit"},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                now = _db._now_iso()
                current = self._insert_definition_version_locked(
                    domain=domain,
                    term=term,
                    definition=definition,
                    source_evidence_ids_json=source_json,
                    source_set_fingerprint=source_fingerprint,
                    strategy="manual_edit",
                    provider=None,
                    model=None,
                    prompt_hash=None,
                    input_hash=input_hash,
                    supersedes_version_id=expected_current_version_id,
                    actor=actor,
                    created_at=now,
                )
                cursor = self._conn.execute(
                    """UPDATE glossary
                       SET definition=?, related=?, current_definition_version_id=?,
                           updated_at=?
                       WHERE domain=? AND term=?
                         AND current_definition_version_id=? AND lock_revision=?
                         AND definition_locked=0""",
                    (
                        definition,
                        related_json,
                        current["definition_version_id"],
                        now,
                        domain,
                        term,
                        expected_current_version_id,
                        expected_lock_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConceptConflictError("concept current pointer CAS 失败")
                self._conn.commit()
                result = self._row_to_concept_definition_version(current)
                result["created"] = True
                return result
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def upsert_glossary_term(
        self,
        domain: str,
        term: str,
        definition: str = "",
        related: list | None = None,
        status: str = "accepted",
        *,
        create_only: bool = False,
    ) -> None:
        """写入/覆盖一条术语(手动维护入口):按 (domain, term) 幂等 upsert,
        保留已有 occurrences,覆盖 definition/related/status。
        related 元素可为字符串或 {term, rel},落库前归一为 [{term, rel}]。"""
        now = _db._now_iso()
        related_json = json.dumps(_norm_related(related), ensure_ascii=False)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    """SELECT created_at, definition, current_definition_version_id,
                              definition_locked
                       FROM glossary WHERE domain=? AND term=?""",
                    (domain, term),
                ).fetchone()
                if row is None:
                    current = self._create_initial_definition_locked(
                        domain=domain,
                        term=term,
                        definition=definition,
                        strategy="manual_upsert",
                        actor="database:manual_upsert",
                        created_at=now,
                    )
                    self._conn.execute(
                        """INSERT INTO glossary
                           (domain, term, definition, occurrences, related, status,
                            created_at, updated_at, current_definition_version_id)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            domain,
                            term,
                            definition,
                            "[]",
                            related_json,
                            status,
                            now,
                            now,
                            current["definition_version_id"],
                        ),
                    )
                elif create_only:
                    raise ConceptConflictError(
                        f"concept already exists: {domain}/{term}"
                    )
                elif str(row["definition"] or "") != definition:
                    if row["definition_locked"]:
                        raise ConceptConflictError("concept definition 已锁定")
                    previous = self._definition_row_locked(
                        str(row["current_definition_version_id"])
                    )
                    if previous is None:
                        raise ConceptConflictError("concept current version 不存在")
                    source_json, source_fingerprint = _concept_source_set([])
                    input_hash = hashlib.sha256(
                        json.dumps(
                            {"definition": definition, "strategy": "manual_upsert"},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    current = self._insert_definition_version_locked(
                        domain=domain,
                        term=term,
                        definition=definition,
                        source_evidence_ids_json=source_json,
                        source_set_fingerprint=source_fingerprint,
                        strategy="manual_upsert",
                        provider=None,
                        model=None,
                        prompt_hash=None,
                        input_hash=input_hash,
                        supersedes_version_id=str(previous["definition_version_id"]),
                        actor="database:manual_upsert",
                        created_at=now,
                    )
                    self._conn.execute(
                        """UPDATE glossary
                           SET definition=?, current_definition_version_id=?, related=?,
                               status=?, updated_at=?
                           WHERE domain=? AND term=?""",
                        (
                            definition,
                            current["definition_version_id"],
                            related_json,
                            status,
                            now,
                            domain,
                            term,
                        ),
                    )
                else:
                    self._conn.execute(
                        """UPDATE glossary SET related=?, status=?, updated_at=?
                           WHERE domain=? AND term=?""",
                        (related_json, status, now, domain, term),
                    )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def add_glossary_suggestion(
        self,
        domain: str,
        term: str,
        job_id: str,
        content_type: str = "",
        location: str | None = None,
        definition: str = "",
        zh_name: str = "",
    ) -> None:
        """采集候选概念(resolve-then-merge):先按 (domain, term) 精确匹配,
        再经 shared.concepts.resolve 用归一键撞现有实体的 term/zh_name/aliases——
        「量化(Quantization)」「多头注意力」等变体挂到既有实体(occurrence 按 job_id 去重,
        新变体名进 aliases),而不是各建一条。都未命中才新建(主名规则见 primary_fields:
        英文为 term、中文进 zh_name)。定义/译名仅补空不覆盖,绝不降级已 accepted 的条目。
        生命周期:命中 rejected 实体 → 整条跳过(驳回后不再重复建议);suggested 实体
        的 occurrence 覆盖 ≥2 个不同 job → 自动晋升 accepted。"""
        from shared.concepts import primary_fields, resolve

        term = (term or "").strip()
        if not term:
            return
        now = _db._now_iso()
        occ = {"job_id": job_id, "content_type": content_type, "location": location}
        cols = (
            "term, occurrences, definition, definition_locked, zh_name, aliases, "
            "status, current_definition_version_id"
        )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    f"SELECT {cols} FROM glossary WHERE domain=? AND term=?",
                    (domain, term),
                ).fetchone()
                if row is None:
                    idx_rows = [
                        {"term": r["term"], "zh_name": r["zh_name"],
                         "aliases": json.loads(r["aliases"] or "[]")}
                        for r in self._conn.execute(
                            "SELECT term, zh_name, aliases FROM glossary "
                            "WHERE domain=? ORDER BY term", (domain,),
                        ).fetchall()
                    ]
                    hit = resolve(idx_rows, term, zh_name or None)
                    if hit is not None:
                        row = self._conn.execute(
                            f"SELECT {cols} FROM glossary WHERE domain=? AND term=?",
                            (domain, hit),
                        ).fetchone()
                if row is not None and row["status"] == "rejected":
                    self._conn.commit()
                    return
                if row is None:
                    p_term, p_zh, p_aliases = primary_fields(term, zh_name)
                    current = self._create_initial_definition_locked(
                        domain=domain,
                        term=p_term,
                        definition=definition,
                        strategy="pipeline_suggestion",
                        actor="database:pipeline_suggestion",
                        created_at=now,
                    )
                    self._conn.execute(
                        """INSERT INTO glossary
                           (domain, term, definition, zh_name, aliases, occurrences,
                            related, status, created_at, updated_at,
                            current_definition_version_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            domain,
                            p_term,
                            definition,
                            p_zh,
                            json.dumps(p_aliases, ensure_ascii=False),
                            json.dumps([occ], ensure_ascii=False),
                            "[]",
                            "suggested",
                            now,
                            now,
                            current["definition_version_id"],
                        ),
                    )
                else:
                    occs = json.loads(row["occurrences"] or "[]")
                    changed = False
                    if not any(o.get("job_id") == job_id for o in occs):
                        occs.append(occ)
                        changed = True
                    new_def = row["definition"]
                    if definition and not (row["definition"] or "").strip() \
                            and not row["definition_locked"]:
                        new_def = definition
                        changed = True
                    new_zh = row["zh_name"]
                    if zh_name and not (row["zh_name"] or "").strip():
                        new_zh = zh_name
                        changed = True
                    aliases = json.loads(row["aliases"] or "[]")
                    if term != row["term"] and term != (new_zh or "") and term not in aliases:
                        aliases.append(term)
                        changed = True
                    new_status = row["status"]
                    if row["status"] == "suggested" \
                            and len({o.get("job_id") for o in occs if o.get("job_id")}) >= 2:
                        new_status = "accepted"
                        changed = True
                    current_id = row["current_definition_version_id"]
                    if str(new_def or "") != str(row["definition"] or ""):
                        previous = self._definition_row_locked(str(current_id))
                        if previous is None:
                            raise ConceptConflictError("concept current version 不存在")
                        input_hash = hashlib.sha256(
                            json.dumps(
                                {
                                    "definition": new_def,
                                    "job_id": job_id,
                                    "strategy": "pipeline_suggestion",
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        version = self._insert_definition_version_locked(
                            domain=domain,
                            term=str(row["term"]),
                            definition=str(new_def or ""),
                            source_evidence_ids_json=previous["source_evidence_ids_json"],
                            source_set_fingerprint=previous["source_set_fingerprint"],
                            strategy="pipeline_suggestion",
                            provider=None,
                            model=None,
                            prompt_hash=None,
                            input_hash=input_hash,
                            supersedes_version_id=str(current_id),
                            actor="database:pipeline_suggestion",
                            created_at=now,
                        )
                        current_id = version["definition_version_id"]
                    if changed:
                        self._conn.execute(
                            """UPDATE glossary
                               SET occurrences=?, definition=?,
                                   current_definition_version_id=?, zh_name=?, aliases=?,
                                   status=?, updated_at=? WHERE domain=? AND term=?""",
                            (
                                json.dumps(occs, ensure_ascii=False),
                                new_def,
                                current_id,
                                new_zh,
                                json.dumps(aliases, ensure_ascii=False),
                                new_status,
                                now,
                                domain,
                                row["term"],
                            ),
                        )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise











    def mark_study_suggestion_batch_queued(
        self,
        batch_id: str,
        *,
        task_id: str,
        expected_revision: int,
    ) -> dict:
        normalized_batch = require_identifier(batch_id, "batch_id")
        normalized_task = require_identifier(task_id, "task_id")
        revision = require_revision(expected_revision)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                if row is None:
                    raise StudySuggestionNotFoundError(
                        "study_suggestion_batch_not_found", "batch not found"
                    )
                request_id, request_json, fingerprint = (
                    self._study_suggestion_lifecycle_operation_payload(
                        operation_kind="batch_queued",
                        batch_id=normalized_batch,
                        task_id=normalized_task,
                        attempt=int(row["attempt"]),
                        expected_revision=revision,
                    )
                )
                if row["status"] == "queued":
                    if (
                        row["task_id"] != normalized_task
                        or int(row["revision"]) != revision + 1
                    ):
                        raise StudySuggestionConflictError(
                            "study_suggestion_batch_not_pending",
                            "batch is not pending for this task",
                        )
                    replay = self._study_suggestion_operation_replay_locked(
                        request_id, fingerprint
                    )
                    current = self._row_to_study_suggestion_batch(row)
                    if not self._study_suggestion_lifecycle_replay_matches_current(
                        request_id=request_id,
                        batch_id=normalized_batch,
                        replay=replay,
                        current=current,
                    ):
                        raise StudySuggestionConflictError(
                            "study_suggestion_lifecycle_conflict",
                            "queued lifecycle operation is missing or inconsistent",
                        )
                    self._conn.commit()
                    return current
                if row["status"] != "pending_enqueue" or row["task_id"] != normalized_task:
                    raise StudySuggestionConflictError(
                        "study_suggestion_batch_not_pending", "batch is not pending for this task"
                    )
                if int(row["revision"]) != revision:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_stale", "batch revision is stale"
                    )
                if revision == MAX_SQLITE_INTEGER:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_exhausted", "batch revision is exhausted"
                    )
                now = self._study_suggestion_monotonic_now_locked(
                    [normalized_batch], _db._now_iso()
                ).isoformat()
                changed = self._conn.execute(
                    """UPDATE study_suggestion_batches
                       SET status='queued', revision=revision+1, updated_at=?
                       WHERE batch_id=? AND status='pending_enqueue'
                         AND task_id=? AND revision=?""",
                    (now, normalized_batch, normalized_task, revision),
                )
                if changed.rowcount != 1:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_stale", "batch revision is stale"
                    )
                updated = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                outcome = self._row_to_study_suggestion_batch(updated)
                self._insert_study_suggestion_operation_locked(
                    request_id=request_id,
                    request_fingerprint=fingerprint,
                    operation_kind="batch_queued",
                    batch_id=normalized_batch,
                    request_json=request_json,
                    outcome=outcome,
                    created_at=now,
                )
                self._conn.commit()
                return outcome
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def fail_study_suggestion_batch(
        self,
        batch_id: str,
        *,
        task_id: str,
        expected_revision: int,
        error_code: str,
        error_message: str,
    ) -> dict:
        normalized_batch = require_identifier(batch_id, "batch_id")
        normalized_task = require_identifier(task_id, "task_id")
        revision = require_revision(expected_revision)
        code = require_identifier(error_code, "error_code", max_length=128)
        message = require_identifier(error_message, "error_message", max_length=2_000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                if row is None:
                    raise StudySuggestionNotFoundError(
                        "study_suggestion_batch_not_found", "batch not found"
                    )
                request_id, request_json, fingerprint = (
                    self._study_suggestion_lifecycle_operation_payload(
                        operation_kind="batch_failed",
                        batch_id=normalized_batch,
                        task_id=normalized_task,
                        attempt=int(row["attempt"]),
                        expected_revision=revision,
                        details={"error_code": code, "error_message": message},
                    )
                )
                if row["status"] == "failed":
                    if (
                        row["task_id"] == normalized_task
                        and int(row["revision"]) == revision + 1
                        and row["error_code"] == code
                        and row["error_message"] == message
                    ):
                        replay = self._study_suggestion_operation_replay_locked(
                            request_id, fingerprint
                        )
                        current = self._row_to_study_suggestion_batch(row)
                        if not self._study_suggestion_lifecycle_replay_matches_current(
                            request_id=request_id,
                            batch_id=normalized_batch,
                            replay=replay,
                            current=current,
                        ):
                            raise StudySuggestionConflictError(
                                "study_suggestion_lifecycle_conflict",
                                "failed lifecycle operation is missing or inconsistent",
                            )
                        self._conn.commit()
                        return current
                    raise StudySuggestionConflictError(
                        "study_suggestion_failure_conflict",
                        "failed batch was already finalized with a different payload",
                    )
                if (
                    row["status"] != "queued"
                    or row["task_id"] != normalized_task
                    or int(row["revision"]) != revision
                ):
                    raise StudySuggestionConflictError(
                        "study_suggestion_batch_state_conflict",
                        "batch task/status/revision no longer matches",
                    )
                if revision == MAX_SQLITE_INTEGER:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_exhausted", "batch revision is exhausted"
                    )
                now = self._study_suggestion_monotonic_now_locked(
                    [normalized_batch], _db._now_iso()
                ).isoformat()
                changed = self._conn.execute(
                    """UPDATE study_suggestion_batches
                       SET status='failed', revision=revision+1, error_code=?,
                           error_message=?, updated_at=?
                       WHERE batch_id=? AND status='queued' AND task_id=? AND revision=?""",
                    (code, message, now, normalized_batch, normalized_task, revision),
                )
                if changed.rowcount != 1:
                    raise StudySuggestionConflictError(
                        "study_suggestion_batch_state_conflict",
                        "batch task/status/revision no longer matches",
                    )
                updated = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                outcome = self._row_to_study_suggestion_batch(updated)
                self._insert_study_suggestion_operation_locked(
                    request_id=request_id,
                    request_fingerprint=fingerprint,
                    operation_kind="batch_failed",
                    batch_id=normalized_batch,
                    request_json=request_json,
                    outcome=outcome,
                    created_at=now,
                )
                self._conn.commit()
                return outcome
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def retry_study_suggestion_batch(
        self,
        batch_id: str,
        *,
        request_id: str,
        expected_revision: int,
        deadline_seconds: int = 1_800,
    ) -> dict:
        normalized_batch = require_identifier(batch_id, "batch_id")
        normalized_request = require_external_request_id(request_id)
        revision = require_revision(expected_revision)
        deadline_sec = require_plain_int(
            deadline_seconds, "deadline_seconds", minimum=60, maximum=86_400
        )
        payload = {
            "operation_kind": "batch_retry",
            "request_id": normalized_request,
            "batch_id": normalized_batch,
            "expected_revision": revision,
            "deadline_seconds": deadline_sec,
        }
        request_json = canonical_json(payload)
        fingerprint = sha256_text(request_json)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                replay = self._study_suggestion_operation_replay_locked(
                    normalized_request, fingerprint
                )
                if replay is not None:
                    self._conn.commit()
                    return replay
                row = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                if row is None:
                    raise StudySuggestionNotFoundError(
                        "study_suggestion_batch_not_found", "batch not found"
                    )
                if row["status"] != "failed":
                    raise StudySuggestionConflictError(
                        "study_suggestion_batch_not_retryable", "only failed batch can retry"
                    )
                if int(row["revision"]) != revision:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_stale", "batch revision is stale"
                    )
                if revision == MAX_SQLITE_INTEGER or int(row["attempt"]) == MAX_SQLITE_INTEGER:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_exhausted", "batch retry counter is exhausted"
                    )
                now_dt = self._study_suggestion_monotonic_now_locked(
                    [normalized_batch], _db.utc_now()
                )
                now = now_dt.isoformat()
                deadline = now_dt + timedelta(seconds=deadline_sec)
                task_id = f"study-suggestions:{uuid.uuid4().hex}"
                changed = self._conn.execute(
                    """UPDATE study_suggestion_batches
                       SET status='pending_enqueue', revision=revision+1, attempt=attempt+1,
                           task_id=?, result_json=NULL, error_code=NULL, error_message=NULL,
                           deadline_at=?, deadline_at_epoch_us=?, updated_at=?
                       WHERE batch_id=? AND status='failed' AND revision=?""",
                    (
                        task_id, deadline.isoformat(),
                        datetime_to_epoch_us(deadline, "deadline_at"), now,
                        normalized_batch, revision,
                    ),
                )
                if changed.rowcount != 1:
                    raise StudySuggestionConflictError(
                        "study_suggestion_revision_stale", "batch revision is stale"
                    )
                updated = self._conn.execute(
                    "SELECT * FROM study_suggestion_batches WHERE batch_id=?",
                    (normalized_batch,),
                ).fetchone()
                outcome = self._row_to_study_suggestion_batch(updated)
                self._insert_study_suggestion_operation_locked(
                    request_id=normalized_request,
                    request_fingerprint=fingerprint,
                    operation_kind="batch_retry",
                    batch_id=normalized_batch,
                    request_json=request_json,
                    outcome=outcome,
                    created_at=now,
                )
                self._conn.commit()
                return outcome
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def create_study_card(
        self,
        *,
        card_id: str,
        domain: str,
        front: str,
        back: str,
        explanation: str = "",
        card_type: str = "basic",
        job_id: str | None = None,
        concept_term: str | None = None,
        evidence: object | None = None,
        status: str = "active",
        source: str = "manual",
        due_at: datetime | str | None = None,
    ) -> dict:
        """创建学习卡片。active 卡片同步初始化复习状态,使新卡立即进入 due 队列。"""
        normalized_domain = domain.strip() if isinstance(domain, str) else ""
        normalized_front = front.strip() if isinstance(front, str) else ""
        normalized_back = back.strip() if isinstance(back, str) else ""
        normalized_source = source.strip() if isinstance(source, str) else ""
        if not normalized_domain or not normalized_front or not normalized_back:
            raise ValueError("domain/front/back 不能为空")
        if not normalized_source:
            raise ValueError("source 不能为空")
        if status not in STUDY_STATUSES:
            raise ValueError("invalid study card status")
        now_dt = _db.utc_now()
        now = now_dt.isoformat()
        initial_due = due_at or now_dt
        due_iso = canonical_utc_iso(initial_due, "due_at")
        due_epoch = datetime_to_epoch_us(initial_due, "due_at")
        evidence_json = json.dumps(evidence if evidence is not None else [], ensure_ascii=False)
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO study_cards
                       (card_id, domain, job_id, concept_term, card_type, front, back,
                        explanation, evidence_json, status, source, revision,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        card_id, normalized_domain, job_id or None, concept_term or None,
                        card_type, normalized_front, normalized_back, explanation or "",
                        evidence_json, status, normalized_source, 1, now, now,
                    ),
                )
                if status == "active":
                    self._conn.execute(
                        """INSERT INTO study_reviews
                           (card_id, due_at, due_at_epoch_us, interval_days, ease,
                            repetitions, lapses, updated_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (card_id, due_iso, due_epoch, 0, 2.5, 0, 0, now),
                    )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        card = self.get_study_card(card_id)
        if card is None:
            raise RuntimeError("study card insert failed")
        return card

    def set_study_card_status(
        self,
        card_id: str,
        status: str,
        *,
        expected_revision: int,
    ) -> dict:
        if status not in STUDY_STATUSES:
            raise ValueError("invalid study card status")
        if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_SQLITE_INTEGER:
            raise ValueError("expected_revision 必须是 SQLite 64 位正整数")
        now_dt = _db.utc_now()
        now = now_dt.isoformat()
        now_epoch = datetime_to_epoch_us(now_dt)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT status, revision FROM study_cards WHERE card_id=?", (card_id,)
                ).fetchone()
                if row is None:
                    raise StudyNotFoundError("card not found")
                current_status = str(row["status"])
                if current_status == status:
                    self._conn.commit()
                    card = self.get_study_card(card_id)
                    if card is None:
                        raise StudyNotFoundError("card not found")
                    return card
                allowed = {
                    ("active", "suspended"),
                    ("suspended", "active"),
                    ("suggested", "rejected"),
                }
                if (current_status, status) not in allowed:
                    raise StudyConflictError(
                        "study_status_transition_invalid",
                        f"study card cannot transition from {current_status} to {status}",
                    )
                if int(row["revision"]) != expected_revision:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                if expected_revision == MAX_SQLITE_INTEGER:
                    raise StudyConflictError(
                        "study_revision_exhausted",
                        "study card revision exhausted SQLite integer range",
                    )
                changed = self._conn.execute(
                    """UPDATE study_cards SET status=?, revision=revision+1, updated_at=?
                       WHERE card_id=? AND revision=? AND status=?""",
                    (status, now, card_id, expected_revision, current_status),
                )
                if changed.rowcount != 1:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                if status == "active":
                    self._conn.execute(
                        """INSERT OR IGNORE INTO study_reviews
                           (card_id, due_at, due_at_epoch_us, interval_days, ease,
                            repetitions, lapses, updated_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (card_id, now, now_epoch, 0, 2.5, 0, 0, now),
                    )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        card = self.get_study_card(card_id)
        if card is None:
            raise StudyNotFoundError("card not found")
        return card

    def delete_study_card(self, card_id: str) -> bool:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "DELETE FROM study_cards WHERE card_id=?", (card_id,)
                )
                self._conn.commit()
                return cur.rowcount > 0
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
