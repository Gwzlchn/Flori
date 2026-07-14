"""延迟解析 shared.db 上保留的测试和时钟注入 seam。"""

from __future__ import annotations

import importlib


class _DatabaseSeams:
    def __getattr__(self, name: str):
        return getattr(importlib.import_module("shared.db"), name)


db = _DatabaseSeams()
