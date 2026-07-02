"""概念趋势雷达 + 本周摘要(服务层纯函数:雷达计算 + 摘要 prompt 构建)。

雷达 = 比较最近 window_days 与紧邻其前的同长窗口,从该 domain 的 glossary occurrences
(经 job_id→源内容时间映射)算出:飙升概念 / 新出现概念 / 窗口内新增内容 / 窗口内最热概念。
时间口径与 db.concept_timeline / db.concept_occurrence_dates 一致(COALESCE(published_at,created_at))。

摘要 = 把雷达结果 + 最近内容标题拼成 prompt(build_digest_prompt),由 api/routes/radar.py
作为独立 AI task 投给 ai-worker 跑 claude。本模块只产 radar/build_digest_prompt,不调 gateway。
雷达是 GET,无 LLM,秒开;摘要是 POST,投 AI task;两者分离,见 api/routes/radar.py。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.db import Database


def _parse(iso: str | None) -> datetime | None:
    """解析 occurrence/job 时间串为 aware-UTC(naive 串补 UTC),解析失败返回 None。"""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def radar(db: Database, domain: str, window_days: int = 7) -> dict:
    """计算该 domain 的概念趋势雷达(对比最近 window_days 与紧邻其前的同长窗口)。

    边界约定(半开区间,避免某条恰落窗口边界被两侧重复计):
      recent = [since, until),until = now;since = now - window_days
      prior  = [prior_since, since),prior_since = now - 2*window_days

    返回 dict:
      rising_concepts: 最近窗口出现次数 > 前窗口 的概念 [{term,recent,prior,delta}],delta 降序
      new_concepts:    首次出现(最早 occurrence 时间)落在最近窗口的概念 [{term,definition,first_seen}]
      recent_jobs:     最近窗口入库/发布的内容 [{job_id,title,published_at,content_type}]
      top_recent_concepts: 最近窗口出现次数最多的概念 [{term,recent}]
      window: {days, since, until}(ISO)
    """
    days = max(1, int(window_days))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    prior_since = now - timedelta(days=2 * days)

    # 概念 → 各 occurrence 时间点(可重复)。一次取齐,纯内存切片,不在 SQL 里按窗口反复查。
    occ_dates = db.concept_occurrence_dates(domain)

    rising: list[dict] = []
    new_concepts: list[dict] = []
    top_recent: list[dict] = []

    # 概念定义(new_concepts 需要),按 term 建索引,避免 N 次单查。
    defs = {g["term"]: (g.get("definition") or "") for g in db.list_glossary(domain)}

    for term, raw_dates in occ_dates.items():
        dts = [d for d in (_parse(x) for x in raw_dates) if d is not None]
        if not dts:
            continue
        recent_n = sum(1 for d in dts if since <= d < now)
        prior_n = sum(1 for d in dts if prior_since <= d < since)
        first_seen = min(dts)

        if recent_n > prior_n:
            rising.append({
                "term": term, "recent": recent_n, "prior": prior_n,
                "delta": recent_n - prior_n,
            })
        # 新出现 = 该概念有史以来最早的一次 occurrence 就落在最近窗口内。
        if since <= first_seen < now:
            new_concepts.append({
                "term": term,
                "definition": defs.get(term, ""),
                "first_seen": first_seen.isoformat(),
            })
        if recent_n > 0:
            top_recent.append({"term": term, "recent": recent_n})

    rising.sort(key=lambda x: (x["delta"], x["recent"], x["term"]), reverse=True)
    new_concepts.sort(key=lambda x: x["first_seen"], reverse=True)
    top_recent.sort(key=lambda x: (x["recent"], x["term"]), reverse=True)

    # 最近窗口的内容(按时间口径 COALESCE(published_at,created_at) 落在窗口内)。
    # 拉一批近期 job 再按时间过滤:单领域一周入库量有限,limit 500 足够覆盖,无需新建专用 SQL。
    _, jobs = db.list_jobs(limit=500, domain=domain)
    recent_jobs: list[dict] = []
    for j in jobs:
        when = j.published_at or j.created_at
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when is not None and since <= when < now:
            recent_jobs.append({
                "job_id": j.id,
                "title": j.title,
                "published_at": when.isoformat(),
                "content_type": j.content_type,
            })
    recent_jobs.sort(key=lambda x: x["published_at"], reverse=True)

    return {
        "domain": domain,
        "rising_concepts": rising,
        "new_concepts": new_concepts,
        "recent_jobs": recent_jobs,
        "top_recent_concepts": top_recent[:10],
        "window": {
            "days": days,
            "since": since.isoformat(),
            "until": now.isoformat(),
        },
    }


def build_digest_prompt(
    radar_data: dict, recent_note_titles: list[str]
) -> tuple[str, str]:
    """构造本周摘要 prompt(system, user)。要求模型产出一段中文短文:本周知识源在聊什么、
    值得注意的新概念、最热的话题/张力。只用提供的数据,不编造,markdown 短段落。"""
    window = radar_data.get("window", {})
    rising = radar_data.get("rising_concepts", [])
    new_c = radar_data.get("new_concepts", [])
    top = radar_data.get("top_recent_concepts", [])
    jobs = radar_data.get("recent_jobs", [])

    system = (
        "你是用户个人知识库的策展助手。基于给定的「本周知识雷达」数据,写一段简洁、有洞察的"
        "中文周报。只用提供的数据,不要编造来源或概念。聚焦三点:① 本周这些知识源主要在聊什么;"
        "② 值得注意的新出现概念;③ 当下最热的话题或其中的张力/分歧。用自然短段落(markdown),"
        "不要罗列原始数字表格,控制在 4 段以内,语气平实专业。"
    )

    def _names(items: list[dict], key: str = "term", limit: int = 12) -> str:
        return "、".join(str(i.get(key, "")) for i in items[:limit]) or "(无)"

    lines = [
        f"时间窗口: 最近 {window.get('days', 7)} 天 ({window.get('since', '')} ~ {window.get('until', '')})",
        f"本周新增内容数: {len(jobs)}",
        f"飙升概念(出现增多): {_names(rising)}",
        f"新出现概念: {_names(new_c)}",
        f"最热概念(出现最多): {_names(top)}",
    ]
    if recent_note_titles:
        titles = "\n".join(f"- {t}" for t in recent_note_titles[:20] if t)
        lines.append("本周内容标题:\n" + titles)

    user = (
        "以下是本周知识雷达数据,请据此写本周摘要:\n\n" + "\n".join(lines)
    )
    return system, user
