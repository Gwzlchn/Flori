"""Fake feed.py for API tests. Like the real one it accepts only the normal URL forms of contract 3
(whole-string match) and exits 20 otherwise. A URL containing "fail" exits 20 with a readable
stderr line; "slow" sleeps 1.5s first. The title lists the environment variable names it received
and the value of FLORI_VIDEO_PROXY."""

import json
import os
import re
import sys
import time

URLS = {
    "bili_up": r"https://space\.bilibili\.com/[0-9]+",
    "bili_season": r"https://space\.bilibili\.com/[0-9]+/lists/[0-9]+\?type=season",
    "bili_series": r"https://space\.bilibili\.com/[0-9]+/lists/[0-9]+\?type=series",
    "bili_fav": r"https://www\.bilibili\.com/medialist/detail/ml[0-9]+",
    "yt_channel": r"https://www\.youtube\.com/(?:@|(?:channel|c|user)/)[A-Za-z0-9._%-]+/videos",
    "yt_playlist": r"https://www\.youtube\.com/playlist\?list=[A-Za-z0-9_-]+",
}


def main(argv: list[str]) -> int:
    kind, url, limit = argv[2], argv[3], int(argv[5])
    if kind not in URLS or not re.fullmatch(URLS[kind], url):
        print(f"unsupported feed URL for {kind}: {url}", file=sys.stderr)
        return 20
    if "slow" in url:
        time.sleep(1.5)
    if "fail" in url:
        print("progress noise", file=sys.stderr)
        print("upstream refused the request", file=sys.stderr)
        return 20
    platform = "youtube" if kind.startswith("yt_") else "bilibili"
    ids = [f"BV1fake{n:05d}" if platform == "bilibili" else f"fakevideo{n:02d}" for n in range(limit)]
    items = [
        {"platform": platform, "source_id": i, "title": f"item {i}", "published": "2026-11-01", "duration_s": 60}
        for i in ids
    ]
    env = ",".join(sorted(os.environ))
    proxy = os.environ.get("FLORI_VIDEO_PROXY")
    print(json.dumps({"title": f"fake {kind} env={env} proxy={proxy}", "items": items}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
