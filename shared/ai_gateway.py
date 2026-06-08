"""AI Gateway：Provider 适配 + 路由 + 成本追踪。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from .errors import AIProviderError, AIRateLimitError, AllProvidersFailedError
from .models import AIUsage, LLMRequest, LLMResponse


# ── 成本表（USD per 1M tokens）──

PRICING: dict[tuple[str, str], dict[str, float]] = {
    ("anthropic", "claude-opus-4-6"): {"input": 15.0, "output": 75.0},
    ("anthropic", "claude-sonnet-4-6"): {"input": 3.0, "output": 15.0},
    ("anthropic", "claude-haiku-4-5"): {"input": 0.80, "output": 4.0},
    ("openai", "gpt-4o"): {"input": 2.5, "output": 10.0},
    ("openai", "gpt-4o-mini"): {"input": 0.15, "output": 0.6},
    ("deepseek", "deepseek-v4-flash"): {"input": 0.07, "output": 0.28},
    ("deepseek", "deepseek-v4-pro"): {"input": 0.49, "output": 1.96},
    ("kimi", "moonshot-v1-8k"): {"input": 0.17, "output": 0.17},
    ("kimi", "moonshot-v1-32k"): {"input": 0.34, "output": 0.34},
    ("kimi", "moonshot-v1-128k"): {"input": 0.84, "output": 0.84},
}


def calc_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICING.get((provider, model), {"input": 0, "output": 0})
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


# ── Provider 实现 ──


class DryRunProvider:
    """DRY_RUN 模式：不调真实 API。"""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=f"[DRY_RUN] {len(request.messages)} messages, model={request.model}",
            model=request.model or "dry-run",
            provider="dry-run",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            duration_sec=0.0,
        )


class AnthropicProvider:
    """Anthropic API（SDK: anthropic）。"""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._get_client()
        start = time.time()

        kwargs: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": self._build_messages(request),
        }
        if request.system:
            kwargs["system"] = request.system

        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None, partial(client.messages.create, **kwargs)
            )
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str:
                raise AIRateLimitError(str(e))
            raise AIProviderError(str(e))

        duration = time.time() - start
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        content = response.content[0].text if response.content else ""

        return LLMResponse(
            content=content,
            model=request.model,
            provider="anthropic",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=calc_cost("anthropic", request.model, input_tokens, output_tokens),
            duration_sec=round(duration, 2),
            cached=getattr(response.usage, "cache_read_input_tokens", 0) > 0,
        )

    def _build_messages(self, request: LLMRequest) -> list[dict]:
        messages = []
        for msg in request.messages:
            if request.images and msg["role"] == "user":
                import base64
                content_parts = [{"type": "text", "text": msg["content"]}]
                for img_path in request.images:
                    img_data = Path(img_path).read_bytes()
                    suffix = Path(img_path).suffix.lstrip(".")
                    media_type = f"image/{suffix}" if suffix != "jpg" else "image/jpeg"
                    content_parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(img_data).decode(),
                        },
                    })
                messages.append({"role": msg["role"], "content": content_parts})
            else:
                messages.append(msg)
        return messages


class OpenAICompatibleProvider:
    """OpenAI 兼容 API（DeepSeek / Qwen / Ollama / vLLM）。"""

    def __init__(self, base_url: str, api_key: str, provider_name: str = "openai_compatible"):
        self._base_url = base_url
        self._api_key = api_key
        self._provider_name = provider_name
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._get_client()
        start = time.time()

        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(request.messages)

        kwargs: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if request.response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None, partial(client.chat.completions.create, **kwargs)
            )
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str:
                raise AIRateLimitError(str(e))
            raise AIProviderError(str(e))

        duration = time.time() - start
        choice = response.choices[0]
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0

        return LLMResponse(
            content=choice.message.content or "",
            model=request.model,
            provider=self._provider_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=calc_cost(self._provider_name, request.model, input_tokens, output_tokens),
            duration_sec=round(duration, 2),
        )


class ClaudeCLIProvider:
    """Claude CLI 订阅（subprocess 调用）。"""

    def __init__(self, command_template: list[str], env: dict | None = None):
        self._command_template = command_template
        self._env = env or {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        prompt_content = ""
        if request.system:
            prompt_content += f"[System]\n{request.system}\n\n"
        for msg in request.messages:
            prompt_content += f"[{msg['role'].title()}]\n{msg['content']}\n\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(prompt_content)
            prompt_file = f.name

        try:
            cmd = [
                part.replace("{prompt_file}", prompt_file)
                for part in self._command_template
            ]
            env = {**os.environ, **self._env}
            start = time.time()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise AIProviderError("CLI timeout after 600s")
            duration = time.time() - start

            if proc.returncode != 0:
                raise AIProviderError(f"CLI failed: {stderr.decode()[:500]}")

            return LLMResponse(
                content=stdout.decode().strip(),
                model="subscription",
                provider="claude-cli",
                cost_usd=0.0,
                duration_sec=round(duration, 2),
            )
        finally:
            Path(prompt_file).unlink(missing_ok=True)


# ── Gateway ──


class AIGateway:
    """面向调用方的门面。路由 + 降级 + 成本追踪。"""

    def __init__(self, providers_config: dict, pipelines_config: dict):
        self._providers_config = providers_config
        self._pipelines_config = pipelines_config
        self._providers: dict[str, Any] = {}
        self._dry_run = os.environ.get("DRY_RUN") == "1"
        self._call_index = 0

    async def call(
        self,
        step_name: str,
        request: LLMRequest,
        job_id: str | None = None,
    ) -> LLMResponse:
        if self._dry_run:
            return await DryRunProvider().complete(request)

        ai_config = self._get_step_ai_config(step_name)
        has_images = bool(request.images)

        for tier in ["primary", "fallback"]:
            if tier not in ai_config:
                continue
            cfg = ai_config[tier]
            request.model = cfg["model"]
            try:
                provider = self._get_provider(cfg["provider"])
                response = await provider.complete(request)
                self._call_index += 1
                return response
            except (AIProviderError, AIRateLimitError):
                continue

        if has_images and "text_fallback" in ai_config:
            cfg = ai_config["text_fallback"]
            request.model = cfg["model"]
            request.images = []
            try:
                provider = self._get_provider(cfg["provider"])
                response = await provider.complete(request)
                self._call_index += 1
                return response
            except (AIProviderError, AIRateLimitError):
                pass

        raise AllProvidersFailedError(
            f"All providers failed for step {step_name}"
        )

    async def compare(
        self,
        step_name: str,
        request: LLMRequest,
        job_id: str | None = None,
    ) -> list[LLMResponse]:
        """多 Provider 并行对比（预留接口，尚未实现）。"""
        raise NotImplementedError("compare mode not implemented")

    def _get_step_ai_config(self, step_name: str) -> dict:
        steps = self._pipelines_config.get("steps", [])
        for s in steps:
            if s.get("name") == step_name:
                return s.get("ai", {})
        return {}

    def _get_provider(self, name: str):
        if name not in self._providers:
            self._providers[name] = self._create_provider(name)
        return self._providers[name]

    def _create_provider(self, name: str):
        cfg = self._providers_config.get("providers", {}).get(name, {})
        ptype = cfg.get("type", "")
        # 密钥不再随 step_cfg 落盘(已脱敏),配置缺省时按 {NAME}_API_KEY 约定从环境读。
        api_key = cfg.get("api_key") or os.environ.get(f"{name.upper()}_API_KEY", "")

        if ptype == "anthropic":
            return AnthropicProvider(api_key=api_key)
        elif ptype in ("openai_compatible", "openai"):
            return OpenAICompatibleProvider(
                base_url=cfg.get("base_url", ""),
                api_key=api_key,
                provider_name=name,
            )
        elif ptype == "cli":
            return ClaudeCLIProvider(
                command_template=cfg.get("command", []),
                env=cfg.get("env"),
            )
        else:
            raise AIProviderError(f"Unknown provider type: {ptype}")


# ── Usage 文件读写 ──


def record_usage_to_file(usage: AIUsage, log_dir: Path) -> None:
    """步骤进程调用：追加到 .{step}.usage.json。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f".{usage.step}.usage.json"
    entries = json.loads(path.read_text()) if path.exists() else []
    entries.append({
        "exec_id": usage.exec_id,
        "provider": usage.provider,
        "model": usage.model,
        "job_id": usage.job_id,
        "step": usage.step,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": usage.cost_usd,
        "duration_sec": usage.duration_sec,
        "cached": usage.cached,
        "created_at": usage.created_at.isoformat(),
    })
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def collect_usage_from_file(log_dir: Path, step: str) -> list[AIUsage]:
    """Worker 调用：读取 usage 文件，返回 AIUsage 列表。"""
    path = log_dir / f".{step}.usage.json"
    if not path.exists():
        return []
    entries = json.loads(path.read_text())
    return [
        AIUsage(
            exec_id=e["exec_id"],
            provider=e["provider"],
            model=e["model"],
            job_id=e.get("job_id"),
            step=e.get("step"),
            input_tokens=e.get("input_tokens", 0),
            output_tokens=e.get("output_tokens", 0),
            cost_usd=e.get("cost_usd", 0.0),
            duration_sec=e.get("duration_sec", 0.0),
            cached=e.get("cached", False),
            created_at=datetime.fromisoformat(e["created_at"]),
        )
        for e in entries
    ]
