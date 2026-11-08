"""faster-whisper transcription, called by the video-transcribe step.

Usage: python3 -I whisper.py <audio> <out_json>
The model directory comes from FLORI_WHISPER_MODEL. Output is transcript.json plus
language_probability; video-transcribe moves that field into the step meta.
"""

import json
import os
import sys

PERMANENT = 20
REPORT_EVERY_S = 300.0


def transcribe(audio: str, out_json: str) -> None:
    from faster_whisper import WhisperModel

    model_dir = os.environ.get("FLORI_WHISPER_MODEL", "")
    if not os.path.isfile(os.path.join(model_dir, "model.bin")):
        raise ValueError(f"FLORI_WHISPER_MODEL has no model.bin: {model_dir!r}")
    model = WhisperModel(
        model_dir, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True
    )
    # Without previous-text conditioning whisper does not fall into repetition loops.
    segments, info = model.transcribe(
        audio, beam_size=5, vad_filter=True, condition_on_previous_text=False
    )
    print(f"whisper: language {info.language} p={info.language_probability:.2f}", flush=True)
    result, next_report = [], REPORT_EVERY_S
    for segment in segments:
        if text := segment.text.strip():
            start_ms, end_ms = round(segment.start * 1000), round(segment.end * 1000)
            result.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})
        if segment.end >= next_report:
            print(f"whisper: {segment.end / 60:.0f} of {info.duration / 60:.0f} min", flush=True)
            next_report += REPORT_EVERY_S
    output = {
        "language": info.language,
        "language_probability": info.language_probability,
        "segments": result,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: whisper.py <audio> <out_json>", file=sys.stderr)
        return PERMANENT
    try:
        transcribe(argv[1], argv[2])
    except Exception as err:  # a decoded WAV fails the same way on every attempt
        print(f"whisper: {type(err).__name__}: {err}", file=sys.stderr)
        return PERMANENT
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
