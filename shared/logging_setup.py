"""统一日志配置:全栈(API / 调度器 / Worker / 步骤子进程)输出带 ISO 时间戳的
结构化 JSON,便于 Dozzle 等统一采集,避免各服务渲染格式不一致。"""

from __future__ import annotations

import structlog


def setup_logging() -> None:
    structlog.configure(
        processors=[
            # 合并 contextvars(bind_contextvars 绑定的字段进每条日志):worker 启动绑 worker_id/type/host/version
            # → 该进程后续所有日志自带身份,排障一眼知道是哪台、什么版本(见 worker.Worker.run)。
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
