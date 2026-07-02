"""取证步的测试:网关 allowed_tools 工具模式 + steps/video/step_evidence.py,见 ADR-0012。"""

import asyncio
import json

from shared.ai_gateway import ClaudeCLIProvider
from shared.models import LLMRequest
from steps.video.step_evidence import EvidenceStep
from tests.steps.conftest import make_step_config


# 网关工具模式(ClaudeCLIProvider 第三档)

class _FakeProc:
    returncode = 0

    async def communicate(self, data=None):
        return (b"web evidence result", b"")


def _patch_exec(monkeypatch, captured):
    async def _fake(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return _FakeProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)


class TestClaudeCLIToolsMode:
    async def test_allowed_tools_mode(self, monkeypatch):
        captured = {}
        _patch_exec(monkeypatch, captured)
        p = ClaudeCLIProvider(["claude", "-p", "--output-format", "text"])
        await p.complete(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            allowed_tools=["WebSearch", "Bash"], max_turns=20))
        cmd = captured["cmd"]
        assert "--allowedTools" in cmd
        assert "WebSearch" in cmd and "Bash" in cmd
        assert cmd[cmd.index("--max-turns") + 1] == "20"
        assert "--tools" not in cmd          # 不是禁工具档

    async def test_allowed_tools_default_max_turns(self, monkeypatch):
        captured = {}
        _patch_exec(monkeypatch, captured)
        p = ClaudeCLIProvider(["claude", "-p"])
        await p.complete(LLMRequest(
            messages=[{"role": "user", "content": "x"}], allowed_tools=["WebSearch"]))
        assert captured["cmd"][captured["cmd"].index("--max-turns") + 1] == "24"   # 默认 24

    async def test_no_tools_mode_unchanged(self, monkeypatch):
        captured = {}
        _patch_exec(monkeypatch, captured)
        p = ClaudeCLIProvider(["claude", "-p"])
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        cmd = captured["cmd"]
        assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
        assert cmd[cmd.index("--max-turns") + 1] == "1"
        assert "--allowedTools" not in cmd


# 取证步 EvidenceStep

_VALID_EV = (
    '{"case_match":{"subject":"马永威操纵","anchors":["马永威"],"confidence":"high","note":"一手"},'
    '"evidence":[{"id":"E1","type":"行政处罚决定","title":"t","url":"http://csrc.gov.cn/x",'
    '"publisher":"证监会","ref":"〔2018〕88号","source_tier":"一手官方","match_confidence":"high",'
    '"excerpt":"x","key_facts":[]}],"notes":"ok"}'
)


class TestEvidenceStep:
    def _job(self, tmp_path, mech="## 案例\n马永威〔2018〕88号 操纵宝鼎科技\n"):
        job = tmp_path / "job"
        job.mkdir()
        (job / "output").mkdir()
        (job / "output" / "notes_mechanical.md").write_text(mech, encoding="utf-8")
        return job

    def test_skip_non_case(self, tmp_path):
        # 默认 domain=general、无 case-study → 自门控 skip,不调 AI、不写 evidence.json
        job = self._job(tmp_path)
        cfg = make_step_config(tmp_path, step_name="10_evidence", pool="ai")
        step = EvidenceStep("10_evidence", job, cfg)
        called = []
        step.call_ai = lambda *a, **k: called.append(1) or "{}"
        assert step.execute() == {"skipped": "non-case"}
        assert not called
        assert not (job / "output" / "evidence.json").exists()
        assert step.input_hashes() == {"skip": "non-case"}

    def test_finance_triggers_and_writes(self, tmp_path):
        job = self._job(tmp_path)
        cfg = make_step_config(tmp_path, step_name="10_evidence", pool="ai")
        cfg["domain"] = {"name": "finance"}
        step = EvidenceStep("10_evidence", job, cfg)
        cap = {}

        def fake(prompt, **kw):
            cap["allowed_tools"] = kw.get("allowed_tools")
            cap["prompt"] = prompt
            return _VALID_EV
        step.call_ai = fake
        out = step.execute()
        assert cap["allowed_tools"] == ["WebSearch", "Bash"]   # 走工具模式
        assert "〔2018〕88号" in cap["prompt"]                   # OCR 锚点喂进 prompt
        assert out["evidence_count"] == 1 and out["confidence"] == "high"
        data = json.loads((job / "output" / "evidence.json").read_text(encoding="utf-8"))
        assert data["evidence"][0]["id"] == "E1"
        assert data["evidence"][0]["source_tier"] == "一手官方"
        assert data["schema_version"] == 1 and data["ocr_refs"] == ["〔2018〕88号"]

    def test_case_study_style_triggers(self, tmp_path):
        # 非 finance 但 style_tags 含 case-study → 同样触发
        job = self._job(tmp_path)
        cfg = make_step_config(tmp_path, step_name="10_evidence", pool="ai")
        cfg["style_tags"] = ["case-study"]
        step = EvidenceStep("10_evidence", job, cfg)
        step.call_ai = lambda *a, **k: '{"case_match":{"confidence":"low"},"evidence":[],"notes":""}'
        assert step.execute()["evidence_count"] == 0
        assert (job / "output" / "evidence.json").exists()

    def test_parse_failed(self, tmp_path):
        job = self._job(tmp_path)
        cfg = make_step_config(tmp_path, step_name="10_evidence", pool="ai")
        cfg["domain"] = {"name": "finance"}
        step = EvidenceStep("10_evidence", job, cfg)
        step.call_ai = lambda *a, **k: "这不是 JSON，只是一段闲聊"
        out = step.execute()
        assert out["parse_failed"] is True
        data = json.loads((job / "output" / "evidence.json").read_text(encoding="utf-8"))
        assert data["parse_failed"] is True and data["evidence"] == []

    def test_refs_regex(self, tmp_path):
        job = self._job(tmp_path, mech="马永威〔2018〕88号 又见 (2025)沪刑终60号 与 [2017]5号 末尾")
        cfg = make_step_config(tmp_path, step_name="10_evidence", pool="ai")
        cfg["domain"] = {"name": "finance"}
        step = EvidenceStep("10_evidence", job, cfg)
        refs = step._refs((job / "output" / "notes_mechanical.md").read_text(encoding="utf-8"))
        assert "〔2018〕88号" in refs
        assert any("沪刑终60号" in r for r in refs)
