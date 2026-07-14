"""步骤内细粒度进度上报组件。"""

from __future__ import annotations

import json
import time
from pathlib import Path


class StepProgressReporter:
    """保持 progress 文件字段、百分比和日志门的既有语义。"""

    def __init__(self, step_name: str, job_dir: Path, log):
        self.step_name = step_name
        self.job_dir = job_dir
        self.log = log

    def report(self, current: int, total: int, message: str = "") -> None:
        pct = round(100 * current / max(total, 1))
        path = self.job_dir / f".{self.step_name}.progress"
        path.write_text(json.dumps({
            "source": "step",
            "current": current,
            "total": total,
            "pct": pct,
            "message": message,
            "updated_at": time.time(),
        }))
        if pct % 10 == 0 or current == total:
            self.log.info("progress", current=current, total=total, pct=pct)
