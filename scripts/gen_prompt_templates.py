"""从各步内联 _DEFAULT 常量生成 configs/prompts/templates/*.md(保证模板 == 代码兜底,零漂移)。
首次外置 prompt 时一次性用;之后改 prompt 直接改 templates/*.md(进指纹触发重跑),不碰代码。
跑:容器内 `python scripts/gen_prompt_templates.py`(需 shared/steps 可导入)。"""
from __future__ import annotations
from pathlib import Path

from steps.article.step_04_translate_article import _DEFAULT as translate_article
from steps.article.step_04_smart_article import _DEFAULT_HEADER as smart_article
from steps.article.step_05_concepts import _DEFAULT_HEADER as concepts
from steps.paper.step_04_translate_paper import _DEFAULT as translate_paper
from steps.paper.step_04_translate_paper import _DEFAULT_PDF as translate_paper_pdf
from steps.paper.step_05_smart_paper import _DEFAULT_HEADER as smart_paper
from steps.audio.step_04_smart_podcast import _DEFAULT_HEADER as smart_podcast
from steps.video.step_08_punctuate import _PUNCTUATE_PROMPT, _TRANSLATE_PROMPT
from steps.video.step_11_smart import _DEFAULT_VISION, _DEFAULT_USER_HEADER
from steps.video.step_evidence import _DEFAULT as evidence
from shared.step_base import StepBase

# 评审 prompt 白盒骨架(单一来源 = StepBase.review_prompt_skeleton;build_review_prompt 运行期注入占位)。
# 4 条 pipeline 评审结构一致 → 三个评审步名文件内容完全相同(各 job 注入自己的维度/参照块)。
_review = StepBase.review_prompt_skeleton()

TEMPLATES = {
    "04_translate_article.md": translate_article,
    "04_smart_article.md": smart_article,
    "05_concepts.md": concepts,
    "04_translate_paper.md": translate_paper,
    "04_translate_paper.pdf.md": translate_paper_pdf,
    "05_smart_paper.md": smart_paper,
    "04_smart_podcast.md": smart_podcast,
    "08_punctuate.zh.md": _PUNCTUATE_PROMPT,
    "08_punctuate.translate.md": _TRANSLATE_PROMPT,
    "11_smart.vision.md": _DEFAULT_VISION,
    "11_smart.md": _DEFAULT_USER_HEADER,
    "10_evidence.md": evidence,
    # 评审步(白盒化):audio=05_review / paper+article=06_review / video=12_review,共享同一骨架。
    "05_review.md": _review,
    "06_review.md": _review,
    "12_review.md": _review,
}

if __name__ == "__main__":
    out = Path("configs/prompts/templates")
    out.mkdir(parents=True, exist_ok=True)
    for name, content in TEMPLATES.items():
        (out / name).write_text(content, encoding="utf-8")
        print(f"  wrote {name} ({len(content)} chars)")
    print(f"共 {len(TEMPLATES)} 个模板 → {out}")
