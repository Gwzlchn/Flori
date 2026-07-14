"""解析任务固化覆盖,运行时热编辑和镜像内 Prompt 模板."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import InputInvalidError


_PROMPT_NAME = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_-]+)*$")

TRACKED_TEMPLATE_NAMES = (
    "04_translate_article",
    "04_smart_article",
    "05_concepts",
    "04_translate_paper",
    "04_translate_paper.pdf",
    "05_smart_paper",
    "04_smart_podcast",
    "08_punctuate.zh",
    "08_punctuate.translate",
    "11_smart.vision",
    "11_smart",
    "10_evidence",
    "05_review",
    "06_review",
    "12_review",
)


class PromptResolutionError(InputInvalidError):
    """Prompt 来源存在但内容无效或全部来源缺失时确定性失败."""


@dataclass(frozen=True)
class ResolvedPrompt:
    """保留解析所得原始字节,避免展示,指纹和执行各自重读."""

    name: str
    raw: bytes
    text: str
    sha256: str
    source: str
    version: int | None = None
    path: str | None = None


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _decode(raw: bytes, *, source: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptResolutionError(f"prompt {source} is not UTF-8") from exc
    if not text:
        raise PromptResolutionError(f"prompt {source} is empty")
    return text


def parse_prompt_override(
    prompt_overrides: Any, step_name: str,
) -> tuple[bytes, int | None] | None:
    if prompt_overrides is None:
        return None
    if not isinstance(prompt_overrides, dict):
        raise PromptResolutionError("prompt override map is invalid")
    if step_name not in prompt_overrides:
        return None
    value = prompt_overrides[step_name]
    if isinstance(value, str):
        if not value:
            raise PromptResolutionError("prompt override content is empty")
        return value.encode("utf-8"), None
    if not isinstance(value, dict) or set(value) != {"content", "version"}:
        raise PromptResolutionError("prompt override shape is invalid")
    content = value.get("content")
    version = value.get("version")
    if not isinstance(content, str) or not content:
        raise PromptResolutionError("prompt override content is invalid")
    if type(version) is not int or not 1 <= version < (1 << 63):
        raise PromptResolutionError("prompt override version is invalid")
    return content.encode("utf-8"), version


class PromptResolver:
    """按 job override,热编辑,镜像模板顺序返回同一份字节快照."""

    def __init__(self, *, hot_dir: Path, image_dir: Path):
        self.hot_dir = Path(hot_dir)
        self.image_dir = Path(image_dir)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not _PROMPT_NAME.fullmatch(name):
            raise PromptResolutionError("prompt template name is invalid")

    @staticmethod
    def _read(path: Path, *, source: str) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PromptResolutionError(f"prompt {source} is unreadable") from exc

    def template_exists(self, name: str) -> bool:
        for directory in (self.hot_dir, self.image_dir):
            path = directory / f"{name}.md"
            try:
                path.stat()
                return True
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PromptResolutionError("prompt template is unreadable") from exc
        return False

    def _override_targets(self, name: str, primary_template: str) -> bool:
        if name == primary_template:
            return True
        if name.startswith(primary_template + "."):
            return not self.template_exists(primary_template)
        return False

    def resolve(
        self,
        name: str,
        *,
        step_name: str,
        prompt_overrides: Any = None,
        primary_template: str | None = None,
    ) -> ResolvedPrompt:
        """只把 ENOENT 当作可回退,已存在但损坏的高优先级来源直接失败."""
        self._validate_name(name)
        self._validate_name(step_name)
        primary = primary_template or step_name
        self._validate_name(primary)

        override = parse_prompt_override(prompt_overrides, step_name)
        if override is not None and self._override_targets(name, primary):
            raw, version = override
            return ResolvedPrompt(
                name=name,
                raw=raw,
                text=_decode(raw, source="override"),
                sha256=_digest(raw),
                source="override",
                version=version,
            )

        for source, directory in (("hot", self.hot_dir), ("image", self.image_dir)):
            path = directory / f"{name}.md"
            raw = self._read(path, source=source)
            if raw is None:
                continue
            return ResolvedPrompt(
                name=name,
                raw=raw,
                text=_decode(raw, source=source),
                sha256=_digest(raw),
                source=source,
                path=str(path),
            )
        raise PromptResolutionError(f"prompt template missing: {name}")
