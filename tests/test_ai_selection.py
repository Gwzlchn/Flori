"""有效 AI 选择:统一进输入指纹(G)与进审计(H)。"""

import importlib
import json
import pkgutil
from pathlib import Path

import pytest

import steps
from shared.ai_selection import (
    EFFORT_FROM_PROVIDER,
    EFFORT_FROM_REQUEST,
    EFFORT_UNSET,
    fingerprint_projection,
    selection_snapshot,
)
from shared.step_base import StepBase


PROVIDERS = {
    "providers": {
        "claude-cli": {
            "type": "claude_cli", "model": "opus5", "reasoning_effort": "xhigh",
            # models 是覆盖的合法域,未声明则任何 model 覆盖都被执行端 fail-closed 拒绝。
            "models": ["opus5", "claude-sonnet-4-6"],
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            "features": ["read", "vision", "websearch"],
        },
        "qoder-cli": {
            "type": "qoder_cli", "model": "ultimate", "reasoning_effort": "max",
            "models": ["ultimate"],
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            "features": ["read", "vision", "websearch"],
        },
        "openai": {"type": "openai", "models": ["gpt-test"], "features": []},
    },
}
DECLARED_AI = {"primary": {"provider": "claude-cli", "model": "declared-model"}}


class AiStep(StepBase):
    """只提供固定自身指纹的 AI 步骤,用来观察共享层往指纹里加了什么。"""

    def execute(self):
        return {}

    def step_input_hashes(self):
        return {"data": "sha256:" + "a" * 64}


