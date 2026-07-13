"""常驻 Compose 服务的日志容量不变量."""

from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize(
    "relative_path",
    [
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "deploy/edge/docker-compose.yml",
        "deploy/edge/worker.yml",
        "deploy/tunnel/docker-compose.tunnel.yml",
    ],
)
def test_every_long_running_service_has_bounded_compressed_logs(relative_path):
    config = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    services = config.get("services") or {}
    assert services, relative_path

    for name, service in services.items():
        logging = service.get("logging") or {}
        options = logging.get("options") or {}
        assert logging.get("driver") == "local", f"{relative_path}:{name} driver"
        assert options.get("compress") == "true", f"{relative_path}:{name} compression"
        assert int(options.get("max-file", "0")) > 0, f"{relative_path}:{name} files"
        size = str(options.get("max-size", ""))
        assert size.endswith("m") and int(size[:-1]) > 0, f"{relative_path}:{name} size"


def test_api_compose_healthcheck_uses_liveness_not_readiness():
    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    command = config["services"]["api"]["healthcheck"]["test"]
    assert any("/api/health/live" in item for item in command)
    assert not any("/api/health/ready" in item for item in command)


def test_caddy_upstreams_use_liveness_not_readiness():
    text = (ROOT / "deploy/edge/Caddyfile").read_text(encoding="utf-8")
    health_uris = re.findall(r"^\s*health_uri\s+(\S+)", text, flags=re.MULTILINE)
    assert len(health_uris) >= 2
    assert set(health_uris) == {"/api/health/live"}


def test_readiness_probe_tuning_reaches_production_and_dev_api():
    names = {
        "FLORI_READINESS_PROBE_TTL_SEC",
        "FLORI_READINESS_PROBE_TIMEOUT_SEC",
    }
    production = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert names <= set(production["x-common-env"])

    development = yaml.safe_load(
        (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    dev_env = development["services"]["api"]["environment"]
    passed = {item.split("=", 1)[0] for item in dev_env}
    assert names <= passed


def test_deployment_docs_match_logging_and_health_contract():
    text = (ROOT / "docs/08-deployment.md").read_text(encoding="utf-8")

    assert "### 7.5 日志轮转 + 健康检查" in text
    assert "### 7.6 版本固定 / 回滚" in text
    assert "Docker `local` logging driver" in text
    assert "`DockerStepRunner`" in text
    assert "`FLORI_STEP_LOG_MAX_BYTES`" in text
    assert "`/api/health/live`" in text
    assert "`/api/health/ready`" in text
    assert "`logging:` json-file" not in text
    assert "探 `/openapi.json`" not in text
