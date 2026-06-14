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

    def call_ai(self, prompt: str, images: list[Path] | None = None, **kwargs) -> str:
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
