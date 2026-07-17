"""collections 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    Collection,
    _now_iso,
    _parse_dt,
    datetime,
    json,
)


class CollectionsRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def get_collection(self, collection_id: str) -> Collection | None:
        row = self._conn.execute(
            "SELECT * FROM collections WHERE id=?", (collection_id,)
        ).fetchone()
        return self._row_to_collection(row) if row else None

    def list_collections(self, domain: str | None = None) -> list[Collection]:
        if domain:
            rows = self._conn.execute(
                "SELECT * FROM collections WHERE domain=?", (domain,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM collections").fetchall()
        return [self._row_to_collection(r) for r in rows]

    def list_subscription_collections(self, enabled_only: bool = False) -> list[Collection]:
        """订阅集合(source_type 非空);enabled_only 时仅自动追更开启的。周期同步用。"""
        q = "SELECT * FROM collections WHERE source_type IS NOT NULL"
        if enabled_only:
            q += " AND sync_enabled=1"
        return [self._row_to_collection(r) for r in self._conn.execute(q).fetchall()]

    def domain_exists(self, domain: str) -> bool:
        """领域键是否已被使用(jobs/collections/glossary 任一有行)。用于 rename 防撞。"""
        with self._lock:
            for tbl in (
                "jobs",
                "collections",
                "glossary",
                "study_cards",
                "study_suggestion_batches",
                "study_suggestions",
            ):
                if self._conn.execute(
                    f"SELECT 1 FROM {tbl} WHERE domain=? LIMIT 1", (domain,)
                ).fetchone():
                    return True
            if self._conn.execute(
                "SELECT 1 FROM study_suggestion_evidence WHERE current_domain=? LIMIT 1",
                (domain,),
            ).fetchone():
                return True
        return False

    def list_domains(self) -> list[dict]:
        """领域总览:每个 domain 的 集合数/内容数/概念数/订阅数/最近活跃(派生,无 domains 表)。"""
        domains: set[str] = set()
        for tbl in (
            "jobs",
            "collections",
            "glossary",
            "study_cards",
            "study_suggestion_batches",
            "study_suggestions",
        ):
            for r in self._conn.execute(
                f"SELECT DISTINCT domain FROM {tbl} WHERE domain IS NOT NULL AND domain<>''"
            ):
                domains.add(r[0])

        def grp(sql: str) -> dict:
            return {r[0]: r[1] for r in self._conn.execute(sql)}

        coll_c = grp("SELECT domain, COUNT(*) FROM collections GROUP BY domain")
        job_c = grp("SELECT domain, COUNT(*) FROM jobs GROUP BY domain")
        concept_c = grp("SELECT domain, COUNT(*) FROM glossary GROUP BY domain")
        sub_c = grp("SELECT domain, COUNT(*) FROM collections WHERE source_type IS NOT NULL GROUP BY domain")
        last = grp("SELECT domain, MAX(updated_at) FROM jobs GROUP BY domain")
        return [
            {
                "domain": d,
                "collection_count": coll_c.get(d, 0),
                "job_count": job_c.get(d, 0),
                "concept_count": concept_c.get(d, 0),
                "subscription_count": sub_c.get(d, 0),
                "last_active_at": last.get(d),
            }
            for d in sorted(domains)
        ]

    def domain_top_terms(self, domain: str, limit: int = 30) -> list[dict]:
        """领域工作台语义栏:该 domain 的术语(含候选 suggested,各带 status;rejected 除外),
        按来源数(佐证强度代理)降序。候选数另由 suggested_count 单独提示;前端可按 status 区分展示。"""
        rows = self._conn.execute(
            "SELECT term, definition, occurrences, status, is_topic FROM glossary "
            "WHERE domain=? AND status != 'rejected'",
            (domain,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            out.append({
                "term": r["term"], "definition": r["definition"],
                "source_count": len(occs) if isinstance(occs, list) else 0,
                "status": r["status"], "is_topic": bool(r["is_topic"]),
            })
        out.sort(key=lambda t: t["source_count"], reverse=True)
        return out[:limit]

    def concept_timeline(self, domain: str, granularity: str = "month") -> dict:
        """概念时间线:把该 domain 各概念的 occurrences 经 job_id→源内容发布时间映射,按粒度分桶计数。
        分桶时间用 COALESCE(published_at, created_at):优先源内容在平台的发布/更新时间("这个概念
        在世界上何时出现"),无已知发布时间的 job 回退入库时间(created_at),不丢计数。
        granularity: day(YYYY-MM-DD) / week(YYYY-Www) / month(YYYY-MM)。无 glossary/job 时返回空。"""
        from collections import defaultdict
        job_dates = {
            r["id"]: r["bucket_at"]
            for r in self._conn.execute(
                "SELECT id, COALESCE(published_at, created_at) AS bucket_at "
                "FROM jobs WHERE domain=?",
                (domain,),
            )
        }

        def bucket(iso: str | None) -> str | None:
            dt = _parse_dt(iso)
            if dt is None:
                return None
            if granularity == "day":
                return dt.strftime("%Y-%m-%d")
            if granularity == "week":
                y, w, _ = dt.isocalendar()
                return f"{y}-W{w:02d}"
            return dt.strftime("%Y-%m")

        rows = self._conn.execute(
            "SELECT term, occurrences FROM glossary "
            "WHERE domain=? AND status != 'rejected'", (domain,)
        ).fetchall()
        totals: dict = defaultdict(int)
        concepts: list[dict] = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            buckets: dict = defaultdict(int)
            for o in occs if isinstance(occs, list) else []:
                b = bucket(job_dates.get(o.get("job_id")))
                if b:
                    buckets[b] += 1
                    totals[b] += 1
            if buckets:
                concepts.append({
                    "term": r["term"], "buckets": dict(buckets),
                    "total": sum(buckets.values()),
                })
        concepts.sort(key=lambda c: c["total"], reverse=True)
        return {
            "granularity": granularity,
            "buckets": sorted(totals),
            "totals": dict(totals),
            "concepts": concepts,
        }

    def concept_occurrence_dates(self, domain: str) -> dict[str, list[str]]:
        """概念趋势雷达基础数据:该 domain 各概念的每条 occurrence 经 job_id→源内容时间映射,
        返回 {term: [iso_date, ...]}(每个 occurrence 一个时间点,可重复)。时间口径与 concept_timeline
        一致:COALESCE(published_at, created_at)("这个概念在世界上何时出现",无发布时间回退入库时间)。
        无映射到时间的 occurrence 略过(不计入)。供 radar 服务按窗口切片算飙升/新出现,纯数据无业务策略。"""
        job_dates = {
            r["id"]: r["bucket_at"]
            for r in self._conn.execute(
                "SELECT id, COALESCE(published_at, created_at) AS bucket_at "
                "FROM jobs WHERE domain=?",
                (domain,),
            )
        }
        out: dict[str, list[str]] = {}
        rows = self._conn.execute(
            "SELECT term, occurrences FROM glossary "
            "WHERE domain=? AND status != 'rejected'", (domain,)
        ).fetchall()
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            dates: list[str] = []
            for o in occs if isinstance(occs, list) else []:
                d = job_dates.get(o.get("job_id")) if isinstance(o, dict) else None
                if d:
                    dates.append(d)
            out[r["term"]] = dates
        return out

    def domain_topics(self, domain: str) -> list[dict]:
        """领域内主题(可浏览标签) = 该 domain 所有 job 的 style_tags distinct + 计数。"""
        from collections import Counter
        c: Counter = Counter()
        for r in self._conn.execute("SELECT style_tags FROM jobs WHERE domain=?", (domain,)):
            try:
                for t in json.loads(r["style_tags"] or "[]"):
                    if t:
                        c[t] += 1
            except (ValueError, TypeError):
                pass
        return [{"topic": t, "count": n} for t, n in c.most_common()]

    def ingested_bvids(self) -> set[str]:
        """已入库的 B站 BV 号集合(从 jobs.url 提取),供订阅同步去重。
        通用去重走 ingested_items 表(见 ingested_item_ids/mark_ingested),按
        (collection_id, item_id) 去重;本方法只作存量 bili 数据的兜底回填——同步首跑时
        可把它的结果并入某集合的 ingested 集合,避免已入库的 B站视频被重复建 job。"""
        import re
        out: set[str] = set()
        for (u,) in self._conn.execute(
            "SELECT url FROM jobs WHERE url LIKE '%BV%'"
        ).fetchall():
            m = re.search(r"(BV[0-9A-Za-z]{8,12})", u or "")
            if m:
                out.add(m.group(1))
        return out

    def ingested_item_ids(self, collection_id: str) -> set[str]:
        """某集合(订阅)已入库过的 item_id 集合,供 source-adapter 通用去重。
        item_id 含义随来源而定(B站=bvid、youtube=videoId、rss=entry id 等)。"""
        rows = self._conn.execute(
            "SELECT item_id FROM ingested_items WHERE collection_id=?",
            (collection_id,),
        ).fetchall()
        return {r["item_id"] for r in rows}

    def create_collection_in_tx(self, connection, collection: Collection) -> None:
        connection.execute(
            """INSERT INTO collections
               (id, name, domain, description, tags, job_count,
                source_type, source_id, sync_enabled, last_synced_at,
                last_sync_status, last_sync_error, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                collection.id,
                collection.name,
                collection.domain,
                collection.description,
                json.dumps(collection.tags, ensure_ascii=False),
                collection.job_count,
                collection.source_type,
                collection.source_id,
                1 if collection.sync_enabled else 0,
                collection.last_synced_at.isoformat() if collection.last_synced_at else None,
                collection.last_sync_status,
                collection.last_sync_error,
                collection.created_at.isoformat(),
                collection.updated_at.isoformat(),
            ),
        )

    def find_collection_by_source(self, source_type: str, source_id: str) -> Collection | None:
        """按来源找订阅集合(建订阅前去重;一个来源全局唯一对应一个订阅集合)。"""
        row = self._conn.execute(
            "SELECT * FROM collections WHERE source_type=? AND source_id=?",
            (source_type, source_id),
        ).fetchone()
        return self._row_to_collection(row) if row else None

    def update_collection_in_tx(
        self,
        connection,
        collection_id: str,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        sync_enabled: bool | None = None,
    ) -> None:
        """更新集合可变字段(name/description/tags/订阅自动追更开关),None 表示不动。"""
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if tags is not None:
            fields["tags"] = json.dumps(tags, ensure_ascii=False)
        if sync_enabled is not None:
            fields["sync_enabled"] = 1 if sync_enabled else 0
        if not fields:
            return
        fields["updated_at"] = _db._now_iso()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [collection_id]
        connection.execute(
            f"UPDATE collections SET {set_clause} WHERE id=?", values
        )

    def mark_collection_synced_in_tx(self, connection, collection_id: str, dt: datetime) -> None:
        """订阅集合同步成功后记录 last_synced_at,并置 last_sync_status=ok、清除错误。"""
        connection.execute(
            """UPDATE collections
               SET last_synced_at=?, last_sync_status='ok', last_sync_error=NULL,
                   updated_at=? WHERE id=?""",
            (dt.isoformat(), _db._now_iso(), collection_id),
        )

    def set_sync_status_in_tx(
        self, connection, collection_id: str, status: str | None, error: str | None = None
    ) -> None:
        """更新订阅集合的同步状态(syncing/ok/error/None)。error 仅 status=error 时存,其余清空。"""
        err = (error or "")[:500] if status == "error" else None
        connection.execute(
            """UPDATE collections
               SET last_sync_status=?, last_sync_error=?, updated_at=? WHERE id=?""",
            (status, err, _db._now_iso(), collection_id),
        )

    def mark_ingested_in_tx(self, connection, collection_id: str, item_id: str) -> None:
        """登记某集合已入库 item_id(幂等:重复 mark 不报错),同步成功后调。"""
        connection.execute(
            "INSERT OR IGNORE INTO ingested_items "
            "(collection_id, item_id, ingested_at) VALUES (?,?,?)",
            (collection_id, item_id, _db._now_iso()),
        )

    def increment_collection_count_in_tx(self, connection, collection_id: str, delta: int) -> None:
        """维护集合的 job_count:建/删 job 时增减;负值不下穿 0。"""
        if not collection_id:
            return
        connection.execute(
            "UPDATE collections SET job_count = MAX(0, job_count + ?) WHERE id=?",
            (delta, collection_id),
        )

    def reconcile_collection_count_in_tx(self, connection, collection_id: str) -> None:
        """按 jobs 真值重算计数;用于可重放快照创建,避免 crash retry 重复 +1。"""
        if not collection_id:
            return
        connection.execute(
            """UPDATE collections
               SET job_count=(SELECT COUNT(*) FROM jobs WHERE collection_id=?)
               WHERE id=?""",
            (collection_id, collection_id),
        )
