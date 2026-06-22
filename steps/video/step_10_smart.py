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

        # 从清单(dedup 保留帧 + ocr)取候选帧,N=清单 index(稳定),而非 glob 顺序——保证视觉 pass
        # 给 AI 看的序号与落盘回填的序号一致。限 10 张:多图时 claude Read-per-轮的上下文超线性膨胀
        # 会拖垮(实测 20 张 >18min)。
        frames = self._select_frames()[:10]

        # 两段式生成:① 视觉 pass——claude 带 Read 多轮看帧,只产"逐帧视觉描述"(按序号 N);
        # ② 文本 pass——用 机械稿 + 视觉描述 走纯文本单轮(--tools "")干净生成笔记,图片用
        # ![描述](img:N) 占位符,落盘时 write_smart_note 按清单回填成真实 assets/ 路径(AI 不碰路径)。
        frame_desc = ""
        if frames:
            imgs = [self.job_dir / "assets" / f["filename"] for f in frames]
            frame_desc = self.call_ai(self._build_vision_prompt(frames), images=imgs)

        result = self.call_ai(self._build_user_prompt(mechanical, frame_desc))

        rel = self.write_smart_note(result, image_assets=frames)  # 回填占位符 + 版本化落盘
        return {"chars": len(result), "images_sent": len(frames),
                "provider": self.last_ai_provider, "model": self.last_ai_model, "note_file": rel}

    def _select_frames(self) -> list[dict]:
        """从 dedup.json(保留帧)取候选并 join ocr.json 文本。返回 [{n,filename,ts,ocr}],n=清单 index。"""
        dd = self.job_dir / "intermediate" / "dedup.json"
        if not dd.exists():
            return []
        dedup = json.loads(dd.read_text(encoding="utf-8"))
        ocr_map: dict = {}
        oc = self.job_dir / "intermediate" / "ocr.json"
        if oc.exists():
            for o in json.loads(oc.read_text(encoding="utf-8")):
                ocr_map[o["index"]] = (o.get("text") or "").strip()
        out = []
        for d in dedup:
            if not d.get("keep"):
                continue
            if not (self.job_dir / "assets" / d["filename"]).exists():
                continue
            out.append({"n": d["index"], "filename": d["filename"],
                        "ts": d.get("timestamp_sec"), "ocr": ocr_map.get(d["index"], "")})
        return out

    def _build_vision_prompt(self, frames: list[dict]) -> str:
        """视觉 pass:让 claude 逐张看帧,只产结构化"逐帧视觉描述"清单(按序号 N),不写笔记正文。"""
        parts = [
            "请用 Read 工具逐张查看下列截图(每张前的 [N] 是它的序号)。为**有信息量**的截图各输出一行,"
            "格式:\n`N | 这张图 OCR 文本给不出的视觉信息(箭头指向、红框位置、K线/分时形态、"
            "放量特征、配色、版式等)`\n"
            "N 原样照抄方括号里的序号。纯氛围/装饰帧(空镜、背景板、片头片尾)直接跳过、不输出。\n"
            "只输出这个清单,**不要写任何笔记正文、不要总结、不要保存文件**。\n\n",
        ]
        for f in frames:
            parts.append(f"[{f['n']}] {(self.job_dir / 'assets' / f['filename']).resolve()}\n")
        return "".join(parts)

    def _build_user_prompt(self, mechanical: str, frame_desc: str = "") -> str:
        profile = self.load_domain_profile()
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
            # 视觉描述已由视觉 pass 文本化喂入(N | 视觉要点),本步纯文本生成、不读图。图片一律用
            # ![中文描述](img:N) 占位符引用(N=下表序号),**不要写文件名/路径**,落盘时按清单回填。
            parts.append(
                "\n以下是各截图的视觉信息(序号 N | 视觉要点)。请在笔记关键处用 "
                "![中文描述](img:N) 内嵌其中最有信息量的几张——括号里写 img:对应序号 这个占位符,"
                "**不要写文件名或路径**,描述要写出 OCR 给不出的视觉信息:\n"
                f"{frame_desc}\n"
            )
        parts.append(f"\n---\n\n{mechanical}")
        return "".join(parts)

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
