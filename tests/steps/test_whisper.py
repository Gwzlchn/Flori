"""whisper.py against a fake faster-whisper model: no model files, no audio."""

import contextlib
import io
import pathlib
import sys
import unittest
from types import SimpleNamespace

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


def run(mode: str, model: FakeModel, **kwargs) -> dict:
    with contextlib.redirect_stdout(io.StringIO()):
        return whisper.MODES[mode](model, "audio.wav", **kwargs)


class WhisperTest(unittest.TestCase):
    def test_detect_decodes_nothing_and_detects_like_transcribe(self) -> None:
        model = FakeModel("zh", ["不该解码"])
        self.assertEqual(run("detect", model), {"language": "zh", "language_probability": 0.97})
        self.assertFalse(model.decoded)
        transcribed = FakeModel("zh", [])
        run("transcribe", transcribed, t2s=str)
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

    def test_chinese_gets_the_hint_in_every_window_and_simplified_text(self) -> None:
        model = FakeModel("zh", ["一隻股票的流動性", "  ", "變得從無到有。"])
        t2s = str.maketrans("隻動變從無", "只动变从无")
        output = run("transcribe", model, t2s=lambda text: text.translate(t2s))
        self.assertEqual(
            [s["text"] for s in output["segments"]], ["一只股票的流动性", "变得从无到有。"]
        )
        # The detected language and its confidence, not the forced second run's.
        self.assertEqual((output["language"], output["language_probability"]), ("zh", 0.97))
        detect, decode = model.calls
        self.assertNotIn("hotwords", detect)
        self.assertEqual(decode, {**whisper.OPTIONS, "language": "zh", "hotwords": whisper.ZH_HINT})
        self.assertFalse(decode["condition_on_previous_text"])


if __name__ == "__main__":
    unittest.main()
