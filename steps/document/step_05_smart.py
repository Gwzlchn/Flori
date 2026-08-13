"""用分层证据卡、主题综合和独立导读生成论文智能笔记。"""

from __future__ import annotations

import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping

from shared.document_contract import (
    MAX_QUALITY_JSON_BYTES,
    validate_document,
    validate_quality,
)
from shared.errors import InputInvalidError
from shared.step_ai import AIInvocation
from shared.step_base import StepBase, file_hash
from steps.document.provenance import (
    extract_attestable_document_markers,
    load_document_source_manifest,
    persist_document_note_provenance,
    require_complete_document_marker_coverage,
    require_unique_document_provenance_anchors,
)
from steps.document.smart_pipeline import (
    MAX_PARALLEL_CALLS,
    MAX_STAGE_PROMPT_BYTES,
    build_chapter_packages,
    build_themes,
    canonical_json,
    enrich_cards,
    inject_source_markers,
    parse_stage_result,
    project_theme_card,
    render_figures,
    render_model_synthesis,
    sha256_bytes,
    validate_chapter_card,
    validate_final,
    validate_introduction,
    validate_theme,
)
from steps.utils.provenance_attestation import persist_semantic_candidates


_TEMPLATES = (
    "05_smart_document",
    "05_smart_document.theme",
    "05_smart_document.final",
    "05_smart_document.introduction",
)
_SCHEMAS = ("chapter", "theme", "final", "introduction")
_QUALITY_NOTE_METRIC_KEYS = (
    "pdf_source_quality", "pdf_crosswalk_blocks", "pdf_crosswalk_visuals",
    "pdf_crosswalk_ambiguous", "pdf_crosswalk_visual_ambiguous",
    "pdf_layout_detector_failures", "html_visual_asset_failures",
    "visual_asset_failures",
)
_PROMPT_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


