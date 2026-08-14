"""GitLab-CI 风格流水线归一化:extends / variables / rules / needs / image 保留。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from shared.ai_routing import (
    AI_ROUTE_ALLOWED_PROVIDERS_REQUIRED,
    CONCRETE_CLI_PROVIDERS,
)
from shared.config import (
    load_yaml,
    load_pipelines,
    normalize_pipelines,
    validate_ai_pipeline_contract,
)


# extends:继承 + 覆盖(按键深合并)


class TestExtends:
    def test_inherits_template_fields(self):
        raw = {
            ".cpu-step": {"pool": "cpu", "timeout": 120, "retry": 1},
            "p": {"jobs": {"A": {"extends": ".cpu-step", "run": "m.a"}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["pool"] == "cpu"
        assert s["timeout_sec"] == 120
        assert s["retries"] == 1
        assert s["module"] == "m.a"

    def test_child_overrides_template(self):
        raw = {
            ".cpu-step": {"pool": "cpu", "timeout": 120, "retry": 1},
            "p": {"jobs": {"A": {"extends": ".cpu-step", "run": "m.a", "timeout": 1800}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["timeout_sec"] == 1800   # 子覆盖模板
        assert s["retries"] == 1          # 未覆盖的继承

    def test_deep_merge_nested_block(self):
        raw = {
            ".cpu-step": {"pool": "cpu", "settings": {
                "primary": {"provider": "a", "model": "x"},
                "fallback": {"provider": "b", "model": "y"},
            }},
            "p": {"jobs": {"A": {"extends": ".cpu-step", "run": "m.a",
                                 "settings": {"primary": {"model": "z"}}}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        # 深合并:primary.model 被覆盖,primary.provider 与 fallback 保留。
        assert s["settings"]["primary"] == {"provider": "a", "model": "z"}
        assert s["settings"]["fallback"] == {"provider": "b", "model": "y"}

    def test_multi_level_extends(self):
        raw = {
            ".ai-step": {
                "pool": "ai", "timeout": 600, "retry": 2,
                "ai": {"allowed_providers": ["claude-cli"]},
            },
            ".review": {"extends": ".ai-step", "timeout": 1800},
            "p": {"jobs": {"A": {"extends": ".review", "run": "m.a"}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["pool"] == "ai"      # 来自 .ai-step
        assert s["timeout_sec"] == 1800  # 被 .review 覆盖
        assert s["retries"] == 2      # 来自 .ai-step

    def test_default_applies_under_extends(self):
        raw = {
            "default": {"image": "flori/step-base", "timeout": 600, "retry": 0},
            ".cpu-step": {"pool": "cpu", "timeout": 120},
            "p": {"jobs": {"A": {"extends": ".cpu-step", "run": "m.a"}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["image"] == "flori/step-base"  # default
        assert s["timeout_sec"] == 120          # 模板覆盖 default
        assert s["retries"] == 0                # default

    def test_unknown_extends_raises(self):
        raw = {"p": {"jobs": {"A": {"extends": ".missing", "run": "m.a"}}}}
        with pytest.raises(ValueError):
            normalize_pipelines(raw)


# variables:覆盖(06_ocr 单一事实源,无 prod/integration 漂移)


class TestVariables:
    def test_var_substitution(self):
        raw = {
            "p": {
                "variables": {"T": 1800, "R": 1},
                "jobs": {"A": {"run": "m.a", "pool": "cpu", "timeout": "$T", "retry": "$R"}},
            }
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["timeout_sec"] == 1800 and isinstance(s["timeout_sec"], int)
        assert s["retries"] == 1 and isinstance(s["retries"], int)

    def test_var_in_ai_allowed_providers(self):
        raw = {
            "p": {
                "variables": {"PROV": "qoder-cli"},
                "jobs": {"A": {"run": "m.a", "pool": "ai",
                               "ai": {"allowed_providers": ["$PROV"]}}},
            }
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["ai"] == {"allowed_providers": ["qoder-cli"]}

    def test_pipeline_var_overrides_global(self):
        raw = {
            "variables": {"PROV": "claude-cli"},
            "p": {"variables": {"PROV": "codex-cli"},
                  "jobs": {"A": {"run": "m.a", "pool": "ai",
                                 "ai": {"allowed_providers": ["$PROV"]}}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["ai"]["allowed_providers"] == ["codex-cli"]

    def test_ocr_timeout_single_source_no_drift(self, configs_dir):
        """06_ocr 的 timeout/retry 只在 variables 定义一次;prod 与 integration 覆盖
        共享同一结构,两侧超时必然一致,漂移无从发生。"""
        prod = load_pipelines(configs_dir / "pipelines.yaml")
        ocr = next(s for s in prod["video"]["steps"] if s["name"] == "06_ocr")
        assert ocr["timeout_sec"] == 1800
        assert ocr["retries"] == 1

        # integration 是一份 variables 覆盖(仅换 provider),结构复用 prod;
        # 06_ocr 不各写一份 → 两侧 timeout/retry 必然一致,漂移不可能发生。
        raw = {
            "default": {"image": "flori/step-base", "timeout": 600, "retry": 0},
            ".cpu-step": {"pool": "cpu", "timeout": 120, "retry": 1},
            "video": {
                "variables": {"OCR_TIMEOUT": 1800, "OCR_RETRIES": 1,
                              "PROV": "claude-cli"},
                "jobs": {
                    "06_ocr": {"extends": ".cpu-step", "run": "steps.video.step_06_ocr",
                               "image": "flori/step-heavy", "needs": ["05_dedup"],
                               "timeout": "$OCR_TIMEOUT", "retry": "$OCR_RETRIES"},
                    "11_smart": {"run": "m.s", "pool": "ai",
                                 "ai": {"allowed_providers": ["$PROV"]}},
                },
            },
        }
        prod_norm = normalize_pipelines(raw)
        # integration overlay:仅覆盖 PROV,OCR_* 不重写。
        raw_int = {
            **{k: raw[k] for k in (".cpu-step", "default")},
            "video": {**raw["video"],
                      "variables": {**raw["video"]["variables"], "PROV": "codex-cli"}},
        }
        int_norm = normalize_pipelines(raw_int)

        prod_ocr = next(s for s in prod_norm["video"]["steps"] if s["name"] == "06_ocr")
        int_ocr = next(s for s in int_norm["video"]["steps"] if s["name"] == "06_ocr")
        assert prod_ocr["timeout_sec"] == int_ocr["timeout_sec"] == 1800
        assert prod_ocr["retries"] == int_ocr["retries"] == 1
        # provider 是两侧唯一差异。
        prod_smart = next(s for s in prod_norm["video"]["steps"] if s["name"] == "11_smart")
        int_smart = next(s for s in int_norm["video"]["steps"] if s["name"] == "11_smart")
        assert prod_smart["ai"]["allowed_providers"] == ["claude-cli"]
        assert int_smart["ai"]["allowed_providers"] == ["codex-cli"]

    def test_video_manifest_producer_waits_for_ocr(self, configs_dir):
        pipeline = load_pipelines(configs_dir / "pipelines.yaml")["video"]
        ocr = next(
            step for step in pipeline["steps"] if step["name"] == "06_ocr"
        )
        punctuate = next(
            step for step in pipeline["steps"] if step["name"] == "08_punctuate"
        )
        assert ocr["version"] == "2"
        assert punctuate["depends_on"] == ["01_download", "02_whisper", "06_ocr"]
        assert punctuate["version"] == "4"

    def test_provenance_writers_invalidate_existing_done_markers(self, configs_dir):
        pipelines = load_pipelines(configs_dir / "pipelines.yaml")
        expected = {
            "video": {"08_punctuate": "4", "11_smart": "7"},
            "document": {"02_parse": "4", "04_translate": "1", "05_smart": "11"},
            "audio": {"03_transcript_parse": "3", "04_smart_podcast": "5"},
        }
        for pipeline, versions in expected.items():
            actual = {step["name"]: step["version"] for step in pipelines[pipeline]["steps"]}
            assert {name: actual[name] for name in versions} == versions


class TestSemanticAttestationPipeline:
    def test_standalone_attestors_remain_between_producer_and_concepts(self, configs_dir):
        raw = load_yaml(configs_dir / "pipelines.yaml")
        producers = {
            "video": {"11_smart": "smart"},
            "audio": {"04_smart_podcast": "smart"},
        }
        attestors = {
            "video": "11_semantic_attestation",
            "audio": "04_semantic_attestation",
        }
        attestor_versions = {"video": "5", "audio": "5"}
        concepts = {"video": "12_concepts", "audio": "05_concepts"}
        concept_versions = {"video": "7", "audio": "7"}
        for pipeline, steps in producers.items():
            jobs = raw[pipeline]["jobs"]

            def depends_on(step: str, target: str) -> bool:
                pending = list(jobs[step].get("needs", []))
                seen = set()
                while pending:
                    current = pending.pop()
                    if current == target:
                        return True
                    if current not in seen:
                        seen.add(current)
                        pending.extend(jobs[current].get("needs", []))
                return False

            for producer, note_type in steps.items():
                assert (
                    f"output/provenance_candidates/{note_type}.json"
                    in jobs[producer]["outputs"]
                )
                assert depends_on(attestors[pipeline], producer)

            attestor = jobs[attestors[pipeline]]
            assert attestor["version"] == attestor_versions[pipeline]
            assert any(path.startswith("output/provenance/") for path in attestor["outputs"])
            assert attestor["timeout"] == 900
            assert depends_on(concepts[pipeline], attestors[pipeline])
            concept = jobs[concepts[pipeline]]
            assert concept["version"] == concept_versions[pipeline]
            assert concept["timeout"] == 3900
            index_actions = [
                action for action in jobs[concepts[pipeline]].get("on_complete", [])
                if action.get("action") == "index_note"
            ]
            for action in index_actions:
                assert all(
                    "provenance_candidates" not in candidate["provenance"]
                    for candidate in action["candidates"]
                )

    def test_document_translation_is_parallel_and_review_owns_smart_attestation(
        self, configs_dir,
    ):
        jobs = load_yaml(configs_dir / "pipelines.yaml")["document"]["jobs"]
        assert jobs["01_download"]["version"] == "6"
        assert "input/html_snapshot.json" in jobs["01_download"]["outputs"]
        assert "input/html_assets/*" in jobs["01_download"]["outputs"]
        assert "assets/*" not in jobs["01_download"]["outputs"]
        assert "06_semantic_attestation" not in jobs
        assert jobs["04_translate"]["needs"] == ["03_structure"]
        assert jobs["04_translate"]["allow_failure"] is True
        assert jobs["05_smart"]["needs"] == ["03_structure"]
        assert jobs["05_smart"]["timeout"] == 21600
        assert jobs["05_smart"]["version"] == "11"
        assert jobs["05_smart"]["tags"] == ["vision"]
        assert "output/smart_pipeline/*" in jobs["05_smart"]["outputs"]
        assert jobs["07_concepts"]["needs"] == ["05_smart"]
        assert jobs["07_concepts"]["timeout"] == 3900
        assert jobs["07_concepts"]["version"] == "5"
        assert jobs["08_review"]["needs"] == ["07_concepts"]
        assert jobs["08_review"]["timeout"] == 5700
        assert jobs["08_review"]["version"] == "8"
        assert jobs["09_publish"]["needs"] == ["08_review"]
        assert jobs["09_publish"]["version"] == "2"
        assert "output/provenance_exact/smart.json" in jobs["05_smart"]["outputs"]
        assert "output/provenance/smart.json" not in jobs["05_smart"]["outputs"]
        assert "output/provenance/smart.json" in jobs["08_review"]["outputs"]
        assert "output/provenance/translated.json" not in jobs["08_review"]["outputs"]
        assert "output_policy" not in jobs["08_review"]
        assert not jobs["07_concepts"].get("on_complete")
        assert [
            action["action"] for action in jobs["09_publish"]["on_complete"]
        ] == ["index_note", "collect_glossary"]
        assert jobs["09_publish"]["effects_require_current_manifest"] is True
        assert jobs["09_publish"]["effects_required_outputs"] == [
            "output/publication.json",
        ]
        assert not any(
            "04_translate" in step.get("needs", [])
            for step in jobs.values()
        )

    @pytest.mark.parametrize("value", ["true", 1, None, []])
    def test_allow_failure_requires_boolean(self, value):
        raw = {"p": {"jobs": {
            "optional": {
                "run": "m.optional", "pool": "cpu", "allow_failure": value,
            },
        }}}
        with pytest.raises(ValueError, match="allow_failure must be boolean"):
            normalize_pipelines(raw)

    def test_allow_failure_step_cannot_be_dependency(self):
        raw = {"p": {"jobs": {
            "optional": {
                "run": "m.optional", "pool": "cpu", "allow_failure": True,
            },
            "consumer": {
                "run": "m.consumer", "pool": "cpu", "needs": ["optional"],
            },
        }}}
        with pytest.raises(ValueError, match="cannot be a dependency"):
            normalize_pipelines(raw)

    @pytest.mark.parametrize("value", ["true", 1, None, []])
    def test_completion_effect_manifest_gate_requires_boolean(self, value):
        raw = {"p": {"jobs": {"publish": {
            "run": "m.publish", "pool": "cpu",
            "outputs": ["output/publication.json"],
            "on_complete": [{"action": "collect_glossary"}],
            "effects_require_current_manifest": value,
            "effects_required_outputs": ["output/publication.json"],
        }}}}
        with pytest.raises(ValueError, match="must be boolean"):
            normalize_pipelines(raw)

    @pytest.mark.parametrize(
        ("required", "message"),
        [
            (None, "non-empty list"),
            (["output/*.json"], "exact paths"),
            (["output/undeclared.json"], "not declared"),
        ],
    )
    def test_completion_effect_manifest_gate_requires_declared_exact_output(
        self, required, message,
    ):
        raw = {"p": {"jobs": {"publish": {
            "run": "m.publish", "pool": "cpu",
            "outputs": ["output/publication.json"],
            "on_complete": [{"action": "collect_glossary"}],
            "effects_require_current_manifest": True,
            "effects_required_outputs": required,
        }}}}
        with pytest.raises(ValueError, match=message):
            normalize_pipelines(raw)


class TestAIRoleContract:
    def test_real_config_has_no_virtual_role_variables_and_14_routes(self, configs_dir):
        raw = load_yaml(configs_dir / "pipelines.yaml")
        assert not any(
            key.startswith("AI_") for key in (raw.get("variables") or {})
        )
        for pipeline in ("video", "document", "audio"):
            assert not any(
                key.startswith("AI_")
                for key in (raw[pipeline].get("variables") or {})
            )

        pipelines = load_pipelines(configs_dir / "pipelines.yaml")
        routes = {
            (pipeline, step["name"]): step["ai"]
            for pipeline, body in pipelines.items()
            for step in body["steps"]
            if step.get("pool") == "ai"
        }
        assert set(routes) == {
            ("video", "08_punctuate"), ("video", "10_evidence"),
            ("video", "11_smart"), ("video", "12_concepts"),
            ("video", "11_semantic_attestation"),
            ("video", "12_review"),
            ("document", "04_translate"), ("document", "05_smart"),
            ("document", "07_concepts"), ("document", "08_review"),
            ("audio", "04_smart_podcast"), ("audio", "05_concepts"),
            ("audio", "05_review"),
            ("audio", "04_semantic_attestation"),
        }
        allowed_route = {"allowed_providers": list(CONCRETE_CLI_PROVIDERS)}
        for key, route in routes.items():
            assert route == allowed_route, key
        assert sum(len(route) for route in routes.values()) == len(routes)
        semantic_steps = {
            (pipeline, step["name"]): step
            for pipeline, body in pipelines.items()
            for step in body["steps"]
            if step["name"] in {"11_semantic_attestation", "04_semantic_attestation"}
        }
        assert {
            key: value["version"] for key, value in semantic_steps.items()
        } == {
            ("video", "11_semantic_attestation"): "5",
            ("audio", "04_semantic_attestation"): "5",
        }
        for (_pipeline, step_name), step in semantic_steps.items():
            assert (
                f"output/ai_logs/{step_name}.semantic.*.jsonl" in step["outputs"]
            )
        validate_ai_pipeline_contract(
            pipelines, load_yaml(configs_dir / "providers.yaml"),
        )
        review_steps = {
            (pipeline, step["name"]): (step["timeout_sec"], step["version"])
            for pipeline, body in pipelines.items()
            for step in body["steps"]
            if step["name"] in {"05_review", "08_review", "12_review"}
        }
        assert review_steps == {
            ("video", "12_review"): (3900, "3"),
            ("document", "08_review"): (5700, "8"),
            ("audio", "05_review"): (3900, "3"),
        }
        video_smart = next(
            step for step in pipelines["video"]["steps"]
            if step["name"] == "11_smart"
        )
        assert video_smart["timeout_sec"] == 3900
        evidence_step = next(
            step for step in pipelines["video"]["steps"]
            if step["name"] == "10_evidence"
        )
        assert "websearch" in (evidence_step.get("tags") or [])

    def test_shared_ai_variables_reject_undefined_unused_and_empty(self):
        base = {
            "variables": {"AI_PROVIDER": "claude-cli"},
            "p": {"jobs": {"A": {
                "run": "m.a", "pool": "ai",
                "ai": {"allowed_providers": ["$AI_PROVIDER"]},
            }}},
        }
        assert normalize_pipelines(base)["p"]["steps"][0]["ai"] == {
            "allowed_providers": ["claude-cli"],
        }

        unused = {**base, "variables": {**base["variables"], "AI_UNUSED_PROVIDER": "x"}}
        with pytest.raises(ValueError, match="unused"):
            normalize_pipelines(unused)

        undefined = {
            **base,
            "p": {"jobs": {"A": {
                "run": "m.a", "pool": "ai",
                "ai": {"allowed_providers": ["$AI_MISSING_PROVIDER"]},
            }}},
        }
        with pytest.raises(ValueError, match="undefined"):
            normalize_pipelines(undefined)

        empty = {**base, "variables": {**base["variables"], "AI_PROVIDER": ""}}
        with pytest.raises(ValueError, match="non-empty"):
            normalize_pipelines(empty)

    @pytest.mark.parametrize("ai", [
        {"primary": {"provider": "claude-cli", "model": "claude-opus-5"}},
        {"fallback": {"provider": "codex-cli", "model": "gpt-5.6-sol"}},
        {"text_fallback": {"provider": "qoder-cli", "model": "ultimate"}},
        {
            "allowed_providers": ["claude-cli"],
            "primary": {"provider": "claude-cli", "model": "claude-opus-5"},
        },
    ])
    def test_ai_routes_reject_legacy_tiers_with_stable_code(self, ai):
        pipelines = {"p": {"steps": [{
            "name": "A", "pool": "ai", "ai": ai,
        }]}}

        with pytest.raises(ValueError, match=AI_ROUTE_ALLOWED_PROVIDERS_REQUIRED):
            validate_ai_pipeline_contract(pipelines)

    def test_provider_defaults_outside_own_domain_fail_at_load(self):
        pipelines = {"p": {"steps": [{
            "name": "A", "pool": "ai",
            "ai": {"allowed_providers": ["qoder-cli"]},
        }]}}
        providers = {"providers": {"qoder-cli": {
            "type": "qoder_cli", "model": "ultimate", "models": ["ultimate"],
            "reasoning_effort": "turbo",
            "reasoning_efforts": ["low", "high", "max"],
        }}}

        with pytest.raises(ValueError, match="reasoning_effort_not_in_provider_domain"):
            validate_ai_pipeline_contract(pipelines, providers)

    def test_allowed_provider_must_exist_in_loaded_config(self):
        providers = {"providers": {"claude-cli": {
            "type": "claude_cli", "model": "claude-opus-5", "models": ["claude-opus-5"],
            "reasoning_effort": "xhigh", "reasoning_efforts": ["xhigh"],
        }}}
        pipelines = {"p": {"steps": [{
            "name": "A", "pool": "ai",
            "ai": {"allowed_providers": ["codex-cli"]},
        }]}}

        with pytest.raises(ValueError, match="unknown AI provider: codex-cli"):
            validate_ai_pipeline_contract(pipelines, providers)

    def test_allowed_providers_reject_non_concrete_cli(self):
        providers = {"providers": {"openai": {"type": "openai", "model": "gpt"}}}
        pipelines = {"p": {"steps": [{
            "name": "A", "pool": "ai",
            "ai": {"allowed_providers": ["openai"]},
        }]}}

        with pytest.raises(ValueError, match="unsupported concrete CLI provider"):
            validate_ai_pipeline_contract(pipelines, providers)


def test_document_smart_parallelism_provider_defaults_are_bounded():
    configs_dir = Path(__file__).resolve().parents[1] / "configs"
    providers = load_yaml(configs_dir / "providers.yaml")
    assert providers["providers"]["qoder-cli"]["document_smart_parallelism"] == 4
    assert providers["providers"]["claude-cli"]["document_smart_parallelism"] == 4
    validate_ai_pipeline_contract(
        {"p": {"steps": [{
            "name": "A", "pool": "ai",
            "ai": {"allowed_providers": ["qoder-cli"]},
        }]}},
        providers,
    )


@pytest.mark.parametrize("value", (True, "8", 8.0, 0, -1, 9))
def test_document_smart_parallelism_rejects_invalid_provider_value(value):
    providers = {"providers": {"qoder-cli": {
        "type": "qoder_cli", "model": "ultimate", "models": ["ultimate"],
        "reasoning_effort": "max", "reasoning_efforts": ["max"],
        "document_smart_parallelism": value,
    }}}
    pipelines = {"p": {"steps": [{
        "name": "A", "pool": "ai",
        "ai": {"allowed_providers": ["qoder-cli"]},
    }]}}
    with pytest.raises(ValueError, match="document_smart_parallelism"):
        validate_ai_pipeline_contract(pipelines, providers)


# rules:声明式跳过/运行(归一化映射为 condition,行为等价)


class TestRules:
    def test_exists_skip_maps_to_no_subtitle(self):
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "gpu",
                                    "rules": [{"exists": "input/*.srt", "when": "skip"}]}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["condition"] == "no_subtitle"

    def test_exists_on_srt_maps_to_has_subtitle(self):
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "ai",
                                    "ai": {"allowed_providers": ["claude-cli"]},
                                    "rules": [{"exists": "input/*.srt", "when": "on"}]}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["condition"] == "has_subtitle"

    def test_exists_on_ass_maps_to_has_danmaku(self):
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "io",
                                    "rules": [{"exists": "input/*.ass", "when": "on"}]}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["condition"] == "has_danmaku"

    def test_yaml_bool_when_on_handled(self, tmp_path):
        """YAML 1.1 把裸 on 解析为布尔 True,归一化仍正确映射。"""
        f = tmp_path / "pl.yaml"
        f.write_text(
            "p:\n  jobs:\n    A:\n      run: m.a\n      pool: io\n"
            "      rules:\n        - exists: \"input/*.ass\"\n          when: on\n"
        )
        s = load_pipelines(f)["p"]["steps"][0]
        assert s["condition"] == "has_danmaku"

    def test_unmapped_rule_kept_no_condition(self):
        # 非已知 glob 的规则不强行映射成 condition,原样保留 rules 供调度器求值。
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "cpu",
                                    "rules": [{"exists": "input/*.pdf", "when": "skip"}]}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert "condition" not in s
        assert s["rules"] == [{"exists": "input/*.pdf", "when": "skip"}]


# needs:归一化为 depends_on(DAG 边)


class TestNeeds:
    def test_needs_become_depends_on(self):
        raw = {"p": {"jobs": {
            "A": {"run": "m.a", "pool": "cpu"},
            "B": {"run": "m.b", "pool": "cpu", "needs": ["A"]},
            "C": {"run": "m.c", "pool": "cpu", "needs": ["A", "B"]},
        }}}
        steps = {s["name"]: s for s in normalize_pipelines(raw)["p"]["steps"]}
        assert steps["A"]["depends_on"] == []
        assert steps["B"]["depends_on"] == ["A"]
        assert steps["C"]["depends_on"] == ["A", "B"]

    def test_topological_order_preserved(self):
        raw = {"p": {"jobs": {
            "A": {"run": "m.a", "pool": "cpu"},
            "B": {"run": "m.b", "pool": "cpu", "needs": ["A"]},
            "C": {"run": "m.c", "pool": "cpu", "needs": ["B"]},
        }}}
        order = [s["name"] for s in normalize_pipelines(raw)["p"]["steps"]]
        assert order == ["A", "B", "C"]


# image:归一化全程保留(每步镜像字段不可丢)


class TestImagePreserved:
    def test_explicit_image_kept(self):
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "gpu", "image": "flori/step-gpu"}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["image"] == "flori/step-gpu"

    def test_default_image_from_default_block(self):
        raw = {"default": {"image": "flori/step-base"},
               "p": {"jobs": {"A": {"run": "m.a", "pool": "cpu"}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["image"] == "flori/step-base"

    def test_image_fallback_when_absent(self):
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "cpu"}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["image"] == "flori/step-base"

    def test_real_pipelines_every_step_has_image(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        for pl in p.values():
            for s in pl["steps"]:
                assert s["image"], s["name"]


# 完成副作用:声明随 step 归一化保留,四类内容都必须具备索引闭环


class TestCompletionEffects:
    @staticmethod
    def _provenance_pipeline() -> dict:
        return {"p": {"jobs": {
            "producer": {
                "run": "m.producer", "pool": "cpu", "version": "3",
                "outputs": ["output/note.md", "output/provenance/smart.json"],
            },
            "indexer": {
                "run": "m.indexer", "pool": "cpu", "needs": ["producer"],
                "on_complete": [{"action": "index_note", "candidates": [{
                    "note_type": "smart", "path": "output/note.md",
                    "source_manifest": "intermediate/source_segments.json",
                    "provenance": "output/provenance/smart.json",
                    "provenance_step": "producer",
                    "provenance_since_version": "2",
                }]}],
            },
        }}}

    def test_on_complete_preserved(self):
        raw = {"p": {"jobs": {"A": {
            "run": "m.a", "pool": "cpu",
            "on_complete": [{"action": "index_note", "candidates": [
                {"note_type": "smart", "path": "output/versions/notes_smart_*"},
            ]}],
        }}}}
        step = normalize_pipelines(raw)["p"]["steps"][0]
        assert step["on_complete"][0]["action"] == "index_note"

    def test_every_real_pipeline_declares_search_index(self, configs_dir):
        pipelines = load_pipelines(configs_dir / "pipelines.yaml")
        assert set(pipelines) == {"video", "document", "audio"}
        for name, pipeline in pipelines.items():
            effects = [
                effect
                for step in pipeline["steps"]
                for effect in step.get("on_complete", [])
            ]
            assert any(effect.get("action") == "index_note" for effect in effects), name
            assert any(effect.get("action") == "sync_metadata" for effect in effects), name
            assert {effect.get("action") for effect in effects} <= {
                "sync_metadata", "index_note", "collect_glossary", "collect_term_pairs",
            }

    def test_document_index_has_source_projection_and_preferred_generated_notes(self, configs_dir):
        steps = load_pipelines(configs_dir / "pipelines.yaml")["document"]["steps"]
        effects = [
            (step["name"], effect)
            for step in steps
            for effect in step.get("on_complete", [])
            if effect.get("action") == "index_note"
        ]
        assert [name for name, _effect in effects] == ["03_structure", "09_publish"]
        assert effects[0][1]["candidates"] == [{
            "note_type": "original",
            "path": "intermediate/document_index.md",
            "source_manifest": "intermediate/source_segments.json",
            "provenance": "output/provenance/original.json",
            "provenance_step": "03_structure",
            "provenance_since_version": "1",
        }]
        assert effects[1][1]["candidates"] == [{
            "note_type": "smart",
            "path": "output/versions/notes_smart_*",
            "source_manifest": "intermediate/source_segments.json",
            "provenance": "output/provenance/smart.json",
            "provenance_step": "08_review",
            "provenance_since_version": "2",
        }]
        assert effects[1][1]["supersede_note_types"] == ["smart", "original"]

    @pytest.mark.parametrize("value", [[], ["smart", "smart"], [""], ["original"]])
    def test_index_supersede_note_types_fail_closed(self, value):
        raw = self._provenance_pipeline()
        raw["p"]["jobs"]["indexer"]["on_complete"][0][
            "supersede_note_types"
        ] = value
        with pytest.raises(ValueError, match="supersede_note_types"):
            normalize_pipelines(raw)

    def test_provenance_boundary_survives_later_producer_version_bump(self):
        pipeline = normalize_pipelines(self._provenance_pipeline())["p"]
        producer = next(step for step in pipeline["steps"] if step["name"] == "producer")
        assert producer["version"] == "3"

    @pytest.mark.parametrize(("field", "value", "message"), [
        ("provenance_step", None, "candidate fields"),
        ("provenance_step", "missing", "producer step is unknown"),
        ("provenance_since_version", "4", "version boundary"),
        ("provenance_since_version", "2.0", "version boundary"),
    ])
    def test_invalid_provenance_boundary_is_rejected(
        self, field, value, message,
    ):
        raw = copy.deepcopy(self._provenance_pipeline())
        candidate = raw["p"]["jobs"]["indexer"]["on_complete"][0]["candidates"][0]
        candidate[field] = value
        with pytest.raises(ValueError, match=message):
            normalize_pipelines(raw)

    def test_provenance_path_must_be_declared_by_producer(self):
        raw = copy.deepcopy(self._provenance_pipeline())
        raw["p"]["jobs"]["producer"]["outputs"] = ["output/note.md"]
        with pytest.raises(ValueError, match="not declared by producer"):
            normalize_pipelines(raw)

    @pytest.mark.parametrize(("field", "value", "message"), [
        ("legacy_provenance_step", None, "boundary fields"),
        ("legacy_provenance_step", "missing", "producer step is unknown"),
        ("legacy_provenance_since_version", "4", "version boundary"),
        ("legacy_provenance_since_version", "2.0", "version boundary"),
    ])
    def test_invalid_legacy_provenance_boundary_is_rejected(
        self, field, value, message,
    ):
        raw = copy.deepcopy(self._provenance_pipeline())
        candidate = raw["p"]["jobs"]["indexer"]["on_complete"][0]["candidates"][0]
        candidate.update({
            "legacy_provenance_step": "producer",
            "legacy_provenance_since_version": "2",
        })
        candidate[field] = value
        with pytest.raises(ValueError, match=message):
            normalize_pipelines(raw)

    def test_legacy_boundary_without_sidecars_is_rejected(self):
        raw = {"p": {"jobs": {"indexer": {
            "run": "m.indexer", "pool": "cpu",
            "on_complete": [{"action": "index_note", "candidates": [{
                "note_type": "smart", "path": "output/note.md",
                "legacy_provenance_step": "producer",
                "legacy_provenance_since_version": "2",
            }]}],
        }}}}
        with pytest.raises(ValueError, match="requires sidecar fields"):
            normalize_pipelines(raw)

    def test_legacy_provenance_path_must_be_declared_by_old_producer(self):
        raw = copy.deepcopy(self._provenance_pipeline())
        raw["p"]["jobs"]["legacy"] = {
            "run": "m.legacy", "pool": "cpu", "version": "3",
            "outputs": ["output/provenance/other.json"],
        }
        candidate = raw["p"]["jobs"]["indexer"]["on_complete"][0]["candidates"][0]
        candidate.update({
            "legacy_provenance_step": "legacy",
            "legacy_provenance_since_version": "2",
        })
        with pytest.raises(ValueError, match="not declared by producer"):
            normalize_pipelines(raw)

    def test_real_provenance_candidates_have_fixed_boundaries(self, configs_dir):
        pipelines = load_pipelines(configs_dir / "pipelines.yaml")
        candidates = [
            candidate
            for pipeline in pipelines.values()
            for step in pipeline["steps"]
            for effect in step.get("on_complete", [])
            if effect.get("action") == "index_note"
            for candidate in effect["candidates"]
            if candidate.get("provenance")
        ]
        assert len(candidates) == 5
        assert all(candidate.get("provenance_step") for candidate in candidates)
        assert all(
            candidate.get("provenance_since_version") for candidate in candidates
        )
        semantic_candidates = [
            candidate for candidate in candidates
            if candidate["provenance_step"].endswith("semantic_attestation")
        ]
        assert len(semantic_candidates) == 2
        legacy_semantic = [
            candidate for candidate in semantic_candidates
            if candidate["provenance_step"] in {
                "11_semantic_attestation", "04_semantic_attestation",
            }
            and candidate["note_type"] == "smart"
            and candidate.get("legacy_provenance_step")
        ]
        assert len(legacy_semantic) == 2
        assert all(
            candidate.get("legacy_provenance_step")
            and candidate.get("legacy_provenance_since_version")
            for candidate in legacy_semantic
        )


# 端到端:pipelines.yaml 归一化输出的契约形状稳定


class TestNormalizedContractStable:
    """归一化输出是 list[dict],含 worker/scheduler 依赖的全部键。"""

    def test_steps_shape(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        assert isinstance(p["video"]["steps"], list)
        for s in p["video"]["steps"]:
            assert {"name", "module", "image", "pool", "depends_on"} <= set(s)

    def test_ai_block_uses_concrete_allowed_providers(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        smart = next(s for s in p["video"]["steps"] if s["name"] == "11_smart")
        assert smart["ai"] == {"allowed_providers": list(CONCRETE_CLI_PROVIDERS)}
