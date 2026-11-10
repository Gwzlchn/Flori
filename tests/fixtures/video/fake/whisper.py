"""Fake whisper.py for the video-transcribe tests. The audio holds the spoken language; every
call appends its mode to fake-whisper.log next to the step's out_dir."""

import json
import pathlib
import sys

mode, audio, out_json = sys.argv[1:]
language = pathlib.Path(audio).read_text(encoding="utf-8").strip()
out = pathlib.Path(out_json)
with open(out.parent.parent / "fake-whisper.log", "a", encoding="utf-8") as log:
    log.write(mode + "\n")
output = {"language": language, "language_probability": 0.75}
if mode == "transcribe":
    output["segments"] = [{"start_ms": 0, "end_ms": 1000, "text": f"whisper {language}"}]
out.write_text(json.dumps(output), encoding="utf-8")
