"""steps/utils/chunking.py 的测试:Markdown 段落贪心分块(翻译等逐块 AI 调用用)。"""

from steps.utils.chunking import split_markdown_chunks


class TestSplitMarkdownChunks:
    def test_fits_single_chunk_identical(self):
        text = "# T\n\npara one\n\npara two"
        assert split_markdown_chunks(text, 1000) == [text]

    def test_paragraph_boundaries_and_order_preserved(self):
        paras = [f"para {i} " + "x" * 50 for i in range(20)]
        text = "\n\n".join(paras)
        chunks = split_markdown_chunks(text, 200)
        assert len(chunks) > 1
        assert all(len(c) <= 200 for c in chunks)
        # 段落边界切:重新拼接 == 原文(无内容丢失/乱序)
        assert "\n\n".join(chunks) == text

    def test_oversized_paragraph_split_by_lines(self):
        big_para = "\n".join(f"line {i} " + "y" * 40 for i in range(30))
        chunks = split_markdown_chunks(big_para, 150)
        assert len(chunks) > 1
        assert all(len(c) <= 150 for c in chunks)
        # 行边界切:所有行都在、顺序不变
        joined = "\n".join(c.replace("\n\n", "\n") for c in chunks)
        for i in range(30):
            assert f"line {i} " in joined

    def test_oversized_single_line_hard_split(self):
        text = "z" * 500
        chunks = split_markdown_chunks(text, 100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks) == text
