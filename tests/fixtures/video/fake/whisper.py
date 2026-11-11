"""Fake whisper.py for the video tests. In detect and transcribe the audio holds the spoken
language; t2s maps a few traditional characters to simplified ones. Every call appends its mode
to fake-whisper.log next to the step's out_dir."""

import json
import pathlib
import sys

mode, source, out_json = sys.argv[1:]
out = pathlib.Path(out_json)
with open(out.parent.parent / "fake-whisper.log", "a", encoding="utf-8") as log:
    log.write(mode + "\n")
if mode == "t2s":
    table = str.maketrans("囉個們這說廣標題譯員審", "啰个们这说广标题译员审")
    texts = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
    out.write_text(json.dumps([t.translate(table) for t in texts], ensure_ascii=False), encoding="utf-8")
    sys.exit(0)
language = pathlib.Path(source).read_text(encoding="utf-8").strip()
output = {"language": language, "language_probability": 0.75}
if mode == "transcribe":
    output["segments"] = [{"start_ms": 0, "end_ms": 1000, "text": f"whisper {language}"}]
out.write_text(json.dumps(output), encoding="utf-8")
