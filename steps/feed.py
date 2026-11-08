"""Feed listing for a collection (contract 3 in docs/contracts.md).

Usage: python3 -I feed.py list <kind> <normalized_url> --limit N
Prints {"title": str, "items": [...]} on stdout; exit codes follow contract 1.
On failure the last stderr line is the reason shown to the user.
"""

import datetime
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY, PERMANENT = 10, 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
API = "https://api.bilibili.com"
CST = datetime.timezone(datetime.timedelta(hours=8))
# B站 requests never go through a proxy, whatever the environment says.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The normalized URL forms produced by the server (pipeline.rs), one per kind.
URLS = {
    "bili_up": r"https://space\.bilibili\.com/(?P<mid>\d+)",
    "bili_season": r"https://space\.bilibili\.com/(?P<mid>\d+)/lists/(?P<id>\d+)\?type=season",
    "bili_series": r"https://space\.bilibili\.com/(?P<mid>\d+)/lists/(?P<id>\d+)\?type=series",
    "bili_fav": r"https://www\.bilibili\.com/medialist/detail/ml(?P<id>\d+)",
    "yt_channel": r"https://www\.youtube\.com/(?:@[^/?#\s]+|channel/UC[\w-]{22})/videos",
    "yt_playlist": r"https://www\.youtube\.com/playlist\?list=[\w-]+",
}
# kind -> (path, largest page size). Every endpoint is asked for newest first.
PAGES = {
    "bili_up": ("/x/series/recArchivesByKeywords?mid={mid}&keywords=&ps={ps}&pn={pn}", 100),
    "bili_season": ("/x/polymer/web-space/seasons_archives_list?mid={mid}&season_id={id}&sort_reverse=true&page_num={pn}&page_size={ps}", 100),
    # sort=asc is newest first here: this endpoint names the order backwards.
    "bili_series": ("/x/series/archives?mid={mid}&series_id={id}&only_normal=true&sort=asc&pn={pn}&ps={ps}", 100),
    "bili_fav": ("/x/v3/fav/resource/list?media_id={id}&order=mtime&type=0&platform=web&pn={pn}&ps={ps}", 20),
}
YT_GONE = {"[Private video]", "[Deleted video]"}


