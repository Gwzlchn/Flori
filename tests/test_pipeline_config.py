"""GitLab-CI 风格流水线归一化:extends / variables / rules / needs / image 保留。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

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

    def test_deep_merge_ai_block(self):
        raw = {
            ".ai-step": {"pool": "ai", "ai": {"primary": {"provider": "anthropic", "model": "x"},
                                              "fallback": {"provider": "deepseek", "model": "y"}}},
            "p": {"jobs": {"A": {"extends": ".ai-step", "run": "m.a",
                                 "ai": {"primary": {"model": "z"}}}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        # 深合并:primary.model 被覆盖,primary.provider 与 fallback 保留。
        assert s["ai"]["primary"] == {"provider": "anthropic", "model": "z"}
        assert s["ai"]["fallback"] == {"provider": "deepseek", "model": "y"}

    def test_multi_level_extends(self):
        raw = {
            ".ai-step": {"pool": "ai", "timeout": 600, "retry": 2},
            ".review": {"extends": ".ai-step", "timeout": 120},
            "p": {"jobs": {"A": {"extends": ".review", "run": "m.a"}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["pool"] == "ai"      # 来自 .ai-step
        assert s["timeout_sec"] == 120  # 被 .review 覆盖
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

    def test_var_in_ai_block(self):
        raw = {
            "p": {
                "variables": {"PROV": "kimi", "MODEL": "moonshot-v1-8k"},
                "jobs": {"A": {"run": "m.a", "pool": "ai",
                               "ai": {"primary": {"provider": "$PROV", "model": "$MODEL"}}}},
            }
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["ai"]["primary"] == {"provider": "kimi", "model": "moonshot-v1-8k"}

    def test_pipeline_var_overrides_global(self):
        raw = {
            "variables": {"PROV": "anthropic"},
            "p": {"variables": {"PROV": "kimi"},
                  "jobs": {"A": {"run": "m.a", "pool": "ai",
                                 "ai": {"primary": {"provider": "$PROV"}}}}},
        }
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["ai"]["primary"]["provider"] == "kimi"

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
                "variables": {"OCR_TIMEOUT": 1800, "OCR_RETRIES": 1, "PROV": "anthropic"},
                "jobs": {
                    "06_ocr": {"extends": ".cpu-step", "run": "steps.video.step_06_ocr",
                               "image": "flori/step-heavy", "needs": ["05_dedup"],
                               "timeout": "$OCR_TIMEOUT", "retry": "$OCR_RETRIES"},
                    "11_smart": {"run": "m.s", "pool": "ai",
                                 "ai": {"primary": {"provider": "$PROV"}}},
                },
            },
        }
        prod_norm = normalize_pipelines(raw)
        # integration overlay:仅覆盖 PROV → kimi,OCR_* 不重写。
        raw_int = {
            **{k: raw[k] for k in (".cpu-step", "default")},
            "video": {**raw["video"],
                      "variables": {**raw["video"]["variables"], "PROV": "kimi"}},
        }
        int_norm = normalize_pipelines(raw_int)

        prod_ocr = next(s for s in prod_norm["video"]["steps"] if s["name"] == "06_ocr")
        int_ocr = next(s for s in int_norm["video"]["steps"] if s["name"] == "06_ocr")
        assert prod_ocr["timeout_sec"] == int_ocr["timeout_sec"] == 1800
        assert prod_ocr["retries"] == int_ocr["retries"] == 1
        # provider 是两侧唯一差异。
        prod_smart = next(s for s in prod_norm["video"]["steps"] if s["name"] == "11_smart")
        int_smart = next(s for s in int_norm["video"]["steps"] if s["name"] == "11_smart")
        assert prod_smart["ai"]["primary"]["provider"] == "anthropic"
        assert int_smart["ai"]["primary"]["provider"] == "kimi"

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
            "video": {"08_punctuate": "4", "11_smart": "5"},
            "paper": {"02_pdf_parse": "5", "05_smart_paper": "4"},
            "article": {"02_parse_article": "5", "04_smart_article": "4"},
            "audio": {"03_transcript_parse": "3", "04_smart_podcast": "4"},
        }
        for pipeline, versions in expected.items():
            actual = {step["name"]: step["version"] for step in pipelines[pipeline]["steps"]}
            assert {name: actual[name] for name in versions} == versions


class TestSemanticAttestationPipeline:
    def test_candidates_are_outputs_only_and_concepts_indexes_final(self, configs_dir):
        raw = load_yaml(configs_dir / "pipelines.yaml")
        producers = {
            "video": {"11_smart": "smart"},
            "paper": {"04_translate_paper": "translated", "05_smart_paper": "smart"},
            "article": {
                "04_translate_article": "translated", "04_smart_article": "smart",
            },
            "audio": {"04_smart_podcast": "smart"},
        }
        attestors = {
            "video": "11_semantic_attestation",
            "paper": "05_semantic_attestation",
            "article": "04_semantic_attestation",
            "audio": "04_semantic_attestation",
        }
        concepts = {"video": "12_concepts", "paper": "05_concepts", "article": "05_concepts", "audio": "05_concepts"}
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
            assert any(path.startswith("output/provenance/") for path in attestor["outputs"])
            assert attestor["timeout"] == 180
            assert depends_on(concepts[pipeline], attestors[pipeline])
            index_actions = [
                action for action in jobs[concepts[pipeline]].get("on_complete", [])
                if action.get("action") == "index_note"
            ]
            for action in index_actions:
                assert all(
                    "provenance_candidates" not in candidate["provenance"]
                    for candidate in action["candidates"]
                )


class TestAIRoleContract:
    def test_real_config_has_22_shared_variables_and_16_routes(self, configs_dir):
        raw = load_yaml(configs_dir / "pipelines.yaml")
        ai_variables = {
            key: value for key, value in raw["variables"].items()
            if key.startswith("AI_")
        }
        assert len(ai_variables) == 22
        for pipeline in ("video", "paper", "article", "audio"):
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
            ("paper", "04_translate_paper"), ("paper", "05_smart_paper"),
            ("paper", "05_concepts"), ("paper", "06_review"),
            ("paper", "05_semantic_attestation"),
            ("article", "04_smart_article"), ("article", "04_translate_article"),
            ("article", "05_concepts"), ("article", "06_review"),
            ("article", "04_semantic_attestation"),
            ("audio", "04_smart_podcast"), ("audio", "05_concepts"),
            ("audio", "05_review"),
            ("audio", "04_semantic_attestation"),
        }
        expected_route = {
            "primary": {"provider": "claude-cli", "model": "claude-opus-4-8[1m]"},
            "fallback": {"provider": "claude-cli", "model": "claude-opus-4-8[1m]"},
        }
        for key, route in routes.items():
            assert route["primary"] == expected_route["primary"], key
            assert route["fallback"] == expected_route["fallback"], key
        assert routes[("video", "11_smart")]["text_fallback"] == expected_route["primary"]
        assert sum(len(route) for route in routes.values()) == 41

    def test_shared_ai_variables_reject_undefined_unused_and_empty(self):
        base = {
            "variables": {"AI_PRIMARY_PROVIDER": "known", "AI_PRIMARY_MODEL": "m"},
            "p": {"jobs": {"A": {
                "run": "m.a", "pool": "ai",
                "ai": {"primary": {
                    "provider": "$AI_PRIMARY_PROVIDER", "model": "$AI_PRIMARY_MODEL",
                }},
            }}},
        }
        assert normalize_pipelines(base)["p"]["steps"][0]["ai"]["primary"]["provider"] == "known"

        unused = {**base, "variables": {**base["variables"], "AI_UNUSED_MODEL": "x"}}
        with pytest.raises(ValueError, match="unused"):
            normalize_pipelines(unused)

        undefined = {
            **base,
            "p": {"jobs": {"A": {
                "run": "m.a", "pool": "ai",
                "ai": {"primary": {
                    "provider": "$AI_PRIMARY_PROVIDER", "model": "$AI_MISSING_MODEL",
                }},
            }}},
        }
        with pytest.raises(ValueError, match="undefined"):
            normalize_pipelines(undefined)

        empty = {**base, "variables": {**base["variables"], "AI_PRIMARY_MODEL": ""}}
        with pytest.raises(ValueError, match="non-empty"):
            normalize_pipelines(empty)

    def test_ai_routes_reject_illegal_tier_shape_and_unknown_provider(self):
        pipelines = {"p": {"steps": [{
            "name": "A", "pool": "ai",
            "ai": {"primary": {"provider": "known", "model": "m"},
                   "shadow": {"provider": "known", "model": "m"}},
        }]}}
        with pytest.raises(ValueError, match="invalid AI tier"):
            validate_ai_pipeline_contract(pipelines)

        pipelines["p"]["steps"][0]["ai"] = {
            "primary": {"provider": "unknown", "model": "m"},
        }
        with pytest.raises(ValueError, match="unknown AI provider"):
            validate_ai_pipeline_contract(pipelines, {"providers": {"known": {}}})


# rules:声明式跳过/运行(归一化映射为 condition,行为等价)


class TestRules:
    def test_exists_skip_maps_to_no_subtitle(self):
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "gpu",
                                    "rules": [{"exists": "input/*.srt", "when": "skip"}]}}}}
        s = normalize_pipelines(raw)["p"]["steps"][0]
        assert s["condition"] == "no_subtitle"

    def test_exists_on_srt_maps_to_has_subtitle(self):
        raw = {"p": {"jobs": {"A": {"run": "m.a", "pool": "ai",
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
        assert {"video", "paper", "article", "audio"} <= set(pipelines)
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

    def test_article_index_has_lightweight_fallbacks(self, configs_dir):
        steps = load_pipelines(configs_dir / "pipelines.yaml")["article"]["steps"]
        effect = next(
            effect
            for step in steps
            for effect in step.get("on_complete", [])
            if effect.get("action") == "index_note"
        )
        assert [candidate["note_type"] for candidate in effect["candidates"]] == [
            "smart", "translated", "original",
        ]

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
        assert len(candidates) == 7
        assert all(candidate.get("provenance_step") for candidate in candidates)
        assert all(
            candidate.get("provenance_since_version") for candidate in candidates
        )
        semantic_candidates = [
            candidate for candidate in candidates
            if candidate["provenance_step"].endswith("semantic_attestation")
        ]
        assert len(semantic_candidates) == 5
        assert all(
            candidate.get("legacy_provenance_step")
            and candidate.get("legacy_provenance_since_version")
            for candidate in semantic_candidates
        )


# 端到端:pipelines.yaml 归一化输出的契约形状稳定


class TestNormalizedContractStable:
    """归一化输出是 list[dict],含 worker/scheduler 依赖的全部键。"""

    def test_steps_shape(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        assert isinstance(p["video"]["steps"], list)
        for s in p["video"]["steps"]:
            assert {"name", "module", "image", "pool", "depends_on"} <= set(s)

    def test_ai_block_provider_model_dict(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        smart = next(s for s in p["video"]["steps"] if s["name"] == "11_smart")
        assert smart["ai"]["primary"]["provider"] == "claude-cli"  # 默认走 Claude CLI 接入方式
        assert smart["ai"]["primary"]["model"] == "claude-opus-4-8[1m]"
        assert "text_fallback" in smart["ai"]
