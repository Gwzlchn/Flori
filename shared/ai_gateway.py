"""AI Gateway：Provider 适配 + 路由 + 成本追踪。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import structlog

from .errors import AIProviderError, AIRateLimitError, AllProvidersFailedError
from .models import AIUsage, LLMRequest, LLMResponse

_log = structlog.get_logger(component="ai_gateway")


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

        # 视觉:把帧图绝对路径写进 prompt,放开 Read 工具让 claude 逐张查看(订阅路径不支持 base64)。
        # 帧目录用 --add-dir 加入可访问范围,容器/无头干净环境也能读到。
        extra_dirs: set[str] = set()
        if request.images:
            prompt_content += "\n截图(用 Read 工具逐张查看):\n"
            for p in request.images:
                ap = str(Path(p).resolve())
                prompt_content += ap + "\n"
                extra_dirs.add(str(Path(ap).parent))

        # 命令模板里的 {prompt_file} 占位已弃用——prompt 改走 stdin(无 ARG_MAX 限制、不依赖文件读)。
        cmd = [part for part in self._command_template if "{prompt_file}" not in part]
        if request.images:
            cmd += ["--allowedTools", "Read"]
            # 限轮数:每张图一个 Read 轮,多图时上下文超线性膨胀会拖垮(实测 20 张丢图无界跑 >18min)。
            # 留几轮给思考+生成。配合 step 侧限图数,把视觉笔记控制在分钟级。
            cmd += ["--max-turns", str(len(request.images) + 5)]
            for d in sorted(extra_dirs):
                cmd += ["--add-dir", d]
        else:
            # 纯文本调用(评审/标点):禁用全部工具(--tools "")强制单次纯文本生成。
            # 否则 claude -p 默认带工具,大 prompt(评审)下会尝试调工具→消耗第 1 轮→
            # max-turns 1 截断报 "Reached max turns (1)"(线上 11_review 实测此因失败);
            # 即便不报错也会多轮 agentic"思考",一个打分跑成 >15min。
            # 工具禁掉后只能产出 1 个文本轮,max-turns 1 即安全(实测 ~14-35s)。
            cmd += ["--tools", "", "--max-turns", "1"]

        env = {**os.environ, **self._env}
        timeout = min(600 + 25 * len(request.images or []), 1800)  # 图越多给越久
        start = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt_content.encode()), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            # 已 SIGKILL,有界回收即可:正常进程瞬间退出。若残留管道/僵尸卡住 wait()
            # (孤儿孙进程持 fd 等),不能让 worker 无限挂起——best-effort 回收后照常抛超时。
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise AIProviderError(f"CLI timeout after {timeout}s")
        duration = time.time() - start

        if proc.returncode != 0:
            detail = (stderr.decode() + stdout.decode())[:500]
            # 订阅用量/限流：归为 AIRateLimitError 走长退避等配额恢复(而非快速重试转终态)。
            low = detail.lower()
            if any(k in low for k in (
                "rate limit", "rate_limit", "usage limit", "429",
                "overloaded", "quota", "too many requests", "limit reached",
            )):
                raise AIRateLimitError(f"CLI rate-limited: {detail}")
            raise AIProviderError(f"CLI failed: {detail}")

        return LLMResponse(
            content=stdout.decode().strip(),
            model="subscription",
            provider="claude-cli",
            cost_usd=0.0,
            duration_sec=round(duration, 2),
        )


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
        errors: list[str] = []   # 累计各 provider 真实报错，附进异常→落 error.json，便于排错
        rate_limited = False     # 任一 provider 限流 → 整体按 ai_rate_limit 走长退避

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
            except (AIProviderError, AIRateLimitError) as e:
                rate_limited = rate_limited or isinstance(e, AIRateLimitError)
                _log.warning("provider_failed", step=step_name, tier=tier,
                             provider=cfg.get("provider"), model=cfg.get("model"),
                             rate_limited=isinstance(e, AIRateLimitError), error=str(e)[:400])
                errors.append(f"{tier}/{cfg.get('provider')}: {str(e)[:200]}")
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
            except (AIProviderError, AIRateLimitError) as e:
                rate_limited = rate_limited or isinstance(e, AIRateLimitError)
                _log.warning("provider_failed", step=step_name, tier="text_fallback",
                             provider=cfg.get("provider"), model=cfg.get("model"),
                             rate_limited=isinstance(e, AIRateLimitError), error=str(e)[:400])
                errors.append(f"text_fallback/{cfg.get('provider')}: {str(e)[:200]}")

        raise AllProvidersFailedError(
            f"All providers failed for step {step_name} :: " + " || ".join(errors),
            error_type="ai_rate_limit" if rate_limited else "ai",
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
