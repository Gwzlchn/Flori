"""faster-whisper language detection and transcription, called by the video-transcribe step.

Usage: python3 -I whisper.py detect|transcribe <audio> <out_json>
The model directory comes from FLORI_WHISPER_MODEL. detect writes {language,
language_probability}; transcribe writes transcript.json plus language_probability, which
video-transcribe moves into the step meta. Chinese comes out in simplified characters.
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


def transcribe(model, audio, t2s=None) -> dict:
    """t2s converts traditional to simplified Chinese; OpenCC's when not given."""
    segments, info = model.transcribe(audio, **OPTIONS)
    print(f"whisper: language {info.language} p={info.language_probability:.2f}", flush=True)
    chinese = info.language == "zh"
    if chinese:
        # The first call only detected the language; its segments were never decoded.
        segments, _ = model.transcribe(audio, language="zh", hotwords=ZH_HINT, **OPTIONS)
        if t2s is None:
            import opencc

            t2s = opencc.OpenCC("t2s").convert
    result, next_report = [], REPORT_EVERY_S
    for segment in segments:
        text = segment.text.strip()
        if chinese:
            # The hint makes simplified output likely; OpenCC makes it certain.
            text = t2s(text)
        if text:
            start_ms, end_ms = round(segment.start * 1000), round(segment.end * 1000)
            result.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})
        if segment.end >= next_report:
            print(f"whisper: {segment.end / 60:.0f} of {info.duration / 60:.0f} min", flush=True)
            next_report += REPORT_EVERY_S
    return {
        "language": info.language,
        "language_probability": info.language_probability,
        "segments": result,
    }


MODES = {"detect": detect, "transcribe": transcribe}


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in MODES:
        print("usage: whisper.py detect|transcribe <audio> <out_json>", file=sys.stderr)
        return PERMANENT
    mode, audio, out_json = argv[1:]
    try:
        output = MODES[mode](load_model(), audio)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False)
    except Exception as err:  # a decoded WAV fails the same way on every attempt
        print(f"whisper: {type(err).__name__}: {err}", file=sys.stderr)
        return PERMANENT
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
