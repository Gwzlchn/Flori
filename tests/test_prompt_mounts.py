"""锁定运行时 Prompt 模板在写入方与重放方之间同源。"""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_MOUNT = (
    "${FLORI_TEMPLATES_DIR:-./configs/prompts/templates}:"
    "/data/prompts/templates:ro"
)


def test_scheduler_and_workers_share_prompt_template_mount() -> None:
    services = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )["services"]

    for service_name in (
        "scheduler",
        "worker-cpu",
        "worker-gpu",
        "worker-ai",
        "worker-source",
        "worker-source-ai",
    ):
        assert TEMPLATE_MOUNT in services[service_name]["volumes"]
