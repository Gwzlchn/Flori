"""Step 02: 关键帧提取。每场景取 SSIM 动态代表帧 + 超长场景保底采样(cv2 帧精确)。

代表帧策略(移植自老原型 analyzer/steps/04_frames.py,经实测对 PPT/手写/K线类更稳):
比较场景首帧与 ratio 位置帧的 SSIM——差异大(画面在变)则取 ratio 位置帧、
差异小(基本静止)则取靠前帧(start+5),避免抓到画到一半的过渡态。
"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash


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
        import cv2

        scenes = self.load_json("intermediate/scenes.json")
        video_path = self.job_dir / "input" / "source.mp4"
        assets_dir = self.job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        fp = self.config.get("domain", {}).get("frame_pick", {})
        sp = self.config.get("domain", {}).get("sampling", {})
        ratio = float(fp.get("dynamic_pick_ratio", 0.7))
        dyn_ssim = float(fp.get("dynamic_scene_ssim", 0.85))
        max_gap = float(sp.get("max_gap_sec", 60))
        interval = float(sp.get("forced_interval_sec", 15))

        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        candidates: list[dict] = []
        frame_index = 0

        try:
            for i, scene in enumerate(scenes):
                self.report_progress(i, len(scenes), "extracting frames")
                start = float(scene["start_sec"])
                end = float(scene["end_sec"])
                sf = int(start * fps)
                ef = int(end * fps) if end > start else sf + 1

                frame, target = self._pick_representative(cap, sf, ef, ratio, dyn_ssim)
                if frame is not None:
                    ts = target / fps if fps > 0 else start
                    frame_index = self._save(cv2, assets_dir, "scene", frame_index, i, ts, frame, candidates)

                # 超长场景:固定间隔保底采样,避免长讲解只有一帧。
                if (end - start) > max_gap:
                    t = start + interval
                    while t < end - 5:
                        fr = self._seek(cap, int(t * fps))
                        if fr is not None:
                            frame_index = self._save(cv2, assets_dir, "sample", frame_index, i, t, fr, candidates)
                        t += interval
        finally:
            cap.release()

        self.report_progress(len(scenes), len(scenes), "done")
        self.write_output("intermediate/candidates.json", candidates)
        scene_n = sum(1 for c in candidates if c.get("source") == "scene")
        return {"total": len(candidates), "scenes": len(scenes), "sampled": len(candidates) - scene_n}

    def _save(self, cv2, assets_dir: Path, source: str, idx: int, scene_i: int,
              ts: float, frame, candidates: list) -> int:
        fn = f"{source}_{idx:04d}_{ts:.1f}s.jpg"
        out = assets_dir / fn
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if out.exists() and out.stat().st_size > 1024:
            candidates.append({
                "index": idx, "scene_index": scene_i,
                "timestamp_sec": round(ts, 2), "filename": fn, "source": source,
            })
            return idx + 1
        return idx

    def _pick_representative(self, cap, sf: int, ef: int, ratio: float, dyn_ssim: float):
        import cv2
        from skimage.metrics import structural_similarity as ssim

        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, sf))
        ok1, head = cap.read()
        mf = sf + int((ef - sf) * ratio)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, mf))
        ok2, mid = cap.read()
        if not ok1 or not ok2 or head is None or mid is None:
            return (head if ok1 else None), sf

        h = cv2.cvtColor(cv2.resize(head, (320, 180)), cv2.COLOR_BGR2GRAY)
        m = cv2.cvtColor(cv2.resize(mid, (320, 180)), cv2.COLOR_BGR2GRAY)
        score = ssim(h, m, data_range=255)
        # 画面在变(SSIM 低)取 ratio 位置帧;基本静止取靠前帧避开过渡态。
        target = mf if score < dyn_ssim else min(sf + 5, ef - 1)
        target = max(0, min(target, ef - 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok3, frame = cap.read()
        return (frame if ok3 else head), target

    def _seek(self, cap, frame_no: int):
        import cv2
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_no))
        ok, fr = cap.read()
        return fr if ok else None


if __name__ == "__main__":
    FramesStep.cli_main("04_frames")
