"""StepBase 统一基类。所有 steps/*.py 继承此类。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import structlog

from .ai_gateway import AIGateway, record_usage_to_file
from .errors import StepError
from .models import AIUsage, LLMRequest


def file_hash(path: Path) -> str:
    """计算文件 SHA-256，返回 'sha256:{hex}' 格式。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


class StepBase:
    def __init__(self, step_name: str, job_dir: Path, config: dict):
        self.step_name = step_name
        self.job_dir = job_dir
        self.config = config
        self.log = self._setup_logger()
        self._gateway: AIGateway | None = None
        self._call_index = 0
        # 最近一次 AI 调用实际命中的 provider / model(供版本化笔记标记)。
        self.last_ai_provider: str | None = None
        self.last_ai_model: str | None = None

    # ── 统一入口 ──

    def run(self) -> None:
        try:
            missing = self.validate_inputs()
            if missing:
                from .errors import InputMissingError
                raise InputMissingError(f"Missing: {missing}")

            if not self.should_run():
                self.log.info("skip: up-to-date")
                return

            start = time.time()
            result = self.execute()
            duration = time.time() - start

            self.mark_done()
            self.write_meta({
                "status": "done",
                "duration_sec": round(duration, 1),
                **(result or {}),
            })
        except StepError as e:
            self.write_error(e.error_type, str(e))
            sys.exit(1)
        except Exception as e:
            self.write_error("unknown", str(e), traceback.format_exc())
            sys.exit(1)

    @classmethod
    def cli_main(cls, step_name: str) -> None:
        """步骤脚本统一入口：解析 --job-dir/--step-config，实例化并 run。"""
        import argparse

        from shared.logging_setup import setup_logging
        setup_logging()  # 步骤子进程日志也输出结构化 JSON,与 scheduler/worker 一致

        parser = argparse.ArgumentParser()
        parser.add_argument("--job-dir", required=True)
        parser.add_argument("--step-config", required=True)
        args = parser.parse_args()
        config = json.loads(Path(args.step_config).read_text())
        cls(step_name, Path(args.job_dir), config).run()

    # ── 子类实现 ──

    def execute(self) -> dict | None:
        raise NotImplementedError

    def validate_inputs(self) -> list[str]:
        return []

    def input_hashes(self) -> dict[str, str]:
        return {}

    # ── 幂等 ──

    def should_run(self) -> bool:
        done_file = self.job_dir / f".{self.step_name}.done"
        if not done_file.exists():
            return True
        stored = json.loads(done_file.read_text())
        return stored.get("input_hashes") != self.input_hashes()

    def mark_done(self) -> None:
        data = {
            "step": self.step_name,
            "input_hashes": self.input_hashes(),
            "finished_at": datetime.now().isoformat(),
        }
        (self.job_dir / f".{self.step_name}.done").write_text(
            json.dumps(data, ensure_ascii=False, indent=2)
        )

    # ── IO 工具 ──

    def write_output(self, filename: str, data) -> None:
        target = self.job_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        if isinstance(data, (dict, list)):
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        elif isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        elif isinstance(data, bytes):
            tmp.write_bytes(data)
        tmp.rename(target)

    def ai_provider_model(self) -> tuple[str, str]:
        """最近一次 AI 调用的 (provider, model)。claude-cli 订阅默认 Opus 4.8——config 里是
        占位 "subscription",此处换成真实模型名,供笔记/评审统一标注。"""
        prov = self.last_ai_provider or "unknown"
        model = self.last_ai_model or "unknown"
        if prov == "claude-cli" and model in ("subscription", "unknown", ""):
            model = "claude-opus-4-8"
        return prov, model

    def write_smart_note(self, content: str) -> str:
        """智能笔记按版本落盘:output/versions/notes_smart_{provider}_{model}_{时间}.md,
        开头加一行说明(生成时间 / 方式 / 模型)。不再写规范 notes_smart.md——前端取最新版本。
        返回相对路径,供评审步在 review.json 里标明评的是哪一版。"""
        prov, model = self.ai_provider_model()
        # 字段内只允许字母数字与 . - (把 _ 也归一为 -),保证文件名按 "_" 切分无歧义。
        safe = lambda s: __import__("re").sub(r"[^0-9A-Za-z.-]+", "-", s).strip("-") or "x"
        now = datetime.now()
        rel = f"output/versions/notes_smart_{safe(prov)}_{safe(model)}_{now.strftime('%Y%m%d-%H%M%S')}.md"
        header = f"> 生成于 {now.strftime('%Y/%m/%d %H:%M:%S')} · 方式 {prov} · 模型 {model}\n\n"
        self.write_output(rel, header + content)
        return rel

    def write_review(self, review: dict, note_file: str | None) -> None:
        """评审结果落盘:补记 生成时间 / 方式 / 模型 + 评的是哪一版智能笔记(note_file)。"""
        prov, model = self.ai_provider_model()
        review["note_file"] = note_file
        review["provider"] = prov
        review["model"] = model
        review["generated_at"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.write_output("output/review.json", review)

    def latest_smart_note(self) -> Path | None:
        """工作目录里最新的智能笔记版本文件(供评审步读取并标注评的是哪一版)。"""
        from shared.notes_versions import latest_smart
        vdir = self.job_dir / "output" / "versions"
        if not vdir.is_dir():
            return None
        rels = [f"output/versions/{p.name}" for p in vdir.glob("notes_smart_*.md")]
        latest = latest_smart(rels)
        return (self.job_dir / latest) if latest else None

    def load_json(self, filename: str) -> dict | list:
        return json.loads((self.job_dir / filename).read_text(encoding="utf-8"))

    def write_meta(self, meta: dict) -> None:
        path = self.job_dir / f".{self.step_name}.meta.json"
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    def write_error(self, error_type: str, message: str, trace: str = "") -> None:
        path = self.job_dir / f".{self.step_name}.error.json"
        path.write_text(json.dumps({
            "step": self.step_name,
            "error_type": error_type,
            "message": message,
            "trace": trace,
            "timestamp": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2))

    # ── 进度 ──

    def report_progress(self, current: int, total: int, message: str = "") -> None:
        pct = round(100 * current / max(total, 1))
        path = self.job_dir / f".{self.step_name}.progress"
        path.write_text(json.dumps({
            "source": "step",
            "current": current,
            "total": total,
            "pct": pct,
            "message": message,
            "updated_at": time.time(),
        }))
        if pct % 10 == 0 or current == total:
            self.log.info("progress", current=current, total=total, pct=pct)

    # ── AI 调用 ──

    def override_provider(self) -> str:
        """读 job.json 里本步的 provider 覆盖(无则空串)。供 input_hashes 纳入,
        使"换 provider 重跑"改变指纹、绕过幂等跳过。"""
        try:
            job = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return (job.get("ai_overrides") or {}).get(self.step_name, "") or ""

    def _apply_provider_override(self) -> None:
        """按 job.json 的 ai_overrides[step] 覆盖本步 provider(供"选 provider 重跑")。
        只用所选 provider(去掉 fallback),避免失败时静默回退到别的 provider,
        保证版本化笔记的 provider 标记如实。"""
        try:
            job = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        provider = (job.get("ai_overrides") or {}).get(self.step_name)
        if not provider:
            return
        pcfg = self.config.get("providers", {}).get("providers", {}).get(provider, {})
        models = pcfg.get("models", [])
        model = models[0] if models else "subscription"
        self.config["ai"] = {"primary": {"provider": provider, "model": model}}
        self._gateway = None  # 强制按新 ai 配置重建

    def call_ai(self, prompt: str, images: list[Path] | None = None, **kwargs) -> str:
        self._apply_provider_override()
        if self._gateway is None:
            self._gateway = AIGateway(
                self.config.get("providers", {}),
                {"steps": [{"name": self.step_name, "ai": self.config.get("ai", {})}]},
            )

        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            images=images or [],
            system=self._load_system_prompt(),
            **kwargs,
        )

        import asyncio
        response = asyncio.run(
            self._gateway.call(self.step_name, request, job_id=self.job_dir.name)
        )
        self.last_ai_provider = response.provider
        self.last_ai_model = response.model

        self.log.info(
            "ai_call",
            provider=response.provider,
            model=response.model,
            cost_usd=response.cost_usd,
            tokens=f"{response.input_tokens}+{response.output_tokens}",
        )

        step_exec_id = os.environ.get("STEP_EXEC_ID", f"{self.job_dir.name}:{self.step_name}")
        log_dir = self.job_dir / "logs"
        record_usage_to_file(
            AIUsage(
                exec_id=f"{step_exec_id}:{self._call_index}",
                provider=response.provider,
                model=response.model,
                job_id=self.job_dir.name,
                step=self.step_name,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                duration_sec=response.duration_sec,
                cached=response.cached,
            ),
            log_dir,
        )
        self._call_index += 1
        return response.content

    def call_ai_json(
        self,
        prompt: str,
        fallback: dict,
        score_keys: list[str] | None = None,
        images: list[Path] | None = None,
        **kwargs,
    ) -> tuple[dict, bool]:
        """调用 AI 并解析 JSON。解析失败时回退到 fallback（附 raw_response/parse_failed）。
        若给 score_keys 且结果缺 overall，按维度均值自动补 overall。
        返回 (result, parse_failed)。"""
        kwargs.setdefault("response_format", "json")
        raw = self.call_ai(prompt, images=images, **kwargs)
        parse_failed = False
        try:
            result = json.loads(self._extract_json(raw))
        except (json.JSONDecodeError, ValueError):
            self.log.warn("ai_json_parse_failed", raw=raw[:200])
            result = {**fallback, "raw_response": raw[:500], "parse_failed": True}
            parse_failed = True
        if score_keys and "overall" not in result:
            scores = [result.get(k, 3) for k in score_keys]
            result["overall"] = round(sum(scores) / max(len(scores), 1), 1)
        return result, parse_failed

    @staticmethod
    def _extract_json(raw: str) -> str:
        """从 AI 输出里抽出 JSON:claude-cli 常包 ```json 围栏或带前后说明文字。
        先剥代码围栏,再退化为取首个 { 到末个 } 的子串。"""
        s = (raw or "").strip()
        if s.startswith("```"):
            import re
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"\n?```\s*$", "", s).strip()
        if not s.startswith("{"):
            i, j = s.find("{"), s.rfind("}")
            if i != -1 and j > i:
                s = s[i:j + 1]
        return s

    # ── 外部命令 ──

    def run_subprocess(
        self, cmd: list[str], timeout: int = 600, **kwargs
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True, **kwargs
        )

    # ── Private ──

    def _setup_logger(self):
        return structlog.get_logger(step=self.step_name, job_dir=str(self.job_dir))

    def _load_system_prompt(self) -> str | None:
        prompts_dir = self.config.get("paths", {}).get("prompts_dir")
        if not prompts_dir:
            return None
        path = Path(prompts_dir) / f"{self.step_name}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None
