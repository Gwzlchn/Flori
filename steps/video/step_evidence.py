"""Step 10: 案例取证 / 权威来源,理由见 ADR-0012。

仅案例类(domain=finance 或 style_tags 含 case-study)触发。从机械稿 OCR 抽锚点:
文号/案号/当事人/股票。让 claude 域名限定搜权威源:证监会处罚决定书优先 csrc.gov.cn
一手,法院案优先裁判文书网/法院官网/上市公司公告。模型只提出候选 URL,
正文由受控下载器禁代理抓取并逐跳校验,写 v2 manifest 与稳定证据文件。

红线:一手优先;抓不到如实标 source_tier/confidence,绝不用二手新闻冒充一手。
"""

from __future__ import annotations

import json
from shared.evidence_contract import (
    MAX_EVIDENCE_ITEMS,
    MAX_MECHANICAL_EVIDENCE_BYTES,
    extract_case_refs,
    materialize_evidence,
)
from shared.step_base import StepBase, file_hash
from shared.storage import read_path_bounded

# 触发:案例类内容才取证(其余 pipeline/心法类自门控 skip,不污染)。
_CASE_DOMAINS = {"finance"}
_CASE_STYLE = "case-study"
_MECH_CLIP = 8000  # 喂给取证 prompt 的机械稿节选上限(锚点+案情段足够)
class EvidenceStep(StepBase):
    def _is_case(self) -> bool:
        domain = (self.config.get("domain") or {}).get("name", "")
        tags = self.config.get("style_tags") or []
        return domain in _CASE_DOMAINS or _CASE_STYLE in tags

    def validate_inputs(self) -> list[str]:
        if not self._is_case():
            return []  # 非案例类不取证:不要求输入,execute 自门控 skip
        if not (self.job_dir / "output" / "notes_mechanical.md").exists():
            return ["output/notes_mechanical.md"]
        return []

    def input_hashes(self) -> dict[str, str]:
        if not self._is_case():
            return {"skip": "non-case"}
        mech = self.job_dir / "output" / "notes_mechanical.md"
        # 指纹=机械稿(锚点来源)+provider+模板;锚点不变不重抓,省外网/省钱。
        h = {
            "mechanical": file_hash(mech) if mech.exists() else "",
            "provider": self.override_provider(),
        }
        t = self.template_hash("10_evidence")
        if t:
            h["template"] = t
        return h

    def _refs(self, mech: str) -> list[str]:
        return extract_case_refs(mech)

    def execute(self) -> dict | None:
        if not self._is_case():
            self.log.info("evidence_skip_non_case",
                          domain=(self.config.get("domain") or {}).get("name"))
            return {"skipped": "non-case"}

        mechanical_data = read_path_bounded(
            self.job_dir / "output" / "notes_mechanical.md",
            MAX_MECHANICAL_EVIDENCE_BYTES,
            trusted_root=self.job_dir,
        )
        if len(mechanical_data) > MAX_MECHANICAL_EVIDENCE_BYTES:
            raise ValueError("mechanical source is too large")
        try:
            mech = mechanical_data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("mechanical source is not UTF-8") from exc
        refs = self._refs(mech)
        raw = self.call_ai(self._build_prompt(refs, mech[:_MECH_CLIP]),
                           allowed_tools=["WebSearch"], max_turns=12)
        candidates, parse_failed = self._parse_candidates(raw)
        evidence = materialize_evidence(
            self.job_dir, self.job_dir.name, candidates, anchors=refs,
        )
        evidence["ocr_refs"] = refs
        evidence["candidate_parse_failed"] = parse_failed
        evidence["provider"] = self.last_ai_provider
        self.write_output("output/evidence.json", evidence)
        return {"evidence_count": len(evidence.get("evidence", [])),
                "eligible_count": sum(1 for x in evidence.get("evidence", []) if x.get("eligible")),
                "parse_failed": parse_failed,
                "refs": refs, "provider": self.last_ai_provider}

    def _build_prompt(self, refs: list[str], mech_clip: str) -> str:
        ref_hint = ("视频 OCR 里的处罚文号/案号：" + "、".join(refs)) if refs else "OCR 未显式给出文号/案号"
        # 用 replace 注入:prompt 含字面 {},不能用 str.format。
        tmpl = self._load_prompt_template("10_evidence")
        return tmpl.replace("<<REF_HINT>>", ref_hint).replace("<<MECH_CLIP>>", mech_clip)

    def _parse_candidates(self, raw: str) -> tuple[list[dict], bool]:
        try:
            if type(raw) is not str:
                raise ValueError("candidate response must be text")
            obj = json.loads(raw.strip())
            if not isinstance(obj, dict) or set(obj) != {"candidates"}:
                raise ValueError("invalid candidate envelope")
            candidates = obj.get("candidates")
            if not isinstance(candidates, list):
                raise ValueError("missing candidates")
            if len(candidates) > MAX_EVIDENCE_ITEMS:
                raise ValueError("too many candidates")
            fields = {"title", "url", "publisher", "reason"}
            clean = []
            for item in candidates:
                if (
                    not isinstance(item, dict)
                    or set(item) != fields
                    or any(
                        type(item.get(field)) is not str
                        or not item.get(field, "").strip()
                        for field in fields
                    )
                ):
                    raise ValueError("invalid candidate item")
                clean.append({field: item[field].strip() for field in fields})
            return clean, False
        except (ValueError, json.JSONDecodeError):
            return [], True


if __name__ == "__main__":
    EvidenceStep.cli_main("10_evidence")
