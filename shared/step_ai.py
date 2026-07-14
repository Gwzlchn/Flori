"""步骤 AI 调用、Prompt 接线、usage 和审计日志组件。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Callable

from .ai_gateway import AIGateway, record_usage_to_file
from .ai_routing import (
    InvalidAIOverrideError,
    READ_TOOL_TAG,
    ai_required_tags,
    parse_ai_override,
    step_required_capability_tags_sync,
)
from .models import AIUsage, DEFAULT_AI_MODEL, LLMRequest, LLMResponse
from .step_artifacts import ArtifactIO, file_hash
from .structured_output import StructuredOutputParser


class AIInvocation:
    """封装单步 AI 路由、Prompt 解析和逐调用审计状态。"""

    def __init__(
        self,
        *,
        step_name: str,
        job_dir: Path,
        config: dict,
        log,
        input_hashes: Callable[[], dict[str, str]],
        artifacts: ArtifactIO,
        structured: StructuredOutputParser,
    ):
        self.step_name = step_name
        self.job_dir = job_dir
        self.config = config
        self.log = log
        self.input_hashes = input_hashes
        self.artifacts = artifacts
        self.structured = structured
        self.gateway: AIGateway | None = None
        self.call_index = 0
        self.last_provider: str | None = None
        self.last_model: str | None = None
        self.last_response: LLMResponse | None = None
        self.resolved_prompts: dict[str, object] = {}
        self.prompt_overrides_snapshot: dict | None = None
        self.active_prompt_name: str | None = None
        self.ai_log_records: list[dict] = []
        self._load_existing_logs()

    def provider_model(self) -> tuple[str, str]:
        """返回最近一次实际命中的 provider 和 model。"""
        provider = self.last_provider or "unknown"
        model = self.last_model or "unknown"
        if provider == "claude-cli" and model in ("unknown", ""):
            model = DEFAULT_AI_MODEL
        return provider, model

    def _read_override(self) -> str:
        try:
            job = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except OSError as exc:
            self.log.warning("ai_override_read_failed", reason="job_json_unreadable")
            raise InvalidAIOverrideError("invalid AI override: job_json_unreadable") from exc
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self.log.warning("ai_override_read_failed", reason="job_json_invalid")
            raise InvalidAIOverrideError("invalid AI override: job_json_invalid") from exc
        override, shape_error = parse_ai_override(
            job, self.step_name, self.config.get("providers", {}),
        )
        if shape_error:
            self.log.warning("ai_override_invalid", reason=shape_error)
            raise InvalidAIOverrideError(f"invalid AI override: {shape_error}")
        return override or ""

    def override_provider(self) -> str:
        """读取本步 provider 覆盖,供输入指纹和调用路由共用。"""
        return self._read_override()

    def apply_provider_override(
        self, *, required_capabilities=(), actual_capabilities: bool = False,
    ) -> None:
        provider = self._read_override()
        selected_ai = self.config.get("ai")
        if provider:
            provider_config = (
                self.config.get("providers", {}).get("providers", {}).get(provider, {})
            )
            models = provider_config.get("models", [])
            model = models[0] if models else (provider_config.get("model") or "unknown")
            selected_ai = {"primary": {"provider": provider, "model": model}}

        try:
            if actual_capabilities:
                capability_tags = sorted(set(required_capabilities))
            else:
                capability_tags = step_required_capability_tags_sync(
                    self.config.get("step", {}),
                    lambda rel: (self.job_dir / rel).is_file()
                    and (self.job_dir / rel).stat().st_size > 0,
                )
                capability_tags = sorted({*capability_tags, *required_capabilities})
            ai_required_tags(
                selected_ai,
                self.config.get("providers", {}),
                required_tags=capability_tags,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise InvalidAIOverrideError(f"invalid AI capability: {exc}") from exc
        if provider:
            self.config["ai"] = selected_ai
            self.gateway = None

    def call(self, prompt: str, images: list[Path] | None = None, **kwargs) -> str:
        allowed_tools = kwargs.get("allowed_tools")
        required_capabilities = {
            READ_TOOL_TAG
            for tool in (
                allowed_tools if isinstance(allowed_tools, (list, tuple)) else []
            )
            if type(tool) is str and tool.strip().lower() == "read"
        }
        self.apply_provider_override(
            required_capabilities=required_capabilities,
            actual_capabilities=True,
        )
        if self.gateway is None:
            self.gateway = AIGateway(
                self.config.get("providers", {}),
                {"steps": [{"name": self.step_name, "ai": self.config.get("ai", {})}]},
            )

        system = self.load_system_prompt()
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            images=images or [],
            system=system,
            **kwargs,
        )
        started_at = datetime.now()
        pending_position = self._write_log_pending(
            prompt, system, images, request, started_at,
        )
        try:
            response = asyncio.run(self.gateway.call(self.step_name, request))
        except Exception as exc:
            self._write_log_safe(
                prompt,
                system,
                images,
                request,
                None,
                started_at,
                datetime.now(),
                error=exc,
                replace_pos=pending_position,
            )
            self.call_index += 1
            raise
        ended_at = datetime.now()
        self.last_provider = response.provider
        self.last_model = response.model
        self.last_response = response

        self.log.info(
            "ai_call",
            provider=response.provider,
            model=response.model,
            cost_usd=response.cost_usd,
            tokens=f"{response.input_tokens}+{response.output_tokens}",
        )
        step_exec_id = os.environ.get(
            "STEP_EXEC_ID", f"{self.job_dir.name}:{self.step_name}",
        )
        record_usage_to_file(
            AIUsage(
                exec_id=f"{step_exec_id}:{self.call_index}",
                provider=response.provider,
                model=response.model,
                job_id=self.job_dir.name,
                step=self.step_name,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_creation_input_tokens=response.cache_creation_input_tokens,
                cache_read_input_tokens=response.cache_read_input_tokens,
                cost_usd=response.cost_usd,
                duration_sec=response.duration_sec,
                num_turns=response.num_turns,
                cached=response.cached,
            ),
            self.job_dir / "logs",
        )
        self._write_log_safe(
            prompt,
            system,
            images,
            request,
            response,
            started_at,
            ended_at,
            error=None,
            replace_pos=pending_position,
        )
        self.call_index += 1
        return response.content

    def call_json(
        self,
        prompt: str,
        fallback: dict,
        score_keys: list[str] | None = None,
        images: list[Path] | None = None,
        **kwargs,
    ) -> tuple[dict, bool]:
        kwargs.setdefault("response_format", "json")
        kwargs.setdefault("temperature", 0)
        raw = self.call(prompt, images=images, **kwargs)
        result, parse_failed, did_salvage = self.structured.parse(
            raw, fallback, score_keys,
        )
        self._amend_last_log({
            "output_processed": {
                "json_parse": {"ok": not parse_failed, "salvaged": did_salvage},
                "parse_failed": parse_failed,
                "extracted": {
                    key: value for key, value in result.items() if key != "raw_response"
                },
            },
        })
        return result, parse_failed

    def _log_path(self) -> Path:
        return self.job_dir / "output" / "ai_logs" / f"{self.step_name}.jsonl"

    def _load_existing_logs(self) -> None:
        try:
            path = self._log_path()
            if not path.exists():
                return
            records = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
            self.ai_log_records = records
            self.call_index = max(
                (int(record.get("call_index", -1)) for record in records), default=-1,
            ) + 1
        except Exception:
            self.log.warn("ai_log_load_existing_failed", step=self.step_name)

    def _flush_logs(self) -> None:
        path = self._log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
                for record in self.ai_log_records
            ),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _write_log_pending(
        self, prompt, system, images, request, started_at,
    ) -> int | None:
        try:
            record = self._build_log_record(
                prompt, system, images, request, None, started_at, started_at, None,
            )
            record["phase"] = "pending"
            record["ok"] = None
            record["error"] = None
            self.ai_log_records.append(record)
            self._flush_logs()
            return len(self.ai_log_records) - 1
        except Exception:
            self.log.warn("ai_log_pending_write_failed", step=self.step_name)
            return None

    def _write_log_safe(
        self,
        prompt,
        system,
        images,
        request,
        response,
        started_at,
        ended_at,
        error=None,
        replace_pos=None,
    ) -> None:
        try:
            record = self._build_log_record(
                prompt,
                system,
                images,
                request,
                response,
                started_at,
                ended_at,
                error,
            )
            record["phase"] = "final"
            if (
                replace_pos is not None
                and 0 <= replace_pos < len(self.ai_log_records)
                and self.ai_log_records[replace_pos].get("phase") == "pending"
                and self.ai_log_records[replace_pos].get("call_index")
                == record["call_index"]
            ):
                self.ai_log_records[replace_pos] = record
            else:
                self.ai_log_records.append(record)
            self._flush_logs()
        except Exception:
            self.log.warn("ai_log_write_failed", step=self.step_name)

    def _amend_last_log(self, patch: dict) -> None:
        try:
            if not self.ai_log_records:
                return
            self.ai_log_records[-1].update(patch)
            self._flush_logs()
        except Exception:
            pass

    @staticmethod
    def _flori_meta() -> dict:
        return {
            "image_tag": os.environ.get("FLORI_IMAGE_TAG") or os.environ.get("IMAGE_TAG"),
            "version": os.environ.get("FLORI_VERSION"),
            "git_commit": os.environ.get("FLORI_GIT_COMMIT"),
        }

    def _collect_transcript(self, response, attempts) -> dict:
        source = getattr(response, "transcript_path", None) if response is not None else None
        if not source:
            for attempt in reversed(attempts or []):
                if attempt.get("transcript_path"):
                    source = attempt["transcript_path"]
                    break
        if not source:
            return {
                "file": None,
                "reason": "no transcript (non-CLI provider or session log unavailable)",
            }
        try:
            data = Path(source).read_bytes()
            rel = f"output/ai_logs/{self.step_name}.turns.{self.call_index}.jsonl"
            destination = self.job_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return {
                "file": rel,
                "turns": data.count(b"\n"),
                "bytes": len(data),
                "source": str(source),
            }
        except Exception as exc:
            return {"file": None, "reason": f"copy failed: {exc}"[:200]}

    def _build_log_record(
        self, prompt, system, images, request, response, started_at, ended_at, error,
    ) -> dict:
        config = self.config or {}
        paths = config.get("paths") or {}
        prompts_dir = paths.get("prompts_dir")
        domain = (config.get("domain") or {}).get("name")

        profile: dict = {}
        try:
            profile = self.load_domain_prompt_profile() or {}
        except Exception:
            profile = {}
        profile_hash = None
        try:
            if prompts_dir and domain:
                profile_path = Path(prompts_dir) / "profiles" / f"{domain}.yaml"
                if profile_path.exists():
                    profile_hash = file_hash(profile_path)
        except Exception:
            pass

        resolved_templates = []
        for name, item in sorted(self.resolved_prompts.items()):
            resolved_templates.append({
                "name": name,
                "source": item.source,
                "sha256": item.sha256,
                "bytes": len(item.raw),
                "version": item.version,
            })
        template_meta = next(
            (
                item
                for item in resolved_templates
                if item["name"] == self.active_prompt_name
            ),
            None,
        ) or {
            "name": None,
            "source": None,
            "sha256": None,
            "bytes": 0,
            "version": None,
        }

        try:
            input_hashes = self.input_hashes()
        except Exception:
            input_hashes = {}
        try:
            job_meta = json.loads(
                (self.job_dir / "job.json").read_text(encoding="utf-8"),
            )
        except Exception:
            job_meta = {}

        image_records = []
        for image in images or []:
            record: dict = {"path": str(image)}
            try:
                path = Path(image)
                if path.exists():
                    record["hash"] = file_hash(path)
                    record["bytes"] = path.stat().st_size
            except Exception:
                pass
            image_records.append(record)

        ok = error is None and response is not None
        if response is not None:
            attempts, tier_used = response.attempts, response.tier_used
        else:
            attempts, tier_used = (getattr(error, "attempts", []) or []), None
        content_type = job_meta.get("content_type") or config.get("content_type")
        return {
            "job_id": self.job_dir.name,
            "step": self.step_name,
            "content_type": content_type,
            "pipeline": content_type,
            "domain": domain,
            "call_index": self.call_index,
            "exec_id": (
                f"{os.environ.get('STEP_EXEC_ID', self.job_dir.name + ':' + self.step_name)}"
                f":{self.call_index}"
            ),
            "session_id": getattr(response, "session_id", None),
            "ts_start": started_at.isoformat(),
            "ts_end": ended_at.isoformat(),
            "flori": self._flori_meta(),
            "config": {
                "step_config_resolved": {
                    "ai": config.get("ai"),
                    "pool": config.get("pool"),
                    "tags": config.get("tags"),
                    "style_tags": config.get("style_tags"),
                },
                "provider_override": self._read_override() or None,
            },
            "injected": {
                "domain_profile": {"name": domain, "hash": profile_hash},
                "style_tags": config.get("style_tags") or [],
                "terminology_snapshot": profile.get("terminology"),
            },
            "input_hashes": input_hashes,
            "routing": {
                "requested_ai": config.get("ai"),
                "tier_used": tier_used,
                "provider": getattr(response, "provider", None),
                "model": getattr(response, "model", None),
                "attempts": attempts,
            },
            "latency": {
                "ttft_ms": getattr(response, "ttft_ms", None),
                "api_ms": getattr(response, "api_ms", None),
                "duration_total_sec": (
                    getattr(response, "duration_sec", None)
                    if response is not None
                    else round((ended_at - started_at).total_seconds(), 2)
                ),
            },
            "call_meta": {
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "response_format": request.response_format,
                "allowed_tools": request.allowed_tools,
                "max_turns": request.max_turns,
                "images_count": len(images or []),
            },
            "prompt": {
                "rendered": {"system": system, "user": prompt},
                "template": template_meta,
                "templates": resolved_templates,
                "values": {
                    "domain_profile_name": domain,
                    "terminology_snapshot": profile.get("terminology"),
                    "style_tags": config.get("style_tags") or [],
                },
                "images": image_records,
            },
            "output": {
                "content": getattr(response, "content", None),
                "num_turns": getattr(response, "num_turns", None),
                "finish_reason": getattr(response, "finish_reason", None),
            },
            "transcript": self._collect_transcript(response, attempts),
            "output_processed": None,
            "usage": {
                "input_tokens": getattr(response, "input_tokens", 0),
                "output_tokens": getattr(response, "output_tokens", 0),
                "cache_creation_input_tokens": getattr(
                    response, "cache_creation_input_tokens", 0,
                ),
                "cache_read_input_tokens": getattr(response, "cache_read_input_tokens", 0),
            },
            "cost": {
                "cost_usd": getattr(response, "cost_usd", 0.0),
                "basis": (
                    "cli-equiv"
                    if getattr(response, "provider", None) == "claude-cli"
                    else "priced"
                ),
            },
            "raw": getattr(response, "raw", None),
            "links": {
                "source": {
                    "job_url": job_meta.get("url"),
                    "collection": job_meta.get("collection_id"),
                    "published_at": job_meta.get("published_at"),
                },
            },
            "feedback": None,
            "env": {
                "worker_id": os.environ.get("WORKER_ID")
                or os.environ.get("FLORI_WORKER_ID"),
                "host": socket.gethostname(),
                "pool": config.get("pool"),
            },
            "ok": ok,
            "error": None if ok else (str(error)[:2000] if error else "unknown"),
        }

    def job_prompt_overrides(self):
        snapshot = self.prompt_overrides_snapshot
        if snapshot is not None:
            return snapshot
        try:
            job = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.prompt_overrides_snapshot = {}
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            from .prompt_resolver import PromptResolutionError

            raise PromptResolutionError("prompt override job metadata is invalid") from exc
        if not isinstance(job, dict):
            from .prompt_resolver import PromptResolutionError

            raise PromptResolutionError("prompt override job metadata is invalid")
        if "prompt_overrides" not in job:
            overrides = {}
        else:
            overrides = job["prompt_overrides"]
            if not isinstance(overrides, dict):
                from .prompt_resolver import PromptResolutionError

                raise PromptResolutionError("prompt override map is invalid")
        self.prompt_overrides_snapshot = overrides
        return overrides

    def injected_prompt_override(self) -> str:
        from .prompt_resolver import parse_prompt_override

        parsed = parse_prompt_override(self.job_prompt_overrides(), self.step_name)
        return parsed[0].decode("utf-8") if parsed is not None else ""

    def primary_prompt_template(self) -> str:
        step = self.config.get("step") or {}
        name = step.get("prompt_template") or self.step_name
        if not isinstance(name, str) or not name:
            from .prompt_resolver import PromptResolutionError

            raise PromptResolutionError("prompt template mapping is invalid")
        return name

    def prompt_resolver(self):
        from .prompt_resolver import PromptResolver

        paths = self.config.get("paths") or {}
        prompts_dir = Path(paths.get("prompts_dir", "/data/prompts"))
        config_dir = Path(paths.get("config_dir", "/app/configs"))
        return PromptResolver(
            hot_dir=prompts_dir / "templates",
            image_dir=config_dir / "prompts" / "templates",
        )

    def resolve_prompt_template(self, name: str):
        if name not in self.resolved_prompts:
            self.resolved_prompts[name] = self.prompt_resolver().resolve(
                name,
                step_name=self.step_name,
                prompt_overrides=self.job_prompt_overrides(),
                primary_template=self.primary_prompt_template(),
            )
        return self.resolved_prompts[name]

    def has_step_template(self) -> bool:
        from .prompt_resolver import TRACKED_TEMPLATE_NAMES

        primary = self.primary_prompt_template()
        if any(
            name == primary or name.startswith(primary + ".")
            for name in TRACKED_TEMPLATE_NAMES
        ):
            return True
        return self.prompt_resolver().template_exists(primary)

    def load_system_prompt(self) -> str | None:
        injected = self.injected_prompt_override()
        if injected and not self.has_step_template():
            return injected
        paths = self.config.get("paths") or {}
        candidates = (
            Path(paths.get("prompts_dir", "/data/prompts")) / f"{self.step_name}.md",
            Path(paths.get("config_dir", "/app/configs"))
            / "prompts"
            / f"{self.step_name}.md",
        )
        for path in candidates:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            except OSError as exc:
                from .prompt_resolver import PromptResolutionError

                raise PromptResolutionError("system prompt is unreadable") from exc
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                from .prompt_resolver import PromptResolutionError

                raise PromptResolutionError("system prompt is not UTF-8") from exc
        return None

    def load_domain_prompt_profile(self) -> dict:
        import yaml

        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        profile_path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if profile_path.exists():
            return yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        return {}

    @staticmethod
    def terminology_block(profile: dict) -> str:
        terms = (profile or {}).get("terminology")
        if not terms:
            return ""
        joined = "; ".join(terms[:30])
        return (
            "\n本领域已沉淀的标准概念（命中时沿用统一措辞、无需重新展开解释；"
            f"只对下列未涵盖的新概念做首次解释）：\n{joined}\n"
        )

    def prompt_profile_style_hashes(self) -> dict[str, str]:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        hashes: dict[str, str] = {}
        prompt_path = prompts_dir / f"{self.step_name}.md"
        if prompt_path.exists():
            hashes["prompt"] = file_hash(prompt_path)
        template = self.template_hash(self.primary_prompt_template())
        if template:
            hashes["template"] = template
        profile_path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if profile_path.exists():
            hashes["profile"] = file_hash(profile_path)
        hashes["styles"] = json.dumps({
            tag: file_hash(prompts_dir / "styles" / f"{tag}.yaml")
            for tag in sorted(self.config.get("style_tags", []))
            if (prompts_dir / "styles" / f"{tag}.yaml").exists()
        }, sort_keys=True)
        return hashes

    def load_prompt_template(self, name: str) -> str:
        resolved = self.resolve_prompt_template(name)
        self.active_prompt_name = name
        return resolved.text

    def template_hash(self, *names: str) -> str:
        present = {
            name: self.resolve_prompt_template(name).sha256
            for name in sorted(names)
        }
        return json.dumps(present, sort_keys=True)
