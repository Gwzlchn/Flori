"""concepts 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    ConceptConflictError,
    ConceptEvidenceError,
    ConceptNotFoundError,
    _concept_definition_version_id,
    _concept_source_set,
    _norm_related,
    _now_iso,
    _optional_sha256,
    hashlib,
    json,
    sqlite3,
)


class ConceptsRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def list_concept_occurrences(
        self,
        domain: str,
        term: str,
        *,
        include_invalid: bool = False,
    ) -> list[dict]:
        """返回正规化 occurrence；默认排除 stale/missing canonical evidence。"""
        valid_clause = "" if include_invalid else " AND c.status='valid'"
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT o.domain, o.term, o.job_id, o.evidence_id, o.created_at,
                           c.status AS evidence_status, c.source_fingerprint,
                           c.chunk_body_sha256,
                           n.body AS evidence_excerpt,
                           j.content_type
                    FROM concept_occurrences o
                    JOIN canonical_evidence c ON c.evidence_id=o.evidence_id
                    JOIN jobs j ON j.id=o.job_id
                    LEFT JOIN note_chunks n
                      ON n.chunk_id=c.chunk_id AND n.job_id=c.job_id
                     AND n.note_type=c.note_type
                    WHERE o.domain=? AND o.term=?{valid_clause}
                    ORDER BY o.job_id, o.evidence_id""",
                (domain, term),
            ).fetchall()
        return [dict(row) for row in rows]

    def current_concept_definition(self, domain: str, term: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT v.* FROM glossary g
                   JOIN concept_definition_versions v
                     ON v.definition_version_id=g.current_definition_version_id
                   WHERE g.domain=? AND g.term=?""",
                (domain, term),
            ).fetchone()
        return self._row_to_concept_definition_version(row) if row is not None else None

    def list_concept_definition_versions(
        self,
        domain: str,
        term: str,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError("limit 必须是 1..1000 的整数")
        suffix = " LIMIT ?" if limit is not None else ""
        params: tuple = (domain, term, limit) if limit is not None else (domain, term)
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM concept_definition_versions
                   WHERE domain=? AND term=? ORDER BY version DESC""" + suffix,
                params,
            ).fetchall()
        return [self._row_to_concept_definition_version(row) for row in rows]

    def count_concept_definition_versions(self, domain: str, term: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                """SELECT COUNT(*) FROM concept_definition_versions
                   WHERE domain=? AND term=?""",
                (domain, term),
            ).fetchone()[0])

    def glossary_term_rows(self, domain: str) -> list[dict]:
        """术语一致性 L1 导出用:该域词条的 (term, zh_name, definition, aliases) 轻量行。
        rejected 不导出(驳回件不该再注入翻译);aliases 供导出层把英文别名也映射到同一译名。"""
        rows = self._conn.execute(
            "SELECT term, zh_name, definition, aliases FROM glossary "
            "WHERE domain=? AND status != 'rejected'", (domain,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["aliases"] = json.loads(d.get("aliases") or "[]")
            except (ValueError, TypeError):
                d["aliases"] = []
            out.append(d)
        return out

    def get_glossary_term(self, domain: str, term: str) -> dict | None:
        """读单条术语,未命中返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM glossary WHERE domain=? AND term=?", (domain, term)
        ).fetchone()
        return self._row_to_glossary(row) if row is not None else None

    def list_glossary(
        self, domain: str | None = None, status: str | None = None,
        q: str | None = None,
    ) -> list[dict]:
        """列术语,可按 domain / status 过滤 + q 检索(term/zh_name/aliases 子串,
        大小写不敏感),按 term 升序。status 未指定时默认排除 rejected。驳回件
        只在显式 status='rejected' 时可见)。"""
        where_parts: list[str] = []
        params: list = []
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if status:
            where_parts.append("status=?")
            params.append(status)
        else:
            where_parts.append("status != 'rejected'")
        if q and q.strip():
            # aliases 是 JSON 文本列,LIKE 子串足够(检索场景,无需精确解析)。
            like = f"%{q.strip()}%"
            where_parts.append("(term LIKE ? OR zh_name LIKE ? OR aliases LIKE ?)")
            params += [like, like, like]
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        rows = self._conn.execute(
            f"SELECT * FROM glossary {where} ORDER BY term", params
        ).fetchall()
        return [self._row_to_glossary(r) for r in rows]

    def get_job_titles(self, job_ids: list[str]) -> dict[str, str]:
        """批量取 job 标题(概念详情出现处 enrich 用):{job_id: title},缺 title 的 job 不返回。"""
        out: dict[str, str] = {}
        ids = [j for j in dict.fromkeys(job_ids) if j]
        for i in range(0, len(ids), 500):   # SQLite 变量数上限保护
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in self._conn.execute(
                f"SELECT id, title FROM jobs WHERE id IN ({ph})", chunk
            ).fetchall():
                if r["title"]:
                    out[r["id"]] = r["title"]
        return out

    def list_topic_concepts(self, domain: str) -> list[dict]:
        """该 domain 中标为主题概念(is_topic=1,rejected 除外)的列表,按出现数降序;
        每项含 term/definition/occurrence_count/related/is_topic。空则 []。"""
        rows = self._conn.execute(
            "SELECT term, definition, occurrences, related, is_topic "
            "FROM glossary WHERE domain=? AND is_topic=1 AND status != 'rejected'",
            (domain,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            try:
                related = _norm_related(json.loads(r["related"] or "[]"))
            except (ValueError, TypeError):
                related = []
            out.append({
                "term": r["term"],
                "definition": r["definition"] or "",
                "occurrence_count": len(occs) if isinstance(occs, list) else 0,
                "related": related,
                "is_topic": True,
            })
        out.sort(key=lambda t: t["occurrence_count"], reverse=True)
        return out

    def _validate_concept_source_evidence_locked_in_tx(
        self,
        connection,
        *,
        domain: str,
        term: str,
        evidence_ids: list[str],
    ) -> tuple[str, str]:
        """验证定义来源都已精确挂到该概念，调用方负责事务与锁。"""
        source_json, fingerprint = _concept_source_set(evidence_ids)
        canonical_ids = json.loads(source_json)
        if not canonical_ids:
            return source_json, fingerprint
        placeholders = ",".join("?" for _ in canonical_ids)
        rows = connection.execute(
            f"""SELECT c.evidence_id, c.job_id, c.status, j.domain,
                       o.evidence_id AS occurrence_evidence_id
                FROM canonical_evidence c
                LEFT JOIN jobs j ON j.id=c.job_id
                LEFT JOIN concept_occurrences o
                  ON o.domain=? AND o.term=? AND o.job_id=c.job_id
                 AND o.evidence_id=c.evidence_id
                WHERE c.evidence_id IN ({placeholders})""",
            (domain, term, *canonical_ids),
        ).fetchall()
        by_id = {str(row["evidence_id"]): row for row in rows}
        for evidence_id in canonical_ids:
            row = by_id.get(evidence_id)
            if row is None:
                raise ConceptEvidenceError(f"canonical evidence 不存在: {evidence_id}")
            if row["status"] != "valid":
                raise ConceptEvidenceError(f"canonical evidence 当前无效: {evidence_id}")
            if row["domain"] != domain:
                raise ConceptEvidenceError(f"canonical evidence domain 不匹配: {evidence_id}")
            if row["occurrence_evidence_id"] is None:
                raise ConceptEvidenceError(f"canonical evidence 未绑定该概念: {evidence_id}")
        return source_json, fingerprint

    def _definition_row_locked_in_tx(self, connection, definition_version_id: str | None) -> sqlite3.Row | None:
        if definition_version_id is None:
            return None
        return connection.execute(
            "SELECT * FROM concept_definition_versions WHERE definition_version_id=?",
            (definition_version_id,),
        ).fetchone()

    def _insert_definition_version_locked_in_tx(
        self,
        connection,
        *,
        domain: str,
        term: str,
        definition: str,
        source_evidence_ids_json: str,
        source_set_fingerprint: str,
        strategy: str,
        provider: str | None,
        model: str | None,
        prompt_hash: str | None,
        input_hash: str | None,
        supersedes_version_id: str | None,
        actor: str,
        created_at: str,
    ) -> sqlite3.Row:
        """只追加 definition history；调用方同事务切 current pointer。"""
        if not domain.strip() or not term.strip() or not strategy.strip() or not actor.strip():
            raise ValueError("concept identity、strategy 和 actor 不能为空")
        prompt_hash = _optional_sha256(prompt_hash, "prompt_hash")
        input_hash = _optional_sha256(input_hash, "input_hash")
        next_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM concept_definition_versions "
                "WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()[0]
        )
        version_id = _concept_definition_version_id(
            domain=domain,
            term=term,
            version=next_version,
            input_hash=input_hash,
            actor=actor,
        )
        connection.execute(
            """INSERT INTO concept_definition_versions (
                   definition_version_id, domain, term, version, definition,
                   source_evidence_ids_json, source_set_fingerprint, strategy,
                   provider, model, prompt_hash, input_hash,
                   supersedes_version_id, actor, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id,
                domain,
                term,
                next_version,
                definition,
                source_evidence_ids_json,
                source_set_fingerprint,
                strategy,
                provider,
                model,
                prompt_hash,
                input_hash,
                supersedes_version_id,
                actor,
                created_at,
            ),
        )
        row = self._definition_row_locked(version_id)
        assert row is not None
        return row

    def _create_initial_definition_locked_in_tx(
        self,
        connection,
        *,
        domain: str,
        term: str,
        definition: str,
        strategy: str,
        actor: str,
        created_at: str,
    ) -> sqlite3.Row:
        """为新建或曾删除后重建的概念追加首个 current version。"""
        previous = connection.execute(
            """SELECT definition_version_id
               FROM concept_definition_versions
               WHERE domain=? AND term=? ORDER BY version DESC LIMIT 1""",
            (domain, term),
        ).fetchone()
        source_json, fingerprint = _concept_source_set([])
        input_hash = hashlib.sha256(
            json.dumps(
                {"definition": definition, "strategy": strategy},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return self._insert_definition_version_locked(
            domain=domain,
            term=term,
            definition=definition,
            source_evidence_ids_json=source_json,
            source_set_fingerprint=fingerprint,
            strategy=strategy,
            provider=None,
            model=None,
            prompt_hash=None,
            input_hash=input_hash,
            supersedes_version_id=(
                str(previous["definition_version_id"]) if previous is not None else None
            ),
            actor=actor,
            created_at=created_at,
        )

    def remove_concept_occurrence_in_tx(
        self,
        connection,
        *,
        domain: str,
        term: str,
        job_id: str,
        evidence_id: str,
    ) -> bool:
        """只删除指定四元组，不影响同 job 的其他证据。"""
        cursor = connection.execute(
            """DELETE FROM concept_occurrences
               WHERE domain=? AND term=? AND job_id=? AND evidence_id=?""",
            (domain, term, job_id, evidence_id),
        )
        return cursor.rowcount > 0

    def add_glossary_relations_in_tx(self, connection, domain: str, term: str, relations: list[dict]) -> int:
        """给该概念并入关系边,供采集链与补边脚本共用:按目标 term 去重(先到先得,
        不覆盖已有 rel),自指跳过。行不存在返回 0(调用方应先 resolve 到主名)。返回新增边数。"""
        rels = [r for r in _norm_related(relations) if r["term"] != term]
        if not rels:
            return 0
        row = connection.execute(
            "SELECT related FROM glossary WHERE domain=? AND term=?",
            (domain, term),
        ).fetchone()
        if row is None:
            return 0
        related = _norm_related(json.loads(row["related"] or "[]"))
        have = {r["term"] for r in related}
        added = 0
        for r in rels:
            if r["term"] not in have:
                related.append(r)
                have.add(r["term"])
                added += 1
        if added:
            connection.execute(
                "UPDATE glossary SET related=?, updated_at=? WHERE domain=? AND term=?",
                (json.dumps(related, ensure_ascii=False), _db._now_iso(), domain, term),
            )
        return added

    def set_glossary_zh_name_in_tx(self, connection, domain: str, term: str, zh_name: str) -> bool:
        """backfill/人工定准写译名;返回是否更新(不存在的词条返回 False)。"""
        cur = connection.execute(
            "UPDATE glossary SET zh_name=?, updated_at=? WHERE domain=? AND term=?",
            (zh_name, _db._now_iso(), domain, term),
        )
        return cur.rowcount > 0

    def accept_glossary_term_in_tx(self, connection, domain: str, term: str) -> None:
        """采纳候选术语:status -> 'accepted'。"""
        connection.execute(
            "UPDATE glossary SET status='accepted', updated_at=? "
            "WHERE domain=? AND term=?",
            (_db._now_iso(), domain, term),
        )

    def reject_glossary_term_in_tx(self, connection, domain: str, term: str) -> bool:
        """驳回概念:status -> 'rejected'。行保留——采集链 resolve 命中 rejected 直接
        跳过,同名/变体不会再被重复建议;各消费面(列表/图谱/雷达/term_map)默认排除。
        命中返回 True,无该行返回 False(供路由判 404)。"""
        cur = connection.execute(
            "UPDATE glossary SET status='rejected', updated_at=? "
            "WHERE domain=? AND term=?",
            (_db._now_iso(), domain, term),
        )
        return cur.rowcount > 0

    def set_glossary_watched_in_tx(self, connection, domain: str, term: str, watched: bool) -> bool:
        """置概念 watch 标记。命中返回 True,无该行返回 False(供路由判 404)。"""
        cur = connection.execute(
            "UPDATE glossary SET watched=?, updated_at=? WHERE domain=? AND term=?",
            (1 if watched else 0, _db._now_iso(), domain, term),
        )
        return cur.rowcount > 0

    def set_glossary_topic_in_tx(self, connection, domain: str, term: str, is_topic: bool) -> bool:
        """置该词 is_topic(主题概念标记)。命中返回 True,无该行返回 False(供路由判 404)。"""
        cur = connection.execute(
            "UPDATE glossary SET is_topic=?, updated_at=? WHERE domain=? AND term=?",
            (1 if is_topic else 0, _db._now_iso(), domain, term),
        )
        return cur.rowcount > 0

    def delete_glossary_term_in_tx(self, connection, domain: str, term: str) -> None:
        connection.execute(
            "DELETE FROM glossary WHERE domain=? AND term=?", (domain, term)
        )
