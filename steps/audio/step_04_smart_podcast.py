"""Step 04: 播客智能笔记。AI 把口语转写重组为中文结构化笔记。"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash

# 喂给 AI 的转写正文上限，防止超长
MAX_TRANSCRIPT_CHARS = 12000


class SmartPodcastStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "transcript.json").exists():
            return ["intermediate/transcript.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {
            "transcript": file_hash(self.job_dir / "intermediate" / "transcript.json"),
        }
        hashes.update(self.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        return hashes

    def execute(self) -> dict | None:
        transcript = self.load_json("intermediate/transcript.json")

        prompt = self._build_prompt(transcript)
        # 结构化中文笔记常超默认 4096 output tokens,显式抬高上限防被静默截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=8192)

        rel = self.write_smart_note(result)   # 版本化落盘(含生成时间/方式/模型),不再写 notes_smart.md
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel}

    def _build_prompt(self, transcript: dict) -> str:
        profile = self.load_domain_prompt_profile()

        parts = [
            "请将以下播客/音频的口语转写重组为中文结构化学习笔记。\n",
            "要求：\n",
            "- 去除口语停顿、重复、语气词，提炼为精准书面表达\n",
            "- 净化中英混用，专业术语保留英文并括号附中文\n",
            "- 按逻辑主题组织章节，不必按口播时间线\n",
            "- 使用 Markdown 格式，包含 ## 章节标题\n",
        ]

        parts.append(self.terminology_block(profile))  # 已沉淀标准概念注入(共用,审计 R-M9)

        parts.append(f"\n时长：约 {int(transcript.get('duration_sec', 0)) // 60} 分钟\n")

        full_text = transcript.get("full_text", "")
        if not full_text:
            full_text = "".join(s.get("text", "") for s in transcript.get("segments", []))

        parts.append("\n--- 转写正文 ---\n")
        parts.append(full_text[:MAX_TRANSCRIPT_CHARS])

        return "".join(parts)

if __name__ == "__main__":
    SmartPodcastStep.cli_main("04_smart_podcast")