def make_step(tmp_path, *, ai=None, providers=None, job=None, cls=AiStep):
    tmp_path.mkdir(parents=True, exist_ok=True)
    if job is not None:
        (tmp_path / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return cls("test_step", tmp_path, {
        "step": {"name": "test_step", "version": "1"},
        "ai": DECLARED_AI if ai is None else ai,
        "providers": PROVIDERS if providers is None else providers,
        "paths": {"prompts_dir": str(tmp_path / "prompts"),
                  "config_dir": str(tmp_path / "configs")},
        "domain": {"name": "general"},
        "style_tags": [],
    })


def selection_of(tmp_path, **kwargs):
    return make_step(tmp_path, **kwargs).input_hashes()["ai_selection"]


# G:统一注入


def _step_classes():
    found = []
    for module in pkgutil.walk_packages(steps.__path__, prefix="steps."):
        try:
            loaded = importlib.import_module(module.name)
        except Exception:
            continue
        for value in vars(loaded).values():
            if isinstance(value, type) and issubclass(value, StepBase) and value is not StepBase:
                found.append(value)
    return sorted(set(found), key=lambda cls: f"{cls.__module__}.{cls.__qualname__}")


def test_no_step_overrides_the_shared_input_hashes_entry():
    """有效 AI 选择由 StepBase 统一并入。步骤一旦自己覆盖 input_hashes 就绕过了它,
    换 provider/模型/档位后会被幂等判成 up-to-date 而静默跳过。"""
    classes = _step_classes()
    assert classes, "没扫到任何步骤类,说明扫描方式失效"
    offenders = [
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in classes if cls.input_hashes is not StepBase.input_hashes
    ]
    assert offenders == []


def test_no_step_hand_rolls_the_ai_selection_fingerprint():
    """指纹里的 AI 选择只能有一份实现,步骤里不许再出现手工版本。"""
    offenders = []
    for path in sorted(Path("steps").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "selection_fingerprint" in text or "override_provider" in text:
            offenders.append(str(path))
    assert offenders == []


def test_non_ai_step_fingerprint_has_no_selection_key(tmp_path):
    """不走 AI 的步骤不加这个键,既有 manifest 的摘要不受牵连。"""
    assert make_step(tmp_path, ai={}).input_hashes() == {"data": "sha256:" + "a" * 64}


def test_ai_step_gets_selection_without_step_side_wiring(tmp_path):
    hashes = make_step(tmp_path).input_hashes()
    assert hashes["data"] == "sha256:" + "a" * 64
    assert hashes["ai_selection"].startswith("sha256:")


# G:指纹用最终有效值


def test_only_reasoning_effort_override_invalidates(tmp_path):
    base = selection_of(tmp_path / "a", job={"ai_overrides": {"test_step": "claude-cli"}})
    changed = selection_of(tmp_path / "b", job={
        "ai_overrides": {"test_step": "claude-cli"},
        "ai_param_overrides": {"test_step": {"reasoning_effort": "low"}},
    })
    assert base != changed


def test_only_model_override_invalidates(tmp_path):
    base = selection_of(tmp_path / "a", job={"ai_overrides": {"test_step": "claude-cli"}})
    changed = selection_of(tmp_path / "b", job={
        "ai_overrides": {"test_step": "claude-cli"},
        "ai_param_overrides": {"test_step": {"model": "claude-sonnet-4-6"}},
    })
    assert base != changed


def test_only_provider_override_invalidates(tmp_path):
    base = selection_of(tmp_path / "a")
    changed = selection_of(tmp_path / "b", job={"ai_overrides": {"test_step": "qoder-cli"}})
    assert base != changed


def test_provider_default_effort_change_does_not_invalidate(tmp_path):
    """provider 默认档位属部署配置,改它不追溯既有成功产物(docs/08-deployment.md 的契约)。
    把它放进输入指纹会让改一次 providers.yaml 失效全部存量 manifest,与该契约相反。
    有效档位仍进审计,只是不进指纹。"""
    other = json.loads(json.dumps(PROVIDERS))
    other["providers"]["claude-cli"]["reasoning_effort"] = "max"
    assert selection_of(tmp_path / "a") == selection_of(tmp_path / "b", providers=other)


def test_request_level_effort_override_invalidates(tmp_path):
    """单变量对照:两侧都覆盖同一个 provider, 只差 effort 覆盖有无, 摘要必须不同。
    这样才能单独证明是 effort 导致失效, 而不是换 provider 导致的。"""
    base_job = {"ai_overrides": {"test_step": "claude-cli"}}
    with_effort = {
        "ai_overrides": {"test_step": "claude-cli"},
        "ai_param_overrides": {"test_step": {"reasoning_effort": "low"}},
    }
    assert selection_of(tmp_path / "a", job=base_job) != selection_of(
        tmp_path / "b", job=with_effort,
    )


def test_provider_default_effort_change_with_provider_override_does_not_invalidate(tmp_path):
    """reviewer 复现:Job 覆盖了 provider 但没覆盖 effort 时, 档位仍来自 providers.yaml 默认。
    只改这个默认值不得让存量产物失效 —— job_owned 不能成为把默认档位拉进指纹的理由。"""
    job = {"ai_overrides": {"test_step": "claude-cli"}}
    other = json.loads(json.dumps(PROVIDERS))
    other["providers"]["claude-cli"]["reasoning_effort"] = "max"
    assert selection_of(tmp_path / "a", job=job) == selection_of(
        tmp_path / "b", job=job, providers=other,
    )


def test_provider_feature_removal_invalidates(tmp_path):
    other = json.loads(json.dumps(PROVIDERS))
    other["providers"]["claude-cli"]["features"] = ["read"]
    assert selection_of(tmp_path / "a") != selection_of(tmp_path / "b", providers=other)


def test_provider_default_model_change_invalidates_when_tier_declares_none(tmp_path):
    """tier 没写模型时真正生效的是 provider 配置里的模型,它变了必须重跑。"""
    tierless = {"primary": {"provider": "claude-cli"}}
    other = json.loads(json.dumps(PROVIDERS))
    other["providers"]["claude-cli"]["model"] = "claude-sonnet-4-6"
    assert (
        selection_of(tmp_path / "a", ai=tierless)
        != selection_of(tmp_path / "b", ai=tierless, providers=other)
    )


def test_claimed_provider_enters_fingerprint(tmp_path):
    """OR claim 物化后的真实 provider 必须进指纹,不同 worker 不可共用产物。"""
    on_claude = selection_of(tmp_path / "a", ai={
        "primary": {"provider": "claude-cli", "provider_source": "claim"},
    })
    on_qoder = selection_of(tmp_path / "b", ai={
        "primary": {"provider": "qoder-cli", "provider_source": "claim"},
    })
    assert on_claude != on_qoder


def test_claimed_provider_uses_its_configured_default_model(tmp_path):
    snapshot = selection_snapshot(
        providers_config=PROVIDERS,
        ai_config={"primary": {"provider": "qoder-cli", "provider_source": "claim"}},
    )
    assert snapshot["tiers"][0]["model"] == "ultimate"


# G:def_digest 与输入指纹的边界


def test_pipeline_declaration_change_stays_out_of_input_fingerprint(tmp_path):
    """声明的 provider 名与模型名由 def_digest 负责,不重复进输入指纹。

    两个 provider 的档位与能力配置取成一样,单独隔离出声明身份这一项:它变了只该动
    def_digest。provider 配置本身不同带来的差异仍然要进输入指纹,那是另外几条用例。
    """
    twins = json.loads(json.dumps(PROVIDERS))
    twins["providers"]["qoder-cli"] = dict(
        twins["providers"]["claude-cli"], type="qoder_cli", model="ultimate",
    )
    first = make_step(tmp_path / "a", providers=twins)
    second = make_step(
        tmp_path / "b", providers=twins,
        ai={"primary": {"provider": "qoder-cli", "model": "another-model"}},
    )
    assert first.input_hashes()["ai_selection"] == second.input_hashes()["ai_selection"]
    assert first._def_digest() != second._def_digest()


def test_job_override_stays_out_of_def_digest(tmp_path):
    plain = make_step(tmp_path / "a")
    overridden = make_step(tmp_path / "b", job={"ai_overrides": {"test_step": "qoder-cli"}})
    assert plain._def_digest() == overridden._def_digest()
    assert plain.input_hashes()["ai_selection"] != overridden.input_hashes()["ai_selection"]


# G:恢复与断点续跑


def test_resume_reruns_only_when_effective_selection_changed(tmp_path):
    """rerun API 会主动删 manifest,但恢复和断点续跑不会,幂等判断必须自己正确。"""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    step = make_step(job_dir)
    step.mark_done()
    assert make_step(job_dir).should_run() is False

    (job_dir / "job.json").write_text(json.dumps({
        "ai_overrides": {"test_step": "claude-cli"},
        "ai_param_overrides": {"test_step": {"reasoning_effort": "low"}},
    }), encoding="utf-8")
    assert make_step(job_dir).should_run() is True


# G:各内容链的真实 AI 步骤


def _document_job(root):
    job = root / "job"
    (job / "intermediate").mkdir(parents=True)
    (job / "output").mkdir()
    (job / "intermediate" / "document.json").write_text("{}", encoding="utf-8")
    (job / "intermediate" / "quality.json").write_text("{}", encoding="utf-8")
    (job / "intermediate" / "source_segments.json").write_text("{}", encoding="utf-8")
    return job


def _audio_job(root):
    job = root / "job"
    (job / "intermediate").mkdir(parents=True)
    (job / "output").mkdir()
    (job / "intermediate" / "transcript.json").write_text("{}", encoding="utf-8")
    return job


def _video_job(root):
    job = root / "job"
    (job / "output").mkdir(parents=True)
    (job / "intermediate").mkdir()
    (job / "output" / "notes_mechanical.md").write_text("## 章\n正文\n", encoding="utf-8")
    return job


def _real_steps():
    from steps.audio.step_04_smart_podcast import SmartPodcastStep
    from steps.audio.step_05_review import PodcastReviewStep
    from steps.document.step_05_smart import DocumentSmartStep
    from steps.document.step_08_review import DocumentReviewStep
    from steps.video.step_11_smart import SmartStep
    from steps.video.step_12_review import ReviewStep
    from steps.video.step_evidence import EvidenceStep

    return [
        ("document_smart", DocumentSmartStep, "05_smart", "05_smart_document", _document_job),
        ("document_review", DocumentReviewStep, "08_review", None, _document_job),
        ("audio_smart", SmartPodcastStep, "04_smart_podcast", None, _audio_job),
        ("audio_review", PodcastReviewStep, "05_review", None, _audio_job),
        ("video_smart", SmartStep, "11_smart", None, _video_job),
        ("video_review", ReviewStep, "12_review", None, _video_job),
        ("video_evidence", EvidenceStep, "10_evidence", None, _video_job),
    ]


@pytest.mark.parametrize(
    "label,cls,step_name,template,build_job",
    _real_steps(),
    ids=[case[0] for case in _real_steps()],
)
def test_real_ai_steps_carry_effective_selection(
    tmp_path, label, cls, step_name, template, build_job,
):
    def hashes(root, job_document=None):
        job_dir = build_job(root)
        if job_document is not None:
            (job_dir / "job.json").write_text(json.dumps(job_document), encoding="utf-8")
        step_node = {"name": step_name, "version": "1"}
        if template:
            step_node["prompt_template"] = template
        step = cls(step_name, job_dir, {
            "step": step_node,
            "ai": DECLARED_AI,
            "providers": PROVIDERS,
            "paths": {"prompts_dir": str(root / "prompts"),
                      "config_dir": str(Path("configs").resolve())},
            "domain": {"name": "general"},
            "style_tags": [],
        })
        return step.input_hashes()

    plain = hashes(tmp_path / "plain")
    assert plain["ai_selection"].startswith("sha256:")
    for override in (
        {"ai_overrides": {step_name: "qoder-cli"}},
        {"ai_overrides": {step_name: "claude-cli"},
         "ai_param_overrides": {step_name: {"model": "claude-sonnet-4-6"}}},
        {"ai_overrides": {step_name: "claude-cli"},
         "ai_param_overrides": {step_name: {"reasoning_effort": "low"}}},
    ):
        changed = hashes(tmp_path / f"o{abs(hash(json.dumps(override, sort_keys=True)))}", override)
        assert changed["ai_selection"] != plain["ai_selection"], override


# H:有效档位与解析后的 provider 进审计


def test_snapshot_reports_provider_default_effort_instead_of_none():
    """provider 默认档位生效时,审计不能只留 None,否则分不清没设过和设成了这个值。"""
    snapshot = selection_snapshot(providers_config=PROVIDERS, ai_config=DECLARED_AI)
    tier = snapshot["tiers"][0]
    assert tier["reasoning_effort"] == "xhigh"
    assert tier["reasoning_effort_source"] == EFFORT_FROM_PROVIDER


def test_snapshot_marks_unset_effort_apart_from_configured_one():
    providers = {"providers": {"openai": {"type": "openai"}}}
    snapshot = selection_snapshot(
        providers_config=providers,
        ai_config={"primary": {"provider": "openai", "model": "gpt-test"}},
    )
    tier = snapshot["tiers"][0]
    assert tier["reasoning_effort"] is None
    assert tier["reasoning_effort_source"] == EFFORT_UNSET


def test_snapshot_request_effort_wins_over_provider_default():
    snapshot = selection_snapshot(
        providers_config=PROVIDERS, ai_config=DECLARED_AI,
        override_provider="claude-cli", override_params={"reasoning_effort": "low"},
    )
    tier = snapshot["tiers"][0]
    assert (tier["reasoning_effort"], tier["reasoning_effort_source"]) == (
        "low", EFFORT_FROM_REQUEST,
    )


def test_snapshot_marks_claimed_concrete_provider():
    snapshot = selection_snapshot(
        providers_config=PROVIDERS,
        ai_config={"primary": {"provider": "qoder-cli", "provider_source": "claim"}},
    )
    tier = snapshot["tiers"][0]
    assert tier["declared_provider"] == "qoder-cli"
    assert tier["provider"] == "qoder-cli"
    assert tier["provider_source"] == "claim"
    assert tier["model"] == "ultimate"
    assert tier["reasoning_effort"] == "max"
    assert fingerprint_projection(snapshot)["tiers"][0]["provider"] == "qoder-cli"


def test_selection_key_passes_manifest_fingerprint_validation(tmp_path):
    """指纹要经 manifest 校验(有界、str->str、无密钥样式),新键必须能过全链路。"""
    from shared.step_manifest import compute_input_digest

    hashes = make_step(tmp_path).input_hashes()
    assert compute_input_digest(hashes).startswith("sha256:")
