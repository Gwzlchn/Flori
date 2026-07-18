"""语义定义摘要测试:纳入/排除清单、未知字段 fail-closed、AI/CPU 同规则。"""

import pytest

from shared.step_semantic_definition import (
    RUNTIME_STEP_KEYS,
    SEMANTIC_STEP_KEYS,
    SemanticDefinitionError,
    build_step_semantic_definition,
    step_semantic_definition_digest,
)


HEX_A = "sha256:" + "a" * 64
HEX_B = "sha256:" + "b" * 64


def base_step() -> dict:
    """按 shared.config 归一化后的 08_punctuate 形状(字段名已过别名映射)。"""
    return {
        "name": "08_punctuate",
        "label": "口播稿",
        "scope": "part",
        "module": "steps.video.step_08_punctuate",
        "pool": "ai",
        "timeout_sec": 1800,
        "retries": 5,
        "tags": [],
        "image": "flori/step-base",
        "depends_on": ["01_download", "02_whisper", "06_ocr"],
        "fan_in": [],
        "rules": [{"exists": "input/*.srt", "when": "on"}],
        "condition": "has_subtitle",
        "version": "4",
        "ai": {
            "primary": {"provider": "claude-cli", "model": "model-a"},
            "fallback": {"provider": "claude-cli", "model": "model-a"},
        },
        "outputs": ["output/transcript.md", "intermediate/source_segments.json"],
        "on_complete": [{"action": "sync_metadata"}],
    }


def digest_of(step: dict, **kwargs) -> str:
    return step_semantic_definition_digest(pipeline="video", step_config=step, **kwargs)


def test_declared_key_sets_are_disjoint() -> None:
    assert not (SEMANTIC_STEP_KEYS & RUNTIME_STEP_KEYS)


def test_build_includes_semantic_and_drops_runtime_fields() -> None:
    result = build_step_semantic_definition(pipeline="video", step_config=base_step())
    assert result["format"] == "flori-step-semantic-definition"
    assert result["pipeline"] == "video"
    assert result["step"] == "08_punctuate"
    definition = result["definition"]
    assert definition["module"] == "steps.video.step_08_punctuate"
    assert definition["version"] == "4"
    assert definition["scope"] == "part"
    assert definition["outputs"] == base_step()["outputs"]
    assert definition["condition"] == "has_subtitle"
    # 排除清单字段不得以任何形式漏进语义定义。
    for runtime_key in ("pool", "timeout_sec", "retries", "label", "tags", "image", "on_complete"):
        assert runtime_key not in definition
    assert "口播稿" not in str(result)
    assert "1800" not in str(result)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("pool", "cpu"),
        ("timeout_sec", 30),
        ("timeout_per_min", 12),
        ("timeout_max_sec", 7200),
        ("retries", 0),
        ("label", "改名"),
        ("tags", ["vision"]),
        ("image", "flori/step-heavy"),
        ("on_complete", []),
        ("weight", 3),
        ("concurrency", 2),
    ],
)
def test_runtime_changes_keep_digest_stable(key: str, value) -> None:
    baseline = digest_of(base_step())
    changed = base_step()
    changed[key] = value
    assert digest_of(changed) == baseline


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("version", "5"),
        ("module", "steps.video.step_08_punctuate_v2"),
        ("scope", "job"),
        ("outputs", ["output/transcript.md"]),
        ("depends_on", ["01_download"]),
        ("fan_in", ["07_danmaku"]),
        ("rules", [{"exists": "input/*.ass", "when": "on"}]),
        ("condition", "has_danmaku"),
        ("ai", {"primary": {"provider": "claude-cli", "model": "model-b"}}),
        ("prompt_template", "05_concepts"),
        ("prompt_locked", True),
        ("output_policy", {"allow_empty": False}),
        ("capability_rules", {"vision": ["frames_exist"]}),
        ("toolchain", {"ffmpeg": "7.1"}),
    ],
)
def test_semantic_changes_move_digest(key: str, value) -> None:
    baseline = digest_of(base_step())
    changed = base_step()
    changed[key] = value
    assert digest_of(changed) != baseline


