"""Job 入口能力投影与 scheduler 任务标签保持同口径。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.job_admission import pipeline_requirements, workers_cover_pipeline


def _requirement(config, content_type: str, name: str, **kwargs):
    requirements = pipeline_requirements(
        config,
        content_type,
        source=kwargs.get("source", "upload"),
        url=kwargs.get("url"),
        domain=kwargs.get("domain", "general"),
        style_tags=kwargs.get("style_tags", []),
        flags=kwargs.get("flags", {"smart_note": True}),
    )
    return next(value for value in requirements if value.name == name)


def _worker(pools: str, tags: str) -> dict[str, str]:
    return {
        "pools": pools, "tags": tags, "reject_tags": "",
        "admin_status": "active", "status": "idle",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
    }


def test_task_tags_match_scheduler_by_pool(test_config):
    cpu = _requirement(
        test_config, "video", "03_scene", domain="tech", style_tags=["formal"],
    )
    ai = _requirement(
        test_config, "document", "07_concepts", domain="tech", style_tags=["formal"],
    )

    assert cpu.task_tags == frozenset()
    assert ai.task_tags == frozenset({"tech", "formal"})


def test_static_and_net_tags_are_both_task_reject_tags(test_config):
    step = test_config.pipelines["video"]["steps"][0]
    original = list(step.get("tags") or [])
    step["tags"] = ["bulk"]
    try:
        requirement = _requirement(
            test_config, "video", "01_download",
            source="bilibili", url="BV1xx411c7mD",
        )
    finally:
        step["tags"] = original

    assert requirement.required_tags == frozenset({"bulk", "net-cn"})
    assert requirement.task_tags == frozenset({"bulk", "net-cn"})


def test_upload_has_no_network_zone_requirement(test_config):
    requirement = _requirement(test_config, "video", "01_download", source="upload")
    assert not requirement.required_tags.intersection({"net-cn", "net-global"})


def test_flag_skip_is_excluded_but_future_artifact_rule_is_reachable(test_config):
    without_smart = pipeline_requirements(
        test_config, "document", source="upload", url=None,
        domain="general", style_tags=[], flags={"smart_note": False},
    )
    with_smart = pipeline_requirements(
        test_config, "document", source="upload", url=None,
        domain="general", style_tags=[], flags={"smart_note": True},
    )
    without_smart_names = {item.name for item in without_smart}
    with_smart_names = {item.name for item in with_smart}

    assert "05_smart" not in without_smart_names
    assert "08_review" not in without_smart_names
    assert "05_smart" in with_smart_names
    assert "08_review" in with_smart_names
    assert "04_translate" in without_smart_names
    assert "read" not in next(
        item.required_tags for item in with_smart if item.name == "04_translate"
    )


def test_paused_stale_and_reject_tag_workers_do_not_cover_pipeline(test_config):
    heartbeat = datetime.now(timezone.utc).isoformat()
    workers = [{
        "pools": "io,cpu,ai", "tags": "claude-cli,net-cn,net-global",
        "reject_tags": "tech", "admin_status": "active", "last_heartbeat": heartbeat,
    }]
    requirements = pipeline_requirements(
        test_config, "document", source="upload", url=None,
        domain="tech", style_tags=[], flags={"smart_note": False},
    )
    assert not workers_cover_pipeline(workers, requirements, test_config)

    workers[0]["reject_tags"] = ""
    workers[0]["last_heartbeat"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    assert not workers_cover_pipeline(workers, requirements, test_config)

    workers[0]["last_heartbeat"] = heartbeat
    workers[0]["admin_status"] = "paused"
    assert not workers_cover_pipeline(workers, requirements, test_config)


def test_pool_or_provider_tag_mismatch_is_rejected(test_config):
    requirements = pipeline_requirements(
        test_config, "document", source="upload", url=None,
        domain="general", style_tags=[], flags={"smart_note": False},
    )
    wrong_pool = [_worker("io,cpu", "claude-cli")]
    wrong_provider = [_worker("io,cpu,ai", "codex-cli")]

    assert not workers_cover_pipeline(wrong_pool, requirements, test_config)
    assert not workers_cover_pipeline(wrong_provider, requirements, test_config)


def test_different_workers_may_cover_different_pipeline_steps(test_config):
    requirements = pipeline_requirements(
        test_config, "document", source="upload", url=None,
        domain="general", style_tags=[], flags={"smart_note": False},
    )
    workers = [
        _worker("io", ""),
        _worker("cpu", ""),
        _worker("ai", "claude-cli"),
    ]

    assert workers_cover_pipeline(workers, requirements, test_config)


def test_net_global_url_is_rejected_by_net_cn_only_worker(test_config):
    requirements = pipeline_requirements(
        test_config, "document", source="web", url="https://example.com/post",
        domain="general", style_tags=[], flags={"smart_note": False},
    )
    workers = [
        _worker("io", "net-cn"),
        _worker("cpu", ""),
        _worker("ai", "claude-cli"),
    ]

    assert not workers_cover_pipeline(workers, requirements, test_config)
