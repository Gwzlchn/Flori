"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import fnmatch
import json
import re

import structlog

from shared.db import _chunk_note_body
from shared.evidence_contract import (
    MAX_CANONICAL_SIDECAR_BYTES,
    build_canonical_evidence_records_with_reader,
)
from shared.models import Job
from shared.note_text import markdown_to_index_text
from shared.prompt_resolver import PromptResolver
from shared.review_contract import verify_persisted_review
from shared.step_base import def_digest_for, pipeline_digest_for
from shared.terms import zh_name_from_glossary_row
from shared.storage import read_file_bounded, read_verification_artifact_bounded, sha256_file

if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

_MAX_STEP_DONE_BYTES = 64 * 1024

def _markdown_to_text(md: str) -> str:
    """兼容旧调用点；归一化实现只保留在 shared.note_text。"""
    return markdown_to_index_text(md)


class EffectDispatcher:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def _run_step_completion_effects(self, job_id: str, step: str) -> bool:
        """执行当前步骤声明的完成副作用。返回 False 时由终态门和周期对账重试。"""
        steps = await self.owner._get_job_pipeline_steps(job_id)
        if not steps:
            return True
        effects = steps.get(step, {}).get("on_complete") or []
        return await self.owner._run_completion_effects(job_id, step, effects)

    async def _run_completion_effects(
        self, job_id: str, step: str, effects: list,
    ) -> bool:
        if not effects or self.owner.storage is None:
            return True
        for effect in effects:
            action = effect.get("action") if isinstance(effect, dict) else None
            try:
                if action == "sync_metadata":
                    await self.owner._sync_published_at(job_id)
                elif action == "index_note":
                    await self.owner._index_first_available_note(
                        job_id, effect.get("candidates") or [],
                    )
                elif action == "collect_glossary":
                    await self.owner._collect_glossary(job_id)
                elif action == "collect_term_pairs":
                    await self.owner._collect_term_pairs(job_id)
                else:
                    raise ValueError(f"unknown completion action: {action!r}")
                logger.info(
                    "completion_effect_done", job_id=job_id, step=step, action=action,
                )
            except Exception:
                logger.warning(
                    "completion_effect_failed", job_id=job_id, step=step,
                    action=action, exc_info=True,
                )
                return False
        return True

    async def _index_first_available_note(
        self, job_id: str, candidates: list[dict],
    ) -> None:
        """按配置顺序索引首个存在的笔记产物,避免回退来源重复进入 Ask。"""
        candidate_types = [note_type for note_type in (
            str(candidate.get("note_type") or "").strip()
            for candidate in candidates if isinstance(candidate, dict)
        ) if note_type]
        files: list[str] | None = None
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            note_type = str(candidate.get("note_type") or "").strip()
            pattern = str(candidate.get("path") or "").strip()
            if not note_type or not pattern:
                continue
            rel = pattern
            if any(ch in pattern for ch in "*?["):
                if files is None:
                    files = await self.owner.storage.list_files(job_id)
                matches = [f for f in files if fnmatch.fnmatch(f, pattern)]
                if note_type == "smart":
                    from shared.notes_versions import latest_smart
                    rel = latest_smart(matches) or ""
                else:
                    rel = max(matches, default="")
            if not rel:
                continue
            data = await self.owner.storage.read_file(job_id, rel)
            if not data:
                continue
            await self.owner._index_job_notes(
                job_id, note_type, rel, data, candidate_types=candidate_types,
                source_manifest_path=(
                    str(candidate.get("source_manifest") or "").strip() or None
                ),
                provenance_path=(
                    str(candidate.get("provenance") or "").strip() or None
                ),
                provenance_step=(
                    str(candidate.get("provenance_step") or "").strip() or None
                ),
                provenance_since_version=(
                    str(candidate.get("provenance_since_version") or "").strip()
                    or None
                ),
                legacy_provenance_step=(
                    str(candidate.get("legacy_provenance_step") or "").strip()
                    or None
                ),
                legacy_provenance_since_version=(
                    str(candidate.get("legacy_provenance_since_version") or "").strip()
                    or None
                ),
            )
            return
        raise FileNotFoundError(f"no indexable note artifact for {job_id}")

    async def _index_job_notes(
        self, job_id: str, note_type: str, rel: str, data: bytes,
        *,
        candidate_types: list[str] | None = None,
        source_manifest_path: str | None = None,
        provenance_path: str | None = None,
        provenance_step: str | None = None,
        provenance_since_version: str | None = None,
        legacy_provenance_step: str | None = None,
        legacy_provenance_since_version: str | None = None,
    ) -> None:
        """把指定 Markdown 产物去标记后写入全文与证据块索引。"""
        md = data.decode("utf-8", errors="replace")
        body = _markdown_to_text(md)
        if not body:
            raise ValueError(f"empty note body: {rel}")
        job = await asyncio.to_thread(self.owner.db.get_job, job_id)
        title = (job.title if job else None) or job_id
        domain = job.domain if job else ""
        content_type = job.content_type if job else ""
        collection_id = (job.collection_id if job else "") or ""
        canonical_evidence: list[dict] | None = None
        if (source_manifest_path is None) != (provenance_path is None):
            raise ValueError(f"incomplete canonical provenance config: {note_type}")
        if source_manifest_path is not None and provenance_path is not None:
            source_manifest_data = await read_file_bounded(
                self.owner.storage, job_id, source_manifest_path,
                MAX_CANONICAL_SIDECAR_BYTES,
            )
            provenance_data = await read_file_bounded(
                self.owner.storage, job_id, provenance_path,
                MAX_CANONICAL_SIDECAR_BYTES,
            )
            canonical_evidence = []
            if source_manifest_data is None or provenance_data is None:
                if source_manifest_data is None and provenance_data is None:
                    if not await self.owner._is_legacy_provenance_completion(
                        job, provenance_path,
                        provenance_step=provenance_step,
                        provenance_since_version=provenance_since_version,
                        legacy_provenance_step=legacy_provenance_step,
                        legacy_provenance_since_version=(
                            legacy_provenance_since_version
                        ),
                    ):
                        raise ValueError(
                            f"missing canonical provenance sidecars: {note_type}"
                        )
                else:
                    raise ValueError(
                        f"incomplete canonical provenance sidecars: {note_type}"
                    )
            if source_manifest_data is not None and provenance_data is not None:
                chunks = [
                    {
                        "chunk_id": f"{job_id}:{note_type}:{index}",
                        "body": chunk["body"],
                        "section": chunk["section"],
                        "char_start": chunk["char_start"],
                        "char_end": chunk["char_end"],
                    }
                    for index, chunk in enumerate(_chunk_note_body(body))
                ]

                async def read_source(
                    source_path: str, max_bytes: int,
                ) -> bytes | None:
                    return await read_file_bounded(
                        self.owner.storage, job_id, source_path, max_bytes,
                    )

                async def hash_source(source_path: str) -> str | None:
                    return await sha256_file(self.owner.storage, job_id, source_path)

                def attestation_protocol() -> str:
                    # reader 与 attestor 同源取协议文本(tracked 模板,hot→image,永不吃覆盖)。
                    config = self.owner.config
                    return PromptResolver(
                        hot_dir=config.prompts_dir / "templates",
                        image_dir=config.config_dir / "prompts" / "templates",
                    ).resolve(
                        "semantic_attestation", step_name="semantic_attestation",
                    ).text

                canonical_evidence = await build_canonical_evidence_records_with_reader(
                    job_id=job_id,
                    pipeline=job.pipeline if job else "",
                    note_type=note_type,
                    note_path=rel,
                    note_data=data,
                    normalized_body=body,
                    chunks=chunks,
                    source_manifest_data=source_manifest_data,
                    source_manifest_path=source_manifest_path,
                    provenance_path=provenance_path,
                    provenance_data=provenance_data,
                    read_file=read_source,
                    sha256_file=hash_source,
                    attestation_protocol=attestation_protocol,
                )
        await asyncio.to_thread(
            self.owner.db.index_job_notes,
            job_id, note_type, title, body,
            content_type, domain, collection_id, candidate_types,
            canonical_evidence,
        )
        logger.info(
            "notes_indexed", job_id=job_id, note_type=note_type, source_file=rel,
            canonical_evidence_count=len(canonical_evidence or []),
        )

    async def _is_legacy_provenance_completion(
        self,
        job: Job | None,
        provenance_path: str,
        *,
        provenance_step: str | None,
        provenance_since_version: str | None,
        legacy_provenance_step: str | None = None,
        legacy_provenance_since_version: str | None = None,
    ) -> bool:
        """仅当当前或显式旧 producer 的 .done 证明早期完成时放行空证据。"""
        if job is None or self.owner.storage is None:
            return False
        pipeline_steps = self.owner.config.pipelines.get(job.pipeline, {}).get("steps", [])
        if (
            job.pipeline_digest
            and job.pipeline_digest == pipeline_digest_for(pipeline_steps)
        ):
            return False
        if legacy_provenance_step or legacy_provenance_since_version:
            proofs = ((
                legacy_provenance_step,
                legacy_provenance_since_version,
            ),)
        else:
            proofs = ((provenance_step, provenance_since_version),)
        for step_name, since_version in proofs:
            if await self._step_completion_predates_provenance(
                job, pipeline_steps, provenance_path,
                step_name=step_name, since_version=since_version,
            ):
                return True
        return False

    async def _step_completion_predates_provenance(
        self,
        job: Job,
        pipeline_steps: list[dict],
        provenance_path: str,
        *,
        step_name: str | None,
        since_version: str | None,
    ) -> bool:
        """验证单个 producer marker 的 step、输出与版本边界。"""
        if (
            not step_name
            or not since_version
            or not since_version.isdigit()
        ):
            return False
        producers = [
            step for step in pipeline_steps
            if step.get("name") == step_name
        ]
        if len(producers) != 1:
            return False
        producer = producers[0]
        raw_version = producer.get("version", "1")
        if (
            producer.get("name") != step_name
            or type(raw_version) not in (str, int)
        ):
            return False
        version_text = str(raw_version)
        if not version_text.isdigit():
            return False
        current_version = int(version_text)
        provenance_since = int(since_version)
        outputs = producer.get("outputs")
        if (
            provenance_since <= 1
            or current_version < provenance_since
            or not isinstance(outputs, list)
            or not any(
                isinstance(pattern, str)
                and fnmatch.fnmatch(provenance_path, pattern)
                for pattern in outputs
            )
        ):
            return False

        raw_done = await read_file_bounded(
            self.owner.storage, job.id, f".{step_name}.done", _MAX_STEP_DONE_BYTES,
        )
        if raw_done is None or len(raw_done) > _MAX_STEP_DONE_BYTES:
            return False
        try:
            done = json.loads(raw_done)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if (
            not isinstance(done, dict)
            or done.get("step") != step_name
            or not isinstance(done.get("input_hashes"), dict)
            or not isinstance(done.get("finished_at"), str)
            or not done["finished_at"].strip()
        ):
            return False

        stored_digest = done.get("def_digest")
        if not isinstance(stored_digest, str):
            return False
        ai = producer.get("ai")
        if ai is not None and not isinstance(ai, dict):
            return False
        return stored_digest in {
            def_digest_for(version, ai)
            for version in range(1, provenance_since)
        }

    async def _reconcile_completed_effects(self, job_id: str) -> bool:
        """幂等重放该 job 所有 done 步骤的声明副作用,闭合事件丢失与崩溃窗口。"""
        steps = await self.owner._get_job_pipeline_steps(job_id)
        if not steps:
            return True
        statuses = await self.owner.redis.get_all_step_statuses(job_id)
        for step, cfg in steps.items():
            if statuses.get(step) != "done":
                continue
            effects = cfg.get("on_complete") or []
            if effects and not await self.owner._run_completion_effects(job_id, step, effects):
                return False
        return True

    async def _export_term_map(self, job: Job) -> None:
        """术语一致性 L1(+L2)导出:把该 domain 的 glossary 译名快照写 input/term_map.json,
        供翻译步(worker 无 DB)按 chunk 命中注入。job 属集合且集合有 terms.json(L2,book)
        则合并(L2 覆盖 L1)。best-effort:失败只 warn,不阻塞提交。"""
        if self.owner.storage is None:
            return
        try:
            rows = await asyncio.to_thread(self.owner.db.glossary_term_rows, job.domain or "general")
            tmap: dict[str, str] = {}
            for r in rows:
                pair = zh_name_from_glossary_row(r.get("term") or "", r.get("zh_name"), r.get("definition") or "")
                if not pair:
                    continue
                tmap[pair[0]] = pair[1]
                # 实体的英文别名(P1 归并变体)映射到同一译名,别名命中同样注入。
                for alias in (r.get("aliases") or []):
                    ap = zh_name_from_glossary_row(alias, pair[1], "")
                    if ap:
                        tmap.setdefault(ap[0], ap[1])
            if job.collection_id:
                raw = await self.owner.storage.read_file(f"collections/{job.collection_id}", "terms.json")
                if raw:
                    try:
                        tmap.update(json.loads(raw.decode("utf-8", errors="replace")))
                    except (json.JSONDecodeError, ValueError):
                        logger.warning("collection_terms_invalid", collection=job.collection_id)
            if not tmap:
                return
            await self.owner.storage.write_file(
                job.id, "input/term_map.json",
                json.dumps(tmap, ensure_ascii=False, indent=1).encode("utf-8"),
            )
            logger.info("term_map_exported", job_id=job.id, terms=len(tmap))
        except Exception:
            logger.warning("term_map_export_failed", job_id=job.id, exc_info=True)

    async def _collect_term_pairs(self, job_id: str) -> None:
        """翻译步完成回流:读取 output/term_pairs.json,写入 glossary suggested 并带 zh_name.
        job 属集合(book)时同步 merge 进 collections/{id}/terms.json(L2,后章注入)。"""
        if self.owner.storage is None:
            return
        data = await self.owner.storage.read_file(job_id, "output/term_pairs.json")
        if not data:
            return
        try:
            pairs = json.loads(data.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(pairs, dict) or not pairs:
            return
        job = await asyncio.to_thread(self.owner.db.get_job, job_id)
        domain = (job.domain if job else "") or "general"
        for en, zh in pairs.items():
            if not isinstance(en, str) or not isinstance(zh, str) or not en or not zh:
                continue
            await asyncio.to_thread(
                self.owner.db.add_glossary_suggestion,
                domain, en, job_id, job.content_type if job else "", None, "", zh,
                document_kind=job.document_kind if job else "",
            )
        if job and job.collection_id:
            try:
                prefix = f"collections/{job.collection_id}"
                raw = await self.owner.storage.read_file(prefix, "terms.json")
                merged: dict = {}
                if raw:
                    try:
                        merged = json.loads(raw.decode("utf-8", errors="replace")) or {}
                    except (json.JSONDecodeError, ValueError):
                        merged = {}
                # 先到先得:已有译名不被后章覆盖(与注入层 L2>L1、篇内首译优先一致)。
                for en, zh in pairs.items():
                    merged.setdefault(en, zh)
                await self.owner.storage.write_file(
                    prefix, "terms.json",
                    json.dumps(merged, ensure_ascii=False, indent=1).encode("utf-8"),
                )
            except Exception:
                logger.warning("collection_terms_merge_failed", job_id=job_id, exc_info=True)
        logger.info("term_pairs_collected", job_id=job_id, count=len(pairs))

    async def _collect_glossary(self, job_id: str) -> None:
        """把 key_terms(这篇讲清楚的概念 + 候选定义)采集为候选术语。
        主喂养源是评审"讲清楚了什么"一节;missing_concepts(知识缺口)只留评审面板,不喂术语库。
        采集源:优先 output/concepts.json,回退 output/review.json。"""
        job = await asyncio.to_thread(self.owner.db.get_job, job_id)
        domain = (job.domain if job else "") or "general"

        async def reconcile_empty(reason: str) -> None:
            await asyncio.to_thread(
                self.owner.db.replace_job_concept_occurrences,
                domain=domain,
                job_id=job_id,
                mapping={},
            )
            logger.info(
                "concept_occurrences_reconciled",
                job_id=job_id,
                count=0,
                reason=reason,
            )

        data = await self.owner.storage.read_file(job_id, "output/concepts.json")
        from_review = False
        if not data:
            data = await self.owner._read_verification_artifact(
                job_id, "output/review.json",
            )
            from_review = bool(data)
        if not data:
            await reconcile_empty("source_missing")
            return
        try:
            review = json.loads(data.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            await reconcile_empty("source_invalid")
            return
        if not isinstance(review, dict):
            await reconcile_empty("source_invalid")
            return
        # 旧版/抢救/截断评审只供诊断,不能沉淀术语或关系边。
        if from_review:
            async def reader(rel: str) -> bytes | None:
                return await self.owner._read_verification_artifact(job_id, rel)

            review = await verify_persisted_review(
                review, job_id=job_id, pipeline=job.pipeline if job else None,
                read_file=reader,
            )
            if review.get("review_reliable") is not True:
                logger.info("glossary_review_rejected", job_id=job_id,
                            reasons=review.get("reliability_reasons") or ["legacy_schema"])
                await reconcile_empty("review_unreliable")
                return
        key_terms = review.get("key_terms") or []
        if not isinstance(key_terms, list):
            await reconcile_empty("key_terms_invalid")
            return
        content_type = job.content_type if job else ""
        collected = 0
        with_related: list[tuple[str, str, list]] = []   # (term, zh_name, related)
        for t in key_terms:
            if isinstance(t, dict):
                term, definition = t.get("term"), (t.get("definition") or "")
                zh_name = t.get("zh_name") if isinstance(t.get("zh_name"), str) else ""
                related = t.get("related") if isinstance(t.get("related"), list) else []
            else:
                term, definition, zh_name, related = t, "", "", []
            if not term or not isinstance(term, str):
                continue
            await asyncio.to_thread(
                self.owner.db.add_glossary_suggestion,
                domain, term, job_id, content_type, None, definition, zh_name,
                document_kind=getattr(job, "document_kind", "") if job else "",
            )
            if related:
                with_related.append((term, zh_name or "", related))
            collected += 1
        edges = 0
        if with_related:
            edges = await asyncio.to_thread(
                self.owner._write_concept_relations, domain, with_related,
            )
        occurrences, synthesis_candidates = await asyncio.to_thread(
            self.owner._replace_concept_occurrences,
            domain,
            job_id,
            key_terms,
            None if from_review else review.get("evidence_note_type"),
        )
        self.owner._schedule_concept_resynthesis(domain, synthesis_candidates)
        logger.info("glossary_collected", job_id=job_id, count=collected, edges=edges)
        logger.info(
            "concept_occurrences_reconciled",
            job_id=job_id,
            count=occurrences,
        )

    async def _read_verification_artifact(
        self, job_id: str, rel: str,
    ) -> bytes | None:
        """调度器评审重验与 API 使用同一有界读取机制。"""
        return await read_verification_artifact_bounded(self.owner.storage, job_id, rel)

    def _write_concept_relations(
        self, domain: str, items: list[tuple[str, str, list]]
    ) -> int:
        """key_terms 的 related 写为实体间关系边:两端都经 resolve 归一到主名.
        目标未入库时不建边,待其被采集后下次出现自动连上;同步调用,
        由 _collect_glossary 包 to_thread。"""
        from shared.concepts import norm_related, resolve

        rows = [
            {"term": r["term"], "zh_name": r.get("zh_name") or "",
             "aliases": r.get("aliases") or []}
            for r in self.owner.db.list_glossary(domain)
        ]
        added = 0
        for term, zh_name, related in items:
            src = resolve(rows, term, zh_name or None)
            if src is None:
                continue
            rels = []
            for r in norm_related(related):
                tgt = resolve(rows, r["term"])
                if tgt is not None and tgt != src:
                    rels.append({"term": tgt, "rel": r["rel"]})
            if rels:
                added += self.owner.db.add_glossary_relations(domain, src, rels)
        return added

    def _replace_concept_occurrences(
        self,
        domain: str,
        job_id: str,
        key_terms: list,
        evidence_note_type: object,
    ) -> tuple[int, list[tuple[str, str, int]]]:
        """把 producer 的来源段引用映射为当前 canonical IDs 后全量对账。"""
        from shared.concepts import resolve

        note_type = evidence_note_type if (
            isinstance(evidence_note_type, str)
            and evidence_note_type in {"smart", "translated", "original"}
        ) else None
        requested_ids: list[str] = []
        if note_type:
            for item in key_terms:
                if not isinstance(item, dict):
                    continue
                refs = item.get("evidence_source_segment_ids")
                if not isinstance(refs, list):
                    continue
                for ref in refs:
                    if (
                        isinstance(ref, str)
                        and re.fullmatch(r"seg_[0-9a-f]{64}", ref)
                        and ref not in requested_ids
                    ):
                        requested_ids.append(ref)

        canonical_by_segment = (
            self.owner.db.canonical_evidence_ids_for_source_segments(
                job_id=job_id,
                note_type=note_type,
                source_segment_ids=requested_ids,
            )
            if note_type and requested_ids else {}
        )
        glossary_rows = self.owner.db.list_glossary(domain)
        rows = [
            {
                "term": row["term"],
                "zh_name": row.get("zh_name") or "",
                "aliases": row.get("aliases") or [],
            }
            for row in glossary_rows
        ]
        mapping: dict[str, list[str]] = {}
        for item in key_terms:
            if not isinstance(item, dict):
                continue
            term = item.get("term")
            if not isinstance(term, str) or not term.strip():
                continue
            zh_name = item.get("zh_name")
            resolved = resolve(
                rows,
                term,
                zh_name if isinstance(zh_name, str) else None,
            )
            if resolved is None:
                continue
            refs = item.get("evidence_source_segment_ids")
            if not isinstance(refs, list):
                continue
            evidence_ids = mapping.setdefault(resolved, [])
            for ref in refs:
                for evidence_id in canonical_by_segment.get(ref, []):
                    if isinstance(evidence_id, str) and evidence_id not in evidence_ids:
                        evidence_ids.append(evidence_id)
            if not evidence_ids:
                mapping.pop(resolved, None)

        self.owner.db.replace_job_concept_occurrences(
            domain=domain,
            job_id=job_id,
            mapping=mapping,
        )
        rows_by_term = {row["term"]: row for row in glossary_rows}
        candidates: list[tuple[str, str, int]] = []
        for term in sorted(mapping):
            row = rows_by_term.get(term) or {}
            current_id = row.get("current_definition_version_id")
            if (
                isinstance(current_id, str)
                and current_id
                and not row.get("definition_locked")
            ):
                candidates.append((term, current_id, int(row.get("lock_revision") or 0)))
        return (
            sum(len(evidence_ids) for evidence_ids in mapping.values()),
            candidates,
        )

    def _schedule_concept_resynthesis(
        self,
        domain: str,
        candidates: list[tuple[str, str, int]],
    ) -> None:
        """异步触发概念综合；失败只记日志，不反向阻塞 pipeline 完成。"""
        for term, current_id, lock_revision in candidates:
            key = (domain, term)
            previous = self.owner._concept_synthesis_tasks.get(key)
            if previous is not None and not previous.done():
                self.owner._concept_synthesis_pending[key] = (current_id, lock_revision)
                continue
            task = asyncio.create_task(
                self.owner._auto_resynthesize_concept(
                    domain,
                    term,
                    current_id,
                    lock_revision,
                ),
                name=f"concept_resynthesis:{domain}:{term}",
            )
            self.owner._concept_synthesis_tasks[key] = task
            task.add_done_callback(
                lambda finished, task_key=key: self.owner._on_concept_synthesis_done(
                    task_key, finished,
                )
            )

    def _on_concept_synthesis_done(
        self,
        key: tuple[str, str],
        task: asyncio.Task,
    ) -> None:
        if self.owner._concept_synthesis_tasks.get(key) is task:
            self.owner._concept_synthesis_tasks.pop(key, None)
            pending = self.owner._concept_synthesis_pending.pop(key, None)
            if pending is not None and not self.owner._shutdown:
                current_id, lock_revision = pending
                self.owner._schedule_concept_resynthesis(
                    key[0], [(key[1], current_id, lock_revision)]
                )
