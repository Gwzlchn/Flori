"""tests for steps/video/step_10_smart.py"""

import json
import os
import shutil
from pathlib import Path

import pytest

from steps.video.step_10_smart import SmartStep
from tests.steps.conftest import make_step_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_PROMPTS = _REPO_ROOT / "configs" / "prompts"


class TestSmartStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        (job_dir / "output" / "notes_mechanical.md").write_text("## 第 1 章\n\n内容\n")
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "output").mkdir()
        config = make_step_config(tmp_path, step_name="10_smart")
        step = SmartStep("10_smart", job_dir, config)
        assert step.validate_inputs() == ["output/notes_mechanical.md"]

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="10_smart", pool="ai")
        step = SmartStep("10_smart", job_dir, config)
        result = step.execute()
        assert result["chars"] > 0
        assert list((job_dir / "output" / "versions").glob("notes_smart_*.md"))

    def test_execute_two_pass_with_images(self, tmp_path):
        # 有截图时走两段:① 带图的视觉 pass(按序号 N 出描述) ② 不带图的纯文本 pass(成稿);
        # 帧从 dedup 清单(非 glob)按 index 选取,图片用 ![](img:N) 占位符,落盘回填成 assets/frame-NNNN.jpg。
        job_dir = self._setup_job(tmp_path)
        (job_dir / "assets" / "frame-0000.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpg")
        (job_dir / "intermediate" / "dedup.json").write_text(json.dumps(
            [{"index": 0, "filename": "frame-0000.jpg", "timestamp_sec": 1.7,
              "source": "scene", "keep": True}]))
        (job_dir / "intermediate" / "ocr.json").write_text(json.dumps(
            [{"index": 0, "filename": "frame-0000.jpg", "timestamp_sec": 1.7, "text": "分时跳水"}]))
        config = make_step_config(tmp_path, step_name="10_smart", pool="ai")
        step = SmartStep("10_smart", job_dir, config)
        calls = []
        note = ("# 力盛赛车案复盘\n\n![分时跳水图](img:0)\n\n"
                + "## 章节\n这是足够长的正文内容用于通过净化长度判废。\n" * 30)

        def fake_call_ai(prompt, images=None, **kw):
            calls.append({"has_images": bool(images), "prompt": prompt})
            return "0 | 红框圈住分时跳水" if images else note
        step.call_ai = fake_call_ai

        result = step.execute()
        assert len(calls) == 2
        assert calls[0]["has_images"] is True                  # ① 视觉 pass 带图
        assert "不要写任何笔记正文" in calls[0]["prompt"]        # 视觉 pass 只要描述
        assert "[0]" in calls[0]["prompt"]                     # 帧按序号 N 标注
        assert calls[1]["has_images"] is False                 # ② 文本 pass 不带图
        assert "红框圈住分时跳水" in calls[1]["prompt"]          # 视觉描述喂进文本 pass
        assert result["images_sent"] == 1
        # 落盘:占位符 img:0 已回填成真实 assets/ 路径,无裸占位符残留
        written = next((job_dir / "output" / "versions").glob("notes_smart_*.md")).read_text(encoding="utf-8")
        assert "](assets/frame-0000.jpg)" in written
        assert "img:0" not in written

    def test_backfill_image_refs(self):
        # ![](img:N) 占位符:命中 N 回填成 assets/<filename>;未命中(AI 编的/越界)整条删掉。
        out = SmartStep._backfill_image_refs(
            "![甲](img:0) 文字 ![乙](img:9) 尾 ![丙](img:1)",
            {0: "frame-0000.jpg", 1: "frame-0001.jpg"},
        )
        assert "![甲](assets/frame-0000.jpg)" in out
        assert "![丙](assets/frame-0001.jpg)" in out
        assert "img:" not in out          # 占位符全部消解
        assert "![乙]" not in out          # 未命中的 N 整条图片删除

    def test_execute_no_images_single_pass(self, tmp_path):
        # 无截图时只一段纯文本调用。
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="10_smart", pool="ai")
        step = SmartStep("10_smart", job_dir, config)
        calls = []
        note = "# 标题\n\n" + "## 章节\n足够长的正文内容用于通过净化判废。\n" * 30

        def fake_call_ai(prompt, images=None, **kw):
            calls.append(bool(images)); return note
        step.call_ai = fake_call_ai
        result = step.execute()
        assert calls == [False]                                # 仅一次、不带图
        assert result["images_sent"] == 0

    def test_input_hashes_includes_styles(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        (prompts_dir / "10_smart.md").write_text("system prompt")
        profiles_dir = prompts_dir / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "deep-learning.yaml").write_text("role: test\n")
        styles_dir = prompts_dir / "styles"
        styles_dir.mkdir()
        (styles_dir / "lecture.yaml").write_text("tag: lecture\nhints: ['test']\n")

        config = make_step_config(tmp_path, step_name="10_smart", pool="ai")
        config["domain"] = {"name": "deep-learning"}
        config["style_tags"] = ["lecture"]
        config["paths"]["prompts_dir"] = str(prompts_dir)

        step = SmartStep("10_smart", job_dir, config)
        hashes = step.input_hashes()
        assert "prompt" in hashes
        assert "profile" in hashes
        assert "styles" in hashes
        assert "lecture" in hashes["styles"]

    def test_build_prompt_with_profile(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        profiles_dir = prompts_dir / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "deep-learning.yaml").write_text(
            "role: 深度学习领域编辑\n"
            "domain_context: 模型架构\n"
            "terminology:\n  - '注意力: 加权聚合'\n"
            "do_not:\n  - '不要编造实验数据'\n"
        )

        config = make_step_config(tmp_path, step_name="10_smart")
        config["domain"] = {"name": "deep-learning"}
        config["paths"]["prompts_dir"] = str(prompts_dir)

        step = SmartStep("10_smart", job_dir, config)
        prompt = step._build_user_prompt("test content")
        assert "模型架构" in prompt
        assert "注意力" in prompt
        assert "不要编造实验数据" in prompt


