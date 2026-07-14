"""步骤产物和生命周期文件的读写组件。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def file_hash(path: Path) -> str:
    """计算文件 SHA-256,返回 `sha256:{hex}`。"""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(8192), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class ArtifactIO:
    """保持步骤产物、done、meta 和 error 文件的既有字节契约。"""

    def __init__(self, step_name: str, job_dir: Path):
        self.step_name = step_name
        self.job_dir = job_dir

    @property
    def done_path(self) -> Path:
        return self.job_dir / f".{self.step_name}.done"

    def read_done(self) -> dict:
        return json.loads(self.done_path.read_text())

    def write_done(self, data: dict) -> None:
        self.done_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def write(self, filename: str, data) -> None:
        target = self.job_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        if isinstance(data, (dict, list)):
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        elif isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        elif isinstance(data, bytes):
            tmp.write_bytes(data)
        tmp.rename(target)

    def load_json(self, filename: str) -> dict | list:
        return json.loads((self.job_dir / filename).read_text(encoding="utf-8"))

    def write_meta(self, meta: dict) -> None:
        path = self.job_dir / f".{self.step_name}.meta.json"
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    def write_error(self, error_type: str, message: str, trace: str = "") -> None:
        path = self.job_dir / f".{self.step_name}.error.json"
        path.write_text(json.dumps({
            "step": self.step_name,
            "error_type": error_type,
            "message": message,
            "trace": trace,
            "timestamp": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2))

    def latest_smart_note(self) -> Path | None:
        """返回工作目录中最新的版本化智能笔记。"""
        from .notes_versions import latest_smart

        version_dir = self.job_dir / "output" / "versions"
        if not version_dir.is_dir():
            return None
        rels = [
            f"output/versions/{path.name}"
            for path in version_dir.glob("notes_smart_*.md")
        ]
        latest = latest_smart(rels)
        return self.job_dir / latest if latest else None
