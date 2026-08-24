import json
import pathlib
import sys

from faster_whisper import WhisperModel


def main():
    if len(sys.argv) != 4:
        raise ValueError("expected model directory, model name and input")
    model_dir = pathlib.Path(sys.argv[1])
    if not model_dir.is_absolute() or model_dir.name != sys.argv[2] or sys.argv[2] != "base":
        raise ValueError("invalid model identity")
    model = WhisperModel(
        str(model_dir),
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        num_workers=1,
        local_files_only=True,
    )
    segments, info = model.transcribe(
        sys.argv[3],
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        word_timestamps=False,
    )
    result = {
        "language": info.language,
        "segments": [
            {
                "start_ms": round(segment.start * 1000),
                "end_ms": round(segment.end * 1000),
                "text": segment.text.strip(),
            }
            for segment in segments
            if segment.text.strip()
        ],
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
