"""tests for shared/notify.py(告警钩子:webhook best-effort,绝不抛)。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from shared.notify import notify


def test_no_webhook_only_logs(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    with patch("urllib.request.urlopen") as up:
        notify("step_stuck", "x", job_id="j1")
        up.assert_not_called()          # 未配 webhook → 不发网络请求


def test_webhook_posts_payload(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/x")
    with patch("urllib.request.urlopen") as up:
        up.return_value = MagicMock()
        notify("step_stuck", "job j1 卡死", job_id="j1", age_sec=120)
        up.assert_called_once()
        req = up.call_args.args[0]
        body = req.data.decode("utf-8")
        assert "step_stuck" in body and "j1" in body   # text/content 里带事件与字段


def test_webhook_failure_swallowed(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/x")
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        notify("step_stuck", "x")        # 不抛(best-effort);异常被吞
