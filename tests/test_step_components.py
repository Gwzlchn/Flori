"""Step 执行组件的装配与职责边界测试。"""

from pathlib import Path

from shared.step_ai import AIInvocation
from shared.step_artifacts import ArtifactIO
from shared.step_base import StepBase
from shared.step_components import StepExecutionServices
from shared.step_progress import StepProgressReporter
from shared.step_review import ReviewExecution
from shared.step_subprocess import SubprocessExecutor
from shared.structured_output import StructuredOutputParser


class _Step(StepBase):
    def execute(self):
        return {}


def test_default_factory_wires_explicit_components(tmp_path: Path) -> None:
    step = _Step("test_step", tmp_path, {})

    assert isinstance(step.artifacts, ArtifactIO)
    assert isinstance(step.progress, StepProgressReporter)
    assert isinstance(step.commands, SubprocessExecutor)
    assert isinstance(step.structured, StructuredOutputParser)
    assert isinstance(step.ai, AIInvocation)
    assert isinstance(step.review, ReviewExecution)


def test_step_base_has_no_execution_compatibility_methods() -> None:
    execution_methods = {
        "write_output",
        "load_json",
        "write_meta",
        "write_error",
        "report_progress",
        "run_subprocess",
        "call_ai",
        "call_ai_json",
        "run_dimension_review",
    }

    assert execution_methods.isdisjoint(StepBase.__dict__)


def test_service_factory_is_explicitly_injectable(tmp_path: Path) -> None:
    built = []

    def factory(context):
        built.append(context)
        return StepExecutionServices.create(context)

    step = _Step("test_step", tmp_path, {}, service_factory=factory)

    assert built[0].step_name == "test_step"
    assert built[0].job_dir == tmp_path
    assert built[0].config is step.config
    assert built[0].input_hashes() == {}
