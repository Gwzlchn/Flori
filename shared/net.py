"""出站抓取安全:拒绝指向内网/回环/保留地址的 URL,挡 SSRF。

文章/播客抓取的目标 URL 由用户提交,远程 worker 又常处可信内网,
故抓取前解析主机名、逐个 IP 校验,只放行公网 http(s)。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from shared.errors import InputInvalidError


def assert_public_url(url: str) -> None:
    """校验 url 为公网 http(s);scheme 非法或解析到私网/回环/链路本地/保留地址即拒。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise InputInvalidError(f"unsupported url scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise InputInvalidError("url missing host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise InputInvalidError(f"cannot resolve host: {host}") from e

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise InputInvalidError(f"refusing to fetch internal address: {ip}")
