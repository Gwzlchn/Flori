#!/usr/bin/env python3
"""等待当前 Actions attempt 的覆盖率生产 job."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, NoReturn


class CoverageWaitError(RuntimeError):
    """覆盖率屏障遇到不可恢复的状态."""


@dataclass(frozen=True)
class Snapshot:
    jobs: list[dict[str, object]]


def expected_producers(normal_splits: int, worker_splits: int) -> set[str]:
    """返回本 run 必须成功的覆盖率生产 job 显示名."""
    return {
        *(f"unit-normal ({group})" for group in range(1, normal_splits + 1)),
        *(f"unit-worker ({group})" for group in range(1, worker_splits + 1)),
        "integration (data)",
        "integration (services)",
    }


def _unique_by_name(
    records: list[dict[str, object]],
    kind: str,
) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for record in records:
        name = record.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in indexed:
            raise CoverageWaitError(f"当前 attempt 出现重复{kind}: {name}")
        indexed[name] = record
    return indexed


def evaluate_snapshot(
    snapshot: Snapshot,
    phase: str,
    normal_splits: int,
    worker_splits: int,
) -> tuple[bool, str]:
    """判断屏障是否就绪;当前 attempt 的终态失败立即阻断."""
    jobs = _unique_by_name(snapshot.jobs, "job")
    if phase == "runtime":
        producer = jobs.get("unit-normal (1)")
        if producer is None or producer.get("status") != "completed":
            return False, "等待 unit-normal (1) 完成"
        conclusion = producer.get("conclusion")
        if conclusion != "success":
            raise CoverageWaitError(
                f"unit-normal (1) 未成功: {conclusion or 'unknown'}",
            )
        return True, "unit-normal (1) 已成功"

    producers = expected_producers(normal_splits, worker_splits)
    failed = sorted(
        name
        for name in producers
        if name in jobs
        and jobs[name].get("status") == "completed"
        and jobs[name].get("conclusion") != "success"
    )
    if failed:
        details = ", ".join(
            f"{name}={jobs[name].get('conclusion') or 'unknown'}"
            for name in failed
        )
        raise CoverageWaitError(f"覆盖率生产 job 未成功: {details}")

    pending_jobs = sorted(
        name
        for name in producers
        if name not in jobs
        or jobs[name].get("status") != "completed"
        or jobs[name].get("conclusion") != "success"
    )
    if pending_jobs:
        return False, f"等待 {len(pending_jobs)} 个覆盖率生产 job"

    return True, f"{len(producers)} 个覆盖率生产 job 已成功"


class GitHubActionsClient:
    """读取固定 workflow attempt 的 job."""

    def __init__(
        self,
        api_url: str,
        repository: str,
        run_id: str,
        run_attempt: str,
        token: str,
    ) -> None:
        base = f"{api_url.rstrip('/')}/repos/{repository}/actions/runs/{run_id}"
        self.jobs_url = f"{base}/attempts/{run_attempt}/jobs"
        self.token = token

    def _get_page(self, url: str, page: int) -> dict[str, object]:
        separator = "&" if "?" in url else "?"
        request = urllib.request.Request(
            f"{url}{separator}per_page=100&page={page}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "flori-ci-coverage-wait",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise CoverageWaitError(f"GitHub Actions API 请求失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise CoverageWaitError("GitHub Actions API 返回值不是 object")
        return payload

    def _get_all(self, url: str, key: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        page = 1
        while True:
            payload = self._get_page(url, page)
            batch = payload.get(key)
            if not isinstance(batch, list):
                raise CoverageWaitError(f"GitHub Actions API 缺少 {key} list")
            records.extend(record for record in batch if isinstance(record, dict))
            total = payload.get("total_count")
            if len(batch) < 100:
                if isinstance(total, int) and total > len(records):
                    raise CoverageWaitError(f"GitHub Actions API {key} 分页不完整")
                return records
            page += 1

    def snapshot(self) -> Snapshot:
        return Snapshot(jobs=self._get_all(self.jobs_url, "jobs"))


def wait_until_ready(
    fetch: Callable[[], Snapshot],
    phase: str,
    normal_splits: int,
    worker_splits: int,
    timeout_seconds: float,
    interval_seconds: float,
    max_api_errors: int = 3,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """轮询屏障;短暂 API 抖动可重试,连续错误和超时必须失败."""
    deadline = monotonic() + timeout_seconds
    api_errors = 0
    last_status = "尚未读取状态"
    while monotonic() < deadline:
        try:
            snapshot = fetch()
            api_errors = 0
            ready, last_status = evaluate_snapshot(
                snapshot,
                phase,
                normal_splits,
                worker_splits,
            )
        except CoverageWaitError as exc:
            if not str(exc).startswith("GitHub Actions API"):
                raise
            api_errors += 1
            last_status = str(exc)
            if api_errors >= max_api_errors:
                raise CoverageWaitError(
                    f"GitHub Actions API 连续失败 {api_errors} 次: {exc}",
                ) from exc
            ready = False
        if ready:
            return last_status
        sleep(interval_seconds)
    raise CoverageWaitError(f"等待覆盖率屏障超时: {last_status}")


def _fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("runtime", "all"), required=True)
    parser.add_argument("--normal-splits", type=int, default=15)
    parser.add_argument("--worker-splits", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--interval", type=float, default=2)
    args = parser.parse_args()
    if args.normal_splits < 1 or args.worker_splits < 1:
        parser.error("coverage shard 数量必须是正整数")
    if args.timeout <= 0 or args.interval < 0:
        parser.error("timeout 必须为正数且 interval 不能为负数")

    required_env = {
        name: os.environ.get(name, "")
        for name in (
            "GH_TOKEN",
            "GITHUB_REPOSITORY",
            "GITHUB_RUN_ID",
            "GITHUB_RUN_ATTEMPT",
        )
    }
    missing = [name for name, value in required_env.items() if not value]
    if missing:
        parser.error("缺少环境变量: " + ", ".join(missing))

    client = GitHubActionsClient(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        required_env["GITHUB_REPOSITORY"],
        required_env["GITHUB_RUN_ID"],
        required_env["GITHUB_RUN_ATTEMPT"],
        required_env["GH_TOKEN"],
    )
    try:
        result = wait_until_ready(
            client.snapshot,
            args.phase,
            args.normal_splits,
            args.worker_splits,
            args.timeout,
            args.interval,
        )
    except CoverageWaitError as exc:
        _fail(str(exc))
    print(result)


if __name__ == "__main__":
    main()
