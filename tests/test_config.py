"""tests for shared/config.py"""

import os
from pathlib import Path

import pytest

from shared.config import (
    AppConfig,
    _coerce_scalar,
    build_step_config,
    load_config,
    load_domain_profile,
    load_pipelines,
    load_yaml,
    normalize_pipeline,
    normalize_pipelines,
    resolve_env_vars,
    sanitize_providers,
)


class TestCoerceScalar:
    def test_int(self):
        assert _coerce_scalar("1800") == 1800 and isinstance(_coerce_scalar("1800"), int)

    def test_float(self):
        assert _coerce_scalar("1.5") == 1.5 and isinstance(_coerce_scalar("1.5"), float)

    def test_non_numeric_stays_str(self):
        assert _coerce_scalar("auto") == "auto"


class TestResolveEnvVars:
    def test_simple_var(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret123")
        assert resolve_env_vars("key=${MY_KEY}") == "key=secret123"

    def test_var_with_default_uses_value(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "actual")
        assert resolve_env_vars("${MY_KEY:-fallback}") == "actual"

    def test_var_with_default_missing(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert resolve_env_vars("${MISSING_VAR:-fallback}") == "fallback"

    def test_var_with_empty_default(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert resolve_env_vars("${MISSING_VAR:-}") == ""

    def test_missing_no_default_preserved(self, monkeypatch):
        monkeypatch.delenv("UNDEFINED_VAR", raising=False)
        assert resolve_env_vars("${UNDEFINED_VAR}") == "${UNDEFINED_VAR}"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert resolve_env_vars("${A}-${B}") == "1-2"

    def test_no_vars(self):
        assert resolve_env_vars("plain text") == "plain text"


class TestLoadYaml:
    def test_load_valid(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PORT", "8080")
        f = tmp_path / "test.yaml"
        f.write_text("server:\n  port: ${PORT}\n  host: localhost\n")
        result = load_yaml(f)
        assert result == {"server": {"port": 8080, "host": "localhost"}}

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_yaml(tmp_path / "nonexistent.yaml")

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        assert load_yaml(f) == {}

    def test_env_in_string(self, tmp_path, monkeypatch):
        monkeypatch.setenv("API_KEY", "sk-123")
        f = tmp_path / "test.yaml"
        f.write_text('api_key: "${API_KEY}"\n')
        result = load_yaml(f)
        assert result["api_key"] == "sk-123"


class TestLoadConfig:
    def test_loads_from_configs_dir(self, configs_dir, tmp_data_dir):
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        assert isinstance(cfg, AppConfig)
        assert cfg.db_path == tmp_data_dir / "db" / "analyzer.db"
        assert cfg.jobs_dir == tmp_data_dir / "jobs"
        assert "video" in cfg.pipelines
        assert "paper" in cfg.pipelines
        assert "pools" in cfg.pools
        assert "providers" in cfg.providers

    def test_paths_correct(self, configs_dir, tmp_data_dir):
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        assert cfg.data_dir == tmp_data_dir
        assert cfg.config_dir == configs_dir
        assert cfg.prompts_dir == tmp_data_dir / "prompts"

    def test_missing_pipelines_raises(self, tmp_path, tmp_data_dir):
        with pytest.raises(FileNotFoundError):
            load_config(config_dir=tmp_path, data_dir=tmp_data_dir)


class TestLoadDomainProfile:
    def test_existing_domain(self, tmp_path):
        domain_dir = tmp_path / "domain"
        domain_dir.mkdir()
        (domain_dir / "deep-learning.yaml").write_text(
            "ocr:\n  confidence_threshold: 0.6\n"
        )
        result = load_domain_profile(tmp_path, "deep-learning")
        assert result == {"ocr": {"confidence_threshold": 0.6}}

    def test_missing_domain(self, tmp_path):
        result = load_domain_profile(tmp_path, "nonexistent")
        assert result == {}


class TestBuildStepConfig:
    def test_basic_structure(self, configs_dir, tmp_data_dir):
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        step_cfg = build_step_config(cfg, "video", "06_ocr", domain="deep-learning")

        assert step_cfg["step"]["name"] == "06_ocr"
        assert step_cfg["step"]["pool"] == "cpu"
        # 超时/重试是可调运维参数,只校验类型与合理范围,不绑定具体数值。
        assert isinstance(step_cfg["step"]["timeout_sec"], int) and step_cfg["step"]["timeout_sec"] > 0
        assert isinstance(step_cfg["step"]["retries"], int) and step_cfg["step"]["retries"] >= 0
        assert step_cfg["domain"]["name"] == "deep-learning"
        assert step_cfg["paths"]["data_dir"] == str(tmp_data_dir)

    def test_ai_config_included(self, configs_dir, tmp_data_dir):
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        step_cfg = build_step_config(cfg, "video", "11_smart")

        assert "primary" in step_cfg["ai"]
        assert step_cfg["ai"]["primary"]["provider"] == "claude-cli"  # 无 key,走订阅
        assert "text_fallback" in step_cfg["ai"]

    def test_no_ai_config(self, configs_dir, tmp_data_dir):
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        step_cfg = build_step_config(cfg, "video", "03_scene")

        assert step_cfg["ai"] == {}

    def test_dynamic_timeout_fields_passthrough(self, configs_dir, tmp_data_dir):
        # audio 02_whisper 配了 timeout_per_min/max → 透传进 step dict(worker 据此动态超时)。
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        step_cfg = build_step_config(cfg, "audio", "02_whisper")
        assert step_cfg["step"]["timeout_per_min"] == 90
        assert step_cfg["step"]["timeout_max_sec"] == 21600

    def test_no_dynamic_timeout_when_unset(self, configs_dir, tmp_data_dir):
        # 未配 timeout_per_min 的步:step dict 不含这俩键(行为不变)。
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        step_cfg = build_step_config(cfg, "video", "03_scene")
        assert "timeout_per_min" not in step_cfg["step"]
        assert "timeout_max_sec" not in step_cfg["step"]

    def test_invalid_step_raises(self, configs_dir, tmp_data_dir):
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        with pytest.raises(StopIteration):
            build_step_config(cfg, "video", "nonexistent_step")


class TestSanitizeProviders:
    """providers 配置下放给步骤前必须剥离明文密钥,密钥改由 env 按需读取。"""

    def test_strips_api_key_keeps_selection(self):
        raw = {"providers": {
            "anthropic": {"type": "anthropic", "api_key": "sk-secret",
                          "models": ["claude-opus-4-8"]},
            "deepseek": {"type": "openai_compatible", "base_url": "https://x",
                         "api_key": "sk-deep", "models": ["deepseek-v4-pro"]},
        }}
        clean = sanitize_providers(raw)
        assert "api_key" not in clean["providers"]["anthropic"]
        assert "api_key" not in clean["providers"]["deepseek"]
        # 非密钥的 provider/model 选择保留,gateway 仍能路由。
        assert clean["providers"]["anthropic"]["type"] == "anthropic"
        assert clean["providers"]["anthropic"]["models"] == ["claude-opus-4-8"]
        assert clean["providers"]["deepseek"]["base_url"] == "https://x"

    def test_does_not_mutate_input(self):
        raw = {"providers": {"anthropic": {"type": "anthropic", "api_key": "sk-secret"}}}
        sanitize_providers(raw)
        assert raw["providers"]["anthropic"]["api_key"] == "sk-secret"

    def test_empty_or_malformed_providers(self):
        assert sanitize_providers({}) == {}
        assert sanitize_providers({"providers": None}) == {"providers": None}


class TestBuildStepConfigNoSecrets:
    """build_step_config 落盘/代理的 step_cfg 绝不含明文密钥。"""

    def test_no_resolved_api_key_in_step_cfg(self, configs_dir, tmp_data_dir, monkeypatch):
        # 模拟运行环境里有真实密钥:加载期会把 ${ANTHROPIC_API_KEY} 解析成它。
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-LEAK-canary")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deep-canary")
        cfg = load_config(config_dir=configs_dir, data_dir=tmp_data_dir)
        step_cfg = build_step_config(cfg, "video", "11_smart")

        # 整个 step_cfg 序列化后不得出现任何明文密钥(落盘 + 代理给 gateway 的就是它)。
        import json as _json
        serialized = _json.dumps(step_cfg)
        assert "sk-LEAK-canary" not in serialized
        assert "sk-deep-canary" not in serialized
        # providers 仍保留可路由的非密钥选择。
        for name, pcfg in step_cfg["providers"]["providers"].items():
            assert "api_key" not in pcfg, f"{name} 仍泄漏 api_key"


class TestNormalizePipelineLegacy:
    """旧 list 格式经归一化保持原状(仅补 image/depends_on 默认值),契约不变。"""

    def test_legacy_list_passthrough(self):
        raw = {"steps": [{"name": "A", "module": "m.a", "pool": "cpu",
                          "depends_on": [], "timeout_sec": 60, "retries": 1}]}
        out = normalize_pipeline(raw)
        steps = out["steps"]
        assert isinstance(steps, list)
        s = steps[0]
        assert s["name"] == "A"
        assert s["module"] == "m.a"
        assert s["pool"] == "cpu"
        assert s["timeout_sec"] == 60
        assert s["retries"] == 1
        # 旧格式无 image 时归一化补 worker 默认镜像。
        assert s["image"] == "flori/step-base"

    def test_legacy_image_preserved(self):
        raw = {"steps": [{"name": "A", "module": "m.a", "pool": "gpu",
                          "image": "flori/step-gpu", "depends_on": []}]}
        s = normalize_pipeline(raw)["steps"][0]
        assert s["image"] == "flori/step-gpu"


class TestLoadPipelinesShape:
    """加载后 pipelines[name]['steps'] 仍是 list[dict],worker/scheduler 契约不变。"""

    def test_steps_is_list_of_dicts(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        assert isinstance(p["video"]["steps"], list)
        assert isinstance(p["paper"]["steps"], list)
        for s in p["video"]["steps"]:
            assert isinstance(s, dict)
            for key in ("name", "module", "image", "pool", "depends_on"):
                assert key in s

    def test_every_step_has_image(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        for pl in ("video", "paper"):
            for s in p[pl]["steps"]:
                assert s["image"], f"{pl}/{s['name']} 缺少 image"

    def test_legacy_conditions_preserved(self, configs_dir):
        p = load_pipelines(configs_dir / "pipelines.yaml")
        by_name = {s["name"]: s for s in p["video"]["steps"]}
        assert by_name["02_whisper"]["condition"] == "no_subtitle"
        assert by_name["07_danmaku"]["condition"] == "has_danmaku"
        assert by_name["08_punctuate"]["condition"] == "has_subtitle"

    def test_ocr_timeout_single_source(self, configs_dir):
        """06_ocr 的超时来自 variables 单一事实源,归一化后为整型 1800。"""
        p = load_pipelines(configs_dir / "pipelines.yaml")
        ocr = next(s for s in p["video"]["steps"] if s["name"] == "06_ocr")
        assert ocr["timeout_sec"] == 1800
        assert isinstance(ocr["timeout_sec"], int)
        assert ocr["retries"] == 1

