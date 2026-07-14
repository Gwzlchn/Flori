"""StepBase 执行组件的显式装配。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .step_ai import AIInvocation
from .step_artifacts import ArtifactIO
from .step_progress import StepProgressReporter
from .step_review import ReviewExecution
from .step_subprocess import SubprocessExecutor
from .structured_output import StructuredOutputParser


@dataclass(frozen=True)
class StepExecutionContext:
    step_name: str
    job_dir: Path
    config: dict
    log: object
    input_hashes: Callable[[], dict[str, str]]


@dataclass(frozen=True)
class StepExecutionServices:
    artifacts: ArtifactIO
    progress: StepProgressReporter
    commands: SubprocessExecutor
    structured: StructuredOutputParser
    ai: AIInvocation
    review: ReviewExecution

    @classmethod
    def create(cls, context: StepExecutionContext) -> "StepExecutionServices":
        artifacts = ArtifactIO(context.step_name, context.job_dir)
        progress = StepProgressReporter(context.step_name, context.job_dir, context.log)
        commands = SubprocessExecutor()
        structured = StructuredOutputParser(context.log)
        ai = AIInvocation(
            step_name=context.step_name,
            job_dir=context.job_dir,
            config=context.config,
            log=context.log,
            input_hashes=context.input_hashes,
            artifacts=artifacts,
            structured=structured,
        )
        review = ReviewExecution(
            step_name=context.step_name,
            job_dir=context.job_dir,
            artifacts=artifacts,
            ai=ai,
        )
        return cls(artifacts, progress, commands, structured, ai, review)
