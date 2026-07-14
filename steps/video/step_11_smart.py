"""Step 11: 智能版笔记。AI 将机械版素材重组为结构化笔记。"""

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
        hashes.update(self.prompt_profile_style_hashes())  # prompt(覆盖)+ 11_smart 模板 + profile + styles
        vt = self.template_hash("11_smart.vision")  # 视觉指令模板(11_smart.md 已由 prompt_profile_style_hashes 含)
        if vt:
            hashes["template_vision"] = vt
        # 取证产物纳入指纹(ADR-0012):evidence 更新→笔记重生成,因为正文引用 [E#]。非案例类无 evidence 则空。
        ev = self.job_dir / "output" / "evidence.json"
        hashes["evidence"] = file_hash(ev) if ev.exists() else ""
        # provider 覆盖纳入指纹:换 provider 重跑时指纹变化,绕过幂等跳过。
        hashes["provider"] = self.override_provider()
        return hashes

    def execute(self) -> dict | None:
        mechanical = (self.job_dir / "output" / "notes_mechanical.md").read_text(encoding="utf-8")

        # 从清单(dedup 保留帧 + ocr)取候选帧。N=清单 index 而非 glob 顺序,序号稳定,
        # 保证视觉 pass 给 AI 看的序号与落盘回填的序号一致。限 10 张:多图时 claude
        # Read-per-轮的上下文超线性膨胀会拖垮,实测 20 张 >18min。
        frames = self._select_frames()[:10]

        # 两段式生成:
        # 1. 视觉 pass:claude 带 Read 多轮看帧,只产"逐帧视觉描述",按序号 N。
        # 2. 文本 pass:用机械稿 + 视觉描述走纯文本单轮(--tools "")干净生成笔记。图片用
        #    ![描述](img:N) 占位符,落盘时 write_smart_note 按清单回填成真实 assets/ 路径,AI 不碰路径。
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
        # 视觉指令头外置 templates/11_smart.vision.md(改文件不碰代码,进指纹);缺失回退 _DEFAULT_VISION。
        parts = [self._load_prompt_template("11_smart.vision", _DEFAULT_VISION)]
        for f in frames:
            parts.append(f"[{f['n']}] {(self.job_dir / 'assets' / f['filename']).resolve()}\n")
        return "".join(parts)

    def _load_evidence(self) -> dict | None:
        """只返回当前 job 中完整且未篡改的 v2 eligible 证据。"""
        from shared.evidence_contract import MAX_EVIDENCE_BYTES, validate_manifest_loaded
        from shared.storage import read_path_bounded

        p = self.job_dir / "output" / "evidence.json"
        if not p.exists():
            return None
        try:
            data = read_path_bounded(
                p, MAX_EVIDENCE_BYTES, trusted_root=self.job_dir,
            )
            if len(data) > MAX_EVIDENCE_BYTES:
                return None
            manifest = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, OSError):
            return None
        valid, errors, texts = validate_manifest_loaded(
            self.job_dir, self.job_dir.name, manifest,
        )
        if errors:
            self.log.warning("evidence_rejected", errors=errors)
        if not valid:
            return None
        return {"schema_version": 2, "evidence": list(valid.values()), "texts": texts}

    def _evidence_block(self, ev: dict) -> str:
        """把取证来源转成可注入 prompt 的"权威来源"块,引导笔记用 [E#] 引用一手事实。"""
        lines = ["\n权威来源（取证所得，可在笔记中用 [E#] 角标引用对应来源；"
                 "引用的精确数据必须出自下列来源，不得引用列表外的精确数字）："]
        for item in ev.get("evidence", []):
            text = ev.get("texts", {}).get(item["id"], "")
            lines.append(f"\n[{item['id']}] {item.get('source_tier', '')} "
                         f"{item.get('title', '')}\n{text}")
        return "\n".join(lines) + "\n"

    def _build_user_prompt(self, mechanical: str, frame_desc: str = "") -> str:
        profile = self.load_domain_prompt_profile()
        style_hints = self._load_style_hints()

        # 用户指令头外置 templates/11_smart.md(经 prompt_profile_style_hashes 进指纹);缺失回退 _DEFAULT_USER_HEADER。
        parts = [self._load_prompt_template("11_smart", _DEFAULT_USER_HEADER)]

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
            parts.append(self.terminology_block(profile))  # 注入已沉淀的标准概念(与其他步共用)
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
            # ![中文描述](img:N) 占位符引用,N=下表序号,不写文件名/路径,落盘时按清单回填。
            parts.append(
                "\n以下是各截图的视觉信息(序号 N | 视觉要点)。请在笔记关键处用 "
                "![中文描述](img:N) 内嵌其中最有信息量的几张——括号里写 img:对应序号 这个占位符,"
                "**不要写文件名或路径**,描述要写出 OCR 给不出的视觉信息:\n"
                f"{frame_desc}\n"
            )
        ev = self._load_evidence()
        if ev and ev.get("evidence"):
            parts.append(self._evidence_block(ev))
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


# 静态指令头,内容与外置模板一致,缺模板时作回退。
# templates/11_smart.vision.md 对应视觉 pass,templates/11_smart.md 对应用户笔记 pass 头。
# 动态部分(profile/术语/风格/帧路径/取证/机械稿)仍在代码拼。
_DEFAULT_VISION = (
    "请用 Read 工具逐张查看下列截图(每张前的 [N] 是它的序号)。为**有信息量**的截图各输出一行,"
    "格式:\n`N | 这张图 OCR 文本给不出的视觉信息(箭头指向、红框位置、K线/分时形态、"
    "放量特征、配色、版式等)`\n"
    "N 原样照抄方括号里的序号。纯氛围/装饰帧(空镜、背景板、片头片尾)直接跳过、不输出。\n"
    "只输出这个清单,**不要写任何笔记正文、不要总结、不要保存文件**。\n\n"
)
_DEFAULT_USER_HEADER = "请将以下机械版笔记重组为结构化学习笔记。\n"


if __name__ == "__main__":
    SmartStep.cli_main("11_smart")
