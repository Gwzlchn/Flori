"""tests for shared/net.py — 出站抓取 SSRF 护栏。"""

from __future__ import annotations

import pytest

from shared.errors import InputInvalidError
from shared.net import assert_public_url


class TestAssertPublicUrl:
    @pytest.mark.parametrize("url", [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "gopher://1.1.1.1/",
        "//example.com/x",          # 无 scheme
    ])
    def test_rejects_non_http_scheme(self, url):
        with pytest.raises(InputInvalidError):
            assert_public_url(url)

    def test_rejects_missing_host(self):
        with pytest.raises(InputInvalidError):
            assert_public_url("http://")

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/x",                       # 回环
        "http://169.254.169.254/latest/meta-data/",  # 云元数据(链路本地)
        "http://10.0.0.5/x",                         # 私网
        "http://192.168.1.1/x",                      # 私网
        "http://172.16.0.1/x",                       # 私网
        "http://[::1]/x",                            # IPv6 回环
        "http://0.0.0.0/x",                          # unspecified
    ])
    def test_rejects_internal_addresses(self, url):
        with pytest.raises(InputInvalidError):
            assert_public_url(url)

    @pytest.mark.parametrize("url", [
        "http://1.1.1.1/x",
        "https://8.8.8.8/feed.xml",
    ])
    def test_allows_public_ip_literal(self, url):
        # IP 字面量无需 DNS;公网地址应放行(不抛)。
        assert_public_url(url)
