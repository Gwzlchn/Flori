"""保留旧导入路径,SRS 真相源在 shared.study."""

from shared.study import GRADE_LABELS, StudyGrade, schedule_next_review, utc_now


now_utc = utc_now


__all__ = [
    "GRADE_LABELS",
    "StudyGrade",
    "now_utc",
    "schedule_next_review",
]