def test_ai_and_cpu_use_the_same_helper_rules() -> None:
    # CPU 步(无 ai 块)与 AI 步走同一函数:改 version 同样失效,改 pool 同样不失效。
    cpu_step = base_step()
    cpu_step.pop("ai")
    baseline = digest_of(cpu_step)
    bumped = dict(cpu_step, version="5")
    assert digest_of(bumped) != baseline
    repooled = dict(cpu_step, pool="io")
    assert digest_of(repooled) == baseline


def test_resolved_context_moves_digest() -> None:
    baseline = digest_of(base_step())
    prompt = {"template": "08_punctuate", "version": "3", "sha256": HEX_A}
    with_prompt = digest_of(base_step(), prompt=prompt)
    assert with_prompt != baseline
    changed_prompt = {"template": "08_punctuate", "version": "3", "sha256": HEX_B}
    assert digest_of(base_step(), prompt=changed_prompt) != with_prompt
    assert digest_of(base_step(), config_digests={"domain": HEX_A}) != baseline
    assert digest_of(base_step(), toolchain={"whisper": "1.0"}) != baseline


def test_unclassified_key_fail_closed() -> None:
    step = base_step()
    step["shiny_new_knob"] = True
    with pytest.raises(SemanticDefinitionError, match="shiny_new_knob"):
        build_step_semantic_definition(pipeline="video", step_config=step)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda step: step["ai"]["primary"].update(api_key="x"),
        lambda step: step["ai"]["primary"].update(model="sk-" + "a" * 24),
        lambda step: step.update(outputs=["/data/escape.md"]),
        lambda step: step.update(prompt_locked="yes"),
        lambda step: step["ai"]["primary"].update(model="m\ud800"),
        lambda step: step.update(condition="c\ud800"),
    ],
    ids=[
        "secret_key_name", "secret_value", "absolute_path",
        "prompt_locked_not_bool", "surrogate_in_ai", "surrogate_in_condition",
    ],
)
def test_semantic_payload_fail_closed(mutate) -> None:
    step = base_step()
    mutate(step)
    with pytest.raises(SemanticDefinitionError):
        build_step_semantic_definition(pipeline="video", step_config=step)


@pytest.mark.parametrize("raw_key", ["run", "needs", "timeout", "retry"])
def test_raw_alias_keys_rejected(raw_key: str) -> None:
    # raw/normalized 双形态会分裂出两个摘要;只接受归一化模板 config。
    step = base_step()
    step[raw_key] = "anything"
    with pytest.raises(SemanticDefinitionError, match="normalized template"):
        build_step_semantic_definition(pipeline="video", step_config=step)


def _expand_part(template: dict, part_id: str, part_index: int) -> dict:
    """镜像 shared.pipeline_scope.expand_pipeline_steps 的 part 展开形状。"""
    scope_key = f"part:{part_id}"
    cfg = dict(template)
    cfg.update(
        name=f"{scope_key}::{template['name']}",
        template_step=template["name"],
        scope_key=scope_key,
        part_id=part_id,
        part_index=part_index,
        depends_on=[f"{scope_key}::{dep}" for dep in template["depends_on"]],
    )
    return cfg


def part_template() -> dict:
    return {
        "name": "01_download",
        "label": "下载",
        "scope": "part",
        "module": "steps.common.step_01_download",
        "pool": "io",
        "timeout_sec": 2400,
        "retries": 3,
        "image": "flori/step-base",
        "depends_on": ["00_probe"],
        "fan_in": [],
        "outputs": ["input/source.mp4", "input/metadata.json"],
        "version": "1",
    }


