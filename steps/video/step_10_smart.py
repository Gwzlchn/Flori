"""Step 08: 智能版笔记。AI 将机械版素材重组为结构化笔记。"""

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
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        hashes: dict[str, str] = {
            "mechanical": file_hash(self.job_dir / "output" / "notes_mechanical.md"),
        }
        prompt_path = prompts_dir / "10_smart.md"
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
        # provider 覆盖纳入指纹:换 provider 重跑时指纹变化,绕过幂等跳过。
        hashes["provider"] = self.override_provider()
        return hashes

    def execute(self) -> dict | None:
        mechanical = (self.job_dir / "output" / "notes_mechanical.md").read_text(encoding="utf-8")

        assets_dir = self.job_dir / "assets"
        images = sorted(assets_dir.glob("*.jpg")) if assets_dir.exists() else []

        prompt = self._build_user_prompt(mechanical)

        # 限 10 张:多图时 claude Read-per-轮的上下文超线性膨胀会拖垮(实测 20 张 >18min,10 张 ~1min)。
        # 10 张代表帧足够覆盖视觉要点。
        if images:
            result = self.call_ai(prompt, images=images[:10])
        else:
            result = self.call_ai(prompt)

        rel = self.write_smart_note(result)   # 版本化落盘(含生成时间/方式/模型),不再写 notes_smart.md
        return {"chars": len(result), "images_sent": min(len(images), 10),
                "provider": self.last_ai_provider, "model": self.last_ai_model, "note_file": rel}

    def _build_user_prompt(self, mechanical: str) -> str:
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
                parts.append(f"\n术语参考：{terms}\n")
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

        parts.append(
            "\n如附有截图路径(见下方),请用 Read 工具逐张查看,并在笔记关键处用 "
            "![中文描述](文件名) 内嵌最有信息量的几张(描述要写出图里 OCR 给不出的视觉信息)。\n"
        )
        parts.append(f"\n---\n\n{mechanical}")
        return "".join(parts)

    def _load_profile(self) -> dict:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        profile_path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if profile_path.exists():
            return yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        return {}

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
