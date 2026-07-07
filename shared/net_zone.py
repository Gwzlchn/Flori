"""URL 到网络可达区域(net-cn / net-global)的分类.

任务分发(scheduler enqueue 下载步)时调用,给下载步设 require_tag,把任务路由到
自报覆盖该区域的 worker(境外给香港或带代理 worker;大陆给大陆 worker).

CN 域名表在 Docker 构建时从 GitHub 上游(felixonmars/dnsmasq-china-list)拉取、烤进镜像,
见 docker/base.Dockerfile;运行时只读不拉,避开 NAS 对 github 的代理不稳。
表缺失或读失败时,回退仅按 .cn/.com.cn TLD 判 cn(保底:境外仍 net-global,不误派到大陆 worker).
"""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlparse

# 构建时烤入的 CN 可注册域集合(每行一个,如 wallstreetcn.com)。路径可经 env 覆盖(测试/自定义)。
_CN_LIST_PATH = os.environ.get("CN_DOMAINS_FILE", "/app/data/cn_domains.txt")

# 平台源的区域是确定的(与 CN 表无关):YouTube 必境外、B站必大陆。
_SOURCE_ZONE = {"youtube": "net-global", "bilibili": "net-cn"}


@lru_cache(maxsize=1)
def _cn_domains() -> frozenset:
    try:
        with open(_CN_LIST_PATH, encoding="utf-8") as f:
            return frozenset(
                ln.strip().lower() for ln in f
                if ln.strip() and not ln.lstrip().startswith("#")
            )
    except OSError:
        return frozenset()


def _host_is_cn(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if host.endswith(".cn") or host == "cn":
        return True
    cn = _cn_domains()
    if not cn:
        return False
    # 逐级剥子域查可注册域:www.x.com,x.com,com.
    parts = host.split(".")
    return any(".".join(parts[i:]) in cn for i in range(len(parts) - 1))


def url_zone(url: str) -> str:
    """按 URL host 判区域:命中 CN 表或 .cn 返回 'net-cn',否则 'net-global'."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    return "net-cn" if _host_is_cn(host) else "net-global"


def required_zone(source: str, url: str) -> str:
    """下载该内容需要的网络区域 tag。平台源(bilibili/youtube)区域确定、最权威(B站短链 b23.tv、
    裸 BV 号都算 net-cn);其余按 URL host(CN 表或 .cn 为 net-cn,否则 net-global)."""
    if source in _SOURCE_ZONE:
        return _SOURCE_ZONE[source]
    try:
        host = urlparse(url or "").hostname or ""
    except Exception:
        host = ""
    if not host:
        return "net-global"
    return url_zone(url)
