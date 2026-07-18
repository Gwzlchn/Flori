"""StepBase 只负责编排步骤生命周期和幂等边界。"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

import structlog

from .errors import StepError
from .step_artifacts import file_hash
from .step_components import StepExecutionContext, StepExecutionServices


def def_digest_for(version: str | int | None, ai: dict | None) -> str:
    """计算单步 pipeline 定义指纹。"""
    definition = {
        "version": str(version if version is not None else "1"),
        "ai": ai or {},
    }
    blob = json.dumps(definition, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pipeline_digest_for(steps: list[dict]) -> str:
    """聚合整条 pipeline 的单步定义指纹。"""
    per_step = {
        step.get("name", ""): def_digest_for(step.get("version"), step.get("ai"))
        for step in steps
    }
    blob = json.dumps(per_step, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StepBase:
    """编排输入校验、幂等判断、执行和最终产物发布。"""

    def __init__(
        self,
        step_name: str,
        job_dir: Path,
        config: dict,
        *,
        service_factory: Callable[
            [StepExecutionContext], StepExecutionServices
        ] | None = None,
    ):
        self.step_name = step_name
        self.job_dir = job_dir
        self.config = config
        self.log = structlog.get_logger(step=step_name, job_dir=str(job_dir))
        context = StepExecutionContext(
            step_name=step_name,
            job_dir=job_dir,
            config=config,
            log=self.log,
            input_hashes=self.input_hashes,
        )
        services = (service_factory or StepExecutionServices.create)(context)
        self.artifacts = services.artifacts
        self.progress = services.progress
        self.commands = services.commands
        self.structured = services.structured
        self.ai = services.ai
        self.review = services.review

    def run(self) -> None:
        try:
            self.ai.override_provider()
            missing = self.validate_inputs()
            if missing:
                from .errors import InputMissingError

                raise InputMissingError(f"Missing: {missing}")
            if not self.should_run():
                self.log.info("skip: up-to-date")
                # 幂等跳过也刷新 candidate(reused 标记):中心 manifest 已一致时 Worker
                # 省去重发 IO,仅缺/不一致时自愈重发(审查 P3-7)。
                self.artifacts.write_manifest_candidate(
                    self.input_hashes(), reused=True,
                )
                return

            started_at = time.time()
            result = self.execute()
            duration = time.time() - started_at
            self.mark_done()
            # candidate 采集与 .done 双写(设计稿 §2.11 阶段 A):.done 字节契约不变,
            # candidate 只承载 Worker 组装 manifest 所需的子进程事实。
            self.artifacts.write_manifest_candidate(self.input_hashes())
            self.artifacts.write_meta({
                "status": "done",
                "duration_sec": round(duration, 1),
                **(result or {}),
            })
        except StepError as exc:
            self.artifacts.write_error(exc.error_type, str(exc))
            print(f"[{exc.error_type}] {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        except Exception as exc:
            self.artifacts.write_error("unknown", str(exc), traceback.format_exc())
            traceback.print_exc()
            sys.exit(1)

    @classmethod
    def cli_main(cls, step_name: str) -> None:
        """从 Worker 固化的 step config 启动步骤。"""
        import argparse

        from .logging_setup import setup_logging

        setup_logging()
        parser = argparse.ArgumentParser()
        parser.add_argument("--job-dir", required=True)
        parser.add_argument("--step-config", required=True)
        args = parser.parse_args()
        config = json.loads(Path(args.step_config).read_text())
        configured_name = (config.get("step") or {}).get("name")
        if not isinstance(configured_name, str) or not configured_name:
            from .errors import InputInvalidError

            raise InputInvalidError("step config is missing runtime name")
        cls(configured_name, Path(args.job_dir), config).run()

    def execute(self) -> dict | None:
        raise NotImplementedError

    def validate_inputs(self) -> list[str]:
        return []

    def input_hashes(self) -> dict[str, str]:
        return {}

    def _def_digest(self) -> str:
        step = self.config.get("step", {}) if isinstance(self.config, dict) else {}
        ai = self.config.get("ai", {}) if isinstance(self.config, dict) else {}
        return def_digest_for(step.get("version", "1"), ai)

    def _read_local_manifest(self) -> dict | None:
        """读 scope 根下已发布 manifest;缺失/损坏/远端不可见(pull 不带内部命名空间)返回 None。"""
        import json as _json

        from .step_manifest import ManifestError, validate_manifest

        path = self.job_dir / ".flori" / "steps" / self.step_name / "manifest.json"
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            validate_manifest(data)
        except (OSError, ValueError, ManifestError):
            return None
        return data

    def should_run(self) -> bool:
        # manifest 优先(§2.11 阶段A dual):有效 done manifest 且 input/definition digest
        # 均与当前一致才可跳过;manifest 在但不兼容时必跑,不再看 .done。
        from .step_completion import MODE_MANIFEST_ONLY, completion_mode

        manifest = self._read_local_manifest()
        if manifest is not None and manifest.get("outcome") == "done":
            try:
                from .step_manifest import compute_input_digest

                fingerprints = dict(self.input_hashes())
                # NAS 源身份(worker 经 step config 注入):与 manifest 生产端同序
                # setdefault 合并,两端计算同一个 current(读写对称)。
                source_fingerprints = (
                    self.config.get("step", {}).get("source_fingerprints")
                    if isinstance(self.config, dict) else None
                )
                if isinstance(source_fingerprints, dict):
                    for key, value in source_fingerprints.items():
                        fingerprints.setdefault(key, value)
                current_input = compute_input_digest(fingerprints)
            except Exception:
                return True
            expected_definition = (
                self.config.get("step", {}).get("definition_digest")
                if isinstance(self.config, dict) else None
            )
            compatibility = manifest["compatibility"]
            if compatibility["input_digest"] != current_input:
                return True
            if (
                expected_definition is not None
                and compatibility["definition_digest"] != expected_definition
            ):
                return True
            return False
        if completion_mode() == MODE_MANIFEST_ONLY:
            return True
        if not self.artifacts.done_path.exists():
            return True
        stored = self.artifacts.read_done()
        if stored.get("input_hashes") != self.input_hashes():
            return True
        stored_definition = stored.get("def_digest")
        if stored_definition is not None and stored_definition != self._def_digest():
            return True
        return False

    def mark_done(self) -> None:
        self.artifacts.write_done({
            "step": self.step_name,
            "input_hashes": self.input_hashes(),
            "def_digest": self._def_digest(),
            "finished_at": datetime.now().isoformat(),
        })
