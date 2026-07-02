"""Step 02: Whisper 语音转写。GPU 可用时 faster-whisper large-v3,CPU 用 base。"""

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

        import os

        from faster_whisper import WhisperModel
        # 模型缓存目录显式可配:设 MODEL_CACHE_DIR 即把权重缓存到该目录,不设则用库默认 HF 缓存。
        # 这是无状态部署唯一该挂的卷,可挂 warm 卷跨重启复用。
        model = WhisperModel(
            model_size, compute_type=compute_type,
            download_root=os.environ.get("MODEL_CACHE_DIR") or None,
        )

        # 不写死语种:faster-whisper 自动检测。本步仅在无原生 srt 时跑,外文无字幕内容若强制
        # language="zh" 会被按中文声学模型解码出乱码,且 info.language 恒为 zh 丢失真实语种(下游 08 据此判翻译)。
        segments, info = model.transcribe(str(video_path))
        total = info.duration or 0  # 真实音频时长作进度分母,segment.end 作已处理位置

        srt_lines = []
        idx = 1
        for segment in segments:
            start = self._format_srt_ts(segment.start)
            end = self._format_srt_ts(segment.end)
            srt_lines.append(f"{idx}\n{start} --> {end}\n{segment.text.strip()}\n")
            idx += 1
            if total and idx % 50 == 0:
                self.report_progress(min(segment.end, total), total, f"transcribing ({idx} segments)")

        srt_content = "\n".join(srt_lines)
        self.write_output("input/subtitle.srt", srt_content)
        if total:
            self.report_progress(total, total, "done")
        return {"segments": idx - 1, "language": info.language, "model": model_size}

    def _format_srt_ts(self, seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


if __name__ == "__main__":
    WhisperStep.cli_main("02_whisper")
