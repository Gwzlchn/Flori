"""轻量节点负载采集(纯 /proc,无 psutil 依赖,便宜非阻塞)。

worker 心跳带本机 cpu%/mem%/loadavg(B 档"各节点 live 负载");api 进程带自身 RSS。
全部容错:任一项读不到返回 None,绝不抛(采集失败 ≠ 服务挂)。Linux 专属;非 Linux
(/proc 缺失)各项回 None,调用方按缺省处理。
"""

from __future__ import annotations


def _read_loadavg() -> float | None:
    """1 分钟平均负载(运行队列长度)。"""
    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            return round(float(f.read().split()[0]), 2)
    except (OSError, ValueError, IndexError):
        return None


def _read_mem_pct() -> float | None:
    """已用内存百分比 = (MemTotal - MemAvailable) / MemTotal * 100。"""
    try:
        total = avail = None
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                if total is not None and avail is not None:
                    break
        if not total or avail is None:
            return None
        return round((total - avail) / total * 100, 1)
    except (OSError, ValueError, IndexError):
        return None


def _read_cpu_jiffies() -> tuple[int, int] | None:
    """读 /proc/stat 首行聚合 cpu:返回 (busy, total) jiffies。供两次采样算瞬时 cpu%。"""
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        if not parts or parts[0] != "cpu":
            return None
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None


# 上一拍 cpu jiffies 快照(模块级,单进程内多次采样算瞬时占用率)。
_prev_cpu: tuple[int, int] | None = None


def _read_cpu_pct() -> float | None:
    """瞬时 cpu 占用率(基于两次采样的 jiffies 差;首次采样无前值→None,下次有)。
    非阻塞(不 sleep):靠相邻两次心跳间的自然时间差,不准但够"live 负载"展示。"""
    global _prev_cpu
    cur = _read_cpu_jiffies()
    if cur is None:
        return None
    prev = _prev_cpu
    _prev_cpu = cur
    if prev is None:
        return None
    busy_d = cur[0] - prev[0]
    total_d = cur[1] - prev[1]
    if total_d <= 0:
        return None
    return round(max(0.0, min(100.0, busy_d / total_d * 100)), 1)


def collect_node_load() -> dict:
    """worker 心跳用:本机 live 负载快照 {cpu_pct, mem_pct, loadavg};各项缺失=None。便宜非阻塞。"""
    return {
        "cpu_pct": _read_cpu_pct(),
        "mem_pct": _read_mem_pct(),
        "loadavg": _read_loadavg(),
    }


def read_process_rss_mb() -> float | None:
    """当前进程常驻内存 RSS(MB),读 /proc/self/status 的 VmRSS;读不到返回 None。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None
