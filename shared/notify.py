"""轻量告警钩子。设了 ALERT_WEBHOOK_URL 就把关键事件 POST 出去(Slack/Discord/通用 webhook
都能吃 {text}/{content} JSON),否则只 structlog。best-effort:超时 5s、吞所有异常,绝不反过来
拖垮主流程。个人工具不引 Prometheus/Alertmanager,这一层补齐"卡死/磁盘低只有 log.warning、
无主动通知"的缺口(审计 #26);指标侧另有 GET /api/metrics。"""

from __future__ import annotations

import json
import os
import urllib.request

import structlog

_log = structlog.get_logger(component="notify")


def notify(event: str, message: str, **fields) -> None:
    """发一条告警。同步、best-effort。在已有 log.warning 旁加一行即可;
    异步上下文用 `await asyncio.to_thread(notify, ...)` 避免阻塞事件循环。"""
    # 注意:structlog 的第一个位置参数即 "event",不能再传 event= kwarg(会冲突),故用 alert_event。
    _log.warning("alert", alert_event=event, msg=message, **fields)
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    text = f"[Mnemo] {event}: {message}" + (f" ({detail})" if detail else "")
    # 同时给 text(Slack)与 content(Discord)字段,通用 webhook 也能取到其一。
    payload = json.dumps({"text": text, "content": text}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:  # 告警失败绝不能反过来拖垮主流程
        _log.warning("alert_webhook_failed", error=str(e)[:200])
