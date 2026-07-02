"""Step 10: 案例取证 / 权威来源,理由见 ADR-0012。

仅案例类(domain=finance 或 style_tags 含 case-study)触发。从机械稿 OCR 抽锚点:
文号/案号/当事人/股票。让 claude 域名限定搜权威源:证监会处罚决定书优先 csrc.gov.cn
一手,法院案优先裁判文书网/法院官网/上市公司公告。再用直连 curl 抓正文 + 抽取:
中国政府/法院站走境外代理会失败,必须 env -u …PROXY 直连。按文号 case-match,
写 output/evidence.json。

红线:一手优先;抓不到如实标 source_tier/confidence,绝不用二手新闻冒充一手。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from shared.step_base import StepBase, file_hash

# 触发:案例类内容才取证(其余 pipeline/心法类自门控 skip,不污染)。
_CASE_DOMAINS = {"finance"}
_CASE_STYLE = "case-study"
_MECH_CLIP = 8000  # 喂给取证 prompt 的机械稿节选上限(锚点+案情段足够)
# OCR 文号/案号锚点:〔2018〕88号 / [2018]88号 / (2025)沪刑终60号 等。
_REF_RE = re.compile(r"[〔\[（(]\s*20\d{2}\s*[〕\]）)][^，。\s]{0,8}?\d{1,4}\s*号")


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
        return sorted({m.strip() for m in _REF_RE.findall(mech)})

    def execute(self) -> dict | None:
        if not self._is_case():
            self.log.info("evidence_skip_non_case",
                          domain=(self.config.get("domain") or {}).get("name"))
            return {"skipped": "non-case"}

        mech = (self.job_dir / "output" / "notes_mechanical.md").read_text(encoding="utf-8")
        refs = self._refs(mech)
        raw = self.call_ai(
            self._build_prompt(refs, mech[:_MECH_CLIP]),
            allowed_tools=["WebSearch", "Bash"], max_turns=24,
        )
        evidence = self._parse(raw, refs)
        self.write_output("output/evidence.json", evidence)
        cm = evidence.get("case_match", {})
        return {"evidence_count": len(evidence.get("evidence", [])),
                "confidence": cm.get("confidence"),
                "parse_failed": evidence.get("parse_failed", False),
                "refs": refs, "provider": self.last_ai_provider}

    def _build_prompt(self, refs: list[str], mech_clip: str) -> str:
        ref_hint = ("视频 OCR 里的处罚文号/案号：" + "、".join(refs)) if refs else "OCR 未显式给出文号/案号"
        # 模板外置 templates/10_evidence.md(含 JSON schema 字面量;改文件不碰代码,进指纹);缺失回退 _DEFAULT。
        # 用 replace 注入:prompt 含字面 {},不能用 str.format。
        tmpl = self._load_prompt_template("10_evidence", _DEFAULT)
        return tmpl.replace("<<REF_HINT>>", ref_hint).replace("<<MECH_CLIP>>", mech_clip)

    def _parse(self, raw: str, refs: list[str]) -> dict:
        try:
            obj = json.loads(self._extract_json(raw))
            if not isinstance(obj, dict) or "evidence" not in obj:
                raise ValueError("missing evidence key")
        except (ValueError, json.JSONDecodeError):
            obj = {
                "case_match": {"subject": "", "anchors": refs, "confidence": "low",
                               "note": "取证结果解析失败"},
                "evidence": [],
                "notes": "AI 返回非有效 JSON",
                "parse_failed": True,
                "raw_response": (raw or "")[:800],
            }
        obj.setdefault("schema_version", 1)
        obj["fetched_at"] = datetime.now().strftime("%Y-%m-%d")
        obj["ocr_refs"] = refs
        return obj


# 静态默认 prompt,内容与外置模板 templates/10_evidence.md 一致;<<REF_HINT>>/<<MECH_CLIP>> 由 replace 注入。
_DEFAULT = (
    "你是案例取证助手。为下面这条视频笔记取**一手权威来源**（证监会处罚决定书 / 法院裁定 / "
    "上市公司公告），不要用泛泛新闻分析冒充。\n\n"
    "<<REF_HINT>>\n\n"
    "任务：\n"
    "1) 从机械稿识别：当事人、涉及股票、处罚文号/案号、年份。\n"
    "2) 用 WebSearch 找一手——在查询里加 `site:csrc.gov.cn`（证监会案，省局子域亦可）或 "
    "`site:wenshu.court.gov.cn`（法院案）优先官方；法院一手常被登录墙挡，可退**上市公司公告**"
    "（《关于收到行政处罚/刑事裁定的公告》逐字转载）。可多次搜。\n"
    "3) 用 Bash curl 抓正文——中国政府/法院/交易所站点**必须直连不走代理**（走代理会失败）：\n"
    "   env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy "
    "curl -sL -m 25 -A \"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0\" \"<url>\"\n"
    "   （csrc 页多为 GBK，原样取字节即可。）\n"
    "4) 文号 case-match：抓回正文含上面 OCR 的文号/当事人→confidence=high；只对上当事人=medium；"
    "对不上或只找到二手新闻=low。\n\n"
    "只输出如下**扁平 JSON**（不要任何别的文字、不要代码围栏、字符串值内用「」不用半角双引号以免坏 JSON）：\n"
    '{"case_match":{"subject":"案件一句话","anchors":["命中锚点"],"confidence":"high|medium|low",'
    '"note":"一手命中/缺口说明"},'
    '"evidence":[{"id":"E1","type":"行政处罚决定|刑事裁定|公司公告|报道",'
    '"title":"标题","url":"真实URL","publisher":"发布方","ref":"文号/案号",'
    '"source_tier":"一手官方|上市公司公告|媒体逐字转载|二手新闻",'
    '"match_confidence":"high|medium|low","excerpt":"原文摘要(一句)",'
    '"key_facts":[{"figure":"金额/数字/事实","quote":"原文片段"}]}],'
    '"notes":"取证说明:抓到哪层、什么没抓到"}\n\n'
    "机械稿（节选）：\n<<MECH_CLIP>>"
)


if __name__ == "__main__":
    EvidenceStep.cli_main("10_evidence")
