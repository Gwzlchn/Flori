"""shared.net_zone:URL → 网络区域(net-cn / net-global)分类。
用临时 CN 表(monkeypatch _CN_LIST_PATH + 清 lru_cache)保证确定、不依赖镜像烤入的表。"""

import pytest

import shared.net_zone as nz


@pytest.fixture
def cn(tmp_path, monkeypatch):
    f = tmp_path / "cn.txt"
    f.write_text("# comment\nwallstreetcn.com\nbilibili.com\nbaidu.com\n")
    monkeypatch.setattr(nz, "_CN_LIST_PATH", str(f))
    nz._cn_domains.cache_clear()
    yield nz
    nz._cn_domains.cache_clear()


class TestUrlZone:
    def test_cn_domain_in_list(self, cn):
        assert cn.url_zone("https://wallstreetcn.com/articles/123") == "net-cn"
        assert cn.url_zone("https://www.wallstreetcn.com/x") == "net-cn"   # 子域剥取

    def test_cn_tld(self, cn):
        assert cn.url_zone("https://example.cn/x") == "net-cn"
        assert cn.url_zone("https://foo.com.cn/x") == "net-cn"

    def test_foreign_is_global(self, cn):
        assert cn.url_zone("https://semianalysis.com/2025/x") == "net-global"
        assert cn.url_zone("https://github.com/x") == "net-global"
        assert cn.url_zone("https://lilianweng.github.io/p") == "net-global"


class TestRequiredZone:
    def test_platform_source_authoritative(self, cn):
        # 平台源区域确定(优先于 host):B站短链/BV → net-cn;YouTube → net-global。
        assert cn.required_zone("bilibili", "https://b23.tv/x") == "net-cn"
        assert cn.required_zone("bilibili", "BV1xxxx") == "net-cn"       # 无 host 也按 source
        assert cn.required_zone("youtube", "https://youtu.be/x") == "net-global"

    def test_article_by_host(self, cn):
        assert cn.required_zone("http_article", "https://semianalysis.com/x") == "net-global"
        assert cn.required_zone("http_article", "https://wallstreetcn.com/x") == "net-cn"

    def test_no_host_defaults_global(self, cn):
        assert cn.required_zone("http_article", "") == "net-global"


class TestFallbackNoList:
    def test_missing_list_falls_back_to_tld(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nz, "_CN_LIST_PATH", str(tmp_path / "missing.txt"))
        nz._cn_domains.cache_clear()
        try:
            # 表缺失 → 仅 .cn TLD 判 cn;非 .cn 一律 global,即便本属 CN 的 wallstreetcn 也是。保守:不误派大陆。
            assert nz.url_zone("https://wallstreetcn.com/x") == "net-global"
            assert nz.url_zone("https://x.cn/y") == "net-cn"
        finally:
            nz._cn_domains.cache_clear()
