"""学习卡片的时间,调度和事务冲突语义."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Mapping


StudyGrade = Literal["again", "hard", "good", "easy"]
StudyStatus = Literal["suggested", "active", "suspended", "rejected"]
StudyFaultInjector = Callable[[str], None]

GRADE_LABELS: dict[str, str] = {
    "again": "重来",
    "hard": "困难",
    "good": "掌握",
    "easy": "简单",
}
STUDY_STATUSES = frozenset({"suggested", "active", "suspended", "rejected"})
MAX_SQLITE_INTEGER = (1 << 63) - 1
MAX_INTERVAL_DAYS = 36_500.0
MIN_EASE = 1.3
MAX_EASE = 3.0
AGAIN_DELAY_SECONDS = 600
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_DATETIME = datetime.max.replace(tzinfo=timezone.utc)


class StudyNotFoundError(LookupError):
    """学习卡片不存在."""

    code = "study_card_not_found"


class StudyConflictError(RuntimeError):
    """学习写入与当前状态,revision 或幂等记录冲突."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_aware_utc(value: datetime | str, field: str) -> datetime:
    """解析新写入的时间并归一到 UTC.naive 值必须拒绝."""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith(("Z", "z")):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{field} 必须是 ISO 8601 时间") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{field} 必须是 datetime 或 ISO 8601 字符串")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须带时区")
    return parsed.astimezone(timezone.utc)


def canonical_utc_iso(value: datetime | str, field: str = "time") -> str:
    return require_aware_utc(value, field).isoformat()


def datetime_to_epoch_us(value: datetime | str, field: str = "time") -> int:
    """用整数运算转 epoch 微秒,避免 float timestamp 丢精度."""
    parsed = require_aware_utc(value, field)
    delta = parsed - _EPOCH
    result = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if not -MAX_SQLITE_INTEGER - 1 <= result <= MAX_SQLITE_INTEGER:
        raise OverflowError(f"{field} 超出 SQLite INTEGER 范围")
    return result


def epoch_us_to_datetime(value: int) -> datetime:
    if type(value) is not int or not -MAX_SQLITE_INTEGER - 1 <= value <= MAX_SQLITE_INTEGER:
        raise ValueError("epoch 微秒必须在 SQLite INTEGER 范围内")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except OverflowError as exc:
        raise ValueError("epoch 微秒超出 datetime 范围") from exc


def review_request_fingerprint(
    *,
    card_id: str,
    grade: StudyGrade,
    response_ms: int | None,
    expected_revision: int,
) -> str:
    payload = json.dumps(
        {
            "card_id": card_id,
            "expected_revision": expected_revision,
            "grade": grade,
            "response_ms": response_ms,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_review_request(
    *,
    request_id: str,
    card_id: str,
    grade: str,
    response_ms: int | None,
    expected_revision: int,
) -> tuple[str, StudyGrade]:
    normalized_request_id = request_id.strip() if isinstance(request_id, str) else ""
    if not normalized_request_id or len(normalized_request_id) > 128:
        raise ValueError("request_id 必须是 1..128 字符的非空字符串")
    if not isinstance(card_id, str) or not card_id.strip():
        raise ValueError("card_id 不能为空")
    if grade not in GRADE_LABELS:
        raise ValueError("grade 必须是 again/hard/good/easy")
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_SQLITE_INTEGER:
        raise ValueError("expected_revision 必须是 SQLite 64 位正整数")
    if response_ms is not None and (
        type(response_ms) is not int or not 0 <= response_ms <= MAX_SQLITE_INTEGER
    ):
        raise ValueError("response_ms 必须是 SQLite 64 位非负整数")
    return normalized_request_id, grade  # type: ignore[return-value]


def schedule_next_review(
    card: Mapping[str, object],
    grade: StudyGrade,
    reviewed_at: datetime | str | None = None,
) -> dict:
    """按四档简化 SM-2 计算下次复习,间隔和 datetime 都安全截断."""
    if grade not in GRADE_LABELS:
        raise ValueError("invalid grade")
    now = require_aware_utc(reviewed_at or utc_now(), "reviewed_at")
    raw_review = card.get("review")
    review = raw_review if isinstance(raw_review, Mapping) else {}
    current_interval = max(0.0, float(review.get("interval_days") or 0))
    ease = min(MAX_EASE, max(MIN_EASE, float(review.get("ease") or 2.5)))
    repetitions = max(0, int(review.get("repetitions") or 0))
    lapses = max(0, int(review.get("lapses") or 0))

    if grade == "again":
        requested_interval = AGAIN_DELAY_SECONDS / 86_400
        ease = max(MIN_EASE, ease - 0.2)
        repetitions = 0
        lapses += 1
    elif grade == "hard":
        requested_interval = 1.0 if repetitions == 0 else max(1.0, current_interval * 1.2)
        ease = max(MIN_EASE, ease - 0.15)
        repetitions += 1
    elif grade == "good":
        if repetitions == 0:
            requested_interval = 1.0
        elif repetitions == 1:
            requested_interval = 3.0
        else:
            requested_interval = max(1.0, current_interval * ease)
        repetitions += 1
    else:
        if repetitions == 0:
            requested_interval = 3.0
        elif repetitions == 1:
            requested_interval = 6.0
        else:
            requested_interval = max(1.0, current_interval * ease * 1.3)
        ease = min(MAX_EASE, ease + 0.15)
        repetitions += 1

    bounded_interval = min(MAX_INTERVAL_DAYS, requested_interval)
    remaining = (_MAX_DATETIME - now).total_seconds() / 86_400
    actual_interval = max(0.0, min(bounded_interval, remaining))
    due_at = now + timedelta(days=actual_interval)
    if due_at > _MAX_DATETIME:
        due_at = _MAX_DATETIME
    return {
        "next_due_at": due_at.isoformat(),
        "next_due_at_epoch_us": datetime_to_epoch_us(due_at, "next_due_at"),
        "interval_days": actual_interval,
        "ease": round(ease, 2),
        "repetitions": repetitions,
        "lapses": lapses,
        "reviewed_at": now.isoformat(),
        "reviewed_at_epoch_us": datetime_to_epoch_us(now, "reviewed_at"),
    }
