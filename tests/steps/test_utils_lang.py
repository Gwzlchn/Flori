"""steps/utils/lang.py 的测试:正文主语言粗判(中/非中,翻译触发用)。"""

from steps.utils.lang import detect_lang


def test_english_is_non_zh():
    assert detect_lang("The quick brown fox jumps over the lazy dog. " * 4) == "non-zh"


def test_chinese_is_zh():
    assert detect_lang("这是一篇讲人工智能发展与应用的中文论文。" * 3) == "zh"


def test_chinese_with_english_terms_still_zh():
    assert detect_lang("人工智能 AI 与机器学习 ML 在中文语境下的长篇内容很多很多") == "zh"


def test_empty_is_unknown():
    assert detect_lang("") == "unknown"
    assert detect_lang("123 !!! ...") == "unknown"
