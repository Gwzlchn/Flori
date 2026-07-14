"""Step 04: 关键帧提取。每场景取 SSIM 动态代表帧 + 超长场景保底采样。

代表帧策略,经实测对 PPT/手写/K线类更稳:比较场景首帧与 ratio 位置帧的 SSIM,
差异大说明画面在变,取 ratio 位置帧;差异小说明基本静止,取靠前帧 start+5,
避免抓到画到一半的过渡态。

解码用 PyAV(系统 libav,含 libdav1d)而非 cv2.VideoCapture:后者用 OpenCV 自带 ffmpeg,
解不了 AV1,场景/关键帧会全空。是什么编码解什么编码,不转码。cv2 仅用于对 numpy 帧
做 resize/SSIM/存图。
"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash


class _VideoReader:
    """PyAV 按帧号读 BGR 帧,替代 cv2.VideoCapture 以支持 AV1 等 OpenCV 解不了的编码。
    seek 到目标时间戳所在关键帧后向前解到目标帧;缩略图用途,无需逐帧精确。"""

    def __init__(self, path: str):
        import av
        self.container = av.open(path)
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "AUTO"
        rate = self.stream.average_rate or self.stream.guessed_rate
        self.fps = float(rate) if rate else 25.0
        self._tb = self.stream.time_base

    def read_at_frame(self, frame_no: int):
        """返回目标帧的 BGR ndarray(format 与 cv2 一致),失败返回 None。"""
        frame_no = max(0, int(frame_no))
        target_pts = int((frame_no / self.fps) / self._tb) if self._tb else 0
        try:
            self.container.seek(target_pts, stream=self.stream, backward=True, any_frame=False)
        except Exception:
            return None
        last = None
        try:
            for frame in self.container.decode(self.stream):
                last = frame
                if frame.pts is None or frame.pts >= target_pts:
                    break
        except Exception:
            return last.to_ndarray(format="bgr24") if last is not None else None
        return last.to_ndarray(format="bgr24") if last is not None else None

    def close(self):
        try:
            self.container.close()
        except Exception:
            pass


class FramesStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "intermediate" / "scenes.json").exists():
            missing.append("intermediate/scenes.json")
        if not (self.job_dir / "input" / "source.mp4").exists():
            missing.append("input/source.mp4")
        return missing

    def input_hashes(self) -> dict[str, str]:
        return {
            "scenes": file_hash(self.job_dir / "intermediate" / "scenes.json"),
            "frame_pick": json.dumps(self.config.get("domain", {}).get("frame_pick", {}), sort_keys=True),
            "sampling": json.dumps(self.config.get("domain", {}).get("sampling", {}), sort_keys=True),
        }

    def execute(self) -> dict | None:
        scenes = self.artifacts.load_json("intermediate/scenes.json")
        video_path = self.job_dir / "input" / "source.mp4"
        assets_dir = self.job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        fp = self.config.get("domain", {}).get("frame_pick", {})
        sp = self.config.get("domain", {}).get("sampling", {})
        ratio = float(fp.get("dynamic_pick_ratio", 0.7))
        dyn_ssim = float(fp.get("dynamic_scene_ssim", 0.85))
        max_gap = float(sp.get("max_gap_sec", 60))
        interval = float(sp.get("forced_interval_sec", 15))

        reader = _VideoReader(str(video_path))
        fps = reader.fps
        candidates: list[dict] = []
        frame_index = 0

        try:
            for i, scene in enumerate(scenes):
                self.progress.report(i, len(scenes), "extracting frames")
                start = float(scene["start_sec"])
                end = float(scene["end_sec"])
                sf = int(start * fps)
                ef = int(end * fps) if end > start else sf + 1

                frame, target = self._pick_representative(reader, sf, ef, ratio, dyn_ssim)
                if frame is not None:
                    ts = target / fps if fps > 0 else start
                    frame_index = self._save(assets_dir, "scene", frame_index, i, ts, frame, candidates)

                # 超长场景:固定间隔保底采样,避免长讲解只有一帧。
                if (end - start) > max_gap:
                    t = start + interval
                    while t < end - 5:
                        fr = reader.read_at_frame(int(t * fps))
                        if fr is not None:
                            frame_index = self._save(assets_dir, "sample", frame_index, i, t, fr, candidates)
                        t += interval
        finally:
            reader.close()

        self.progress.report(len(scenes), len(scenes), "done")
        self.artifacts.write("intermediate/candidates.json", candidates)
        scene_n = sum(1 for c in candidates if c.get("source") == "scene")
        return {"total": len(candidates), "scenes": len(scenes), "sampled": len(candidates) - scene_n}

    def _save(self, assets_dir: Path, source: str, idx: int, scene_i: int,
              ts: float, frame, candidates: list) -> int:
        import cv2  # 仅对 numpy 帧存图;模块已缓存,无开销
        # 统一命名 frame-{NNNN}.jpg(扁平、前端按 assets/<flat> 解析)。source/时间戳不进文件名,
        # 留在清单(下方 candidates 条目)与图注;idx 是跨场景全局自增序号,即占位符 [img:N] 的 N。
        fn = f"frame-{idx:04d}.jpg"
        out = assets_dir / fn
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if out.exists() and out.stat().st_size > 1024:
            candidates.append({
                "index": idx, "scene_index": scene_i,
                "timestamp_sec": round(ts, 2), "filename": fn, "source": source,
            })
            return idx + 1
        return idx

    def _pick_representative(self, reader: _VideoReader, sf: int, ef: int, ratio: float, dyn_ssim: float):
        import cv2
        from skimage.metrics import structural_similarity as ssim

        head = reader.read_at_frame(max(0, sf))
        mf = sf + int((ef - sf) * ratio)
        mid = reader.read_at_frame(max(0, mf))
        if head is None or mid is None:
            return head, sf

        h = cv2.cvtColor(cv2.resize(head, (320, 180)), cv2.COLOR_BGR2GRAY)
        m = cv2.cvtColor(cv2.resize(mid, (320, 180)), cv2.COLOR_BGR2GRAY)
        score = ssim(h, m, data_range=255)
        # 画面在变(SSIM 低)取 ratio 位置帧;基本静止取靠前帧避开过渡态。
        target = mf if score < dyn_ssim else min(sf + 5, ef - 1)
        target = max(0, min(target, ef - 1))
        frame = reader.read_at_frame(target)
        return (frame if frame is not None else head), target


if __name__ == "__main__":
    FramesStep.cli_main("04_frames")
