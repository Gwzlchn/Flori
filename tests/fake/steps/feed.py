"""Fake feed.py for API tests. A URL containing "fail" exits 20 with a readable stderr line."""

import json
import sys


def main(argv: list[str]) -> int:
    kind, url, limit = argv[2], argv[3], int(argv[5])
    if "fail" in url:
        print("upstream refused the request", file=sys.stderr)
        return 20
    platform = "youtube" if kind.startswith("yt_") else "bilibili"
    ids = [f"BV1fake{n:05d}" if platform == "bilibili" else f"fakevideo{n:02d}" for n in range(limit)]
    items = [
        {"platform": platform, "source_id": i, "title": f"item {i}", "published": "2026-11-01", "duration_s": 60}
        for i in ids
    ]
    print(json.dumps({"title": f"fake {kind}", "items": items}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
