"""Step 02: Whisper 语音转写。GPU 可用时 faster-whisper large-v3，CPU 用 base。"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash


class WhisperStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "input" / "source.mp4").exists():
            return ["input/source.mp4"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "video": file_hash(self.job_dir / "input" / "source.mp4"),
        }

    def execute(self) -> dict | None:
        from steps.utils.device import select_whisper_model

        video_path = self.job_dir / "input" / "source.mp4"
        model_size, compute_type = select_whisper_model()
        self.log.info("whisper_config", model=model_size, compute_type=compute_type)

        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, compute_type=compute_type)

        segments, info = model.transcribe(str(video_path), language="zh")

        srt_lines = []
        idx = 1
        for segment in segments:
            start = self._format_srt_ts(segment.start)
            end = self._format_srt_ts(segment.end)
            srt_lines.append(f"{idx}\n{start} --> {end}\n{segment.text.strip()}\n")
            idx += 1
            if idx % 50 == 0:
                self.report_progress(idx, idx + 100, f"transcribing ({idx} segments)")

        srt_content = "\n".join(srt_lines)
        self.write_output("input/subtitle.srt", srt_content)
        return {"segments": idx - 1, "language": info.language, "model": model_size}

    def _format_srt_ts(self, seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


if __name__ == "__main__":
    WhisperStep.cli_main("02_whisper")
