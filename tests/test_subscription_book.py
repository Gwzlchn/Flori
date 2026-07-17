"""book_toc 适配器和章序投递测试。"""

from __future__ import annotations

import pytest

from shared.subscriptions.book import parse_toc

_TOC = """<html><head><title>A First Course — Intro</title></head><body><nav>
<a class="reference internal" href="about.html">1. About</a>
<a class="reference internal" href="growth.html">2. Growth</a>
<a class="reference internal" href="growth.html#anchor">2.1 dup</a>
<a class="reference internal" href="https://github.com/x/y">GitHub</a>
<a class="reference internal" href="cycle.html">3. Cycles</a>
<a class="reference internal" href="genindex.html">Index</a>
</nav></body></html>"""


class TestParseToc:
    def test_orders_dedups_filters(self):
        title, items = parse_toc(_TOC, "https://book.example.org/intro.html", 5)
        assert title == "A First Course"
        assert [i.item_id for i in items] == ["about", "growth", "cycle"]  # 去锚点重复/外链/genindex
        assert items[0].url == "https://book.example.org/about.html"
        assert all(
            i.content_type == "document" and i.document_kind == "book_chapter"
            for i in items
        )

    def test_max_chapters(self):
        _, items = parse_toc(_TOC, "https://book.example.org/", 2)
        assert [i.item_id for i in items] == ["about", "growth"]


class TestBookChain:
    @pytest.mark.asyncio
    async def test_next_chapter_ordering_and_serialization(self, tmp_path):
        from tests.conftest import make_fakeredis
        from shared.book_chain import next_chapter_job
        from shared.db import Database
        from shared.models import Job

        from datetime import datetime, timezone
        db = Database(tmp_path / "t.db"); db.init_schema()
        redis = make_fakeredis()
        for i, jid in enumerate(["jobs_article_ch1", "jobs_article_ch2"]):
            db.create_job(Job(id=jid, content_type="document", document_kind="book_chapter",
                              pipeline="document",
                              domain="finance", collection_id="col_book_b",
                              created_at=datetime(2026, 7, 6, i, tzinfo=timezone.utc)))
        # 全部待投 → 返回最早章
        assert await next_chapter_job(db, redis, "col_book_b") == "jobs_article_ch1"
        # ch1 已初始化(在跑)→ 严格串行,不投
        await redis.set_step_status("jobs_article_ch1", "01_download", "running")
        assert await next_chapter_job(db, redis, "col_book_b") is None
        # ch1 终态 completed → 投 ch2
        db.update_job("jobs_article_ch1", status="done")
        assert await next_chapter_job(db, redis, "col_book_b") == "jobs_article_ch2"
        # 全部投过 → None
        await redis.set_step_status("jobs_article_ch2", "01_download", "ready")
        db.update_job("jobs_article_ch2", status="processing")
        assert await next_chapter_job(db, redis, "col_book_b") is None
