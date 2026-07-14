"""AI 结构化输出解析和既有分数抢救组件。"""

from __future__ import annotations

import json
import re


class StructuredOutputParser:
    """保持 JSON 抽取、fallback 和 score salvage 的既有行为。"""

    def __init__(self, log):
        self.log = log

    def parse(
        self, raw: str, fallback: dict, score_keys: list[str] | None = None,
    ) -> tuple[dict, bool, bool]:
        parse_failed = False
        did_salvage = False
        try:
            result = json.loads(self.extract_json(raw))
            if score_keys and isinstance(result.get("scores"), dict):
                result = {**result.pop("scores"), **result}
        except (json.JSONDecodeError, ValueError):
            salvaged = self.salvage_scores(raw, score_keys)
            if salvaged is not None:
                did_salvage = True
                result = {**fallback, **salvaged, "raw_response": raw[:500]}
                result.pop("overall", None)
            else:
                self.log.warn("ai_json_parse_failed", raw=raw[:200])
                result = {**fallback, "raw_response": raw[:500], "parse_failed": True}
                parse_failed = True
        if score_keys and "overall" not in result:
            scores = [result.get(key, 3) for key in score_keys]
            result["overall"] = round(sum(scores) / max(len(scores), 1), 1)
        return result, parse_failed, did_salvage

    @staticmethod
    def salvage_scores(raw: str, score_keys: list[str] | None) -> dict | None:
        if not score_keys:
            return None
        found: dict = {}
        for key in score_keys:
            match = re.search(rf'"{re.escape(key)}"\s*:\s*([1-5])\b', raw or "")
            if match:
                found[key] = int(match.group(1))
        if not found or len(found) * 2 < len(score_keys):
            return None
        if len(found) < len(score_keys):
            average = round(sum(found.values()) / len(found))
            for key in score_keys:
                found.setdefault(key, average)
        return found

    @staticmethod
    def extract_json(raw: str) -> str:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text).strip()
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                text = text[start:end + 1]
        return text
