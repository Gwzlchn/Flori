"""tests for steps/utils/ass_parser.py"""

from steps.utils.ass_parser import parse_ass


SAMPLE_ASS = """\
[Script Info]
Title: Test

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:05.00,0:00:07.00,Default,,0,0,0,,Hello world
Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,Earlier line
Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,{\\b1}Bold text{\\b0}
Dialogue: 0,0:00:15.00,0:00:17.00,Default,,0,0,0,,{\\move(100,200,300,400)}Moving text
Dialogue: 0,0:00:20.00,0:00:22.00,Default,,0,0,0,,{\\pos(100,200)}Positioned
Dialogue: 0,0:00:25.00,0:00:27.00,Default,,0,0,0,,Normal danmaku
"""


class TestParseAss:
    def test_basic_parse(self):
        entries = parse_ass(SAMPLE_ASS)
        texts = [e.text for e in entries]
        assert "Hello world" in texts
        assert "Earlier line" in texts
        assert "Normal danmaku" in texts

    def test_sorted_by_time(self):
        entries = parse_ass(SAMPLE_ASS)
        times = [e.time_sec for e in entries]
        assert times == sorted(times)

    def test_keeps_move(self):
        # B 站滚动弹幕都带 {\\move(...)},必须保留(剥标签后保留文字)。
        entries = parse_ass(SAMPLE_ASS)
        texts = [e.text for e in entries]
        assert "Moving text" in texts

    def test_keeps_pos(self):
        # 顶/底固定弹幕带 {\\pos(...)},同样保留。
        entries = parse_ass(SAMPLE_ASS)
        texts = [e.text for e in entries]
        assert "Positioned" in texts

    def test_strips_formatting_tags(self):
        entries = parse_ass(SAMPLE_ASS)
        texts = [e.text for e in entries]
        assert "Bold text" in texts
        assert "{\\b1}" not in str(texts)

    def test_empty_input(self):
        assert parse_ass("") == []

    def test_no_dialogue(self):
        header = "[Script Info]\nTitle: Empty\n"
        assert parse_ass(header) == []

    def test_time_parsing(self):
        entries = parse_ass(SAMPLE_ASS)
        earlier = next(e for e in entries if e.text == "Earlier line")
        assert earlier.time_sec == 2.0
