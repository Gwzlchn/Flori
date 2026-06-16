"""Step 21: 播客智能笔记。AI 把口语转写重组为中文结构化笔记。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from shared.step_base import StepBase, file_hash

# 喂给 AI 的转写正文上限，防止超长
MAX_TRANSCRIPT_CHARS = 12000


class SmartPodcastStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "transcript.json").exists():
            return ["intermediate/transcript.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        hashes: dict[str, str] = {
            "transcript": file_hash(self.job_dir / "intermediate" / "transcript.json"),
        }
        prompt_path = prompts_dir / "04_smart_podcast.md"
        if prompt_path.exists():
            hashes["prompt"] = file_hash(prompt_path)
        profile_path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if profile_path.exists():
            hashes["profile"] = file_hash(profile_path)
        hashes["styles"] = json.dumps({
            tag: file_hash(prompts_dir / "styles" / f"{tag}.yaml")
            for tag in sorted(self.config.get("style_tags", []))
            if (prompts_dir / "styles" / f"{tag}.yaml").exists()
        }, sort_keys=True)
        return hashes

    def execute(self) -> dict | None:
        transcript = self.load_json("intermediate/transcript.json")

        prompt = self._build_prompt(transcript)
        result = self.call_ai(prompt)

        self.write_output("output/notes_smart.md", result)
        return {"chars": len(result)}

    def _build_prompt(self, transcript: dict) -> str:
        profile = self._load_profile()

        parts = [
            "请将以下播客/音频的口语转写重组为中文结构化学习笔记。\n",
            "要求：\n",
            "- 去除口语停顿、重复、语气词，提炼为精准书面表达\n",
            "- 净化中英混用，专业术语保留英文并括号附中文\n",
            "- 按逻辑主题组织章节，不必按口播时间线\n",
            "- 使用 Markdown 格式，包含 ## 章节标题\n",
        ]

        if profile and profile.get("terminology"):
            terms = "; ".join(profile["terminology"][:30])
            parts.append(f"\n术语参考：{terms}\n")

        parts.append(f"\n时长：约 {int(transcript.get('duration_sec', 0)) // 60} 分钟\n")

        full_text = transcript.get("full_text", "")
        if not full_text:
            full_text = "".join(s.get("text", "") for s in transcript.get("segments", []))

        parts.append("\n--- 转写正文 ---\n")
        parts.append(full_text[:MAX_TRANSCRIPT_CHARS])

        return "".join(parts)

    def _load_profile(self) -> dict:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {}


if __name__ == "__main__":
    SmartPodcastStep.cli_main("04_smart_podcast")
