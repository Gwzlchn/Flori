"""whisper.py against fake faster_whisper and opencc modules: no model files, no audio."""

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "steps"))

import whisper  # noqa: E402


class FakeModel:
    """Detects `language` unless one is passed; its segments record whether they were decoded."""

    def __init__(self, language: str, texts: list[str]) -> None:
        self.language = language
        self.texts = texts
        self.calls: list[dict] = []
        self.decoded = False

    def transcribe(self, audio, **options):
        self.calls.append(options)
        forced = options.get("language")
        info = SimpleNamespace(
            language=forced or self.language,
            language_probability=1.0 if forced else 0.97,
            duration=60.0,
        )

        def segments():
            self.decoded = True
            for i, text in enumerate(self.texts):
                yield SimpleNamespace(start=i * 1.5, end=i * 1.5 + 1.25, text=text)

        return segments(), info


def run(mode: str, model: FakeModel) -> dict:
    with contextlib.redirect_stdout(io.StringIO()):
        return whisper.MODES[mode](model, "audio.wav")


class WhisperTest(unittest.TestCase):
    def setUp(self) -> None:
        # opencc converts a few characters and records the configs it is built with;
        # faster_whisper hands out self.model.
        self.configs: list[str] = []
        table = str.maketrans("隻動變從無為勝", "只动变从无为胜")
        configs = self.configs

        class OpenCC:
            def __init__(self, config: str) -> None:
                configs.append(config)

            def convert(self, text: str) -> str:
                return text.translate(table)

        self.model = FakeModel("zh", [])
        modules = {
            "opencc": types.ModuleType("opencc"),
            "faster_whisper": types.ModuleType("faster_whisper"),
        }
        modules["opencc"].OpenCC = OpenCC
        modules["faster_whisper"].WhisperModel = lambda *args, **kwargs: self.model
        patch = mock.patch.dict(sys.modules, modules)
        patch.start()
        self.addCleanup(patch.stop)

    def test_detect_decodes_nothing_and_detects_like_transcribe(self) -> None:
        model = FakeModel("zh", ["不该解码"])
        self.assertEqual(run("detect", model), {"language": "zh", "language_probability": 0.97})
        self.assertFalse(model.decoded)
        transcribed = FakeModel("zh", [])
        run("transcribe", transcribed)
        self.assertEqual(model.calls, transcribed.calls[:1])

    def test_transcribe_keeps_timed_non_empty_segments(self) -> None:
        model = FakeModel("en", [" Hello there. ", "  ", "General Kenobi."])
        self.assertEqual(
            run("transcribe", model),
            {
                "language": "en",
                "language_probability": 0.97,
                "segments": [
                    {"start_ms": 0, "end_ms": 1250, "text": "Hello there."},
                    {"start_ms": 3000, "end_ms": 4250, "text": "General Kenobi."},
                ],
            },
        )
        self.assertFalse(model.calls[0]["condition_on_previous_text"])
        self.assertEqual(len(model.calls), 1, "no hint outside Chinese")
        self.assertEqual(self.configs, [], "no conversion outside Chinese")

    def test_chinese_gets_the_hint_in_every_window_and_simplified_text(self) -> None:
        model = FakeModel("zh", ["一隻股票的流動性", "  ", "變得從無到有。"])
        output = run("transcribe", model)
        self.assertEqual(
            [s["text"] for s in output["segments"]], ["一只股票的流动性", "变得从无到有。"]
        )
        self.assertEqual(self.configs, ["t2s"])
        # The detected language and its confidence, not the forced second run's.
        self.assertEqual((output["language"], output["language_probability"]), ("zh", 0.97))
        detect, decode = model.calls
        self.assertNotIn("hotwords", detect)
        self.assertEqual(decode, {**whisper.OPTIONS, "language": "zh", "hotwords": whisper.ZH_HINT})
        self.assertFalse(decode["condition_on_previous_text"])

    def test_chinese_segments_are_cut_where_an_ideograph_follows_punctuation(self) -> None:
        text = "仅用19个交易日,吃进1,300万股。一度浮盈3个亿,Paykin在这里解释!好"
        self.assertEqual(
            whisper.zh_pieces(10_000, 40_000, text),
            [
                {"start_ms": 10_000, "end_ms": 16_750, "text": "仅用19个交易日,"},
                {"start_ms": 16_750, "end_ms": 24_250, "text": "吃进1,300万股。"},
                {"start_ms": 24_250, "end_ms": 39_250, "text": "一度浮盈3个亿,Paykin在这里解释!"},
                {"start_ms": 39_250, "end_ms": 40_000, "text": "好"},
            ],
        )
        # Through transcribe: a Chinese segment is cut, another language never is.
        zh = run("transcribe", FakeModel("zh", ["本以為勝券在握，只可惜。"]))
        self.assertEqual(
            zh["segments"],
            [
                {"start_ms": 0, "end_ms": 833, "text": "本以为胜券在握，"},
                {"start_ms": 833, "end_ms": 1250, "text": "只可惜。"},
            ],
        )
        ja = run("transcribe", FakeModel("ja", ["東京,大阪。京都"]))
        self.assertEqual([s["text"] for s in ja["segments"]], ["東京,大阪。京都"])

    def test_main_writes_the_mode_output_as_utf8_json(self) -> None:
        self.model = FakeModel("zh", ["一隻股票"])
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "model.bin").write_bytes(b"")
            out = pathlib.Path(tmp, "out.json")
            with mock.patch.dict(os.environ, {"FLORI_WHISPER_MODEL": tmp}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(whisper.main(["whisper.py", "detect", "a.wav", str(out)]), 0)
                    detected = json.loads(out.read_text(encoding="utf-8"))
                    self.assertEqual(whisper.main(["whisper.py", "transcribe", "a.wav", str(out)]), 0)
            raw = out.read_text(encoding="utf-8")
        self.assertEqual(detected, {"language": "zh", "language_probability": 0.97})
        self.assertIn("一只股票", raw)
        self.assertEqual(json.loads(raw)["segments"][0]["text"], "一只股票")


if __name__ == "__main__":
    unittest.main()
