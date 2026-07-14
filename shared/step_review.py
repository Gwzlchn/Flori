"""智能笔记落盘和可靠评审执行组件。"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .errors import ProcessingError
from .models import LLMResponse
from .step_ai import AIInvocation
from .step_artifacts import ArtifactIO


class ReviewExecution:
    """封装智能笔记净化、评审输入留痕和可靠性解析。"""

    _PREAMBLE_MARK = (
        "已完成", "我做了什么", "我做的", "我的处理", "处理思路", "重组思路", "笔记结构一览",
        "结构化学习笔记", "保存在", "保存到", "已生成并保存", "思路如下",
        "I've ", "I have ", "I now ", "Here'", "Here is", "Let me ", "I'll ",
    )
    _OFFER_MARK = (
        "要不要我", "需要我", "如需", "需要的话", "如果需要", "我可以再", "我还可以",
        "是否需要", "可以帮你", "如有需要", "Let me know", "Would you like", "If you",
    )
    _TRAIL_META = (
        "我已", "我把", "我按", "已按", "我对", "我将", "我用", "我把视频", "我已经",
        "I've ", "I have ", "I've reorganized", "I restructured",
    )
    _META_HEAD = (
        "我做了什么", "我做的", "我的处理", "处理说明", "处理思路", "重组思路",
        "笔记结构一览", "改动说明", "What I did", "Summary",
    )
    _API_PROVIDERS = ("anthropic", "deepseek", "kimi", "openai", "ollama", "local")

    def __init__(
        self,
        *,
        step_name: str,
        job_dir: Path,
        artifacts: ArtifactIO,
        ai: AIInvocation,
    ):
        self.step_name = step_name
        self.job_dir = job_dir
        self.artifacts = artifacts
        self.ai = ai

    @classmethod
    def sanitize_smart_note(cls, content: str, provider: str | None = None) -> str:
        text = (content or "").strip()
        if os.environ.get("DRY_RUN") == "1":
            return text
        strict = (provider or "") not in cls._API_PROVIDERS
        if any(marker in text[:160] for marker in cls._PREAMBLE_MARK):
            match = re.search(r"(?m)^#{1,6} ", text)
            if match:
                text = text[match.start():].strip()
        paragraphs = text.split("\n\n")
        while paragraphs:
            tail = paragraphs[-1].strip().lstrip("-*># ").strip()
            is_offer = len(tail) < 200 and any(
                marker in tail[:24] for marker in cls._OFFER_MARK
            )
            is_meta = len(tail) < 500 and any(
                tail.startswith(marker) for marker in cls._TRAIL_META
            )
            if tail and (is_offer or is_meta):
                paragraphs.pop()
                while paragraphs and paragraphs[-1].strip() in ("---", "***", "___"):
                    paragraphs.pop()
            else:
                break
        text = "\n\n".join(paragraphs).strip()
        first_heading = next(
            (line for line in text.splitlines() if line.lstrip().startswith("#")), "",
        )
        heading_is_meta = any(marker in first_heading for marker in cls._META_HEAD)
        if strict and (len(text) < 500 or heading_is_meta):
            raise ProcessingError(
                f"智能笔记疑似 agentic 退化(len={len(text)}, 首标题={first_heading[:40]!r}):"
                "claude 可能只回了过程汇报而非笔记正文,触发重试。",
            )
        return re.sub(
            r"(!\[[^\]]*\]\()(?!https?:|/|assets/)([^)\s]+\.(?:jpg|jpeg|png|webp|gif))(\))",
            r"\1assets/\2\3",
            text,
        )

    @staticmethod
    def backfill_image_refs(content: str, image_map: dict) -> str:
        def replace(match):
            filename = image_map.get(int(match.group(2)))
            return f"{match.group(1)}assets/{filename}{match.group(3)}" if filename else ""

        return re.sub(
            r"(!\[[^\]]*\]\()\s*img:(\d+)\s*(\))", replace, content or "",
        )

    def write_smart_note(
        self, content: str, image_assets: list | None = None,
    ) -> str:
        provider, model = self.ai.provider_model()
        if image_assets:
            image_map = {
                int(asset["n"]): asset["filename"]
                for asset in image_assets
                if asset.get("filename")
            }
            content = self.backfill_image_refs(content, image_map)
        content = self.sanitize_smart_note(content, provider)

        def safe(value: str) -> str:
            return re.sub(r"[^0-9A-Za-z.-]+", "-", value).strip("-") or "x"

        now = datetime.now()
        rel = (
            f"output/versions/notes_smart_{safe(provider)}_{safe(model)}_"
            f"{now.strftime('%Y%m%d-%H%M%S')}.md"
        )
        header = (
            f"> 生成于 {now.strftime('%Y/%m/%d %H:%M:%S')} · "
            f"方式 {provider} · 模型 {model}\n\n"
        )
        self.artifacts.write(rel, header + content)
        return rel

    @staticmethod
    def clip_note_for_review(smart: str) -> tuple[str, dict]:
        return smart, {
            "note_chars": len(smart),
            "reviewed_chars": len(smart),
            "truncated": False,
        }

    def write_review(self, review: dict, note_file: str | None) -> None:
        provider, model = self.ai.provider_model()
        review["note_file"] = note_file
        review["provider"] = provider
        review["model"] = model
        review["generated_at"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.artifacts.write("output/review.json", review)
        if note_file:
            from .notes_versions import review_path_for_note

            rel = review_path_for_note(note_file)
            if rel:
                self.artifacts.write(rel, review)

    def build_prompt(
        self, *, intro: str, dimensions: list[tuple[str, str]], ref_block: str,
    ) -> str:
        dimension_lines = "".join(
            f"{index}. {key}: {description}\n"
            for index, (key, description) in enumerate(dimensions, 1)
        )
        example_scores = ", ".join(f'"{key}": 4' for key, _ in dimensions)
        template = self.ai.load_prompt_template(self.step_name)
        rendered = (
            template
            .replace("{{intro}}", intro)
            .replace("{{dimensions}}", dimension_lines)
            .replace("{{score_example}}", example_scores)
            .replace("{{ref_block}}", ref_block)
        )
        if "{{ref_block}}" not in template:
            rendered = rendered.rstrip() + "\n\n" + ref_block
        return rendered

    @staticmethod
    def fallback(score_keys: list[str]) -> dict:
        fallback = {key: 3 for key in score_keys}
        fallback.update(
            overall=3.0,
            key_terms=[],
            missing_concepts=[],
            top3_improvements=["AI 返回的不是有效 JSON"],
        )
        return fallback

    def prepare_smart(self) -> tuple[str, dict, str, dict]:
        from .review_contract import source_record

        smart_path = self.artifacts.latest_smart_note()
        if smart_path is None:
            raise ValueError("review source has no smart note")
        note_file = str(smart_path.relative_to(self.job_dir))
        smart, record = source_record(self.job_dir, note_file, label="smart")
        smart_clip, coverage = self.clip_note_for_review(smart)
        return smart_clip, coverage, note_file, record

    def run_dimension(
        self,
        prompt,
        fallback,
        score_keys,
        note_file,
        coverage,
        *,
        review_sources: list[dict] | None = None,
        review_source_texts: dict[str, str] | None = None,
        citation_validation: dict | None = None,
        evidence_manifest_record: dict | None = None,
    ):
        del fallback
        from .review_contract import (
            MAX_REVIEW_SOURCE_AGGREGATE_BYTES,
            MAX_REVIEW_SOURCE_BYTES,
            MAX_REVIEW_SOURCES,
            parse_review,
            sha256_bytes,
        )

        if type(prompt) is not str:
            raise ValueError("review input must be a string")
        try:
            prompt_data = prompt.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("review input must be UTF-8") from exc
        if len(prompt_data) > MAX_REVIEW_SOURCE_BYTES:
            raise ValueError(f"review input exceeds {MAX_REVIEW_SOURCE_BYTES} bytes")
        validate_sources = review_sources is not None or review_source_texts is not None
        sources = review_sources or []
        if validate_sources and (not sources or len(sources) > MAX_REVIEW_SOURCES):
            raise ValueError("review sources count is invalid")
        labels: set[str] = set()
        declared_total = 0
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("review source record is invalid")
            label = source.get("label")
            size = source.get("bytes")
            if type(label) is not str or not label or label in labels:
                raise ValueError("review source label is invalid")
            if type(size) is not int or size < 0 or size > MAX_REVIEW_SOURCE_BYTES:
                raise ValueError("review source size is invalid")
            labels.add(label)
            declared_total += size
        if declared_total > MAX_REVIEW_SOURCE_AGGREGATE_BYTES:
            raise ValueError("review sources exceed aggregate byte limit")
        source_texts = review_source_texts or {}
        actual_total = 0
        for label in labels:
            text = source_texts.get(label)
            if type(text) is not str:
                raise ValueError("review source text is missing")
            actual_total += len(text.encode("utf-8"))
        if validate_sources and (
            actual_total != declared_total
            or actual_total > MAX_REVIEW_SOURCE_AGGREGATE_BYTES
        ):
            raise ValueError("review source bytes do not match records")
        self.artifacts.write("output/review_input.md", prompt)
        prompt_rel = "output/review_input.md"
        if note_file:
            name = Path(note_file).name.replace("notes_smart_", "review_input_", 1)
            prompt_rel = f"output/versions/{name}"
            self.artifacts.write(prompt_rel, prompt)
        review_input = {
            "artifact": prompt_rel,
            "sha256": sha256_bytes(prompt_data),
            "bytes": len(prompt_data),
            "chars": len(prompt),
            "truncated": bool(coverage.get("truncated")),
            "sources": sources,
        }
        if evidence_manifest_record is not None:
            review_input["evidence_manifest"] = evidence_manifest_record
        raw = self.ai.call(prompt, response_format="json", temperature=0)
        response = self.ai.last_response or LLMResponse(
            content=raw,
            model=self.ai.last_model or "unknown",
            provider=self.ai.last_provider or "unknown",
            finish_reason=None,
        )
        review, parse_failed = parse_review(
            raw,
            score_keys,
            response,
            review_input=review_input,
            review_source_texts=source_texts,
            citation_validation=citation_validation,
        )
        review["review_coverage"] = coverage
        self.write_review(review, note_file)
        return review, parse_failed
