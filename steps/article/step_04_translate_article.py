"""Step 04: 文章翻译。AI 把非中文正文忠实翻译为简体中文,保留 Markdown 结构与图片引用。

仅非中文文章触发:02_parse 检测到非中文写 intermediate/needs_translation.json,本步经 rules:exists 门控。
与 04_smart(意译重组为笔记)不同——这里是忠实全文翻译,产出 output/translated.md 供前端「译文」tab。
译原文 markdown(已含内联图)→ 译文天然保留图位。
"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash


class TranslateArticleStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "output" / "original.md").exists():
            return ["output/original.md"]
        return []

    def input_hashes(self) -> dict[str, str]:
        h = {"original": file_hash(self.job_dir / "output" / "original.md")}
        t = self.template_hash("04_translate_article")
        if t:
            h["template"] = t
        return h

    def execute(self) -> dict | None:
        md = (self.job_dir / "output" / "original.md").read_text(encoding="utf-8")

        prompt = self._build_prompt(md)
        # 全文译文常超默认 4096 output tokens,抬高上限防截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=16384)

        self.write_output("output/translated.md", result)
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model}

    def _build_prompt(self, md: str) -> str:
        # 默认模板外置 configs/prompts/templates/04_translate_article.md(改文件不碰代码);缺失回退 _DEFAULT。
        tmpl = self._load_prompt_template("04_translate_article", _DEFAULT)
        return tmpl.replace("<<BODY>>", md)


# 静态默认 prompt 骨架(= 外置模板内容;<<BODY>> 注入原文)。templates/04_translate_article.md 由此生成。
_DEFAULT = (
    "请将以下文章【忠实翻译】为简体中文。这是翻译,不是笔记/摘要,要求:\n"
    "- 忠实原意,逐段完整翻译,不增删、不概括、不评论;\n"
    "- 完整保留 Markdown 结构:标题层级(#/##)、列表、表格、引用、代码块、加粗/斜体等原样;\n"
    "- 图片引用 ![](assets/...) 必须原样保留在原位置,不改路径、不删除、不新增;\n"
    "- 专有名词/人名/公司名/产品名首次出现用「中文(English)」,代码、公式、变量名不译;\n"
    "- 只输出翻译后的 Markdown 正文,不要任何前言、说明或结尾提议。\n\n"
    "--- 原文 ---\n<<BODY>>"
)


if __name__ == "__main__":
    TranslateArticleStep.cli_main("04_translate_article")
