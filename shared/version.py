"""构建版本单一事实源。

FLORI_VERSION 在构建/部署时经 env 注入(通常是 git short sha);未注入="dev"。
api / scheduler / worker 三处共用此常量,避免各自读 env 漂移(worker 历史在
worker.py:_worker_spec 直接读 env,沿用兼容,新代码统一 import 本常量)。
"""

from __future__ import annotations

import os

FLORI_VERSION: str = os.environ.get("FLORI_VERSION", "dev")