class FeedError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def http_get(url: str) -> tuple[int, bytes]:
    headers = {"User-Agent": UA, "Referer": "https://space.bilibili.com/"}
    sessdata = os.environ.get("FLORI_BILI_SESSDATA", "")
    if sessdata and urllib.parse.urlsplit(url).hostname == "api.bilibili.com":
        headers["Cookie"] = f"SESSDATA={sessdata}"
    try:
        with OPENER.open(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (OSError, http.client.HTTPException) as err:
        raise FeedError(RETRY, f"连不上 B站: {err}") from None


def bili_data(path: str, get) -> dict:
    for wait in (5, None):
        status, body = get(API + path)
        payload = json.loads(body) if status == 200 else {}
        if status != 412 and payload.get("code") not in (-352, -412):
            break
        if wait is None:
            raise FeedError(RETRY, "B站风控,稍后再试")
        print(f"B站风控(HTTP {status} code {payload.get('code')}),{wait}s 后重试", file=sys.stderr, flush=True)
        time.sleep(wait)
    if status != 200:
        raise FeedError(RETRY if status >= 500 else PERMANENT, f"B站返回 HTTP {status}")
    if payload.get("code") == -404:
        raise FeedError(PERMANENT, "B站来源不存在或已删除")
    if payload.get("code") != 0:
        raise FeedError(PERMANENT, f"B站接口错误 {payload.get('code')}: {payload.get('message')}")
    return payload.get("data") or {}


def day(stamp) -> str | None:
    return datetime.datetime.fromtimestamp(stamp, CST).strftime("%Y-%m-%d") if stamp else None


def bili(kind: str, ids: dict, limit: int, get) -> tuple[str, list[dict]]:
    path, page_size = PAGES[kind]
    ps = min(limit, page_size)
    title, items = "", []
    for pn in range(1, 11):
        if pn > 1:
            time.sleep(1)  # the list endpoints rate-limit by IP
        data = bili_data(path.format(ps=ps, pn=pn, **ids), get)
        raw = data.get("archives") or data.get("medias") or []
        info = data.get("info") or {}
        if kind == "bili_fav" and pn == 1 and not raw and info.get("media_count"):
            raise FeedError(PERMANENT, "收藏夹私密,需要 SESSDATA")
        title = title or (data.get("meta") or {}).get("title") or info.get("title") or ""
        for entry in raw:
            gone = entry.get("state", 0) < 0 or entry.get("attr", 0) & 1 or entry.get("type", 2) != 2
            fresh = entry.get("bvid") and entry["bvid"] not in {i["source_id"] for i in items}
            if fresh and not gone and entry.get("title") != "已失效视频":
                items.append({
                    "platform": "bilibili", "source_id": entry["bvid"], "title": entry["title"],
                    "published": day(entry.get("pubdate") or entry.get("pubtime")),
                    "duration_s": entry.get("duration"),
                })
        if len(items) >= limit or len(raw) < ps:
            break
    fallback = {"bili_up": f"UP主 {ids.get('mid')} 的投稿", "bili_series": f"系列 {ids.get('id')}"}
    return title or fallback.get(kind, ""), items[:limit]


def run_ytdlp(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=50, check=False)
    except subprocess.TimeoutExpired:
        raise FeedError(RETRY, "YouTube 列表 50s 未返回,检查代理") from None
    except FileNotFoundError:
        raise FeedError(PERMANENT, "找不到 yt-dlp") from None


def youtube(url: str, limit: int, run) -> tuple[str, list[dict]]:
    cmd = ["yt-dlp", "--flat-playlist", "--playlist-end", str(limit),
           "--extractor-args", "youtubetab:approximate_date", "-J"]
    if os.environ.get("FLORI_PROXY"):
        cmd += ["--proxy", os.environ["FLORI_PROXY"]]
    done = run(cmd + ["--", url])
    if done.returncode != 0:
        lines = [line for line in done.stderr.splitlines() if line.startswith("ERROR:")] or done.stderr.splitlines()
        raise FeedError(RETRY, "YouTube: " + (lines[-1] if lines else f"yt-dlp 退出码 {done.returncode}"))
    data = json.loads(done.stdout)
    items = []
    for entry in data.get("entries") or []:
        if entry.get("title") in YT_GONE or entry.get("availability") in ("private", "needs_auth", "subscriber_only", "premium_only"):
            continue
        if entry.get("live_status") in ("is_live", "is_upcoming") or not entry.get("id"):
            continue
        date = entry.get("upload_date") or ""
        duration = entry.get("duration")
        items.append({
            "platform": "youtube", "source_id": entry["id"], "title": entry.get("title") or entry["id"],
            "published": f"{date[:4]}-{date[4:6]}-{date[6:]}" if re.fullmatch(r"\d{8}", date) else day(entry.get("timestamp")),
            "duration_s": round(duration) if duration is not None else None,
        })
    return data.get("title") or url, items[:limit]


def main(argv: list[str], get=http_get, run=run_ytdlp) -> int:
    if len(argv) != 6 or argv[1] != "list" or argv[2] not in URLS or argv[4] != "--limit" or not argv[5].isdigit():
        print("usage: feed.py list <kind> <normalized_url> --limit N", file=sys.stderr)
        return PERMANENT
    kind, url, limit = argv[2], argv[3], int(argv[5])
    try:
        match = re.fullmatch(URLS[kind], url)
        if match is None or not 1 <= limit <= 100:
            raise FeedError(PERMANENT, f"不支持的订阅地址或数量: {url} {limit}")
        title, items = youtube(url, limit, run) if kind.startswith("yt_") else bili(kind, match.groupdict(), limit, get)
    except FeedError as err:
        print(err, file=sys.stderr)
        return err.code
    except (ValueError, KeyError, TypeError, AttributeError) as err:
        print(f"上游返回了看不懂的数据: {type(err).__name__} {err}", file=sys.stderr)
        return PERMANENT
    print(json.dumps({"title": title, "items": items}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
