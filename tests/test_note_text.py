"""索引与 producer 共用 Markdown 归一化时不得发生行为漂移。"""

import pytest

from scheduler.scheduler import _markdown_to_text
from shared.note_text import markdown_to_index_text


@pytest.mark.parametrize(
    "markdown",
    [
        "# 标题\r\n\r\n- **正文** [链接](https://example.com)",
        "```python\nvalue_with_underscore = 1\n```\n\n> 引用",
        "![图注](assets/frame.jpg)\n\n<div>HTML</div>",
        "前段\n\n\n后段\n",
    ],
)
def test_public_normalizer_matches_scheduler_legacy_behavior(markdown):
    assert markdown_to_index_text(markdown) == _markdown_to_text(markdown)