def test_expanded_part_nodes_and_template_share_digest() -> None:
    # 同一模板的每个 Part 节点与模板本身必须同摘要,否则复用判定按 Part 分裂。
    template = part_template()
    part1 = _expand_part(template, "pt_a1", 1)
    part2 = _expand_part(template, "pt_b2", 2)
    assert digest_of(part1) == digest_of(template)
    assert digest_of(part2) == digest_of(template)
    built = build_step_semantic_definition(pipeline="video", step_config=part1)
    assert built["step"] == "01_download"
    assert built["definition"]["depends_on"] == ["00_probe"]


def test_expanded_job_reduce_with_fan_in_matches_template() -> None:
    # job 步 fan-in 按 Part 数展开进 depends_on;语义摘要必须还原,不随 Part 数抖动。
    template = {
        "name": "09_merge_parts",
        "scope": "job",
        "module": "steps.video.step_09_merge_parts",
        "pool": "io",
        "timeout_sec": 120,
        "retries": 1,
        "image": "flori/step-base",
        "depends_on": [],
        "fan_in": ["07_danmaku", "08_punctuate"],
        "outputs": ["output/transcript.md"],
        "version": "1",
    }
    expanded = dict(template)
    expanded.update(
        template_step="09_merge_parts",
        scope_key="job",
        depends_on=[
            "part:pt_a1::07_danmaku", "part:pt_b2::07_danmaku",
            "part:pt_a1::08_punctuate", "part:pt_b2::08_punctuate",
        ],
    )
    assert digest_of(expanded) == digest_of(template)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda cfg: cfg.update(name="01_download"),
        lambda cfg: cfg.pop("scope_key"),
        lambda cfg: cfg.update(part_id="pt_zz"),
        lambda cfg: cfg.update(part_index=0),
    ],
    ids=["name_not_execution_key", "missing_scope_key", "part_id_mismatch", "part_index_zero"],
)
def test_expansion_identity_fail_closed(mutate) -> None:
    cfg = _expand_part(part_template(), "pt_a1", 1)
    mutate(cfg)
    with pytest.raises(SemanticDefinitionError):
        build_step_semantic_definition(pipeline="video", step_config=cfg)


def test_job_expansion_must_not_carry_part_identity() -> None:
    cfg = dict(part_template(), scope="job", depends_on=[])
    cfg.update(name="01_download", template_step="01_download", scope_key="job", part_id="pt_a1")
    with pytest.raises(SemanticDefinitionError, match="part identity"):
        build_step_semantic_definition(pipeline="video", step_config=cfg)


def test_version_normalization_matches_existing_semantics() -> None:
    as_int = dict(base_step(), version=4)
    as_str = dict(base_step(), version="4")
    assert digest_of(as_int) == digest_of(as_str)
    missing = base_step()
    missing.pop("version")
    defaulted = dict(base_step(), version="1")
    assert digest_of(missing) == digest_of(defaulted)


def test_digest_deterministic_across_key_order() -> None:
    forward = base_step()
    shuffled = {key: forward[key] for key in reversed(list(forward))}
    assert digest_of(forward) == digest_of(shuffled)
    assert digest_of(forward) == digest_of(base_step())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": {"template": "x"}},
        {"prompt": {"template": "x", "version": "1", "sha256": "bad"}},
        {"prompt": {"template": "", "version": "1", "sha256": HEX_A}},
        {"config_digests": {"domain": "not-a-digest"}},
        {"toolchain": {"ffmpeg": ""}},
    ],
)
def test_resolved_context_validation_fail_closed(kwargs) -> None:
    with pytest.raises(SemanticDefinitionError):
        build_step_semantic_definition(pipeline="video", step_config=base_step(), **kwargs)


def test_toolchain_conflict_between_config_and_declared() -> None:
    step = dict(base_step(), toolchain={"ffmpeg": "7.1"})
    with pytest.raises(SemanticDefinitionError, match="conflicting"):
        build_step_semantic_definition(
            pipeline="video", step_config=step, toolchain={"ffmpeg": "8.0"},
        )
    merged = build_step_semantic_definition(
        pipeline="video", step_config=step, toolchain={"whisper": "1.0"},
    )
    assert merged["toolchain"] == {"ffmpeg": "7.1", "whisper": "1.0"}
