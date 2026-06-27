"""概念趋势雷达 + 本周摘要(api.services.radar + api/routes/radar)。

雷达:用受控时间的 job + 指向它们的 glossary occurrences,验证飙升/新出现/最近内容/最热的窗口切片。
摘要:注入 fake gateway(罐装 LLMResponse),不打真 LLM;断言 markdown 返回 + AIUsage 落库。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from shared.models import Job, LLMResponse


def _job(db, jid: str, when: datetime, *, domain="finance", title=None, ct="video"):
    """建一条带受控 published_at 的 job(雷达时间口径=COALESCE(published_at,created_at))。"""
    db.create_job(Job(
        id=jid, content_type=ct, pipeline=ct, domain=domain,
        title=title or jid, published_at=when, created_at=when, updated_at=when,
    ))


def _glossary(db, term: str, job_ids: list[str], *, domain="finance", definition=""):
    """直接插一条 glossary,occurrences 指向给定 job(每个 job 一条 occurrence)。"""
    occs = [{"job_id": j, "content_type": "video", "location": None} for j in job_ids]
    now = datetime.now(timezone.utc).isoformat()
    with db._lock:
        db._conn.execute(
            "INSERT INTO glossary (domain, term, definition, occurrences, related, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (domain, term, definition, json.dumps(occs), "[]", "accepted", now, now),
        )
        db._conn.commit()


def _seed_radar(db):
    """构造可判定的雷达场景(window=7d, now≈调用时)。
    时间锚点:
      recent 窗口 [now-7d, now);  prior 窗口 [now-14d, now-7d)
    概念:
      量化交易: recent=2 (d2,d5)  prior=1 (d10)            → 飙升 delta=1,且历史更早(d20)→非新
      高频量化: recent=1 (d3)     prior=0,且最早=d3        → 飙升 delta=1 + 新出现
      JEPQ:     recent=1 (d1)     最早=d1                   → 新出现(prior=0,无更早)
      宏观经济: recent=0 prior=2 (d9,d12)                   → 既不飙升也不新出现
    """
    now = datetime.now(timezone.utc)

    def d(days_ago):
        return now - timedelta(days=days_ago)

    # jobs(id 隐含其时间)
    _job(db, "r1", d(1), title="JEPQ 解读")          # recent
    _job(db, "r2", d(2), title="量化交易入门")        # recent
    _job(db, "r3", d(3), title="高频量化 vs 散户")    # recent
    _job(db, "r5", d(5), title="量化交易进阶")        # recent
    _job(db, "p9", d(9), title="宏观九")             # prior
    _job(db, "p10", d(10), title="量化十")           # prior
    _job(db, "p12", d(12), title="宏观十二")         # prior
    _job(db, "old20", d(20), title="量化老文")        # 更早(窗口外)

    _glossary(db, "量化交易", ["r2", "r5", "p10", "old20"])
    _glossary(db, "高频量化", ["r3"])
    _glossary(db, "JEPQ", ["r1"], definition="摩根大通主动型高股息 ETF")
    _glossary(db, "宏观经济", ["p9", "p12"])


# ── 服务层纯函数 ──

class TestRadarService:
    def test_rising_new_recent_top(self, db):
        from api.services.radar import radar
        _seed_radar(db)
        out = radar(db, "finance", window_days=7)

        rising = {c["term"]: c for c in out["rising_concepts"]}
        assert "量化交易" in rising and rising["量化交易"]["recent"] == 2 and rising["量化交易"]["prior"] == 1
        assert rising["量化交易"]["delta"] == 1
        assert "高频量化" in rising and rising["高频量化"]["delta"] == 1
        assert "宏观经济" not in rising  # recent=0 < prior=2

        new_terms = {c["term"] for c in out["new_concepts"]}
        assert "JEPQ" in new_terms and "高频量化" in new_terms
        assert "量化交易" not in new_terms  # 历史最早=20天前,不算新
        assert "宏观经济" not in new_terms
        jepq = next(c for c in out["new_concepts"] if c["term"] == "JEPQ")
        assert jepq["definition"] == "摩根大通主动型高股息 ETF" and jepq["first_seen"]

        recent_ids = {j["job_id"] for j in out["recent_jobs"]}
        assert recent_ids == {"r1", "r2", "r3", "r5"}  # 仅窗口内 4 篇

        top = {c["term"]: c["recent"] for c in out["top_recent_concepts"]}
        assert top["量化交易"] == 2 and top.get("宏观经济") is None  # recent=0 不入最热

        assert out["window"]["days"] == 7 and out["window"]["since"] < out["window"]["until"]

    def test_empty_domain(self, db):
        from api.services.radar import radar
        out = radar(db, "empty-domain", window_days=7)
        assert out["rising_concepts"] == [] and out["new_concepts"] == []
        assert out["recent_jobs"] == [] and out["top_recent_concepts"] == []

    def test_build_digest_prompt_chinese(self, db):
        from api.services.radar import build_digest_prompt, radar
        _seed_radar(db)
        out = radar(db, "finance", window_days=7)
        system, user = build_digest_prompt(out, ["JEPQ 解读", "量化交易入门"])
        assert "周报" in system or "知识库" in system
        assert "量化交易" in user and "JEPQ" in user
        assert "本周新增内容数: 4" in user


# ── 路由 ──

class TestRadarRoutes:
    @pytest.mark.asyncio
    async def test_get_radar(self, client, app):
        _seed_radar(app.state.db)
        r = await client.get("/api/domains/finance/radar?window_days=7")
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(c["term"] == "量化交易" for c in body["rising_concepts"])
        assert any(c["term"] == "JEPQ" for c in body["new_concepts"])
        assert len(body["recent_jobs"]) == 4
        assert body["window"]["days"] == 7

    @pytest.mark.asyncio
    async def test_get_radar_bad_window_422(self, client):
        assert (await client.get("/api/domains/finance/radar?window_days=0")).status_code == 422
        assert (await client.get("/api/domains/finance/radar?window_days=999")).status_code == 422

    @pytest.mark.asyncio
    async def test_post_digest_enqueues_task(self, client, app):
        _seed_radar(app.state.db)
        r = await client.post("/api/domains/finance/digest?window_days=7")
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["task_id"] and body["task_id"].startswith("at_")
        assert body["window"]["days"] == 7
        # 投了一个 digest AI task 进 queue:ai(claude 在 ai-worker 跑,API 不调);用量/审计在 worker 侧。
        redis = app.state.redis
        assert redis.enqueue_ai_task.await_count == 1
        payload = redis.enqueue_ai_task.await_args.args[0]
        assert payload["kind"] == "ai" and payload["step"] == "digest"
        assert payload["domain"] == "finance" and payload["require_tags"] == ["claude-cli"]
        assert payload["task_id"] == body["task_id"]
        # 雷达数据进了 prompt。
        assert "量化交易" in payload["request"]["messages"][0]["content"]
