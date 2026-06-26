"""ECS↔NAS 反向 SSH 隧道流量上报器(容器 flori-tunnel-stats)。

为什么独立容器 + pid:host:隧道是若干 autossh 容器(api/minio/redis/dozzle/mcp),其字节在各自
netns 的 eth0。api 不挂 docker.sock(安全约定),故本进程用 pid:host 扫 /proc 找隧道 ssh 进程
(comm==ssh 且带 -N,即"只转发不执行命令"的隧道连接),按 netns 去重,读 /proc/<pid>/net/dev
得每条隧道 eth0 的累计 RX/TX。顺带统一采样网关中转累计量(traffic:{dir}:total)+ 算当前速率,
写 link:traffic 快照 + traffic:timeline 时间线(供前端速率/趋势)。

全程 best-effort:读 /proc / redis 失败都不影响隧道本身,只是这一轮统计丢。无隧道进程 → tunnel.up=False
(正好反映 ECS 不可达/隧道全断)。
"""
from __future__ import annotations

import asyncio
import glob
import json  # noqa: F401  (保留:未来如需本地落盘调试)
import os
import time

from shared.redis_client import RedisClient

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
INTERVAL = int(os.environ.get("TUNNEL_STATS_INTERVAL", "20"))
TIMELINE_CAP = int(os.environ.get("TUNNEL_STATS_TIMELINE_CAP", "180"))  # ~1h @ 20s

# 隧道转发目标端口 → 友好名(对齐 deploy/tunnel 的 5 条 -R/-L)。
_PORT_NAME = {
    "8000": "api", "9000": "minio", "6379": "redis",
    "7007": "dozzle", "8090": "mcp", "8091": "mcp",
}


def _read_tunnels() -> list[dict]:
    """扫 /proc 找隧道 ssh 进程(comm==ssh 且带 -N),按 netns 去重,读每隧道 eth0 累计 RX/TX。
    返回 [{name, rx, tx, fwd}](顺序不保证)。需 pid:host 才能见到其它容器的进程与其 netns。"""
    by_ns: dict[str, dict] = {}
    for cl in glob.glob("/proc/[0-9]*/cmdline"):
        pid = cl.split("/")[2]
        try:
            if open(f"/proc/{pid}/comm").read().strip() != "ssh":
                continue
            raw = open(cl, "rb").read()
            args = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
        except Exception:
            continue
        if "-N" not in args:  # 隧道连接用 ssh -N(纯转发);排除普通 ssh 会话
            continue
        # 取 -R/-L 的转发规格,末段端口定友好名
        fwd = ""
        for i, a in enumerate(args):
            if a in ("-R", "-L") and i + 1 < len(args):
                fwd = args[i + 1]
                break
            if (a.startswith("-R") or a.startswith("-L")) and len(a) > 2:
                fwd = a[2:]
                break
        try:
            ino = os.readlink(f"/proc/{pid}/ns/net")  # 同容器多进程共享 netns → 去重
        except Exception:
            continue
        if ino in by_ns:
            continue
        dport = fwd.rsplit(":", 1)[-1] if fwd else ""
        name = _PORT_NAME.get(dport, dport or "?")
        rx = tx = 0
        try:
            for ln in open(f"/proc/{pid}/net/dev"):
                if ":" not in ln:
                    continue
                f = ln.split()
                if f[0].rstrip(":") == "lo":
                    continue
                rx += int(f[1])
                tx += int(f[9])
        except Exception:
            continue
        by_ns[ino] = {"name": name, "rx": rx, "tx": tx, "fwd": fwd}
    return list(by_ns.values())


async def main() -> None:
    redis = RedisClient(REDIS_URL)
    await redis.connect()
    prev: tuple[float, int, int, int, int] | None = None  # ts, rx, tx, gw_pull, gw_push
    while True:
        try:
            tunnels = _read_tunnels()
            rx = sum(t["rx"] for t in tunnels)
            tx = sum(t["tx"] for t in tunnels)
            pull_d = await redis.get_traffic("pull")
            push_d = await redis.get_traffic("push")
            pull, push = pull_d["total"], push_d["total"]
            now = time.time()

            rx_bps = tx_bps = gw_pull_bps = gw_push_bps = 0.0
            if prev is not None:
                dt = now - prev[0]
                if dt > 0:
                    rx_bps = max(0.0, (rx - prev[1]) / dt)
                    tx_bps = max(0.0, (tx - prev[2]) / dt)
                    gw_pull_bps = max(0.0, (pull - prev[3]) / dt)
                    gw_push_bps = max(0.0, (push - prev[4]) / dt)
            prev = (now, rx, tx, pull, push)

            await redis.set_link_traffic({
                "ts": now,
                "gateway": {
                    "pull": pull, "push": push,
                    "pull_bps": round(gw_pull_bps, 1), "push_bps": round(gw_push_bps, 1),
                },
                "tunnel": {
                    "rx": rx, "tx": tx,
                    "rx_bps": round(rx_bps, 1), "tx_bps": round(tx_bps, 1),
                    "up": len(tunnels) > 0,
                    "tunnels": sorted(tunnels, key=lambda t: t["name"]),
                },
            })
            # 富时间线样本:总量 + 每隧道 + 每(远程)worker 累计字节;供 /api/link-traffic/history → 按节点切片画趋势。
            by_worker = {
                wid: {"pull": pull_d["by_worker"].get(wid, 0), "push": push_d["by_worker"].get(wid, 0)}
                for wid in set(pull_d["by_worker"]) | set(push_d["by_worker"])
            }
            await redis.push_traffic_sample({
                "ts": round(now, 1),
                "gw": {"pull": pull, "push": push},
                "tun": {"rx": rx, "tx": tx},
                "t": {t["name"]: {"rx": t["rx"], "tx": t["tx"]} for t in tunnels},
                "w": by_worker,
            }, TIMELINE_CAP)
        except Exception:
            # 连接级异常:重连后下一轮再试,不让上报器死循环退出。
            try:
                await redis.reconnect()
            except Exception:
                pass
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
