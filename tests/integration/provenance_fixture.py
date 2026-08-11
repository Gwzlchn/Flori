"""为合成 pipeline 工件发布可复算的来源与笔记溯源 sidecar。"""

from __future__ import annotations

import hashlib
import fnmatch
import mimetypes
from collections.abc import Mapping

from shared.note_text import markdown_to_index_text
from shared.provenance import (
    EXACT_QUOTE_POLICY,
    build_provenance_manifest,
    build_source_manifest,
    canonical_json_bytes,
    make_segment_id,
)
from shared.step_completion import step_definition_digest_for
from shared.step_manifest import (
    canonical_json_bytes as canonical_manifest_bytes,
    compute_input_digest,
    manifest_relative_path,
    validate_manifest,
)
from shared.step_scope import part_id_from_scope
from shared.storage import StorageBackend


SOURCE_ARTIFACT_PATH = "input/provenance_fixture.html"
SOURCE_MANIFEST_PATH = "intermediate/source_segments.json"


def _anchor_for(note_data: bytes) -> tuple[str, str]:
    markdown = note_data.decode("utf-8")
    body = markdown_to_index_text(markdown)
    candidates = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    anchor = next(
        (
            line for line in candidates
            if body.count(line) == 1 and any(char.isalpha() for char in line)
        ),
        None,
    )
    if anchor is None:
        raise AssertionError("合成笔记缺少唯一的可复算文本锚点")
    return body, anchor


async def publish_provenance_fixture(
    storage: StorageBackend,
    *,
    job_id: str,
    pipeline: str,
    notes: Mapping[str, tuple[str, bytes]],
) -> None:
    """为当前 pipeline fixture 同步写来源清单和每个笔记的 provenance。"""
    prepared: dict[str, tuple[str, bytes, str, str]] = {}
    source_parts = ["<article>\n"]
    offsets: dict[str, tuple[int, int]] = {}
    for note_type in sorted(notes):
        note_path, note_data = notes[note_type]
        body, anchor = _anchor_for(note_data)
        source_parts.append(f'<p data-note-type="{note_type}">')
        start = sum(len(part) for part in source_parts)
        source_parts.append(anchor)
        end = start + len(anchor)
        offsets[note_type] = (start, end)
        source_parts.append("</p>\n")
        prepared[note_type] = (note_path, note_data, body, anchor)
    source_parts.append("</article>\n")
    source_text = "".join(source_parts)
    source_data = source_text.encode("utf-8")
    source_sha256 = hashlib.sha256(source_data).hexdigest()

    segments = []
    segment_ids: dict[str, str] = {}
    for note_type in sorted(prepared):
        _note_path, _note_data, _body, anchor = prepared[note_type]
        start, end = offsets[note_type]
        locator = {
            "kind": "text",
            "exact": anchor,
            "prefix": source_text[max(0, start - 32):start],
            "suffix": source_text[end:end + 32],
            "dom_path": None,
        }
        segment_id = make_segment_id(
            "fixture-html",
            start=start,
            end=end,
            section=note_type,
            locator=locator,
        )
        segment_ids[note_type] = segment_id
        segments.append({
            "segment_id": segment_id,
            "source_id": "fixture-html",
            "start": start,
            "end": end,
            "section": note_type,
            "locator": locator,
            "support_text": anchor,
            "support_artifact": {
                "kind": "html",
                "path": SOURCE_ARTIFACT_PATH,
                "sha256": source_sha256,
                "selector": {"start": start, "end": end},
            },
        })

    source_manifest = build_source_manifest(
        job_id=job_id,
        pipeline=pipeline,
        source_artifacts=[{
            "source_id": "fixture-html",
            "path": SOURCE_ARTIFACT_PATH,
            "sha256": source_sha256,
            "revision": "synthetic-current-pipeline",
            "media_duration_ms": None,
            "page_count": None,
        }],
        segments=segments,
    )
    await storage.write_file(job_id, SOURCE_ARTIFACT_PATH, source_data)
    await storage.write_file(
        job_id, SOURCE_MANIFEST_PATH, canonical_json_bytes(source_manifest),
    )

    for note_type, (note_path, note_data, body, anchor) in prepared.items():
        provenance = build_provenance_manifest(
            job_id=job_id,
            note_type=note_type,
            note_artifact=note_path,
            note_bytes=note_data,
            normalized_body=body,
            source_manifest_path=SOURCE_MANIFEST_PATH,
            source_manifest=source_manifest,
            segments=[{
                "anchor": anchor,
                "prefix": "",
                "suffix": "",
                "section": note_type,
                "source_segment_ids": [segment_ids[note_type]],
                "verification_policy": EXACT_QUOTE_POLICY,
            }],
        )
        await storage.write_file(
            job_id,
            f"output/provenance/{note_type}.json",
            canonical_json_bytes(provenance),
        )


async def publish_step_manifest_fixture(
    storage: StorageBackend,
    *,
    job,
    config,
    step_name: str,
    step_config: dict,
    job_generation: int,
) -> None:
    """为合成完成事件发布当前 definition 的真实 manifest 形状。"""
    scope_key = step_config["scope_key"]
    template_step = step_config["template_step"]
    part_id = part_id_from_scope(scope_key)
    prefix = f"parts/{part_id}/" if part_id else ""
    if template_step == "09_publish":
        await storage.write_file(job.id, "output/publication.json", b"{}\n")

    patterns = step_config.get("outputs") or []
    output_records = []
    for job_rel in sorted(await storage.list_files(job.id)):
        if prefix:
            if not job_rel.startswith(prefix):
                continue
            rel = job_rel[len(prefix):]
        else:
            if job_rel.startswith("parts/"):
                continue
            rel = job_rel
        if not any(fnmatch.fnmatch(rel, pattern) for pattern in patterns):
            continue
        data = await storage.read_file(job.id, job_rel)
        if data is None:
            continue
        output_records.append({
            "path": rel,
            "size_bytes": len(data),
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "media_type": mimetypes.guess_type(rel)[0],
        })

    definition_digest = step_definition_digest_for(
        job.pipeline,
        step_config,
        config=config,
        domain=job.domain,
        style_tags=job.style_tags,
    )
    manifest = {
        "format": "flori-step-manifest",
        "format_version": 1,
        "job_id": job.id,
        "scope": {
            "kind": "part" if part_id else "job",
            "scope_key": scope_key,
            "part_id": part_id,
            "part_index": step_config.get("part_index") if part_id else None,
        },
        "step": template_step,
        "outcome": "done",
        "execution": {
            "exec_id": f"fixture:{step_name}",
            "job_generation": job_generation,
            "attempt": 1,
            "started_at": "2026-08-11T00:00:00Z",
            "committed_at": "2026-08-11T00:00:01Z",
            "duration_sec": 0.001,
        },
        "compatibility": {
            "input_fingerprints": {},
            "input_digest": compute_input_digest({}),
            "definition_digest": definition_digest,
        },
        "producer": {
            "flori_version": "test",
            "build_sha": None,
            "worker_id": "integration-fixture",
            "runner": "fixture",
            "image": None,
            "image_digest": None,
            "tool_versions": {},
        },
        "outputs": output_records,
        "skip": None,
    }
    encoded = validate_manifest(manifest)
    assert encoded == canonical_manifest_bytes(manifest)
    await storage.write_file(
        job.id,
        manifest_relative_path(scope_key, template_step),
        encoded,
    )
