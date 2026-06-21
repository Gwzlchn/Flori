"""Step 10: 智能版笔记。AI 将机械版素材重组为结构化笔记。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from shared.step_base import StepBase, file_hash


class SmartStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "output" / "notes_mechanical.md").exists():
            return ["output/notes_mechanical.md"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {
            "mechanical": file_hash(self.job_dir / "output" / "notes_mechanical.md"),
        }
        hashes.update(self.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        # provider 覆盖纳入指纹:换 provider 重跑时指纹变化,绕过幂等跳过。
        hashes["provider"] = self.override_provider()
        return hashes

    def execute(self) -> dict | None:
        mechanical = (self.job_dir / "output" / "notes_mechanical.md").read_text(encoding="utf-8")

        assets_dir = self.job_dir / "assets"
        # 限 10 张:多图时 claude Read-per-轮的上下文超线性膨胀会拖垮(实测 20 张 >18min)。
        images = (sorted(assets_dir.glob("*.jpg")) if assets_dir.exists() else [])[:10]

        # 两段式生成:① 视觉 pass——claude 带 Read 多轮看帧,只产"逐帧视觉描述"(产物当输入,
        # 其 agentic 口水无害);② 文本 pass——用 机械稿 + 视觉描述 走纯文本单轮(--tools "")干净
        # 生成笔记。把"看图"(必须 agentic)与"成稿"(必须单轮纯输出)解耦,正文不再被 agentic 跑偏/
        # 丢正文污染;落盘净化(_sanitize_smart_note)退居兜底而非主力。
        frame_desc = ""
        if images:
            frame_desc = self.call_ai(self._build_vision_prompt(images), images=images)

        result = self.call_ai(self._build_user_prompt(mechanical, frame_desc))

        rel = self.write_smart_note(result)   # 版本化落盘(含生成时间/方式/模型)
        return {"chars": len(result), "images_sent": len(images),
                "provider": self.last_ai_provider, "model": self.last_ai_model, "note_file": rel}

    def _build_vision_prompt(self, images: list[Path]) -> str:
        """视觉 pass:让 claude 逐张看帧,只产结构化"逐帧视觉描述"清单,不写笔记正文。"""
        parts = [
            "请用 Read 工具逐张查看下列截图,为**有信息量**的截图各输出一行,格式:\n"
            "`文件名 | 这张图 OCR 文本给不出的视觉信息(箭头指向、红框位置、K线/分时形态、"
            "放量特征、配色、版式等)`\n"
            "纯氛围/装饰帧(空镜、背景板、片头片尾)直接跳过、不输出。\n"
            "只输出这个清单,**不要写任何笔记正文、不要总结、不要保存文件**。\n\n",
        ]
        for p in images:
            parts.append(str(Path(p).resolve()) + "\n")
        return "".join(parts)

    def _build_user_prompt(self, mechanical: str, frame_desc: str = "") -> str:
        profile = self._load_profile()
        style_hints = self._load_style_hints()

        parts = ["请将以下机械版笔记重组为结构化学习笔记。\n"]

        if profile:
            if profile.get("role"):
                parts.append(f"\n你的角色：{profile['role']}\n")
            if profile.get("domain_context"):
                parts.append(f"领域背景：{profile['domain_context']}\n")
            if profile.get("output_style"):
                style = profile["output_style"]
                if isinstance(style, dict):
                    for k, v in style.items():
                        parts.append(f"- {k}：{v}\n")
            if profile.get("terminology"):
                terms = "; ".join(profile["terminology"][:30])
                # 回流(§1.8 ③)：注入本域已沉淀概念的标准定义,命中用统一措辞、不重复展开,
                # 只对未列出的新概念做首次解释——避免同概念每篇换一套说法。
                parts.append(
                    "\n本领域已沉淀的标准概念（命中时沿用统一措辞、无需重新展开解释；"
                    f"只对下列未涵盖的新概念做首次解释）：\n{terms}\n"
                )
            if profile.get("do_not"):
                parts.append("\n注意：\n")
                for item in profile["do_not"]:
                    parts.append(f"- {item}\n")

        if style_hints:
            parts.append("\n内容形式提示：\n")
            for hint in style_hints:
                for h in hint.get("hints", []):
                    parts.append(f"- {h}\n")
                if hint.get("screenshot_focus"):
                    parts.append(f"- 截图重点：{hint['screenshot_focus']}\n")

        if frame_desc.strip():
            # 视觉描述已由视觉 pass 文本化喂入,本步纯文本生成、不读图:按文件名内嵌即可。
            parts.append(
                "\n以下是各截图的视觉信息(文件名 | 视觉要点)。请在笔记关键处用 "
                "![中文描述](文件名) 内嵌其中最有信息量的几张,描述要写出 OCR 给不出的视觉信息:\n"
                f"{frame_desc}\n"
            )
        parts.append(f"\n---\n\n{mechanical}")
        return "".join(parts)

    def _load_profile(self) -> dict:
        return self.load_domain_profile()

    def _load_style_hints(self) -> list[dict]:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        hints = []
        for tag in self.config.get("style_tags", []):
            style_path = prompts_dir / "styles" / f"{tag}.yaml"
            if style_path.exists():
                data = yaml.safe_load(style_path.read_text(encoding="utf-8"))
                if data:
                    hints.append(data)
        return hints


if __name__ == "__main__":
    SmartStep.cli_main("10_smart")
