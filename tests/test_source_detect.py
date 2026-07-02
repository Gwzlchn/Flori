"""tests for shared/source_detect.py"""

from shared.source_detect import (
    detect_source,
    extract_arxiv_id,
    extract_audio_enclosure,
    extract_bilibili_bvid,
)


class TestDetectSource:
    def test_bilibili_full_url(self):
        assert detect_source("https://www.bilibili.com/video/BV1xx411c7mD") == "bilibili"

    def test_bilibili_bvid_only(self):
        assert detect_source("BV1xx411c7mD") == "bilibili"

    def test_bilibili_short_url(self):
        assert detect_source("https://b23.tv/abc123") == "bilibili"

    def test_youtube_full(self):
        assert detect_source("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube"

    def test_youtube_short(self):
        assert detect_source("https://youtu.be/dQw4w9WgXcQ") == "youtube"

    def test_arxiv(self):
        assert detect_source("https://arxiv.org/abs/2301.00001") == "arxiv"

    def test_arxiv_pdf(self):
        assert detect_source("https://arxiv.org/pdf/2301.00001") == "arxiv"

    def test_direct_pdf_non_arxiv(self):
        # 非 arxiv 直链 PDF(OSDI/usenix 等)→ pdf 源,走论文流水线,下载存 source.pdf。
        assert detect_source("https://www.usenix.org/system/files/osdi23-li-zhuohan.pdf") == "pdf"
        assert detect_source("https://example.com/papers/foo.PDF?x=1") == "pdf"

    def test_empty_string(self):
        assert detect_source("") == "other"

    def test_http_article(self):
        assert detect_source("https://example.com/post/intro-to-rust") == "http_article"

    def test_http_article_no_path(self):
        assert detect_source("http://blog.example.org") == "http_article"

    def test_podcast_mp3(self):
        assert detect_source("https://cdn.example.com/ep/42.mp3") == "podcast"

    def test_podcast_m4a_with_query(self):
        assert detect_source("https://cdn.example.com/ep/42.m4a?token=abc") == "podcast"

    def test_podcast_wav(self):
        assert detect_source("https://cdn.example.com/clip.wav") == "podcast"

    def test_podcast_flac(self):
        assert detect_source("https://cdn.example.com/clip.flac") == "podcast"

    def test_non_http_unknown_stays_other(self):
        assert detect_source("ftp://example.com/file") == "other"

    def test_bare_identifier_stays_other(self):
        assert detect_source("just-some-text") == "other"

    def test_known_sources_unchanged(self):
        # 新判定不得改动既有来源识别。
        assert detect_source("https://www.bilibili.com/video/BV1xx411c7mD") == "bilibili"
        assert detect_source("https://youtu.be/dQw4w9WgXcQ") == "youtube"
        assert detect_source("https://arxiv.org/abs/2301.00001") == "arxiv"


class TestExtractBvid:
    def test_from_url(self):
        assert extract_bilibili_bvid("https://www.bilibili.com/video/BV1xx411c7mD") == "BV1xx411c7mD"

    def test_bare_bvid(self):
        assert extract_bilibili_bvid("BV1xx411c7mD") == "BV1xx411c7mD"

    def test_no_match(self):
        assert extract_bilibili_bvid("https://example.com") is None


class TestExtractArxivId:
    def test_abs_url(self):
        assert extract_arxiv_id("https://arxiv.org/abs/2301.00001") == "2301.00001"

    def test_pdf_url(self):
        assert extract_arxiv_id("https://arxiv.org/pdf/2301.00001") == "2301.00001"

    def test_no_match(self):
        assert extract_arxiv_id("https://example.com") is None

    def test_version_preserved(self):
        assert extract_arxiv_id("https://arxiv.org/abs/2301.00001v2") == "2301.00001v2"

    def test_bare_new_style_id(self):
        assert extract_arxiv_id("2301.00001") == "2301.00001"

    def test_old_style_url(self):
        assert extract_arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001"

    def test_bare_old_style_id(self):
        assert extract_arxiv_id("hep-th/9901001") == "hep-th/9901001"

    def test_old_style_with_subclass(self):
        assert extract_arxiv_id("arxiv.org/pdf/math.AG/0601001") == "math.AG/0601001"


class TestExtractAudioEnclosure:
    """从播客页面/RSS HTML 解析音频真链,供页面 URL 回退取音源。"""

    def test_og_audio(self):
        html = '<meta property="og:audio" content="https://cdn.x.com/ep.mp3">'
        assert extract_audio_enclosure(html) == "https://cdn.x.com/ep.mp3"

    def test_og_audio_content_first(self):
        # content 在 property 之前的写法也要命中。
        html = '<meta content="https://cdn.x.com/ep.m4a" property="og:audio:secure_url">'
        assert extract_audio_enclosure(html) == "https://cdn.x.com/ep.m4a"

    def test_audio_tag_src(self):
        html = '<audio controls src="https://cdn.x.com/show.mp3"></audio>'
        assert extract_audio_enclosure(html) == "https://cdn.x.com/show.mp3"

    def test_source_tag_with_query(self):
        html = '<audio><source src="https://cdn.x.com/a.aac?token=1" type="audio/aac"></audio>'
        assert extract_audio_enclosure(html) == "https://cdn.x.com/a.aac?token=1"

    def test_enclosure_tag(self):
        html = '<item><enclosure url="https://cdn.x.com/ep.wav" type="audio/wav"/></item>'
        assert extract_audio_enclosure(html) == "https://cdn.x.com/ep.wav"

    def test_bare_anchor_fallback(self):
        html = '<a href="https://cdn.x.com/download/ep42.mp3">下载</a>'
        assert extract_audio_enclosure(html) == "https://cdn.x.com/download/ep42.mp3"

    def test_relative_resolved_against_base(self):
        html = '<audio src="/media/ep.mp3"></audio>'
        got = extract_audio_enclosure(html, base_url="https://pod.example.com/show/1")
        assert got == "https://pod.example.com/media/ep.mp3"

    def test_og_audio_preferred_over_anchor(self):
        html = ('<meta property="og:audio" content="https://cdn.x.com/real.mp3">'
                '<a href="https://cdn.x.com/other.mp3">x</a>')
        assert extract_audio_enclosure(html) == "https://cdn.x.com/real.mp3"

    def test_no_audio_returns_none(self):
        html = '<html><body><p>付费墙,无音频</p><img src="cover.jpg"></body></html>'
        assert extract_audio_enclosure(html) is None

    def test_empty_returns_none(self):
        assert extract_audio_enclosure("") is None
