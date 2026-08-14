"""AI Gateway: Provider 适配 + 路由 + 成本追踪。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import re
import threading
from functools import lru_cache
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from .ai_routing import AI_PARAM_MODEL_DOMAIN_MISSING, validate_ai_param_override
from .errors import AIProviderError, AIRateLimitError, AllProvidersFailedError
from .models import AIUsage, DEFAULT_AI_MODEL, LLMRequest, LLMResponse, MAX_AI_CREDITS

_log = structlog.get_logger(component="ai_gateway")

# 走 CLI 子进程的 provider type。按 type 而非名字判:自定义 CLI provider 也必须过参数复核。
from .ai_routing import CLI_PROVIDER_TYPES as _CLI_PROVIDER_TYPES
from .ai_routing import resolve_api_credential


# 成本表(USD per 1M tokens)

PRICING: dict[tuple[str, str], dict[str, float]] = {
    ("anthropic", "claude-opus-4-8"): {"input": 15.0, "output": 75.0},
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


def calc_cost(provider: str, model: str, input_tokens: int, output_tokens: int,
              cache_creation_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    prices = PRICING.get((provider, model))
    if prices is None:
        # 未命中 PRICING:成本按 0 计,但打 warning,用来区分刻意免费和配置漂移漏命中。
        # 刻意免费指 cli/dry-run 不走本函数;配置漂移指 providers.yaml 模型名与 PRICING 键不一致。
        _log.warning("pricing_miss", provider=provider, model=model,
                     msg="未命中 PRICING,成本按 0 计——核对模型名与 PRICING 键是否一致")
        prices = {"input": 0, "output": 0}
    inp = prices["input"]
    # 缓存感知,Anthropic 口径(同 codeburn/LiteLLM):写缓存 5min 档 ≈1.25× 输入价、读缓存 ≈0.1× 输入价。
    return (
        input_tokens * inp
        + cache_creation_tokens * inp * 1.25
        + cache_read_tokens * inp * 0.1
        + output_tokens * prices["output"]
    ) / 1_000_000


def _extract_cli_model(obj: dict) -> str:
    """从 `claude -p --output-format json` 顶层 JSON 取真实模型名,兼容多种 CLI 输出。
    优先顶层 `model`,部分输出格式没有;否则在 `modelUsage`(形如 {模型名: {token...}})里
    取 token 总数最大的键,即主力模型。都拿不到时返回空串,由调用方用请求模型兜底。"""
    m = obj.get("model")
    if isinstance(m, str) and m:
        return m
    usage = obj.get("modelUsage")
    if isinstance(usage, dict) and usage:
        def _tok_total(v: object) -> int:
            if not isinstance(v, dict):
                return 0
            return sum(
                int(x or 0)
                for x in v.values()
                if isinstance(x, (int, float)) and not isinstance(x, bool)
            )
        best = max(usage.items(), key=lambda kv: _tok_total(kv[1]))[0]
        if isinstance(best, str) and best:
            return best
    return ""


# CLI 额度/限流关键词:命中归 AIRateLimitError 走长退避等配额恢复(而非快速重试转终态)。
_CLI_RATE_LIMIT_MARKERS = (
    "rate limit", "rate_limit", "usage limit", "429",
    "overloaded", "quota", "too many requests", "limit reached",
)

_MAX_CLI_ENVELOPE_ERROR_CHARS = 500
_MAX_CLI_ENVELOPE_ERROR_ITEMS = 8


def _detail_is_rate_limited(detail: str) -> bool:
    low = detail.lower()
    return any(marker in low for marker in _CLI_RATE_LIMIT_MARKERS)


def _cli_envelope_error_messages(obj: dict[str, Any]) -> list[str]:
    """从 CLI 错误信封提取有界文本;不序列化信封,避免把原始响应写进异常。"""
    values: list[Any] = []
    errors = obj.get("errors")
    if isinstance(errors, list):
        values.extend(errors[:_MAX_CLI_ENVELOPE_ERROR_ITEMS])
    elif errors is not None:
        values.append(errors)
    values.extend((obj.get("error"), obj.get("message")))

    messages: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("message")
        if not isinstance(value, str):
            continue
        text = " ".join(value[:_MAX_CLI_ENVELOPE_ERROR_CHARS].split())
        if text:
            messages.append(text)
        if len(messages) >= _MAX_CLI_ENVELOPE_ERROR_ITEMS:
            break
    return messages


def _require_cli_envelope_result(
    provider: str,
    obj: dict[str, Any],
    *,
    transcript_path: str | None,
) -> str:
    """验证 CLI 顶层信封并返回正文;错误终态或无字符串正文一律抛出。"""
    subtype = obj.get("subtype")
    subtype_prefix = subtype[:81].lower() if isinstance(subtype, str) else ""
    subtype_is_error = subtype_prefix == "error" or subtype_prefix.startswith("error_")
    reasons: list[str] = []
    if obj.get("is_error") is True:
        reasons.append("is_error=true")
    if subtype_is_error:
        reasons.append(f"subtype={subtype[:80]}")
    if "result" not in obj:
        reasons.append("result is missing")
    elif not isinstance(obj["result"], str):
        reasons.append("result is not a string")

    if not reasons:
        return obj["result"]

    messages = _cli_envelope_error_messages(obj)
    detail = "; ".join((*reasons, *messages))[:_MAX_CLI_ENVELOPE_ERROR_CHARS]
    error_type = (
        AIRateLimitError
        if any(_detail_is_rate_limited(item) for item in (*reasons, *messages))
        else AIProviderError
    )
    error = error_type(f"{provider} returned an invalid or error envelope: {detail}")
    error.transcript_path = transcript_path  # type: ignore[attr-defined]
    raise error


@lru_cache(maxsize=1)
def codex_sandbox_available() -> bool:
    """codex 的 read 能力是否真能用:探沙箱能不能起来,而不是只看二进制与凭证在不在。

    codex 的读文件是让模型跑 shell 命令,由 bubblewrap 建 user namespace 圈起来。
    容器默认 seccomp 挡 unshare(CLONE_NEWUSER),沙箱起不来,此时每条模型命令都失败
    而 codex exec 仍然 rc=0 —— 步骤会拿着"什么都没读到"的结论正常返回。
    所以 read 必须以沙箱可用为前提自证,否则调度器会把取证类步骤派给一台干不了的 worker。

    要在容器里启用,AI worker 需 seccomp=unconfined 加 apparmor=unconfined;
    那是拿容器自身的外层约束换 codex 的内层沙箱,代价见 docs/08-deployment.md。
    探针走 codex 自己的沙箱入口,不假设底层是 bubblewrap 还是 Landlock,零 API 成本。
    结果按进程缓存:宿主能力在 worker 生命周期内不变。
    """
    import shutil
    import subprocess

    if not shutil.which("codex"):
        return False
    try:
        proc = subprocess.run(
            ["codex", "sandbox", "--", "/bin/true"],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def cli_provider_ready(provider: str) -> bool:
    """CLI 就绪判据的唯一入口:二进制在且本机有凭证。
    不能只看二进制:镜像统一烤入全部 CLI,凭证决定这台 worker 实际能用哪个。"""
    import shutil

    home = os.environ.get("HOME") or os.path.expanduser("~")
    if provider == "claude-cli":
        if not shutil.which("claude"):
            return False
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True
        cred = Path(
            os.environ.get("CLAUDE_CONFIG_DIR") or Path(home) / ".claude",
        ) / ".credentials.json"
        try:
            return cred.is_file() and cred.stat().st_size > 0
        except OSError:
            return False
    if provider == "codex-cli":
        if not shutil.which("codex"):
            return False
        cred = Path(
            os.environ.get("CODEX_HOME") or Path(home) / ".codex",
        ) / "auth.json"
        try:
            return cred.is_file() and cred.stat().st_size > 0
        except OSError:
            return False
    if provider == "qoder-cli":
        if not shutil.which("qodercli"):
            return False
        cred = Path(
            os.environ.get("QODER_CONFIG_DIR") or Path(home) / ".qoder",
        ) / ".auth" / "user"
        try:
            return cred.is_file() and cred.stat().st_size > 0
        except OSError:
            return False
    return False


CLI_PROVIDER_ENV = "FLORI_CLI_PROVIDER"


def resolve_bound_cli_provider(providers_config: dict | None) -> str | None:
    """解析 worker 显式绑定的 concrete CLI;未设置返回 None,绝不按本机凭证自动任选。"""
    from .ai_routing import CLI_PROVIDER_TYPES_BY_NAME

    selected = (os.environ.get(CLI_PROVIDER_ENV) or "").strip()
    if not selected:
        return None
    expected_type = CLI_PROVIDER_TYPES_BY_NAME.get(selected)
    if expected_type is None:
        raise AIProviderError(
            f"{CLI_PROVIDER_ENV}={selected!r} 非法:只允许 claude-cli/codex-cli/qoder-cli"
        )
    providers = (providers_config or {}).get("providers") or {}
    entry = providers.get(selected)
    if not isinstance(entry, dict) or entry.get("type") != expected_type:
        raise AIProviderError(
            f"{CLI_PROVIDER_ENV}={selected!r} 与 provider 配置不一致:"
            f"需要 type={expected_type}"
        )
    if not cli_provider_ready(selected):
        raise AIProviderError(
            f"{CLI_PROVIDER_ENV}={selected!r} 未就绪:需要对应二进制与凭证"
        )
    return selected


# AI 子进程环境最小白名单:CLI 起得来所需的通用项 + 各 CLI 自己的配置根与原生凭证。
# worker 进程环境里的 Flori secrets(worker/registration token、MinIO、API_TOKEN、
# 其它 provider 的 API key、Redis/Gateway 地址)一律不继承;providers.yaml 的 env
# 覆盖仍在白名单之上生效,作为显式配置的逃生口。
_CLI_ENV_COMMON_PASSTHROUGH = (
    # 进程基础:PATH 找二进制,HOME 定位默认配置根,TMPDIR 临时文件。
    "PATH", "HOME", "TMPDIR",
    # 语言/时区/终端:CLI 输出 unicode 与时间戳依赖。
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    # 出网代理与 CA:CLI 联网(鉴权刷新/WebSearch)走本机代理与证书配置。
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    # XDG 家族:容器里常用来重定向 CLI 状态/缓存目录。
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
)
# 各 CLI 只拿自己的配置根与原生凭证,不给其它 provider 的 key。
# ANTHROPIC_API_KEY 是 claude CLI 的原生鉴权路径(就绪判据同源),属该 CLI 自己的凭证。
_CLI_ENV_PROVIDER_PASSTHROUGH: dict[str, tuple[str, ...]] = {
    "claude-cli": ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "DISABLE_UPDATES"),
    "codex-cli": ("CODEX_HOME",),
    "qoder-cli": ("QODER_CONFIG_DIR",),
}


def build_cli_env(provider: str, overrides: dict | None = None) -> dict[str, str]:
    """AI 子进程环境:白名单继承 + provider 配置 env 覆盖,绝不透传整份 os.environ。"""
    keys = _CLI_ENV_COMMON_PASSTHROUGH + _CLI_ENV_PROVIDER_PASSTHROUGH.get(provider, ())
    env = {key: val for key in keys if (val := os.environ.get(key)) is not None}
    for key, val in (overrides or {}).items():
        env[str(key)] = str(val)
    return env


# 模板禁改的安全参数:沙箱/审批/权限跳过/工具面/目录授权/profile 由 provider 代码强制,
# 模板预置等于绕过 read-only 与工具门控不变量,fail-closed 拒绝而非尊重。
_CLAUDE_FORBIDDEN_TEMPLATE_ARGS = frozenset({
    "--dangerously-skip-permissions", "--permission-mode", "--permission-prompt-tool",
    "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
    "--tools", "--add-dir", "--settings", "--mcp-config", "--strict-mcp-config",
})
_QODER_FORBIDDEN_TEMPLATE_ARGS = _CLAUDE_FORBIDDEN_TEMPLATE_ARGS
_CODEX_FORBIDDEN_TEMPLATE_ARGS = frozenset({
    "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
    "--sandbox", "-s", "--ask-for-approval", "-a", "--add-dir",
    "--profile", "-p", "--cd", "-C",
    # --enable/--disable 只是 `-c features.<name>=` 的别名,等于绕过下面的 -c 键审查。
    "--enable", "--disable",
    # 钩子信任开关:放开后未经信任的 hook 也会执行,而 hook 跑在沙箱外。
    "--dangerously-bypass-hook-trust",
})
# -c 可达且能绕开沙箱的配置根。前四个直接弱化沙箱与审批;后面几个都在沙箱外拿到执行或
# 出网能力:notify 与 hooks 由 codex 自己 spawn,mcp_servers 是常驻子进程,
# model_provider/model_providers/chatgpt_base_url 能把 prompt 和鉴权头引到任意端点。
# permissions 与 default_permissions 由代码构造:模板改写它们等于改写读取拒绝名单。
_CODEX_FORBIDDEN_CONFIG_ROOTS = frozenset({
    "sandbox_mode", "approval_policy", "sandbox_workspace_write", "shell_environment_policy",
    "notify", "hooks", "mcp_servers",
    "model_provider", "model_providers", "chatgpt_base_url",
    "permissions", "default_permissions",
})

# 读取拒绝名单的固定项:凭证、cookie 与容器控制面,任何步骤都没有读它们的正当理由。
# 各 CLI 的配置根另按 env 动态解析,见 _deny_read_paths。
_CODEX_DENY_READ_FIXED = (
    "/var/run/docker.sock", "/run/docker.sock",
    "/data/cookies",
)
# 部署侧追加拒绝路径,冒号分隔。挂载面因部署而异,不进代码。
_CODEX_DENY_READ_ENV = "FLORI_CODEX_DENY_READ"
# codex 用 profile 名索引权限档案,名字本身无语义,固定一个不与用户档案重名的值。
_CODEX_PROFILE_NAME = "flori-locked"


def _reject_unsafe_template(
    provider: str,
    template: list[str],
    forbidden: frozenset[str],
    forbidden_config_roots: frozenset[str] = frozenset(),
) -> None:
    """command template 含安全参数时抛错;--flag=value 与 -c key=value 两种形态都查。"""
    parts = [str(part) for part in template]
    hits = sorted({
        part.split("=", 1)[0] for part in parts
        if part.startswith("-") and part.split("=", 1)[0] in forbidden
    })
    if hits:
        raise AIProviderError(
            f"{provider} command template 不得预置安全参数 {','.join(hits)}:"
            "沙箱/审批/权限/工具面/目录授权由代码强制,模板冲突一律拒绝(fail-closed)。"
        )
    if not forbidden_config_roots:
        return
    config_values = [
        parts[i + 1] for i, part in enumerate(parts[:-1]) if part in ("-c", "--config")
    ] + [part.split("=", 1)[1] for part in parts if part.startswith("--config=")]
    for value in config_values:
        root = value.split("=", 1)[0].split(".", 1)[0].strip()
        if root in forbidden_config_roots:
            raise AIProviderError(
                f"{provider} command template 不得用 -c 覆盖安全配置键 {root!r}:"
                "沙箱与审批策略由代码强制,模板冲突一律拒绝(fail-closed)。"
            )


# Provider 实现


class DryRunProvider:
    """DRY_RUN 模式:不调真实 API。"""

    @staticmethod
    def _prompt_json(prompt: str, label: str):
        match = re.search(rf"(?m)^{re.escape(label)}:\n([^\n]+)$", prompt)
        return json.loads(match.group(1)) if match else None

    @classmethod
    def _document_smart_content(cls, prompt: str) -> str | None:
        if "章节包的结构化学习卡" in prompt:
            package = cls._prompt_json(prompt, "PACKAGE")
            if not isinstance(package, dict):
                return None
            source_refs = list(package.get("source_aliases") or {})
            if not source_refs:
                return None
            first_ref = source_refs[0]
            figures = [{
                "figure_alias": item["figure_alias"],
                "visual_analysis": "DRY_RUN 已读取图片结构。" if item.get("media") else "",
                "reading_guide": "先看结构和标注，再对照正文。",
                "supported_claim": "图片用于解释本章节的核心论点。",
                "limits": "图片不能独立证明超出正文的数据结论。",
                "source_refs": [item["source_alias"]],
            } for item in package.get("figures") or []]
            return json.dumps({
                "package_id": package["package_id"],
                "overview": "DRY_RUN 章节卡覆盖问题、方法、结果与边界。",
                "knowledge": [{
                    "kind": "method", "topic": "DRY_RUN 方法",
                    "claim": "来源描述了一个可验证的方法。",
                    "explanation": "该方法从研究问题出发并由实验检查。",
                    "why_it_matters": "它连接论文动机、实现与结论。",
                    "author_claim": True, "source_refs": [first_ref],
                }],
                "cross_section_links": [], "figures": figures,
                "unresolved": [], "coverage_refs": source_refs,
                "synthesis": {
                    "analysis": "章节论证形成问题到验证的局部闭环。",
                    "basis": "本包全部来源分片。",
                    "uncertainty": "DRY_RUN 只验证接线，不代表真实内容质量。",
                },
            }, ensure_ascii=False, separators=(",", ":"))
        if "综合成供最终论文笔记写作的主题学习图" in prompt:
            theme = cls._prompt_json(prompt, "THEME")
            refs = cls._prompt_json(prompt, "EXPECTED_KNOWLEDGE_REFS")
            figures = cls._prompt_json(prompt, "FIGURE_CATALOG")
            if not isinstance(theme, dict) or not isinstance(refs, list) or not refs:
                return None
            figure_refs = list(figures or {})
            sections = [{
                "title": "问题、方法与验证",
                "purpose": "恢复章节之间的论证关系。",
                "explanation": "DRY_RUN 将章节知识连接为主题学习图。",
                "knowledge_refs": refs[index:index + 512],
                "figure_refs": figure_refs if index == 0 else [],
            } for index in range(0, len(refs), 512)]
            guides = [{
                "figure_ref": ref, "placement_hint": "相关论点之后",
                "reading_guide": "按标签和对比关系阅读。",
                "supports": "支持主题中的对应论点。",
                "limits": "不支持输入之外的因果推断。",
                "knowledge_refs": [refs[0]],
            } for ref in figure_refs]
            return json.dumps({
                "theme_id": theme["theme_id"], "overview": "DRY_RUN 主题综合。",
                "learning_sections": sections, "cross_theme_links": [],
                "tensions": [], "limitations": [], "figure_guides": guides,
                "coverage_refs": refs,
                "synthesis": {
                    "analysis": "主题内的知识已建立关系。",
                    "basis": "全部章节知识引用。",
                    "uncertainty": "DRY_RUN 不评价真实论文质量。",
                },
            }, ensure_ascii=False, separators=(",", ":"))
        if "撰写完整中文智能笔记" in prompt:
            theme_refs = cls._prompt_json(prompt, "EXPECTED_THEME_REFS")
            knowledge_refs = cls._prompt_json(prompt, "EXPECTED_KNOWLEDGE_REFS")
            figures = cls._prompt_json(prompt, "FIGURE_CATALOG")
            if not isinstance(theme_refs, list) or not isinstance(knowledge_refs, list) or not knowledge_refs:
                return None
            selected = list(figures or {})[:256]
            body = (
                "论文从一个明确的研究问题出发，给出方法、验证设计和主要结论。"
                "阅读时应区分作者主张、实验支持与仍需保留的不确定性。"
            ) * 16
            figure_markdown = "\n\n".join(f"{{{{FIGURE:{ref}}}}}" for ref in selected)
            markdown = (
                f"## 问题、方法与验证\n\n{body}[证据: {knowledge_refs[0]}]"
                + ("\n\n" + figure_markdown if figure_markdown else "")
            )
            return (
                "---FLORI-FINAL-MARKDOWN-BEGIN---\n"
                + markdown
                + "\n---FLORI-FINAL-SYNTHESIS-BEGIN---\n"
                + "论文主线已按主题重建。\n"
                + "---FLORI-FINAL-SYNTHESIS-BASIS---\n"
                + "全部主题学习图。\n"
                + "---FLORI-FINAL-SYNTHESIS-UNCERTAINTY---\n"
                + "DRY_RUN 只用于接线验收。\n"
                + "---FLORI-FINAL-SYNTHESIS-KNOWLEDGE-REFS---\n"
                + str(knowledge_refs[0])
                + "\n---FLORI-FINAL-SYNTHESIS-END---\n"
                + "---FLORI-FINAL-MARKDOWN-END---"
            )
        if "论文精读笔记的导读编辑" in prompt:
            refs = cls._prompt_json(prompt, "VALID_REFS")
            if not isinstance(refs, list) or not refs:
                return None
            paragraph = (
                "论文首先说明研究背景与现有方法的缺口，再提出解决方案，"
                "并通过实验检验方法能否回答最初的问题。"
            ) * 5
            markdown = (
                "## 论文导读：这篇论文要解决什么\n\n"
                f"### 背景与问题\n\n背景：{paragraph}[证据: {refs[0]}]\n\n"
                f"### 解决思路\n\n方案：{paragraph}\n\n"
                f"### 如何验证\n\n验证：{paragraph}\n\n"
                f"### 主要结论与阅读边界\n\n边界：{paragraph}"
            )
            return json.dumps({
                "introduction_markdown": markdown,
                "used_knowledge_refs": [refs[0]],
            }, ensure_ascii=False, separators=(",", ":"))
        return None

    @staticmethod
    def _content(request: LLMRequest) -> str:
        prompt = "\n".join(
            str(message.get("content") or "")
            for message in request.messages
            if isinstance(message, dict)
        )
        marker = "\nINPUT="
        document_smart = DryRunProvider._document_smart_content(prompt)
        if document_smart is not None:
            return document_smart
        if (
            request.response_format == "json"
            and "Document 流水线的忠实翻译器" in prompt
            and marker in prompt
        ):
            try:
                payload = json.loads(prompt.rsplit(marker, 1)[1])
                segments = payload["segments"]
                if isinstance(segments, list):
                    return json.dumps({
                        "segments": [
                            {"id": item["id"], "text": item["text"]}
                            for item in segments
                            if isinstance(item, dict)
                        ],
                    }, ensure_ascii=False, separators=(",", ":"))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        if request.response_format == "json" and marker in prompt:
            try:
                payload = json.loads(prompt.rsplit(marker, 1)[1])
                items = payload.get("items")
                if (
                    isinstance(items, list)
                    and "独立证据核验器" in prompt
                ):
                    return json.dumps({
                        "schema_version": 3,
                        "decisions": [{
                            "decision_id": item["decision_id"],
                            "decision": "supported",
                            "confidence_ppm": 990000,
                            "reason_codes": [
                                "semantic_equivalent", "critical_facts_match",
                            ],
                        } for item in items if isinstance(item, dict)],
                    }, ensure_ascii=False, separators=(",", ":"))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        if request.response_format == "json" and "已验证概念证据锚点(JSON)" in prompt:
            match = re.search(
                r"已验证概念证据锚点\(JSON\) ---\n(\{[^\n]+\})", prompt,
            )
            try:
                anchors = json.loads(match.group(1))["anchors"] if match else []
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                anchors = []
            anchor = next(
                (value for value in anchors if isinstance(value, str) and value.strip()),
                "",
            )
            token = re.search(r"[\u3400-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", anchor)
            term = token.group(0) if token else anchor[:32].strip()
            return json.dumps({
                "summary": "DRY_RUN 概念摘要",
                "key_terms": [{
                    "term": term,
                    "definition": "DRY_RUN 概念定义",
                    "zh_name": None,
                    "related": [],
                }] if term else [],
            }, ensure_ascii=False, separators=(",", ":"))
        if request.response_format == "json" and "评分维度（每项打 1-5" in prompt:
            score_keys = re.findall(r"(?m)^\d+\. ([a-z_]+):", prompt)
            return json.dumps({
                **{key: 4 for key in score_keys},
                "key_terms": [],
                "missing_concepts": [],
                "top3_improvements": ["DRY_RUN 1", "DRY_RUN 2", "DRY_RUN 3"],
                "issues": [],
            }, ensure_ascii=False, separators=(",", ":"))
        if "可引用来源坐标" in prompt:
            match = re.search(
                r"(?m)^\[\[source:([^\]]+)\]\][ \t]+([^\n]+)$", prompt,
            )
            if match:
                return (
                    "## 核心结论\n\n"
                    + match.group(2).strip()
                    + f"[[source:{match.group(1)}]]"
                )
        return f"[DRY_RUN] {len(request.messages)} messages, model={request.model}"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or "dry-run"
        return LLMResponse(
            content=self._content(request),
            model=model,
            provider="dry-run",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            duration_sec=0.0,
            finish_reason="stop",
            raw={"dry_run": True},
            tier_used="primary",
            attempts=[{
                "tier": "primary",
                "provider": "dry-run",
                "model": model,
                "ok": True,
            }],
        )


class AnthropicProvider:
    """Anthropic API(SDK: anthropic)。"""

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

        try:
            response = await asyncio.to_thread(client.messages.create, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str:
                raise AIRateLimitError(str(e))
            raise AIProviderError(str(e))

        duration = time.time() - start
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        # 合并所有 text block:多 block / 思考型响应只取 [0] 会丢正文(无 type 视为 text)。
        content = "".join(
            b.text for b in (response.content or [])
            if getattr(b, "type", "text") == "text"
        )

        cc = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        # 原始返回尽量保真(SDK 对象 → dict);失败不影响主流程。
        try:
            raw = response.model_dump(mode="json")
        except Exception:
            raw = {"id": getattr(response, "id", None),
                   "stop_reason": getattr(response, "stop_reason", None)}
        return LLMResponse(
            content=content,
            model=request.model,
            provider="anthropic",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cc,
            cache_read_input_tokens=cr,
            cost_usd=calc_cost("anthropic", request.model, input_tokens, output_tokens, cc, cr),
            duration_sec=round(duration, 2),
            cached=cr > 0,
            api_ms=round(duration * 1000, 1),
            finish_reason=getattr(response, "stop_reason", None),
            session_id=getattr(response, "id", None),
            raw=raw,
        )

    # Anthropic 支持的图片 media subtype,后缀大小写归一。其余后缀显式报错,避免发出非法 media_type 被 API 拒。
    _IMG_SUBTYPE = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}

    def _build_messages(self, request: LLMRequest) -> list[dict]:
        messages = [dict(m) for m in request.messages]
        if not request.images:
            return messages
        # 只给最后一条 user message 附图:逐条附会在多 message(多轮对话)时把同组图按 base64 重复 N 遍。
        last_user = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
            None,
        )
        if last_user is None:
            return messages
        import base64
        msg = messages[last_user]
        content_parts = [{"type": "text", "text": msg["content"]}]
        for img_path in request.images:
            suffix = Path(img_path).suffix.lstrip(".").lower()
            subtype = self._IMG_SUBTYPE.get(suffix)
            if subtype is None:
                raise AIProviderError(f"unsupported image type {suffix!r}: {img_path}")
            content_parts.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": f"image/{subtype}",
                    "data": base64.b64encode(Path(img_path).read_bytes()).decode(),
                },
            })
        messages[last_user] = {"role": msg["role"], "content": content_parts}
        return messages


class OpenAICompatibleProvider:
    """OpenAI 兼容 API(DeepSeek / Qwen / Ollama / vLLM)。"""

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

        try:
            response = await asyncio.to_thread(client.chat.completions.create, **kwargs)
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
        try:
            raw = response.model_dump(mode="json")
        except Exception:
            raw = {"id": getattr(response, "id", None),
                   "finish_reason": getattr(choice, "finish_reason", None)}

        return LLMResponse(
            content=choice.message.content or "",
            model=request.model,
            provider=self._provider_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=calc_cost(self._provider_name, request.model, input_tokens, output_tokens),
            duration_sec=round(duration, 2),
            api_ms=round(duration * 1000, 1),
            finish_reason=getattr(choice, "finish_reason", None),
            session_id=getattr(response, "id", None),
            raw=raw,
        )


class ClaudeCLIProvider:
    """Claude CLI 接入(subprocess 调用)。"""

    def __init__(self, command_template: list[str], env: dict | None = None,
                 model: str | None = None, reasoning_effort: str | None = None):
        _reject_unsafe_template(
            "claude-cli", command_template or [], _CLAUDE_FORBIDDEN_TEMPLATE_ARGS,
        )
        self._command_template = command_template
        self._env = env or {}
        # provider 默认模型(providers.yaml claude-cli.model,如 claude-opus-4-8[1m])。
        # 不配则不传 --model,沿用挂载 HOME 里 CLI 自己的默认——可用但不可复现(宿主换模型会静默跟变),建议配置钉死。
        self._model = model
        # provider 默认推理档位(providers.yaml reasoning_effort)。优先级:请求级 > 此默认 > CLI 自定。
        self._reasoning_effort = reasoning_effort

    @staticmethod
    def _find_transcript(session_id: str | None, env: dict) -> str | None:
        """按 session_id 定位 claude CLI 自写的会话 transcript:
        `$CLAUDE_CONFIG_DIR|$HOME/.claude/projects/<cwd编码>/<session_id>.jsonl`。
        agentic 中间轮(WebSearch/Bash/逐图 Read)只存在于 transcript——顶层 --output-format json
        仅回最终汇总;审计要全轨迹白盒必须回收它。session id 为 UUID,直接按文件名 glob,
        免猜 cwd 编码规则。找不到(HOME 未挂/无档)回 None,绝不影响主流程。"""
        if not session_id:
            return None
        try:
            cfg_dir = env.get("CLAUDE_CONFIG_DIR") or str(
                Path(env.get("HOME") or os.path.expanduser("~")) / ".claude")
            hits = sorted(Path(cfg_dir).glob(f"projects/*/{session_id}.jsonl"))
            return str(hits[0]) if hits else None
        except Exception:
            return None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        prompt_content = ""
        if request.system:
            prompt_content += f"[System]\n{request.system}\n\n"
        for msg in request.messages:
            prompt_content += f"[{msg['role'].title()}]\n{msg['content']}\n\n"

        # 视觉:把帧图绝对路径写进 prompt,放开 Read 工具让 claude 逐张查看(CLI 路径不支持 base64)。
        # 帧目录用 --add-dir 加入可访问范围,容器/无头干净环境也能读到。
        extra_dirs: set[str] = set()
        if request.images:
            # 路径可能已被 step 以 [N] 形式列进 prompt(如视频视觉 pass);只补 prompt 里尚未出现的,
            # 避免同组绝对路径注入两遍。--add-dir 仍按全部 images 授权,不影响 Read。
            missing = []
            for p in request.images:
                ap = str(Path(p).resolve())
                extra_dirs.add(str(Path(ap).parent))
                if ap not in prompt_content:
                    missing.append(ap)
            if missing:
                prompt_content += "\n截图(用 Read 工具逐张查看):\n" + "\n".join(missing) + "\n"

        # 剔除命令模板里的 {prompt_file} 占位:prompt 走 stdin,无 ARG_MAX 限制、不依赖文件读。
        cmd = [part for part in self._command_template if "{prompt_file}" not in part]
        # 结构化输出强制 json:拿真实 usage(in/out/cache token)+ total_cost_usd + num_turns。
        # 模板里可能已带 `--output-format text`(providers.yaml 默认),先剔除该对再设 json,
        # 否则 text 输出解析失败→回退零统计,批量统计形同虚设。
        if "--output-format" in cmd:
            _i = cmd.index("--output-format")
            del cmd[_i:_i + 2]
        cmd += ["--output-format", "json"]
        # 模型可配置(yaml 单一来源,优先级:步级 > provider 默认)。命令模板已带 --model 时尊重模板。
        model_arg = request.model or self._model
        if model_arg and "--model" not in cmd:
            cmd += ["--model", model_arg]
        # 推理档位(claude --effort,域 low/medium/high/xhigh/max):请求级 > provider 默认;模板钉死时尊重模板。
        effort = request.reasoning_effort or self._reasoning_effort
        if effort and "--effort" not in cmd:
            cmd += ["--effort", effort]
        if request.allowed_tools:
            # 取证等联网步骤:放开指定工具(如 WebSearch + Bash),让 claude agentic 搜+抓+抽。
            # 与 images 分支互斥(取证不喂帧图);max_turns 给足,多轮流程为搜索、直连 curl、抽取。
            cmd += ["--allowedTools", *request.allowed_tools]
            cmd += ["--max-turns", str(request.max_turns or 24)]
            # Read 本地文件(pdf-only 直喂等):--add-dir 放行 prompt 引用的目录,否则 Read 出沙箱失败。
            for d in request.add_dirs or []:
                cmd += ["--add-dir", d]
        elif request.images:
            cmd += ["--allowedTools", "Read"]
            # 限轮数:每张图一个 Read 轮,多图时上下文超线性膨胀会拖垮(实测 20 张丢图无界跑 >18min)。
            # 留几轮给思考+生成。配合 step 侧限图数,把视觉笔记控制在分钟级。
            cmd += ["--max-turns", str(len(request.images) + 5)]
            for d in sorted(extra_dirs):
                cmd += ["--add-dir", d]
        else:
            # 纯文本调用(评审/标点):用 --tools "" 禁用全部工具,强制单次纯文本生成。
            # 否则 claude -p 默认带工具,评审这类大 prompt 会尝试调工具,消耗掉第 1 轮,
            # 被 max-turns 1 截断报 "Reached max turns (1)",线上 11_review 实测此因失败;
            # 即便不报错也会多轮 agentic"思考",一个打分跑成 >15min。
            # 工具禁掉后只能产出 1 个文本轮,max-turns 1 即安全(实测 ~14-35s)。
            cmd += ["--tools", "", "--max-turns", "1"]

        env = build_cli_env("claude-cli", self._env)
        # 完整论文纯文本生成在 Opus 上也会超过 600 秒。统一给到步骤级上限 30 分钟,
        # 仍由 Worker 心跳、租约和外层 step timeout 约束。
        timeout = 1800
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
            if _detail_is_rate_limited(detail):
                err: Exception = AIRateLimitError(f"CLI rate-limited: {detail}")
            else:
                err = AIProviderError(f"CLI failed: {detail}")
            # 失败也尽力回收 transcript(审计要失败留痕):失败输出常仍是 json(is_error),session_id 可取。
            # 附在异常上,经 gateway 尝试链(_attempt)带给审计层。
            try:
                obj = json.loads(stdout.decode().strip())
                sid = obj.get("session_id") if isinstance(obj, dict) else None
                err.transcript_path = self._find_transcript(sid, env)  # type: ignore[attr-defined]
            except Exception:
                pass
            raise err

        raw = stdout.decode().strip()
        # 解析 --output-format json:result(正文)+ usage(in/out/cache)+ total_cost_usd + num_turns。
        # 解析失败(旧 CLI / 非 json 输出)回退原始文本 + 零统计:向后兼容,不让步骤失败。
        content, in_tok, out_tok = raw, 0, 0
        cc = cr = turns = 0
        cost = 0.0
        model = model_arg or DEFAULT_AI_MODEL
        raw_obj: dict | None = None
        session_id = None
        api_ms = None
        finish_reason = None
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                raw_obj = obj
                raw_session_id = obj.get("session_id")
                session_id = raw_session_id if isinstance(raw_session_id, str) else None
                content = _require_cli_envelope_result(
                    "Claude CLI", obj,
                    transcript_path=self._find_transcript(session_id, env),
                )
                u = obj.get("usage") or {}
                in_tok = int(u.get("input_tokens", 0) or 0)
                out_tok = int(u.get("output_tokens", 0) or 0)
                cc = int(u.get("cache_creation_input_tokens", 0) or 0)
                cr = int(u.get("cache_read_input_tokens", 0) or 0)
                cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
                turns = int(obj.get("num_turns", 0) or 0)
                model = _extract_cli_model(obj) or model
                _api = obj.get("duration_api_ms") or obj.get("duration_ms")
                api_ms = float(_api) if isinstance(_api, (int, float)) else None
                finish_reason = obj.get("subtype") or obj.get("stop_reason")
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        # CLI 路径:claude -p 一般已回 total_cost_usd,即等价 API 成本而非真实账单,前端标「等价」。
        # 若缺失(回 0)而有 token,则按 PRICING 折算等价成本,缓存感知;model 未命中价表则仍为 0,已 warn。
        if cost == 0.0 and (in_tok or out_tok or cc or cr):
            cost = round(calc_cost("anthropic", model, in_tok, out_tok, cc, cr), 6)
        return LLMResponse(
            content=content,
            model=model,
            provider="claude-cli",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_input_tokens=cc,
            cache_read_input_tokens=cr,
            cost_usd=round(cost, 6),
            duration_sec=round(duration, 2),
            num_turns=turns,
            cached=cr > 0,
            session_id=session_id,
            api_ms=api_ms,
            finish_reason=finish_reason,
            raw=raw_obj,
            transcript_path=self._find_transcript(session_id, env),
        )


class CodexCLIProvider:
    """Codex CLI 接入(subprocess 调用,`codex exec --json`)。

    隔离模型与 claude/qoder 不同,实测结论记在这里,别按 claude 的心智模型推断。
    Linux 上沙箱是 bubblewrap。`--sandbox read-only` 等价 `--ro-bind / /` 加
    `--unshare-net`:禁写禁网但整盘可读,`--cd` 只定工作根,收窄不了读取范围。
    要收窄读取只能改用 permissions 档案,而 `--sandbox` 会把档案整体顶掉,所以这里不传
    该 flag,靠 `default_permissions` 生效;档案万一没生效,codex 默认档位仍是 read-only,
    不会比原来更宽。档案取"整盘 read 加逐条 deny":只列允许根会让 /lib64 之类进不了沙箱,
    动态链接的二进制全起不来。

    因此能保证:不可写、不可直连外网、工作根是每次调用新建的空目录、模型 shell 拿不到
    worker 环境里的 Flori secrets、拒绝名单内的凭证与 cookie 读到 Permission denied。
    仍保证不了的:名单外的路径整盘可读,尤其其它 Job 的产物目录,那要靠容器挂载面收窄。
    另外容器默认 seccomp 挡 unshare(CLONE_NEWUSER) 时 bubblewrap 起不来,此时模型的
    shell 命令全部失败,Read 类请求按无依据处理直接失败,见 complete。"""

    def __init__(self, command_template: list[str], env: dict | None = None,
                 model: str | None = None, reasoning_effort: str | None = None):
        _reject_unsafe_template(
            "codex-cli", command_template or [], _CODEX_FORBIDDEN_TEMPLATE_ARGS,
            _CODEX_FORBIDDEN_CONFIG_ROOTS,
        )
        self._command_template = command_template
        self._env = env or {}
        self._model = model
        self._reasoning_effort = reasoning_effort

    @staticmethod
    def _has_flag(cmd: list[str], *names: str) -> bool:
        return any(part in names for part in cmd)

    @staticmethod
    def _has_config_key(cmd: list[str], key: str) -> bool:
        """模板可能用 `-c key=value` 钉死配置;逐对扫描,避免误判其它 -c 键。"""
        for i, part in enumerate(cmd[:-1]):
            if part in ("-c", "--config") and cmd[i + 1].split("=", 1)[0] == key:
                return True
        return False

    @staticmethod
    def _classify_error(detail: str) -> Exception:
        if _detail_is_rate_limited(detail):
            return AIRateLimitError(f"Codex CLI rate-limited: {detail[:500]}")
        return AIProviderError(f"Codex CLI failed: {detail[:500]}")

    # bwrap 构建失败时的固定说法。容器默认 seccomp 挡 unshare(CLONE_NEWUSER),
    # 部分内核还会挡挂载传播变更。取词要贴死 bwrap 原文,免得模型正文谈到沙箱就误判。
    _SANDBOX_UNAVAILABLE_MARKERS = (
        "bwrap: No permissions to create a new namespace",
        "bwrap: Failed to make / slave",
        "Codex's Linux sandbox uses bubblewrap",
    )

    @classmethod
    def _sandbox_unavailable_marker(cls, raw: str) -> str | None:
        return next((m for m in cls._SANDBOX_UNAVAILABLE_MARKERS if m in raw), None)

    @staticmethod
    def _deny_read_paths(env: dict[str, str]) -> list[str]:
        """模型 shell 不该读到的路径。只返回当前真实存在的:codex 为拒绝项创建挂载点,
        指向不存在的路径会让整个沙箱构建失败(bwrap "Can't create file at ...")。"""
        home = env.get("HOME") or os.path.expanduser("~")
        candidates = [
            env.get("CODEX_HOME") or str(Path(home) / ".codex"),
            env.get("CLAUDE_CONFIG_DIR") or str(Path(home) / ".claude"),
            env.get("QODER_CONFIG_DIR") or str(Path(home) / ".qoder"),
            *_CODEX_DENY_READ_FIXED,
        ]
        # token 路径取自 worker 进程环境:该变量刻意不在子进程白名单里,但拒绝名单要用它。
        if token_file := os.environ.get("WORKER_TOKEN_FILE"):
            candidates.append(token_file)
        candidates += [
            p for p in (os.environ.get(_CODEX_DENY_READ_ENV) or "").split(":") if p.strip()
        ]
        seen: dict[str, None] = {}
        for raw in candidates:
            try:
                path = Path(raw).expanduser()
                if path.exists():
                    seen.setdefault(str(path), None)
            except OSError:
                continue
        return list(seen)

    @classmethod
    def _permissions_override(cls, env: dict[str, str]) -> list[str]:
        """构造 codex 权限档案的 -c 覆盖对。

        整盘 read 加逐条 deny,而不是只列允许根:codex 会把允许根规范化后逐个 bind,
        窄名单下 /lib64 之类的路径进不去沙箱,动态链接的二进制全部起不来。
        deny 项由 --tmpfs 挂空目录实现,读到的是 Permission denied。
        写与出网不受影响,仍然全禁,与 read-only 档位一致。"""
        entries = ['"/"="read"'] + [
            f'{json.dumps(p)}="deny"' for p in cls._deny_read_paths(env)
        ]
        return [
            "-c", f"permissions.{_CODEX_PROFILE_NAME}.filesystem={{{','.join(entries)}}}",
            "-c", f"default_permissions={_CODEX_PROFILE_NAME}",
        ]

    @staticmethod
    def _parse_events(raw: str) -> tuple[dict | None, dict | None, int, list[dict], str | None]:
        thread: dict | None = None
        usage: dict | None = None
        turns = 0
        errors: list[dict] = []
        last_message: str | None = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "thread.started":
                thread = ev
            elif etype == "turn.completed":
                turns += 1
                if isinstance(ev.get("usage"), dict):
                    usage = ev["usage"]
            elif etype in ("turn.failed", "error"):
                errors.append({
                    "type": etype,
                    "message": str(ev.get("message") or ev.get("error") or "")[:500],
                })
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    last_message = text
        return thread, usage, turns, errors, last_message

    async def complete(self, request: LLMRequest) -> LLMResponse:
        # Read 映射到 read-only 沙箱的文件读取;WebSearch 映射到 Responses API 原生
        # web_search 工具——它在服务端执行,不进本地沙箱,--sandbox read-only 的断网
        # 约束管不到它,不存在假证据问题。其余 Claude 工具语义(Bash 等)仍不映射:
        # 静默降级会产出假证据,必须 fail-closed。
        tools = [str(t).strip() for t in (request.allowed_tools or [])]
        unmapped = [t for t in tools if t.lower() not in ("read", "websearch")]
        if unmapped:
            raise AIProviderError(
                "Codex CLI only maps the Read and WebSearch tools; "
                f"unmapped tools requested: {','.join(unmapped)}"
            )
        read_request = any(t.lower() == "read" for t in tools)
        web_search_request = any(t.lower() == "websearch" for t in tools)

        prompt_content = ""
        if request.system:
            prompt_content += f"[System]\n{request.system}\n\n"
        for msg in request.messages:
            prompt_content += f"[{msg['role'].title()}]\n{msg['content']}\n\n"
        if read_request:
            # codex 无 Read 工具:read-only 沙箱里模型用 shell 读命令(cat 等)看文件,提示语对齐语义。
            prompt_content += "\n涉及的本地文件请直接用读命令(如 cat)查看原文。\n"

        run_root = Path(os.environ.get("FLORI_CODEX_TRACE_DIR", "/tmp/flori-work/codex-runs"))
        run_dir = run_root / f"{int(time.time() * 1000)}-{os.getpid()}-{uuid4().hex[:8]}"
        work_dir = run_dir / "work"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise AIProviderError(f"Codex trace dir create failed: {e}") from e
        events_path = run_dir / "events.jsonl"
        final_path = run_dir / "final.txt"

        env = build_cli_env("codex-cli", self._env)
        cmd = list(self._command_template)
        if not cmd:
            cmd = ["codex", "exec"]
        # 可弱化的安全参数(approval/sandbox/working dir)无条件由代码追加,不看模板:
        # 模板在构造期已被拒绝携带同名 flag,不存在"模板钉死则尊重"的分支。
        # 保护性布尔开关(--ignore-user-config 等)无值可弱化,模板带上等价,条件追加防重复。
        # --ignore-user-config 恒在:宿主 ~/.codex/config.toml 常配 danger-full-access 与
        # approval never,一旦继承用户配置就等于放开模型 shell 全权限;需要的配置键
        # (model_reasoning_effort、web_search 等)一律经 -c 显式传入。
        # 审批策略只能走 -c:codex exec 没有 -a/--ask-for-approval flag,传了会被参数解析
        # 判成 unexpected argument,整个调用以 rc=2 退出。
        cmd += ["-c", "approval_policy=never"]
        if not self._has_flag(cmd, "--ignore-user-config"):
            cmd += ["--ignore-user-config"]
        if not self._has_flag(cmd, "--ignore-rules"):
            cmd += ["--ignore-rules"]
        if not self._has_flag(cmd, "--skip-git-repo-check"):
            cmd += ["--skip-git-repo-check"]
        # 无 --strict-config 时 codex 静默忽略不认识的 -c 键。安全键被静默忽略等于沙箱悄悄
        # 变宽,web_search 被静默忽略则产出没有联网依据的假证据,两者都必须响而不是默。
        if not self._has_flag(cmd, "--strict-config"):
            cmd += ["--strict-config"]
        # 不传 --sandbox:该 flag 会整体顶掉权限档案,deny 名单被静默忽略。省掉它时
        # codex 的默认档位仍是 read-only,即档案万一没生效也不会比原来更宽。
        cmd += self._permissions_override(env)
        # 模型 shell 的环境走 core 白名单,不整份继承 codex 进程环境:CODEX_HOME 与代理
        # URL 里可能带的凭据会出现在模型可读的 /proc/self/environ 里。取值域 core/all/none
        # 由 --strict-config 实测得到;none 连 PATH 一起清掉,读命令找不到二进制,故取 core。
        cmd += ["-c", "shell_environment_policy.inherit=core"]
        if not self._has_flag(cmd, "--ephemeral"):
            cmd += ["--ephemeral"]
        # 工作根是每次调用新建的空目录,模型的相对路径操作落不到任何既有产物上。
        cmd += ["--cd", str(work_dir)]
        if not self._has_flag(cmd, "--json"):
            cmd += ["--json"]
        if not self._has_flag(cmd, "-o", "--output-last-message"):
            cmd += ["-o", str(final_path)]
        # add_dirs 不透传:codex 的 --add-dir 是"额外可写目录",与 claude/qoder 的读授权
        # 语义相反。read-only 下它换不到任何读收益,却会在沙箱档位一旦放宽时变成写授权。
        # 读取范围由沙箱决定,见本类 docstring。
        model_arg = request.model or self._model
        if model_arg and not self._has_flag(cmd, "--model", "-m"):
            cmd += ["--model", model_arg]
        # 推理档位:codex 无专用 flag,走 -c model_reasoning_effort(取值域由服务端按模型声明,
        # 当前一代为 low/medium/high/xhigh[/max]);请求级 > provider 默认;模板已钉同键时尊重模板。
        effort = request.reasoning_effort or self._reasoning_effort
        if effort and not self._has_config_key(cmd, "model_reasoning_effort"):
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        # web_search 只能走 -c:codex exec 没有交互 CLI 的 --search flag。键与取值域
        # (disabled/cached/indexed/live 字符串枚举)经 --strict-config 实测。因为恒带
        # --ignore-user-config,宿主配置里的 web_search 不会漏入,必须在此显式开启;
        # 取证要一手来源,固定 live。模板已钉同键时尊重模板。
        if web_search_request and not self._has_config_key(cmd, "web_search"):
            cmd += ["-c", "web_search=live"]
        for img in request.images or []:
            cmd += ["--image", str(Path(img).resolve())]
        cmd += ["-"]

        # 工具请求(Read/WebSearch)与 claude 的 allowed_tools 同窗;纯文本/图片按图数给。
        timeout = 1800 if tools else min(600 + 25 * len(request.images or []), 1800)
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
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise AIProviderError(f"Codex CLI timeout after {timeout}s")
        duration = time.time() - start

        raw = stdout.decode(errors="replace")
        try:
            events_path.write_text(raw, encoding="utf-8")
        except OSError:
            pass
        thread, usage, turns, errors, last_message = self._parse_events(raw)
        if proc.returncode != 0:
            detail = (stderr.decode(errors="replace") + raw)[-1000:]
            err = self._classify_error(detail)
            err.transcript_path = str(events_path)  # type: ignore[attr-defined]
            raise err
        # 沙箱起不来时 codex 不报错,只是让每条 shell 命令带着 bwrap 的错误返回,rc 仍是 0。
        # 请求要 Read 却一个文件都没真读到,产出的就是无依据的结论,必须失败而不是照收。
        if read_request and (hit := self._sandbox_unavailable_marker(raw)):
            err = AIProviderError(
                f"Codex CLI sandbox unavailable, local reads all failed: {hit}"
            )
            err.transcript_path = str(events_path)  # type: ignore[attr-defined]
            raise err

        try:
            content = final_path.read_text(encoding="utf-8")
        except OSError:
            content = last_message or ""
        if not content and last_message:
            content = last_message
        usage = usage or {}
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        cr = int(usage.get("cached_input_tokens", 0) or 0)
        thread_id = thread.get("thread_id") if isinstance(thread, dict) else None
        model = model_arg or "unknown"
        return LLMResponse(
            content=content,
            model=model,
            provider="codex-cli",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cr,
            cost_usd=0.0,
            duration_sec=round(duration, 2),
            num_turns=turns,
            cached=cr > 0,
            session_id=thread_id,
            finish_reason=(
                "turn.failed" if errors
                else "turn.completed" if turns > 0
                else None
            ),
            raw={
                "source": "codex-jsonl",
                "thread_id": thread_id,
                "usage": usage,
                "events_path": str(events_path),
                "errors": errors,
            },
            transcript_path=str(events_path),
        )


class QoderCLIProvider:
    """Qoder CLI 接入(subprocess 调用)。`qodercli -p -o json` 顶层 JSON 与 claude CLI 同构
    (result/usage/num_turns/session_id/subtype/is_error),解析路径共用同一套字段。"""

    def __init__(self, command_template: list[str], env: dict | None = None,
                 model: str | None = None, reasoning_effort: str | None = None):
        _reject_unsafe_template(
            "qoder-cli", command_template or [], _QODER_FORBIDDEN_TEMPLATE_ARGS,
        )
        self._command_template = command_template
        self._env = env or {}
        self._model = model
        self._reasoning_effort = reasoning_effort

    @staticmethod
    def _find_transcript(session_id: str | None, env: dict) -> str | None:
        """按 session_id 定位 qodercli 自写的会话 transcript:
        `$QODER_CONFIG_DIR|$HOME/.qoder/projects/<cwd编码>/<session_id>.jsonl`。
        找不到(HOME 未挂/无档)回 None,绝不影响主流程。"""
        if not session_id:
            return None
        try:
            cfg_dir = env.get("QODER_CONFIG_DIR") or str(
                Path(env.get("HOME") or os.path.expanduser("~")) / ".qoder")
            hits = sorted(Path(cfg_dir).glob(f"projects/*/{session_id}.jsonl"))
            return str(hits[0]) if hits else None
        except Exception:
            return None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        prompt_content = ""
        if request.system:
            prompt_content += f"[System]\n{request.system}\n\n"
        for msg in request.messages:
            prompt_content += f"[{msg['role'].title()}]\n{msg['content']}\n\n"

        # 视觉:与 claude 同方式,把帧图绝对路径写进 prompt 放开 Read 工具逐张查看;
        # 帧目录用 --add-dir 加入可访问范围。
        extra_dirs: set[str] = set()
        if request.images:
            missing = []
            for p in request.images:
                ap = str(Path(p).resolve())
                extra_dirs.add(str(Path(ap).parent))
                if ap not in prompt_content:
                    missing.append(ap)
            if missing:
                prompt_content += "\n截图(用 Read 工具逐张查看):\n" + "\n".join(missing) + "\n"

        cmd = [part for part in self._command_template if "{prompt_file}" not in part]
        # 结构化输出强制 json(同 claude:剔除模板可能带的输出格式再钉死),拿真实 usage 和终态。
        for flag in ("-o", "--output-format"):
            if flag in cmd:
                _i = cmd.index(flag)
                del cmd[_i:_i + 2]
        cmd += ["-o", "json"]
        model_arg = request.model or self._model
        if model_arg and "--model" not in cmd and "-m" not in cmd:
            cmd += ["--model", model_arg]
        # 推理档位(qoder --reasoning-effort):请求级 > provider 默认;模板钉死时尊重模板。
        # CLI 自己不校验取值,越界档位会被服务端静默换成默认;取值域在 AIGateway
        # 落地前按 providers.yaml 复核并拒绝,到这里的值一定合法。
        effort = request.reasoning_effort or self._reasoning_effort
        if effort and "--reasoning-effort" not in cmd:
            cmd += ["--reasoning-effort", effort]
        if request.allowed_tools:
            # 联网/取证类请求透传工具名。qodercli 无 --max-turns,轮数无界,靠下方 timeout 兜底;
            # 工具集取决于 qodercli 内置工具表(对二进制 strings 实测含 WebSearch/WebFetch,
            # 与 Read/Bash/Grep 等并列),WebSearch 经 --allowed-tools 透传即生效,无独立开关。
            cmd += ["--allowed-tools", *request.allowed_tools]
            for d in request.add_dirs or []:
                cmd += ["--add-dir", d]
        elif request.images:
            cmd += ["--allowed-tools", "Read"]
            for d in sorted(extra_dirs):
                cmd += ["--add-dir", d]
        else:
            # 纯文本调用:--tools "" 禁用全部工具,强制单次纯文本生成(与 claude 的坑同源:
            # 默认带工具会多轮 agentic 空转)。qodercli 无 --max-turns,禁工具后自然单轮。
            cmd += ["--tools", ""]

        env = build_cli_env("qoder-cli", self._env)
        # Qoder 的大论文纯文本生成实测会超过 600 秒。统一给到步骤级上限 30 分钟,
        # 仍由 Worker 心跳、租约和外层 step timeout 约束,避免 provider 先于步骤预算误杀。
        timeout = 1800
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
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise AIProviderError(f"Qoder CLI timeout after {timeout}s")
        duration = time.time() - start

        if proc.returncode != 0:
            detail = (stderr.decode() + stdout.decode())[:500]
            if _detail_is_rate_limited(detail):
                err: Exception = AIRateLimitError(f"Qoder CLI rate-limited: {detail}")
            else:
                err = AIProviderError(f"Qoder CLI failed: {detail}")
            try:
                obj = json.loads(stdout.decode().strip())
                sid = obj.get("session_id") if isinstance(obj, dict) else None
                err.transcript_path = self._find_transcript(sid, env)  # type: ignore[attr-defined]
            except Exception:
                pass
            raise err

        raw = stdout.decode().strip()
        # 解析失败(非 json 输出)回退原始文本 + 零统计,不让步骤失败。
        content, in_tok, out_tok = raw, 0, 0
        cc = cr = turns = 0
        model = model_arg or "unknown"
        raw_obj: dict | None = None
        session_id = None
        api_ms = None
        finish_reason = None
        credits = None
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                raw_obj = obj
                raw_session_id = obj.get("session_id")
                session_id = raw_session_id if isinstance(raw_session_id, str) else None
                content = _require_cli_envelope_result(
                    "Qoder CLI", obj,
                    transcript_path=self._find_transcript(session_id, env),
                )
                u = obj.get("usage") or {}
                in_tok = int(u.get("input_tokens", 0) or 0)
                out_tok = int(u.get("output_tokens", 0) or 0)
                cc = int(u.get("cache_creation_input_tokens", 0) or 0)
                cr = int(u.get("cache_read_input_tokens", 0) or 0)
                turns = int(obj.get("num_turns", 0) or 0)
                model = _extract_cli_model(obj) or model
                _api = obj.get("duration_api_ms") or obj.get("duration_ms")
                api_ms = float(_api) if isinstance(_api, (int, float)) else None
                finish_reason = obj.get("subtype") or obj.get("stop_reason")
                raw_credits = obj.get("total_credits")
                if type(raw_credits) in (int, float):
                    try:
                        numeric_credits = float(raw_credits)
                    except (OverflowError, ValueError):
                        numeric_credits = None
                    if (
                        numeric_credits is not None
                        and math.isfinite(numeric_credits)
                        and 0 <= numeric_credits <= MAX_AI_CREDITS
                    ):
                        credits = numeric_credits
        except (json.JSONDecodeError, ValueError, TypeError, OverflowError):
            pass
        # Qoder 是包月订阅,无按量成本:cost 恒 0,token 用量照实入账(用于用量观测,不折算等价美元)。
        return LLMResponse(
            content=content,
            model=model,
            provider="qoder-cli",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_input_tokens=cc,
            cache_read_input_tokens=cr,
            cost_usd=0.0,
            credits=credits,
            duration_sec=round(duration, 2),
            num_turns=turns,
            cached=cr > 0,
            session_id=session_id,
            api_ms=api_ms,
            finish_reason=finish_reason,
            raw=raw_obj,
            transcript_path=self._find_transcript(session_id, env),
        )


# Gateway


class AIGateway:
    """面向调用方的门面。路由 + 降级 + 成本追踪。"""

    def __init__(self, providers_config: dict, pipelines_config: dict):
        self._providers_config = providers_config
        self._pipelines_config = pipelines_config
        self._providers: dict[str, Any] = {}
        self._dry_run = os.environ.get("DRY_RUN") == "1"

    async def call(
        self,
        step_name: str,
        request: LLMRequest,
    ) -> LLMResponse:
        if self._dry_run:
            return await DryRunProvider().complete(request)

        ai_config = self._get_step_ai_config(step_name)
        has_images = bool(request.images)
        errors: list[str] = []   # 累计各 provider 真实报错,附进异常→落 error.json,便于排错
        rate_limited = False     # 任一 provider 限流 → 整体按 ai_rate_limit 走长退避
        # 逐 tier 尝试链,含成功/失败。成功时写进返回 response.attempts,全败时写进异常 .attempts,供 AI 审计。
        attempts: list[dict] = []
        # 请求原值要在进循环前取:下面每个 tier 都会覆写 request.model。
        requested_effort = request.reasoning_effort

        def _attempt(
            tier: str, declared: dict, resolved: dict | None,
            *, ok: bool, err: Exception | None = None,
        ) -> dict:
            audit = self._attempt_selection(declared, resolved, requested_effort)
            a = {"tier": tier, **audit, "ok": ok}
            if err is not None:
                a["error_class"] = type(err).__name__
                a["error"] = str(err)[:500]
                # CLI 失败仍可能留有会话 transcript(失败留痕):经尝试链带给审计层回收。
                if getattr(err, "transcript_path", None):
                    a["transcript_path"] = err.transcript_path
            return a

        for tier in ["primary", "fallback"]:
            if tier not in ai_config:
                continue
            declared = ai_config[tier]
            resolved: dict | None = None
            try:
                resolved = self._resolve_tier(declared, request)
                request.model = resolved["model"]
                provider = self._get_provider(resolved["provider"])
                response = await provider.complete(request)
                attempts.append(_attempt(tier, declared, resolved, ok=True))
                response.tier_used = tier
                response.attempts = attempts
                self._annotate_selection(response, declared, resolved, requested_effort)
                return response
            except (AIProviderError, AIRateLimitError) as e:
                rate_limited = rate_limited or isinstance(e, AIRateLimitError)
                attempts.append(_attempt(tier, declared, resolved, ok=False, err=e))
                _log.warning("provider_failed", step=step_name, tier=tier,
                             provider=(resolved or declared).get("provider"),
                             model=(resolved or declared).get("model"),
                             rate_limited=isinstance(e, AIRateLimitError), error=str(e)[:400])
                errors.append(f"{tier}/{(resolved or declared).get('provider')}: {str(e)[:200]}")
                continue

        if has_images and "text_fallback" in ai_config:
            declared = ai_config["text_fallback"]
            resolved = None
            try:
                # 文本兜底按去图请求解析与复核 feature:vision 不是纯文本兜底的必需能力。
                resolved = self._resolve_tier(declared, dataclasses.replace(request, images=[]))
                # 用副本调用,别原地改调用方的 request(去图/换模型会污染后续重试/复用)。
                fb_request = dataclasses.replace(request, model=resolved["model"], images=[])
                provider = self._get_provider(resolved["provider"])
                response = await provider.complete(fb_request)
                attempts.append(_attempt("text_fallback", declared, resolved, ok=True))
                response.tier_used = "text_fallback"
                response.attempts = attempts
                self._annotate_selection(response, declared, resolved, requested_effort)
                return response
            except (AIProviderError, AIRateLimitError) as e:
                rate_limited = rate_limited or isinstance(e, AIRateLimitError)
                attempts.append(_attempt("text_fallback", declared, resolved, ok=False, err=e))
                _log.warning("provider_failed", step=step_name, tier="text_fallback",
                             provider=(resolved or declared).get("provider"),
                             model=(resolved or declared).get("model"),
                             rate_limited=isinstance(e, AIRateLimitError), error=str(e)[:400])
                errors.append(
                    f"text_fallback/{(resolved or declared).get('provider')}: {str(e)[:200]}",
                )

        raise AllProvidersFailedError(
            f"All providers failed for step {step_name} :: " + " || ".join(errors),
            error_type="ai_rate_limit" if rate_limited else "ai",
            attempts=attempts,
        )

    def _attempt_selection(
        self, declared: dict, resolved: dict | None, requested_effort: str | None,
    ) -> dict:
        """一次尝试的请求原值与实际生效值。resolved 为 None 表示 tier 还没解析成真实
        provider 就失败了,此时执行侧字段留空,虚拟 provider 只出现在 requested_provider。"""
        from .ai_selection import effective_model, effective_reasoning_effort

        provider = (resolved or {}).get("provider")
        if provider is None:
            model, effort, effort_source = None, None, None
        else:
            model, _ = effective_model(
                self._providers_config, provider, (resolved or {}).get("model"),
            )
            effort, effort_source = effective_reasoning_effort(
                self._providers_config, provider, requested_effort,
            )
        return {
            "requested_provider": declared.get("provider"),
            "requested_model": declared.get("model"),
            "requested_reasoning_effort": requested_effort,
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
            "reasoning_effort_source": effort_source,
        }

    def _annotate_selection(
        self,
        response: LLMResponse,
        declared: dict,
        resolved: dict,
        requested_effort: str | None,
    ) -> None:
        """把有效选择回填到响应。response.provider/model 仍由 provider 自己写(CLI 回报的
        真实模型比配置更权威),这里只补请求原值与档位。"""
        selection = self._attempt_selection(declared, resolved, requested_effort)
        response.requested_provider = selection["requested_provider"]
        response.requested_model = selection["requested_model"]
        response.requested_reasoning_effort = selection["requested_reasoning_effort"]
        response.reasoning_effort = selection["reasoning_effort"]
        response.reasoning_effort_source = selection["reasoning_effort_source"]

    def _get_step_ai_config(self, step_name: str) -> dict:
        steps = self._pipelines_config.get("steps", [])
        for s in steps:
            if s.get("name") == step_name:
                return s.get("ai", {})
        return {}

    def _resolve_tier(self, cfg: dict, request: LLMRequest) -> dict:
        """复核 concrete tier 的能力与参数;provider 缺失或未知由创建层 fail-closed。"""
        resolved = dict(cfg)
        self._verify_cli_request_features(resolved.get("provider"), request)
        self._verify_cli_request_params(resolved, request)
        return resolved

    def _verify_cli_request_params(self, resolved: dict, request: LLMRequest) -> None:
        """CLI 落地前复核本次生效的 model 与推理档位,越界一律拒绝。

        这是最后一道:三个 CLI 对越界档位都不报错,claude/qoder 按自家默认跑、codex 由
        服务端兜底,静默降级会产出一份看起来正常但档位不对的笔记。生效值口径必须与各
        provider complete 一致,即请求级优先、provider 默认兜底。
        """
        provider = resolved.get("provider")
        cfg = (self._providers_config.get("providers") or {}).get(provider)
        if not isinstance(cfg, dict) or cfg.get("type") not in _CLI_PROVIDER_TYPES:
            # 非 CLI provider 不消费 reasoning_effort;未配置的 provider 由 _create_provider 拒绝。
            return
        effective = {
            "model": resolved.get("model") or cfg.get("model"),
            "reasoning_effort": request.reasoning_effort or cfg.get("reasoning_effort"),
        }
        params = {
            key: value for key, value in effective.items()
            if type(value) is str and value.strip()
        }
        violation = validate_ai_param_override(
            str(provider), params, self._providers_config,
        )
        # 未声明模型取值域时无从核对,交 CLI 自己报错;档位没有这条豁免。
        if violation is None or violation.code == AI_PARAM_MODEL_DOMAIN_MISSING:
            return
        raise AIProviderError(violation.message())

    def _verify_cli_request_features(self, provider: Any, request: LLMRequest) -> None:
        """选定 CLI provider 后按运行配置 features 复核请求所需能力。
        静态 capability 映射只反映实现支持;配置摘掉 feature 必须在执行端也 fail-closed。"""
        if provider not in ("claude-cli", "codex-cli", "qoder-cli"):
            return
        needed = set()
        for tool in request.allowed_tools or []:
            t = str(tool).strip().lower()
            if t == "read":
                needed.add("read")
            elif t == "websearch":
                needed.add("websearch")
        if request.images:
            needed.add("vision")
        if not needed:
            return
        cfg = (self._providers_config.get("providers") or {}).get(provider)
        features = cfg.get("features") if isinstance(cfg, dict) else None
        enabled = {f for f in features if type(f) is str} if isinstance(features, list) else set()
        missing = sorted(needed - enabled)
        if missing:
            raise AIProviderError(
                f"provider '{provider}' config features 缺少本次请求需要的能力 "
                f"{','.join(missing)}:配置未启用即视为不可用(fail-closed)。"
            )

    def _get_provider(self, name: str):
        if name not in self._providers:
            self._providers[name] = self._create_provider(name)
        return self._providers[name]

    def _create_provider(self, name: str):
        cfg = self._providers_config.get("providers", {}).get(name, {})
        ptype = cfg.get("type", "")
        # 密钥解析与 worker 的能力自证同源(shared.ai_routing.resolve_api_credential),
        # 两处各写一份会出现 Gateway 调得通、worker 却永远不注册标签的死角。
        api_key = resolve_api_credential(name, cfg)

        if ptype == "anthropic":
            return AnthropicProvider(api_key=api_key)
        elif ptype in ("openai_compatible", "openai"):
            return OpenAICompatibleProvider(
                base_url=cfg.get("base_url", ""),
                api_key=api_key,
                provider_name=name,
            )
        elif ptype == "claude_cli":
            return ClaudeCLIProvider(
                command_template=cfg.get("command", []),
                env=cfg.get("env"),
                model=cfg.get("model"),
                reasoning_effort=cfg.get("reasoning_effort"),
            )
        elif ptype == "codex_cli":
            return CodexCLIProvider(
                command_template=cfg.get("command", []),
                env=cfg.get("env"),
                model=cfg.get("model"),
                reasoning_effort=cfg.get("reasoning_effort"),
            )
        elif ptype == "qoder_cli":
            return QoderCLIProvider(
                command_template=cfg.get("command", []),
                env=cfg.get("env"),
                model=cfg.get("model"),
                reasoning_effort=cfg.get("reasoning_effort"),
            )
        else:
            raise AIProviderError(f"Unknown provider type: {ptype}")


# Usage 文件读写


_USAGE_FILE_LOCK = threading.Lock()


def record_usage_to_file(usage: AIUsage, log_dir: Path) -> None:
    """步骤进程调用:追加到 .{step}.usage.json。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f".{usage.step}.usage.json"
    with _USAGE_FILE_LOCK:
        entries = json.loads(path.read_text()) if path.exists() else []
        entries.append({
            "exec_id": usage.exec_id,
            "provider": usage.provider,
            "model": usage.model,
            "job_id": usage.job_id,
            "step": usage.step,
            "worker_id": usage.worker_id,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cost_usd": usage.cost_usd,
            "credits": usage.credits,
            "duration_sec": usage.duration_sec,
            "num_turns": usage.num_turns,
            "cached": usage.cached,
            "created_at": usage.created_at.isoformat(),
        })
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def collect_usage_from_file(log_dir: Path, step: str) -> list[AIUsage]:
    """Worker 调用:读取 usage 文件,返回 AIUsage 列表。"""
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
            worker_id=e.get("worker_id"),
            input_tokens=e.get("input_tokens", 0),
            output_tokens=e.get("output_tokens", 0),
            cache_creation_input_tokens=e.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=e.get("cache_read_input_tokens", 0),
            cost_usd=e.get("cost_usd", 0.0),
            credits=e.get("credits"),
            duration_sec=e.get("duration_sec", 0.0),
            num_turns=e.get("num_turns", 0),
            cached=e.get("cached", False),
            created_at=datetime.fromisoformat(e["created_at"]),
        )
        for e in entries
    ]
