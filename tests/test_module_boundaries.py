"""模块边界守门:api 层只能依赖 shared/ 契约层,不得 import steps/scheduler/worker。

api 层是 HTTP 边界,steps / scheduler / worker 是独立部署单元,经 Redis + 文件 + HTTP 通信。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_DIR = REPO_ROOT / "api"

_FORBIDDEN = re.compile(r"^\s*(?:from|import)\s+(steps|scheduler|worker)\b", re.MULTILINE)


def test_api_does_not_import_engine_modules():
    offenders = []
    for py in sorted(API_DIR.rglob("*.py")):
        for m in _FORBIDDEN.finditer(py.read_text(encoding="utf-8")):
            offenders.append(f"{py.relative_to(REPO_ROOT)}: {m.group(0).strip()}")
    assert not offenders, (
        "api 层不得 import steps/scheduler/worker:\n"
        + "\n".join(offenders)
    )
