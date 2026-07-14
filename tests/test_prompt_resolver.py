"""Prompt 解析器与四类 AI 执行身份契约."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def _resolver(tmp_path: Path):
    from shared.prompt_resolver import PromptResolver

    hot = tmp_path / "data" / "prompts" / "templates"
    image = tmp_path / "app" / "configs" / "prompts" / "templates"
    hot.mkdir(parents=True)
    image.mkdir(parents=True)
    return PromptResolver(hot_dir=hot, image_dir=image), hot, image


def test_resolver_precedence_and_exact_bytes_hash(tmp_path):
    resolver, hot, image = _resolver(tmp_path)
    image_bytes = "镜像正文\n".encode()
    hot_bytes = "热编辑正文\r\n".encode()
    (image / "11_smart.md").write_bytes(image_bytes)
    (hot / "11_smart.md").write_bytes(hot_bytes)

    hot_result = resolver.resolve("11_smart", step_name="11_smart")
    assert hot_result.raw == hot_bytes
    assert hot_result.text == hot_bytes.decode("utf-8")
    assert hot_result.source == "hot"
    assert hot_result.sha256 == "sha256:" + hashlib.sha256(hot_bytes).hexdigest()

    override = {"content": "任务固定正文\n", "version": 7}
    result = resolver.resolve(
        "11_smart", step_name="11_smart", prompt_overrides={"11_smart": override},
    )
    assert result.raw == override["content"].encode()
    assert result.source == "override"
    assert result.version == 7


def test_resolver_only_enoent_falls_back(tmp_path, monkeypatch):
    resolver, hot, image = _resolver(tmp_path)
    (image / "11_smart.md").write_text("IMAGE", encoding="utf-8")
    assert resolver.resolve("11_smart", step_name="11_smart").text == "IMAGE"

    (hot / "11_smart.md").write_bytes(b"\xff")
    from shared.prompt_resolver import PromptResolutionError
    with pytest.raises(PromptResolutionError, match="UTF-8"):
        resolver.resolve("11_smart", step_name="11_smart")

    (hot / "11_smart.md").unlink()
    original = Path.read_bytes

    def denied(path: Path):
        if path == hot / "11_smart.md":
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PromptResolutionError, match="unreadable"):
        resolver.resolve("11_smart", step_name="11_smart")


@pytest.mark.parametrize(
    "overrides",
    [
        [],
        {"11_smart": 1},
        {"11_smart": {"content": 1, "version": 1}},
        {"11_smart": {"content": "x", "version": 0}},
        {"11_smart": {"content": "x", "version": True}},
        {"11_smart": {"content": "x", "version": 1 << 63}},
        {"11_smart": {"content": "x", "version": 1, "extra": 1}},
    ],
)
def test_resolver_rejects_malformed_job_override(tmp_path, overrides):
    resolver, _, image = _resolver(tmp_path)
    (image / "11_smart.md").write_text("IMAGE", encoding="utf-8")
    from shared.prompt_resolver import PromptResolutionError
    with pytest.raises(PromptResolutionError, match="override"):
        resolver.resolve("11_smart", step_name="11_smart", prompt_overrides=overrides)


def test_resolver_variant_override_targets_runtime_primary_template(tmp_path):
    resolver, _, image = _resolver(tmp_path)
    (image / "11_smart.md").write_text("MAIN", encoding="utf-8")
    (image / "11_smart.vision.md").write_text("VISION", encoding="utf-8")
    overrides = {"11_smart": {"content": "OVERRIDE", "version": 2}}
    assert resolver.resolve(
        "11_smart", step_name="11_smart", prompt_overrides=overrides,
    ).text == "OVERRIDE"
    assert resolver.resolve(
        "11_smart.vision", step_name="11_smart", prompt_overrides=overrides,
    ).text == "VISION"


def test_video_concepts_template_maps_runtime_step_but_override_uses_runtime_identity(tmp_path):
    resolver, _, image = _resolver(tmp_path)
    (image / "05_concepts.md").write_text("TRACKED", encoding="utf-8")
    result = resolver.resolve(
        "05_concepts",
        step_name="12_concepts",
        prompt_overrides={"12_concepts": {"content": "VIDEO OVERRIDE", "version": 3}},
        primary_template="05_concepts",
    )
    assert result.text == "VIDEO OVERRIDE"
    assert result.name == "05_concepts"


def test_cli_main_uses_step_config_runtime_identity(tmp_path, monkeypatch):
    from shared.step_base import StepBase

    captured = {}

    class ProbeStep(StepBase):
        def run(self):
            captured["name"] = self.step_name

    cfg = {"step": {"name": "12_concepts"}}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["probe", "--job-dir", str(tmp_path), "--step-config", str(cfg_path)],
    )
    ProbeStep.cli_main("05_concepts")
    assert captured["name"] == "12_concepts"
