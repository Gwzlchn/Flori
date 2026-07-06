"""概念实体归一:norm_key 身份键 + resolve 变体归并(工单 2026-07-06/09 P1)。

glossary 主键是 (domain, term) 字面精确匹配,同一概念的变体(大小写/全半角/括号注音/
中英说法)各建一条,概念↔内容串不起来。本模块提供纯函数(零 DB 依赖,db 层与脚本共用):
  - norm_key:        变体归一键(小写 + 全半角统一 + 空白折叠 + 剥「主名 (Note)」注音尾)。
  - split_annotation: 拆「主名 (Note)」;非注音形态返回 (原串, None)。
  - candidate_keys:   一条建议(term + zh_name)可用于匹配现有实体的全部键,按优先级有序。
  - resolve:          在域内现有行(term/zh_name/aliases)里找同一实体,返回主名或 None。
  - primary_fields:   新建实体的主名规则(英文为 term、中文进 zh_name),变体入 aliases。

归一保守取舍:不做词形还原/去连字符(Multi-Head vs Multihead 视为不同,交给 LLM 清洗段),
错并的代价(两个概念混成一条)远大于漏并(下次清洗可补)。
"""

from __future__ import annotations

import re

# 「主名 (Note)」注音尾:主名非空,括号(半角,norm 后)内不再含括号。只剥"结尾"的一段——
# 括号在中间(如 "P(X) given Y")不是注音形态,不拆。
_ANNOT_RE = re.compile(r"^(?P<main>.+?)\s*\((?P<note>[^()]+)\)$")

# 纯英文术语形态(与 shared/terms._EN_TERM_RE 同判定,此处独立以免互相依赖)。
_EN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 .+/&'-]{0,59}$")
_CJK_RE = re.compile(r"[一-鿿]")


def _to_halfwidth(s: str) -> str:
    """全角 ASCII/括号/空格 → 半角(FF01-FF5E 平移,全角空格 U+3000 → 空格)。"""
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def split_annotation(term: str) -> tuple[str, str | None]:
    """拆「主名 (Note)」→ (主名, Note);无注音尾/主名为空 → (原串, None)。输入先全半角统一。"""
    s = _to_halfwidth((term or "").strip())
    m = _ANNOT_RE.match(s)
    if not m:
        return s, None
    main = m.group("main").strip()
    note = m.group("note").strip()
    if not main or not note:
        return s, None
    return main, note


def norm_key(term: str) -> str:
    """变体归一键:剥注音尾 + 小写 + 空白折叠。空串入返回空串(调用方跳过)。

    「量化 (Quantization)」「量化(Quantization)」「量化」→ 同键 "量化";
    "Multi-Head  Attention" / "multi-head attention" → 同键。
    """
    main, _ = split_annotation(term)
    return re.sub(r"\s+", " ", main.lower()).strip()


def candidate_keys(term: str, zh_name: str | None = None) -> list[str]:
    """一条建议可用于实体匹配的键,按优先级有序去重:主名键 > 注音键 > zh_name 键。"""
    main, note = split_annotation(term)
    keys: list[str] = []
    for x in (main, note, zh_name):
        k = norm_key(x) if x else ""
        if k and k not in keys:
            keys.append(k)
    return keys


def build_key_index(rows: list[dict]) -> dict[str, str]:
    """域内现有行 → {norm_key: 主名 term}。term 键优先于 zh_name/aliases 键(先建者不覆盖),
    行序即建键序,同键冲突时先出现的行赢(调用方按稳定序传入,如 term 升序)。"""
    idx: dict[str, str] = {}
    for r in rows:
        t = (r.get("term") or "").strip()
        if not t:
            continue
        for k in candidate_keys(t):
            idx.setdefault(k, t)
    for r in rows:
        t = (r.get("term") or "").strip()
        if not t:
            continue
        extras = [r.get("zh_name") or ""]
        aliases = r.get("aliases") or []
        if isinstance(aliases, list):
            extras += [a for a in aliases if isinstance(a, str)]
        for x in extras:
            k = norm_key(x)
            if k:
                idx.setdefault(k, t)
    return idx


def resolve(rows: list[dict], term: str, zh_name: str | None = None) -> str | None:
    """在域内现有行里找该建议对应的实体,命中返回主名 term,否则 None。

    匹配序:norm_key(主名) → norm_key(注音) → norm_key(zh_name),对撞 term/zh_name/aliases
    的归一键。「多头注意力」经 zh_name 命中「Multi-Head Attention」即同一实体。
    """
    idx = build_key_index(rows)
    for k in candidate_keys(term, zh_name):
        hit = idx.get(k)
        if hit is not None:
            return hit
    return None


# 概念关系边类型(P2):prerequisite 有方向(src 需先懂 tgt),其余无向。
REL_TYPES = ("prerequisite", "is_a", "part_of", "related")


def norm_related(items) -> list[dict]:
    """related 列的规范形态 [{term, rel}]:字符串(手动/存量)视为 rel='related',
    未知 rel 降级 'related',按 term 去重(先到先得)。非法元素丢弃。"""
    out: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        if isinstance(it, str):
            t, rel = it.strip(), "related"
        elif isinstance(it, dict):
            t = (it.get("term") or "").strip()
            rel = it.get("rel") if it.get("rel") in REL_TYPES else "related"
        else:
            continue
        if t and t not in seen:
            seen.add(t)
            out.append({"term": t, "rel": rel})
    return out


def primary_fields(term: str, zh_name: str = "") -> tuple[str, str, list[str]]:
    """新建实体的主名规则 → (主名 term, zh_name, aliases)。

    英文术语为 term、中文进 zh_name;纯中文概念 term=中文。「中文 (English)」组合形态拆开:
    英文半边做主名、中文半边进 zh_name(反向组合同理)。原始串与主名不同则入 aliases 备查。
    """
    raw = _to_halfwidth((term or "").strip())
    main, note = split_annotation(raw)
    zh = (zh_name or "").strip()
    primary = main
    if note:
        main_en, note_en = bool(_EN_RE.match(main)), bool(_EN_RE.match(note))
        main_cjk, note_cjk = bool(_CJK_RE.search(main)), bool(_CJK_RE.search(note))
        if main_cjk and note_en:
            primary = note
            zh = zh or main
        elif main_en and note_cjk:
            primary = main
            zh = zh or note
        # 其余(英英/中中)取主名半边,注音只留在 aliases(原始串)里。
    aliases = [raw] if raw and raw != primary else []
    return primary, zh, aliases