class TestP1PromptAssets:
    """P1 (ADR-0010 Loop2)：新增 finance Profile + case-study「机制说明」hint，
    并验证『改 prompt 资产 → 指纹失效 → 重生成』链路（指纹只 hash 文件字节，不解析 YAML）。"""

    def _job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ("output", "intermediate", "assets"):
            (job_dir / d).mkdir()
        (job_dir / "output" / "notes_mechanical.md").write_text("## 案例\n\n内容\n", encoding="utf-8")
        return job_dir

    def test_finance_profile_parses_and_injects(self, tmp_path):
        # 真实 configs/prompts/profiles/finance.yaml：能解析 + domain_context / 数字口径要求注入 prompt
        job_dir = self._job(tmp_path)
        config = make_step_config(tmp_path, step_name="10_smart")
        config["domain"] = {"name": "finance"}
        config["paths"]["prompts_dir"] = str(_REAL_PROMPTS)
        step = SmartStep("10_smart", job_dir, config)

        profile = step.load_domain_profile()
        assert profile and profile.get("domain_context")          # 文件存在且解析
        prompt = step._build_user_prompt("正文")
        assert profile["domain_context"] in prompt
        assert "口径" in prompt                                    # do_not 的「数字自洽」注入

    def test_case_study_injects_mechanism_hint(self, tmp_path):
        # 真实 case-study.yaml：「机制说明」hint 注入 prompt（治「只列名词不解释」）
        job_dir = self._job(tmp_path)
        config = make_step_config(tmp_path, step_name="10_smart")
        config["style_tags"] = ["case-study"]
        config["paths"]["prompts_dir"] = str(_REAL_PROMPTS)
        step = SmartStep("10_smart", job_dir, config)

        prompt = step._build_user_prompt("正文")
        assert "机制说明" in prompt

    def test_editing_style_busts_fingerprint(self, tmp_path):
        # 链路核心：相同输入 → should_run False（幂等跳过）；改 prompt 资产 → should_run True（重生成）
        job_dir = self._job(tmp_path)
        prompts = tmp_path / "prompts"
        (prompts / "profiles").mkdir(parents=True)
        (prompts / "styles").mkdir(parents=True)
        shutil.copy(_REAL_PROMPTS / "profiles" / "finance.yaml", prompts / "profiles" / "finance.yaml")
        shutil.copy(_REAL_PROMPTS / "styles" / "case-study.yaml", prompts / "styles" / "case-study.yaml")
        config = make_step_config(tmp_path, step_name="10_smart")
        config["domain"] = {"name": "finance"}
        config["style_tags"] = ["case-study"]
        config["paths"]["prompts_dir"] = str(prompts)
        step = SmartStep("10_smart", job_dir, config)

        step.mark_done()                                          # 落 .done（当前指纹）
        assert step.should_run() is False                         # 输入未变 → 跳过

        sp = prompts / "styles" / "case-study.yaml"
        sp.write_text(sp.read_text(encoding="utf-8") + "\n# 编辑标记\n", encoding="utf-8")
        assert step.should_run() is True                          # 指纹失效 → 重生成

    def test_adding_profile_busts_fingerprint(self, tmp_path):
        # 从无到有新建 finance.yaml：prompt_profile_style_hashes 的 profile 键从缺失到出现 → 指纹变
        job_dir = self._job(tmp_path)
        prompts = tmp_path / "prompts"
        (prompts / "profiles").mkdir(parents=True)
        config = make_step_config(tmp_path, step_name="10_smart")
        config["domain"] = {"name": "finance"}
        config["paths"]["prompts_dir"] = str(prompts)
        step = SmartStep("10_smart", job_dir, config)

        assert "profile" not in step.prompt_profile_style_hashes()  # finance.yaml 尚不存在
        step.mark_done()
        assert step.should_run() is False
        shutil.copy(_REAL_PROMPTS / "profiles" / "finance.yaml", prompts / "profiles" / "finance.yaml")
        assert "profile" in step.prompt_profile_style_hashes()      # 文件出现
        assert step.should_run() is True                            # 指纹变 → 重生成
