"""feed.py against B站 responses and yt-dlp -J output saved under tests/fixtures/feed (no network).

rec/season/series/fav were fetched with the same query parameters feed.py sends (series with
sort=asc, newest first). fav_private is an anonymous answer for a private folder, owner fields
blanked. yt_channel comes from a yt-dlp recent enough that approximate_date fills timestamp.
"""

import contextlib
import copy
import email.message
import io
import json
import pathlib
import re
import subprocess
import sys
import unittest
import urllib.request
import urllib.response
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "feed"
sys.path.insert(0, str(ROOT / "steps"))

import feed  # noqa: E402

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeBili:
    """Answers each request with the next (status, payload) and records the URLs."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        status, payload = self.answers.pop(0)
        return status, json.dumps(payload).encode() if isinstance(payload, dict) else payload


class FakeYtdlp:
    def __init__(self, payload: dict | None, returncode: int = 0, stderr: str = "") -> None:
        self.payload, self.returncode, self.stderr = payload, returncode, stderr
        self.cmd: list[str] = []

    def __call__(self, cmd: list[str]):
        self.cmd = cmd
        stdout = json.dumps(self.payload) if self.payload is not None else ""
        return subprocess.CompletedProcess(cmd, self.returncode, stdout, self.stderr)


def run(kind: str, url: str, limit: int, get=None, ytdlp=None) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(feed.time, "sleep") as sleep:
        code = feed.main(["feed.py", "list", kind, url, "--limit", str(limit)], get=get, run=ytdlp)
    run.sleeps = [c.args[0] for c in sleep.call_args_list]
    return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()


class BiliTest(unittest.TestCase):
    def assert_items(self, listing: dict, platform: str = "bilibili") -> None:
        self.assertIsInstance(listing["title"], str)
        self.assertTrue(listing["title"])
        for item in listing["items"]:
            self.assertEqual(set(item), {"platform", "source_id", "title", "published", "duration_s"})
            self.assertEqual(item["platform"], platform)
            self.assertTrue(item["title"])
            self.assertTrue(item["published"] is None or DATE.fullmatch(item["published"]), item)
            self.assertTrue(item["duration_s"] is None or isinstance(item["duration_s"], int), item)

    def test_four_kinds_ask_newest_first_and_keep_that_order(self) -> None:
        cases = [
            ("bili_up", "https://space.bilibili.com/88461692", "rec.json",
             "/x/series/recArchivesByKeywords?mid=88461692&keywords=&ps=5&pn=1", "UP主 88461692 的投稿"),
            ("bili_season", "https://space.bilibili.com/1567748478/lists/32744?type=season", "season.json",
             "/x/polymer/web-space/seasons_archives_list?mid=1567748478&season_id=32744&sort_reverse=true&page_num=1&page_size=5",
             "【更新中】AI 论文精读"),
            ("bili_series", "https://space.bilibili.com/88461692/lists/1528929?type=series", "series.json",
             "/x/series/archives?mid=88461692&series_id=1528929&only_normal=true&sort=asc&pn=1&ps=5", "系列 1528929"),
        ]
        for kind, url, name, path, title in cases:
            with self.subTest(kind=kind):
                get = FakeBili((200, load(name)))
                code, listing, _ = run(kind, url, 5, get)
                self.assertEqual(code, 0)
                self.assertEqual(get.urls, [feed.API + path])
                self.assert_items(listing)
                self.assertEqual(listing["title"], title)
                self.assertEqual([i["source_id"] for i in listing["items"]], [a["bvid"] for a in load(name)["data"]["archives"]][:5])
                dates = [i["published"] for i in listing["items"]]
                self.assertEqual(dates, sorted(dates, reverse=True))

        get = FakeBili((200, load("fav.json")))
        code, listing, _ = run("bili_fav", "https://www.bilibili.com/medialist/detail/ml213003412", 5, get)
        self.assertEqual(code, 0)
        self.assertEqual(get.urls, [feed.API + "/x/v3/fav/resource/list?media_id=213003412&order=mtime&type=0&platform=web&pn=1&ps=5"])
        self.assert_items(listing)
        self.assertEqual(listing["title"], "游戏教学")
        self.assertEqual(listing["items"][0], {
            "platform": "bilibili", "source_id": "BV1oT4y1F7dX", "title": "从购买到上手——群星新手教学",
            "published": "2020-11-06", "duration_s": 1148,
        })

    def test_dead_entries_are_dropped_and_next_page_fills_the_limit(self) -> None:
        fav = load("fav.json")
        page1 = copy.deepcopy(fav)
        medias = page1["data"]["medias"]
        medias[1] = dict(medias[1], attr=9, title="已失效视频")
        medias[2] = dict(medias[2], type=12, bvid="")
        medias[3] = dict(medias[3], attr=1)
        page2 = copy.deepcopy(fav)
        get = FakeBili((200, page1), (200, page2))
        code, listing, _ = run("bili_fav", "https://www.bilibili.com/medialist/detail/ml213003412", 5, get)
        self.assertEqual(code, 0)
        ids = [i["source_id"] for i in listing["items"]]
        # Page 2 repeats two entries of page 1 (the folder shifted between requests); they appear once.
        self.assertEqual(ids, ["BV1oT4y1F7dX", "BV13W41147aT", "BV1Jy4y1y7pa", "BV1Ga4y1s75q", "BV1TE411W7GE"])
        self.assertTrue(get.urls[1].endswith("&pn=2&ps=5"))
        self.assertEqual(run.sleeps, [1])

        season = load("season.json")
        season["data"]["archives"][0]["state"] = -100
        code, listing, _ = run("bili_season", "https://space.bilibili.com/1567748478/lists/32744?type=season", 3, FakeBili((200, season)))
        self.assertEqual([i["source_id"] for i in listing["items"]], ["BV1U65SzuE4p", "BV1VdcxesEAt", "BV1c8HbeaEXi"])

    def test_short_page_stops_paging(self) -> None:
        get = FakeBili((200, load("series.json")))
        code, listing, _ = run("bili_series", "https://space.bilibili.com/88461692/lists/1528929?type=series", 30, get)
        self.assertEqual(code, 0)
        self.assertEqual(len(listing["items"]), 5)
        self.assertEqual(len(get.urls), 1)

    def test_risk_control_waits_5s_and_retries_once(self) -> None:
        url = "https://space.bilibili.com/88461692"
        code, listing, _ = run("bili_up", url, 5, FakeBili((412, b"<html>412</html>"), (200, load("rec.json"))))
        self.assertEqual(code, 0)
        self.assertEqual(len(listing["items"]), 5)
        self.assertEqual(run.sleeps, [5])

        blocked = {"code": -352, "message": "风控校验失败", "ttl": 1}
        code, listing, err = run("bili_up", url, 5, FakeBili((200, blocked), (200, blocked)))
        self.assertEqual(code, feed.RETRY)
        self.assertIsNone(listing)
        self.assertEqual(err.splitlines()[-1], "B站风控,稍后再试")

    def test_readable_failures(self) -> None:
        fav_url = "https://www.bilibili.com/medialist/detail/ml12345678"
        code, _, err = run("bili_fav", fav_url, 30, FakeBili((200, load("fav_private.json"))))
        self.assertEqual((code, err.splitlines()[-1]), (feed.PERMANENT, "收藏夹私密,需要 SESSDATA"))
        code, _, err = run("bili_season", "https://space.bilibili.com/1/lists/2?type=season", 30,
                           FakeBili((200, {"code": -404, "message": "啥都木有", "ttl": 1})))
        self.assertEqual((code, err.splitlines()[-1]), (feed.PERMANENT, "B站来源不存在或已删除"))
        code, _, err = run("bili_up", "https://space.bilibili.com/1", 30, FakeBili((502, b"bad gateway")))
        self.assertEqual((code, err.splitlines()[-1]), (feed.RETRY, "B站返回 HTTP 502"))

    def test_rejects_urls_that_are_not_the_normalized_form(self) -> None:
        for kind, url in (
            ("bili_up", "https://space.bilibili.com/88461692/upload/video"),
            ("bili_up", "https://www.bilibili.com/video/BV1HBnH6JEAZ"),
            ("bili_season", "https://space.bilibili.com/88461692/lists/1528929?type=series"),
            ("bili_fav", "https://space.bilibili.com/84912/favlist?fid=213003412"),
            ("yt_channel", "https://www.youtube.com/watch?v=LKrwhjOI8Nc"),
            ("yt_channel", "https://www.youtube.com/c/Vsauce"),
            ("yt_channel", "https://www.youtube.com/user/a/b/videos"),
            ("yt_playlist", "https://evil.example/playlist?list=PL1"),
        ):
            with self.subTest(url=url):
                get, ytdlp = FakeBili(), FakeYtdlp(None)
                code, _, err = run(kind, url, 30, get, ytdlp)
                self.assertEqual(code, feed.PERMANENT)
                self.assertIn("不支持的订阅地址", err)
                self.assertEqual((get.urls, ytdlp.cmd), ([], []))
        code, _, _ = run("bili_up", "https://space.bilibili.com/1", 101, FakeBili())
        self.assertEqual(code, feed.PERMANENT)

    def test_sessdata_goes_only_to_api_bilibili(self) -> None:
        sent = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def fake_open(request, timeout):
            sent.append((request.full_url, request.get_header("Cookie"), request.get_header("Referer")))
            return Response()

        with mock.patch.dict(feed.os.environ, {"FLORI_BILI_SESSDATA": "abc"}), mock.patch.object(feed.OPENER, "open", fake_open):
            feed.http_get("https://api.bilibili.com/x/v3/fav/resource/list?media_id=1")
            feed.http_get("https://www.bilibili.com/x")
            feed.http_get("https://api.bilibili.com.evil.example/x")
        with mock.patch.dict(feed.os.environ, {"FLORI_BILI_SESSDATA": ""}), mock.patch.object(feed.OPENER, "open", fake_open):
            feed.http_get("https://api.bilibili.com/x")
        self.assertEqual([cookie for _, cookie, _ in sent], ["SESSDATA=abc", None, None, None])
        self.assertTrue(all(referer == "https://space.bilibili.com/" for _, _, referer in sent))

    def test_sessdata_is_not_carried_across_a_redirect(self) -> None:
        seen = []

        class Stub(urllib.request.HTTPSHandler):
            handler_order = 100  # ahead of the real HTTPHandler for the http:// hop

            def https_open(self, request):
                seen.append((request.full_url, dict(request.header_items())))
                headers = email.message.Message()
                if request.host == "api.bilibili.com":
                    headers["Location"] = "http://evil.example/collect"
                    response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                else:
                    response = urllib.response.addinfourl(io.BytesIO(b"{}"), headers, request.full_url, 200)
                response.msg = "stub"
                return response

            http_open = https_open

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Stub())
        with mock.patch.dict(feed.os.environ, {"FLORI_BILI_SESSDATA": "abc"}), mock.patch.object(feed, "OPENER", opener):
            self.assertEqual(feed.http_get("https://api.bilibili.com/x/v3/fav/resource/list?media_id=1"), (200, b"{}"))
        self.assertEqual([url for url, _ in seen], ["https://api.bilibili.com/x/v3/fav/resource/list?media_id=1", "http://evil.example/collect"])
        self.assertEqual(seen[0][1].get("Cookie"), "SESSDATA=abc")
        self.assertNotIn("Cookie", seen[1][1])


class YoutubeTest(unittest.TestCase):
    def test_channel_and_playlist(self) -> None:
        channel = FakeYtdlp(load("yt_channel.json"))
        with mock.patch.dict(feed.os.environ, {"FLORI_PROXY": "http://proxy.example:3128"}):
            code, listing, _ = run("yt_channel", "https://www.youtube.com/@3blue1brown/videos", 5, ytdlp=channel)
        self.assertEqual(code, 0)
        self.assertEqual(channel.cmd, [
            "yt-dlp", "--flat-playlist", "--playlist-end", "5", "--extractor-args", "youtubetab:approximate_date", "-J",
            "--proxy", "http://proxy.example:3128", "--", "https://www.youtube.com/@3blue1brown/videos",
        ])
        BiliTest.assert_items(self, listing, "youtube")
        self.assertEqual(listing["title"], "3Blue1Brown - Videos")
        self.assertEqual(listing["items"][0], {
            "platform": "youtube", "source_id": "LKrwhjOI8Nc", "title": "There's never been a better time to go into math",
            "published": "2026-10-18", "duration_s": 2444,
        })
        dates = [i["published"] for i in listing["items"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

        payload = load("yt_playlist.json")
        payload["entries"].insert(1, {"_type": "url", "id": "AAAAAAAAAAA", "title": "[Private video]", "duration": None})
        payload["entries"].insert(2, {"_type": "url", "id": "BBBBBBBBBBB", "title": "Soon", "live_status": "is_upcoming"})
        payload["entries"].insert(3, dict(payload["entries"][0]))
        playlist = FakeYtdlp(payload)
        with mock.patch.dict(feed.os.environ, {"FLORI_PROXY": ""}):
            code, listing, _ = run("yt_playlist", "https://www.youtube.com/playlist?list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab", 3, ytdlp=playlist)
        self.assertEqual(code, 0)
        self.assertNotIn("--proxy", playlist.cmd)
        BiliTest.assert_items(self, listing, "youtube")
        self.assertEqual(listing["title"], "Essence of linear algebra")
        self.assertEqual([i["source_id"] for i in listing["items"]], ["fNk_zzaMoSs", "k7RM-ot2NWY", "kYB8IZa5AuE"])
        everything = run("yt_playlist", "https://www.youtube.com/playlist?list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab", 30, ytdlp=FakeYtdlp(payload))[1]
        ids = [i["source_id"] for i in everything["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [e["id"] for e in load("yt_playlist.json")["entries"]])
        self.assertEqual(listing["items"][0]["published"], None)

    def test_channel_url_forms_from_the_contract(self) -> None:
        for url in (
            "https://www.youtube.com/@3blue1brown/videos",
            "https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw/videos",
            "https://www.youtube.com/c/Vsauce/videos",
            "https://www.youtube.com/user/minutephysics/videos",
        ):
            with self.subTest(url=url):
                ytdlp = FakeYtdlp(load("yt_channel.json"))
                code, listing, _ = run("yt_channel", url, 5, ytdlp=ytdlp)
                self.assertEqual(code, 0)
                self.assertEqual(ytdlp.cmd[-2:], ["--", url])
                self.assertEqual(len(listing["items"]), 5)

    def test_ytdlp_failure_reports_its_error_line(self) -> None:
        stderr = "ERROR: [youtube:tab] @nobody: This channel does not exist.\nWARNING: update yt-dlp\n"
        code, listing, err = run("yt_channel", "https://www.youtube.com/@nobody/videos", 30, ytdlp=FakeYtdlp(None, 1, stderr))
        self.assertEqual(code, feed.RETRY)
        self.assertIsNone(listing)
        self.assertEqual(err.splitlines()[-1], "YouTube: ERROR: [youtube:tab] @nobody: This channel does not exist.")


class CliTest(unittest.TestCase):
    def test_isolated_script_rejects_unknown_url_without_network(self) -> None:
        run_ = subprocess.run(
            [sys.executable, "-I", str(ROOT / "steps/feed.py"), "list", "bili_up", "https://example.org/1", "--limit", "30"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(run_.returncode, feed.PERMANENT)
        self.assertEqual(run_.stdout, "")
        self.assertIn("不支持的订阅地址", run_.stderr.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
