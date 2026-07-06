"""翻译术语一致性:分层 TermMap 的命中/注入/回收纯函数(工单 2026-07-06/04)。

TermMap = {english_term: 中文译名}。分层(L3 篇内 > L2 集合 > L1 领域)由调用方合并,
本模块只提供纯函数,零外部依赖——worker(翻译步)与 scheduler(导出/回流)共用:
  - hit_terms:       对将翻译的文本命中术语(词边界/大小写/复数所有格归一),频次降序限量。
  - render_term_block: 命中结果 → prompt 术语段(空命中返回空串,prompt 无痕)。
  - extract_pairs:   从译文回收「中文(English)」双语对照(保守正则,宁缺勿滥)。
  - zh_name_from_glossary_row: L1 导出时从 glossary 行提炼 (en, zh)(P1a 规则,提不出返 None)。
"""

from __future__ import annotations

import re

# 注入上限:命中过滤后仍超此数则按频次取前 N,控 prompt token(工单 §6)。
TERM_LIMIT = 40

# 「中文(English)」对照:全角括号;zh=1-24 个汉字(含·);en=字母开头词组(2-60 字符)。
# 保守三重限制(错收会教坏后续 chunk,宁缺勿滥):不匹配半角括号(代码/公式常用);
# 括号内不得含中文;★zh 左边界必须是非汉字(行首/标点/空格)——中文行文里「引入鞅(martingale)」
# 无法机器判定译名是「鞅」还是「入鞅」,界不清即放弃(漏收只少注入,不出错)。
_PAIR_RE = re.compile(
    r"(?<![一-鿿A-Za-z0-9·])([一-鿿·]{1,24})（([A-Za-z][A-Za-z0-9 .+/&'-]{1,59})）"
)

# 纯英文 term(P1a 提炼与命中词形归一共用判定)。
_EN_TERM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 .+/&'-]{1,59}$")


def hit_terms(text: str, term_map: dict[str, str], limit: int = TERM_LIMIT) -> list[tuple[str, str]]:
    """返回 text 中命中的 (english, 中文) 列表,按出现频次降序,截断 limit。

    命中规则:大小写不敏感;词边界=术语前后不是字母/数字(避免 "AI" 命中 "SAID");
    容忍简单词形:复数 s/es 与所有格 's(把术语视作前缀再看后缀)。
    """
    if not text or not term_map:
        return []
    hits: list[tuple[int, str, str]] = []
    for en, zh in term_map.items():
        if not en or not zh or not _EN_TERM_RE.match(en):
            continue
        pat = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(en) + r"(?:'s|es|s)?(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        n = len(pat.findall(text))
        if n:
            hits.append((n, en, zh))
    hits.sort(key=lambda t: (-t[0], t[1].lower()))
    return [(en, zh) for _, en, zh in hits[:limit]]


def render_term_block(hits: list[tuple[str, str]]) -> str:
    """命中术语 → prompt 术语段;空命中返回空串(模板占位替换后无痕)。"""
    if not hits:
        return ""
    lines = ["术语对照表(严格按此翻译,不得使用其它译名):"]
    lines += [f"- {en} → {zh}" for en, zh in hits]
    lines.append("表外专有名词:首次出现译作「中文(English)」,此后全篇保持同一译法。")
    return "\n".join(lines) + "\n\n"


def extract_pairs(translated_md: str) -> dict[str, str]:
    """从译文回收「中文（English）」对照 → {english: 中文}。

    保守取舍(工单 §6):只认全角括号 + 纯英文词组;同一 english 多次出现取首次
    (篇内首译优先,与「避免中途改名」一致);代码围栏内不回收(``` 块剔除)。
    """
    if not translated_md:
        return {}
    text = re.sub(r"```.*?```", " ", translated_md, flags=re.DOTALL)
    pairs: dict[str, str] = {}
    for m in _PAIR_RE.finditer(text):
        zh, en = m.group(1), m.group(2).strip()
        if en in pairs:
            continue
        # ★复现验证:zh 必须在括号对照之外再出现 ≥1 次才收。一石二鸟:
        # ① 滤掉左界误捕(行首「引入鞅(martingale)」——「引入鞅」整串不复现 → 拒,而真译名「鞅」
        #    会以「该鞅」等形式复用);② 只出现一次的术语本无一致性问题,漏收无害。
        if text.count(zh) >= 2:
            pairs[en] = zh
    return pairs


def zh_name_from_glossary_row(term: str, zh_name: str | None, definition: str) -> tuple[str, str] | None:
    """L1 导出提炼(P1a):从 glossary 行提出 (english, 中文译名),提不出返 None(宁缺勿滥)。

    优先级:① zh_name 列(P1b 回填后的权威译名);② term 本身是「中文(English)」形态;
    ③ 纯英文 term 且 definition 以「≤12 字纯中文短名 + ,/,/、」开头(如 "近因偏差,过分强调…")。
    """
    term = (term or "").strip()
    if not term:
        return None
    if zh_name and _EN_TERM_RE.match(term):
        zn = zh_name.strip()
        if re.fullmatch(r"[一-鿿·]{1,24}", zn):
            return term, zn
    m = _PAIR_RE.fullmatch(term.replace("(", "（").replace(")", "）"))
    if m:
        return m.group(2).strip(), m.group(1)
    if _EN_TERM_RE.match(term):
        # {1,12}:单字译名合法(「鞅,一种随机过程」);以逗号定界,误抓面小。
        m2 = re.match(r"^([一-鿿·]{1,12})[,，、]", (definition or "").strip())
        if m2:
            return term, m2.group(1)
    return None
