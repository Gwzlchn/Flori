"""faster-whisper transcription, called by the video-transcribe step.

Usage: python3 -I whisper.py <media> <out_json>
Model directory comes from FLORI_WHISPER_MODEL.
"""

import sys

PERMANENT = 20


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: whisper.py <media> <out_json>", file=sys.stderr)
        return PERMANENT
    print("whisper transcription is not implemented yet", file=sys.stderr)
    return PERMANENT


if __name__ == "__main__":
    sys.exit(main(sys.argv))
