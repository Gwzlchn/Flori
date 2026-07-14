"""校验 tracked Prompt 模板清单并输出内容指纹."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from shared.prompt_resolver import TRACKED_TEMPLATE_NAMES

TEMPLATE_NAMES = TRACKED_TEMPLATE_NAMES


def template_manifest(root: Path = Path("configs/prompts/templates")) -> dict[str, dict]:
    """严格读取权威模板;生成脚本不再从步骤常量反向覆盖正文."""
    manifest: dict[str, dict] = {}
    expected = {f"{name}.md" for name in TEMPLATE_NAMES}
    actual = {path.name for path in root.glob("*.md")}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"prompt template inventory mismatch: missing={missing}, extra={extra}")
    for name in TEMPLATE_NAMES:
        path = root / f"{name}.md"
        raw = path.read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"prompt template is not UTF-8: {name}") from exc
        if not raw:
            raise ValueError(f"prompt template is empty: {name}")
        manifest[name] = {
            "bytes": len(raw),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
    return manifest


if __name__ == "__main__":
    print(json.dumps(template_manifest(), ensure_ascii=False, indent=2, sort_keys=True))
