"""tests for shared/status.compute_component_status + shared/sysload + shared/version。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared import sysload
from shared.status import (
    COMPONENT_DEGRADED,
    COMPONENT_DOWN,
    COMPONENT_UNKNOWN,
    COMPONENT_UP,
    compute_component_status,
)


NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


class TestComputeComponentStatus:
    def test_no_heartbeat_is_unknown(self):
        # 无心跳记录(从未上报/老版本)→ unknown,不误报为挂了。
        assert compute_component_status(None, NOW) == COMPONENT_UNKNOWN

    def test_fresh_is_up(self):
        hb = NOW - timedelta(seconds=5)
        assert compute_component_status(hb, NOW) == COMPONENT_UP

    def test_within_stale_window_is_degraded(self):
        # 30 < age <= 900 → degraded(错过几拍的缓冲带)。
        hb = NOW - timedelta(seconds=120)
        assert compute_component_status(hb, NOW) == COMPONENT_DEGRADED

    def test_beyond_stale_is_down(self):
        hb = NOW - timedelta(seconds=1000)
        assert compute_component_status(hb, NOW) == COMPONENT_DOWN

    def test_naive_heartbeat_treated_as_utc(self):
        hb = (NOW - timedelta(seconds=5)).replace(tzinfo=None)
        assert compute_component_status(hb, NOW) == COMPONENT_UP

    def test_custom_windows(self):
        hb = NOW - timedelta(seconds=40)
        # 自定义窗口 online=60 → 40s 仍 up。
        assert compute_component_status(hb, NOW, online_window_sec=60) == COMPONENT_UP


class TestSysload:
    def test_collect_node_load_keys(self):
        load = sysload.collect_node_load()
        assert set(load) == {"cpu_pct", "mem_pct", "loadavg"}
        # 各值或为 float 或 None(非 Linux/读不到),不抛。
        for v in load.values():
            assert v is None or isinstance(v, float)

    def test_read_process_rss_mb(self):
        rss = sysload.read_process_rss_mb()
        # 容器(Linux)内应读到正数;非 Linux 回 None——两者都不抛。
        assert rss is None or rss > 0

    def test_cpu_pct_first_then_value(self):
        # 首次采样无前值(_prev_cpu 复位)→ None;第二次有差值。
        sysload._prev_cpu = None
        first = sysload._read_cpu_pct()
        assert first is None
        second = sysload._read_cpu_pct()
        assert second is None or 0.0 <= second <= 100.0


def test_flori_version_default():
    # FLORI_VERSION = 语义版本(pyproject,经 importlib.metadata)+ 可选构建短sha;非空字符串。
    from shared.version import FLORI_VERSION
    assert isinstance(FLORI_VERSION, str) and FLORI_VERSION
