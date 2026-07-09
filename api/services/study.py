"""学习复习调度服务。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal


StudyGrade = Literal["again", "hard", "good", "easy"]

GRADE_LABELS: dict[str, str] = {
    "again": "重来",
    "hard": "困难",
    "good": "掌握",
    "easy": "简单",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def schedule_next_review(card: dict, grade: StudyGrade, reviewed_at: datetime | None = None) -> dict:
    """简化 SM-2:根据当前 review 状态和四档评分计算下一次 due。

    again 进入 10 分钟后重来;hard/good/easy 分别保守拉长间隔。ease 限制在
    1.3 到 3.0,避免少数评分把间隔推到不可用的极端值。
    """
    if grade not in GRADE_LABELS:
        raise ValueError("invalid grade")
    now = reviewed_at or now_utc()
    review = card.get("review") or {}
    current_interval = float(review.get("interval_days") or 0)
    ease = float(review.get("ease") or 2.5)
    repetitions = int(review.get("repetitions") or 0)
    lapses = int(review.get("lapses") or 0)

    if grade == "again":
        interval_days = 10 / 1440
        ease = max(1.3, ease - 0.2)
        repetitions = 0
        lapses += 1
    elif grade == "hard":
        interval_days = 1 if repetitions == 0 else max(1, current_interval * 1.2)
        ease = max(1.3, ease - 0.15)
        repetitions += 1
    elif grade == "good":
        if repetitions == 0:
            interval_days = 1
        elif repetitions == 1:
            interval_days = 3
        else:
            interval_days = max(1, current_interval * ease)
        repetitions += 1
    else:
        if repetitions == 0:
            interval_days = 3
        elif repetitions == 1:
            interval_days = 6
        else:
            interval_days = max(1, current_interval * ease * 1.3)
        ease = min(3.0, ease + 0.15)
        repetitions += 1

    due_at = now + timedelta(days=interval_days)
    return {
        "next_due_at": due_at.isoformat(),
        "interval_days": round(interval_days, 4),
        "ease": round(ease, 2),
        "repetitions": repetitions,
        "lapses": lapses,
        "reviewed_at": now.isoformat(),
    }
