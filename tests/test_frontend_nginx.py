"""验证前端静态服务器必须保留的浏览器契约。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pdfjs_worker_mjs_has_javascript_mime_override() -> None:
    config = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert r"location ~* \.mjs$" in config
    assert "default_type application/javascript;" in config
