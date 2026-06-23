"""Step 03: 场景检测。PySceneDetect AdaptiveDetector。"""

from __future__ import annotations

import json

from shared.step_base import StepBase, file_hash


class SceneStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "input" / "source.mp4").exists():
            return ["input/source.mp4"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "video": file_hash(self.job_dir / "input" / "source.mp4"),
            "config": json.dumps(self.config.get("domain", {}).get("scene", {}), sort_keys=True),
        }

    def execute(self) -> dict | None:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import AdaptiveDetector

        video_path = self.job_dir / "input" / "source.mp4"
        scene_cfg = self.config.get("domain", {}).get("scene", {})
        threshold = scene_cfg.get("adaptive_threshold", 3.0)
        min_scene_len_sec = scene_cfg.get("min_scene_len_sec", 2.0)
        window_width = scene_cfg.get("window_width", 2)
        min_content_val = scene_cfg.get("min_content_val", 15.0)

        # backend='pyav':用 PyAV(系统 libav,含 libdav1d)解码,而非 OpenCV 自带 ffmpeg
        # ——后者解不了 AV1(报 "Failed to get pixel format")→ 场景检测 0 结果。是什么编码解什么,不转码。
        video = open_video(str(video_path), backend="pyav")
        fps = video.frame_rate
        min_scene_len_frames = int(min_scene_len_sec * fps)

        scene_manager = SceneManager()
        scene_manager.add_detector(
            AdaptiveDetector(
                adaptive_threshold=threshold,
                min_scene_len=min_scene_len_frames,
                window_width=window_width,
                min_content_val=min_content_val,
            )
        )

        total_frames = video.duration.frame_num
        scene_manager.detect_scenes(video, show_progress=False, callback=lambda frame_img, position: (
            self.report_progress(position.frame_num, total_frames, "scanning frames")
            if position.frame_num % 500 == 0 else None
        ))

        scene_list = scene_manager.get_scene_list()

        scenes = []
        for i, (start, end) in enumerate(scene_list):
            scenes.append({
                "index": i,
                "start_sec": round(start.get_seconds(), 2),
                "end_sec": round(end.get_seconds(), 2),
                "start_frame": start.get_frames(),
                "end_frame": end.get_frames(),
                "duration_sec": round((end - start).get_seconds(), 2),
            })

        self.report_progress(total_frames, total_frames, "done")
        self.write_output("intermediate/scenes.json", scenes)
        return {"scenes": len(scenes)}


if __name__ == "__main__":
    SceneStep.cli_main("03_scene")
