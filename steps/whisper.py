"""faster-whisper language detection and transcription, called by the video-transcribe step,
and the OpenCC conversion to simplified Chinese, called by video-fetch.

Usage: python3 -I whisper.py detect|transcribe <audio> <out_json>
       python3 -I whisper.py t2s <in_json> <out_json>
The model directory comes from FLORI_WHISPER_MODEL. detect writes {language,
language_probability}; transcribe writes transcript.json plus language_probability, which
video-transcribe moves into the step meta. Chinese comes out in simplified characters. t2s reads
a JSON array of strings and writes the same array converted with OpenCC t2s; it loads no model.
"""

import json
import os
import sys

PERMANENT = 20
REPORT_EVERY_S = 300.0
# Without previous-text conditioning whisper does not fall into repetition loops.
OPTIONS = {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False}
# Unprompted, whisper writes Mandarin mostly without punctuation and drifts into traditional
# characters after the first windows. Without previous-text conditioning initial_prompt only
# reaches the first window; hotwords go into the prompt of every window. Only for zh: in another
# language a Chinese prompt would pull the text towards Chinese.
ZH_HINT = "以下是普通话的句子，使用简体中文和标点符号。"
# With the hint whisper often makes a whole 30s window one segment, and video-frames files a
# segment's text under the section its start falls in.
ZH_BREAKS = frozenset("。！？；，,!?;")


def zh_pieces(start_ms: int, end_ms: int, text: str) -> list[dict]:
    """A Chinese segment cut after each ZH_BREAKS mark that an ideograph follows, every piece
    timed by its share of the characters. push_text puts no space next to an ideograph, so the
    pieces join back into the same text, and digits like 1,300 are never cut."""
    cuts = [
        i + 1
        for i, c in enumerate(text[:-1])
        if c in ZH_BREAKS and "\u4e00" <= text[i + 1] <= "\u9fff"
    ]
    bounds = [0, *cuts, len(text)]

    def ms(i: int) -> int:
        return start_ms + (end_ms - start_ms) * i // len(text)

    return [
        {"start_ms": ms(a), "end_ms": ms(b), "text": text[a:b]} for a, b in zip(bounds, bounds[1:])
    ]


def load_model():
    from faster_whisper import WhisperModel

    model_dir = os.environ.get("FLORI_WHISPER_MODEL", "")
    if not os.path.isfile(os.path.join(model_dir, "model.bin")):
        raise ValueError(f"FLORI_WHISPER_MODEL has no model.bin: {model_dir!r}")
    return WhisperModel(
        model_dir, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True
    )


def detect(model, audio) -> dict:
    """The language transcribe would detect: model.transcribe detects it on the VAD-filtered
    audio before it returns, and its segments are a lazy generator that is never consumed."""
    _, info = model.transcribe(audio, **OPTIONS)
    print(f"whisper: language {info.language} p={info.language_probability:.2f}", flush=True)
    return {"language": info.language, "language_probability": info.language_probability}


def transcribe(model, audio) -> dict:
    segments, info = model.transcribe(audio, **OPTIONS)
    print(f"whisper: language {info.language} p={info.language_probability:.2f}", flush=True)
    chinese = info.language == "zh"
    if chinese:
        import opencc

        # The first call only detected the language; its segments were never decoded.
        segments, _ = model.transcribe(audio, language="zh", hotwords=ZH_HINT, **OPTIONS)
        t2s = opencc.OpenCC("t2s").convert
    result, next_report = [], REPORT_EVERY_S
    for segment in segments:
        text = segment.text.strip()
        if chinese:
            # The hint makes simplified output likely; OpenCC makes it certain.
            text = t2s(text)
        if text:
            start_ms, end_ms = round(segment.start * 1000), round(segment.end * 1000)
            if chinese:
                result.extend(zh_pieces(start_ms, end_ms, text))
            else:
                result.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})
        if segment.end >= next_report:
            print(f"whisper: {segment.end / 60:.0f} of {info.duration / 60:.0f} min", flush=True)
            next_report += REPORT_EVERY_S
    return {
        "language": info.language,
        "language_probability": info.language_probability,
        "segments": result,
    }


def t2s(in_json: str) -> list[str]:
    """Every string of the array in simplified characters; anything else is refused."""
    import opencc

    with open(in_json, encoding="utf-8") as f:
        texts = json.load(f)
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise ValueError("t2s takes a JSON array of strings")
    convert = opencc.OpenCC("t2s").convert
    return [convert(text) for text in texts]


MODES = {"detect": detect, "transcribe": transcribe}


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in (*MODES, "t2s"):
        print("usage: whisper.py detect|transcribe <audio> <out_json> | t2s <in_json> <out_json>", file=sys.stderr)
        return PERMANENT
    mode, source, out_json = argv[1:]
    try:
        output = t2s(source) if mode == "t2s" else MODES[mode](load_model(), source)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False)
    except Exception as err:  # a decoded WAV fails the same way on every attempt
        print(f"whisper: {type(err).__name__}: {err}", file=sys.stderr)
        return PERMANENT
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