class DocumentSmartStep(StepBase):
    def validate_inputs(self) -> list[str]:
        return [
            path for path in (
                "intermediate/document.json",
                "intermediate/quality.json",
                "intermediate/source_segments.json",
            )
            if not (self.job_dir / path).is_file()
        ]

    def _schema_path(self, name: str) -> Path:
        config_dir = Path(self.config["paths"]["config_dir"])
        return config_dir / "prompts" / "schemas" / f"05_smart_document.{name}.json"

    def _schema(self, name: str) -> dict[str, Any]:
        return json.loads(self._schema_path(name).read_text(encoding="utf-8"))

    def step_input_hashes(self) -> dict[str, str]:
        hashes = {
            "document": file_hash(self.job_dir / "intermediate/document.json"),
            "quality": file_hash(self.job_dir / "intermediate/quality.json"),
            "source_segments": file_hash(
                self.job_dir / "intermediate/source_segments.json"
            ),
            "smart_templates": self.ai.template_hash(*_TEMPLATES),
        }
        for name in _SCHEMAS:
            hashes[f"smart_schema_{name}"] = file_hash(self._schema_path(name))
        profile_path = (
            Path(self.config["paths"]["prompts_dir"])
            / "profiles" / f"{self.config['domain']['name']}.yaml"
        )
        if profile_path.is_file():
            hashes["profile"] = file_hash(profile_path)
        return hashes

    def execute(self) -> dict:
        document = validate_document(
            self.artifacts.load_json("intermediate/document.json"),
            expected_job_id=self.job_dir.name,
        )
        quality = self._load_quality()
        if quality["status"] == "rejected":
            raise InputInvalidError("document smart note rejects rejected source quality")
        source_manifest = load_document_source_manifest(self.job_dir)
        if source_manifest is None:
            raise ValueError("document smart note requires source manifest")

        paper_map, packages, coverage = build_chapter_packages(
            document, source_manifest,
        )
        schemas = {name: self._schema(name) for name in _SCHEMAS}
        for name in _TEMPLATES:
            self.ai.resolve_prompt_template(name)
        invocations: list[AIInvocation] = []
        try:
            cards = self._run_chapters(
                paper_map, packages, quality, schemas["chapter"], invocations,
            )
            enriched, knowledge, figures, source_map = enrich_cards(packages, cards)
            themes = build_themes(packages)
            theme_results = self._run_themes(
                paper_map, themes, enriched, knowledge, figures,
                schemas["theme"], invocations,
            )
            final_result, final_figures, final_invocation = self._run_final(
                paper_map, theme_results, knowledge, figures,
                schemas["final"], invocations,
            )
            introduction, introduction_invocation = self._run_introduction(
                paper_map, knowledge, schemas["introduction"], invocations,
            )
        finally:
            self.ai.merge_forks(invocations)

        final_markdown = render_figures(
            render_model_synthesis(final_result), final_result["figure_placements"],
            final_figures, self.job_dir,
        )
        marked_introduction = inject_source_markers(
            introduction["introduction_markdown"], knowledge, source_map,
            deduplicate_sources_by_evidence=False,
        )
        clean_introduction, intro_exact, intro_semantic = (
            extract_attestable_document_markers(
                marked_introduction, source_manifest, ai=introduction_invocation,
                deduplicate_sources_by_anchor=True,
            )
        )
        require_complete_document_marker_coverage(
            marked_introduction, intro_exact, intro_semantic,
        )
        marked_final = inject_source_markers(
            final_markdown, knowledge, source_map,
            deduplicate_sources_by_evidence=False,
        )
        clean_final, final_exact, final_semantic = extract_attestable_document_markers(
            marked_final, source_manifest, ai=final_invocation,
            deduplicate_sources_by_anchor=True,
        )
        require_complete_document_marker_coverage(
            marked_final, final_exact, final_semantic,
        )
        result = clean_introduction.strip() + "\n\n" + clean_final.strip()
        exact = intro_exact + final_exact
        semantic = intro_semantic + final_semantic
        quality_notice = self._quality_notice(quality)
        if quality_notice:
            result = f"{quality_notice}\n\n{result}"
        note_title = str(final_result["title"]).strip()
        rel = self.review.write_smart_note(result, title=note_title)
        try:
            require_unique_document_provenance_anchors(
                self.job_dir, rel, [*exact, *semantic],
            )
        except Exception:
            (self.job_dir / rel).unlink(missing_ok=True)
            raise
        provenance = persist_document_note_provenance(
            self.job_dir,
            note_type="smart",
            note_artifact=rel,
            candidates=exact,
            provenance_dir="output/provenance_exact",
        )
        candidate_state = persist_semantic_candidates(
            self.job_dir,
            pipeline="document",
            note_type="smart",
            note_artifact=rel,
            candidates=semantic,
        )
        self._write_pipeline_artifacts(
            paper_map=paper_map, packages=packages, coverage=coverage,
            cards=cards, themes=theme_results, knowledge=knowledge, figures=figures,
            final_result=final_result, introduction=introduction,
        )
        return {
            "chars": len(result), "note_file": rel, "title": note_title,
            "source": "document", "quality_status": quality["status"],
            "quality_disclosed": bool(quality_notice),
            "provider": self.ai.last_provider, "model": self.ai.last_model,
            "chapter_packages": len(packages), "themes": len(theme_results),
            "knowledge_items": len(knowledge), "figures": len(final_figures),
            "provenance_segments": provenance["segments"],
            "provenance_status": provenance["status"],
            "semantic_candidates": candidate_state["candidates"],
        }

    def _load_quality(self) -> dict[str, Any]:
        with (self.job_dir / "intermediate/quality.json").open("rb") as handle:
            quality_data = handle.read(MAX_QUALITY_JSON_BYTES + 1)
        if len(quality_data) > MAX_QUALITY_JSON_BYTES:
            raise InputInvalidError("document quality exceeds byte limit")
        return validate_quality(
            json.loads(quality_data), expected_job_id=self.job_dir.name,
        )

    def _run_chapters(
        self, paper_map: Mapping[str, Any], packages: list[dict[str, Any]],
        quality: Mapping[str, Any], schema: Mapping[str, Any],
        invocations: list[AIInvocation],
    ) -> dict[str, dict[str, Any]]:
        prompt_map = {
            "title": paper_map["title"], "abstract": paper_map["abstract"],
            "major_headings": [
                item for item in paper_map["headings"]
                if int(item.get("level") or 9) <= 3
            ],
        }
        tasks = []
        for package in packages:
            package_id = str(package["package_id"])
            invocation = self.ai.fork(f"01-chapter-{package_id}")
            invocations.append(invocation)
            images = [self.job_dir / value for value in self._package_images(package)]
            tasks.append((
                package_id, invocation, "05_smart_document",
                {
                    "OUTPUT_SCHEMA": canonical_json(schema),
                    "PAPER_MAP": canonical_json(prompt_map),
                    "QUALITY": self._quality_prompt_block(quality),
                    "PACKAGE": canonical_json(package),
                },
                images,
                lambda value, package=package: validate_chapter_card(value, package),
            ))
        return self._parallel_validated(tasks, schema)

    def _run_themes(
        self, paper_map: Mapping[str, Any], themes: list[dict[str, Any]],
        cards: list[dict[str, Any]], knowledge: Mapping[str, Any],
        figures: Mapping[str, Any], schema: Mapping[str, Any],
        invocations: list[AIInvocation],
    ) -> list[dict[str, Any]]:
        tasks = []
        expected: dict[str, tuple[list[str], list[str]]] = {}
        for theme in themes:
            selected = [card for card in cards if card["logical_parent"] in theme["packages"]]
            knowledge_refs = [
                item["knowledge_id"] for card in selected for item in card["knowledge"]
            ]
            figure_refs = [
                item["figure_ref"] for card in selected for item in card["figures"]
                if item["artifact_paths"]
            ]
            theme_id = str(theme["theme_id"])
            expected[theme_id] = (knowledge_refs, figure_refs)
            invocation = self.ai.fork(f"02-theme-{theme_id}")
            invocations.append(invocation)
            tasks.append((
                theme_id, invocation, "05_smart_document.theme",
                {
                    "OUTPUT_SCHEMA": canonical_json(schema),
                    "THEME": canonical_json(theme),
                    "PAPER_MAP": canonical_json(paper_map),
                    "EXPECTED_KNOWLEDGE_REFS": canonical_json(knowledge_refs),
                    "FIGURE_CATALOG": canonical_json({
                        ref: figures[ref] for ref in figure_refs
                    }),
                    "CHAPTER_CARDS": canonical_json([
                        project_theme_card(card) for card in selected
                    ]),
                },
                [],
                lambda value, theme=theme, knowledge_refs=knowledge_refs,
                figure_refs=figure_refs: validate_theme(
                    value, theme, knowledge_refs, figure_refs,
                ),
            ))
        results = self._parallel_validated(tasks, schema)
        return [results[str(theme["theme_id"])] for theme in themes]

    def _run_final(
        self, paper_map: Mapping[str, Any], themes: list[dict[str, Any]],
        knowledge: Mapping[str, Any], figures: Mapping[str, Any],
        schema: Mapping[str, Any], invocations: list[AIInvocation],
    ) -> tuple[dict[str, Any], dict[str, Any], AIInvocation]:
        theme_refs = [str(item["theme_id"]) for item in themes]
        selected_figures = {
            guide["figure_ref"]
            for theme in themes for guide in theme["figure_guides"]
        }
        final_figures = {
            ref: {
                "figure_ref": ref, "label": figures[ref]["label"],
                "caption": figures[ref]["caption"],
                "artifact_paths": figures[ref]["artifact_paths"],
            }
            for ref in sorted(selected_figures)
        }
        invocation = self.ai.fork("03-final")
        invocations.append(invocation)
        result = self._call_validated(
            invocation, "05_smart_document.final",
            {
                "OUTPUT_SCHEMA": canonical_json(schema),
                "PAPER_MAP": canonical_json(paper_map),
                "EXPECTED_THEME_REFS": canonical_json(theme_refs),
                "EXPECTED_KNOWLEDGE_REFS": canonical_json(sorted(knowledge)),
                "KNOWLEDGE_SOURCE_MAP": canonical_json({
                    ref: knowledge[ref]["source_refs"] for ref in sorted(knowledge)
                }),
                "FIGURE_CATALOG": canonical_json(final_figures),
                "THEME_SYNTHESES": canonical_json([
                    {key: value for key, value in theme.items() if key != "coverage_refs"}
                    for theme in themes
                ]),
            },
            schema,
            lambda value: validate_final(
                value, theme_refs, knowledge, list(final_figures),
            ),
        )
        return result, final_figures, invocation

    def _run_introduction(
        self, paper_map: Mapping[str, Any], knowledge: Mapping[str, Any],
        schema: Mapping[str, Any], invocations: list[AIInvocation],
    ) -> tuple[dict[str, Any], AIInvocation]:
        candidates = list(knowledge)
        kinds = (
            "context", "motivation", "definition", "assumption",
            "method", "result", "finding", "limitation",
        )
        selected_refs: list[str] = []
        for kind in kinds:
            matching = [
                ref for ref in candidates
                if knowledge[ref]["kind"] == kind and ref not in selected_refs
            ]
            selected_refs.extend(matching[:8])
            if len(selected_refs) >= 64:
                break
        for ref in candidates:
            if len(selected_refs) >= 64:
                break
            if ref not in selected_refs:
                selected_refs.append(ref)
        valid_refs = selected_refs[:64]
        if not valid_refs:
            raise ValueError("paper introduction has no evidence catalog")
        selected_catalog = {ref: knowledge[ref] for ref in valid_refs}
        catalog = list(selected_catalog.values())
        invocation = self.ai.fork("04-introduction")
        invocations.append(invocation)
        result = self._call_validated(
            invocation, "05_smart_document.introduction",
            {
                "OUTPUT_SCHEMA": canonical_json(schema),
                "ABSTRACT": canonical_json(paper_map["abstract"]),
                "INTRODUCTION_CATALOG": canonical_json(catalog),
                "VALID_REFS": canonical_json(valid_refs),
            },
            schema,
            lambda value: validate_introduction(value, selected_catalog),
        )
        return result, invocation

    def _parallel_validated(
        self, tasks: list[tuple], schema: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CALLS, len(tasks))) as pool:
            futures = {
                pool.submit(
                    self._call_validated, invocation, template, values,
                    schema, validator, images,
                ): task_id
                for task_id, invocation, template, values, images, validator in tasks
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def _call_validated(
        self, invocation: AIInvocation, template_name: str,
        values: Mapping[str, str], schema: Mapping[str, Any],
        validator: Callable[[dict[str, Any]], dict[str, Any]],
        images: list[Path] | None = None,
    ) -> dict[str, Any]:
        template = invocation.load_prompt_template(template_name)
        prompt = self._render_stage_prompt(template, values)
        last_error: ValueError | None = None
        for attempt in range(2):
            attempt_prompt = prompt
            if last_error is not None:
                feedback = canonical_json({"validation_error": str(last_error)})
                attempt_prompt += (
                    "\n\n上一次输出未通过确定性结构与证据闭包校验。"
                    "重新生成完整 JSON，不要返回补丁或解释。校验反馈=" + feedback
                )
            if len(attempt_prompt.encode("utf-8")) > MAX_STAGE_PROMPT_BYTES:
                raise InputInvalidError("document smart stage prompt exceeds byte limit")
            raw = invocation.call(
                attempt_prompt, images=images or [], response_format="json",
                temperature=0, max_tokens=32768,
            )
            try:
                result = validator(parse_stage_result(raw, schema))
                invocation._amend_last_log({
                    "output_processed": {
                        "json_parse": {"ok": True, "salvaged": False},
                        "contract": "valid", "attempt": attempt + 1,
                    },
                })
                return result
            except (KeyError, TypeError, ValueError) as exc:
                last_error = ValueError(str(exc))
                invocation._amend_last_log({
                    "output_processed": {
                        "json_parse": {"ok": False, "salvaged": False},
                        "contract": "invalid", "error": str(exc)[:1000],
                        "attempt": attempt + 1,
                    },
                })
        assert last_error is not None
        raise last_error

    @staticmethod
    def _render_stage_prompt(template: str, values: Mapping[str, str]) -> str:
        """单次替换受控占位符；输入文本中的占位形状不得触发二次注入。"""
        expected = set(_PROMPT_PLACEHOLDER_RE.findall(template))
        if expected != set(values):
            raise ValueError("document smart prompt placeholders do not match values")
        return _PROMPT_PLACEHOLDER_RE.sub(
            lambda match: values[match.group(1)], template,
        )

    @staticmethod
    def _package_images(package: Mapping[str, Any]) -> list[str]:
        return sorted({
            str(media["artifact_path"])
            for figure in package["figures"] for media in figure["media"]
            if media.get("artifact_path")
        })

    def _write_pipeline_artifacts(self, **values: Any) -> None:
        root = "output/smart_pipeline"
        packages = values.pop("packages")
        cards = values.pop("cards")
        themes = values.pop("themes")
        relative_values: list[tuple[str, Any]] = []
        for package in packages:
            package_id = package["package_id"]
            relative_values.extend((
                (f"chapter-package-{package_id}.json", package),
                (f"chapter-card-{package_id}.json", cards[package_id]),
            ))
        for theme in themes:
            relative_values.append((f"theme-{theme['theme_id']}.json", theme))
        for key, value in values.items():
            name = key.replace("_", "-")
            relative_values.append((f"{name}.json", value))
        root_path = self.job_dir / root
        if root_path.exists():
            shutil.rmtree(root_path)
        root_path.mkdir(parents=True, exist_ok=True)
        for name, value in relative_values:
            self.artifacts.write(f"{root}/{name}", value)
        artifact_paths = [root_path / name for name, _ in relative_values]
        artifacts = [
            {
                "path": str(path.relative_to(self.job_dir)),
                "bytes": path.stat().st_size,
                "sha256": file_hash(path),
            }
            for path in artifact_paths
        ]
        manifest = {
            "schema_version": 1,
            "packages": len(packages), "themes": len(themes),
            "artifacts": artifacts,
        }
        manifest["artifact_set_sha256"] = sha256_bytes(canonical_json(artifacts).encode())
        self.artifacts.write(f"{root}/manifest.json", manifest)

    @staticmethod
    def _quality_prompt_block(quality: Mapping[str, Any]) -> str:
        return canonical_json({
            "status": quality["status"], "reasons": quality["reasons"],
            "metrics": {
                key: quality["metrics"][key]
                for key in _QUALITY_NOTE_METRIC_KEYS if key in quality["metrics"]
            },
        })

    @staticmethod
    def _quality_notice(quality: Mapping[str, Any]) -> str:
        if quality["status"] == "complete":
            return ""
        reasons = "、".join(quality["reasons"])
        metrics = "、".join(
            f"{key}={quality['metrics'][key]}"
            for key in _QUALITY_NOTE_METRIC_KEYS if key in quality["metrics"]
        )
        detail = f"；相关指标：{metrics}" if metrics else ""
        return (
            f"> 来源质量提示：结构化解析状态为 {quality['status']}；"
            f"已知限制代码：{reasons}{detail}。"
            "涉及公式、图表、页码与定位的结论需回到原始 HTML/PDF 核验。"
        )


if __name__ == "__main__":
    DocumentSmartStep.cli_main("05_smart")
