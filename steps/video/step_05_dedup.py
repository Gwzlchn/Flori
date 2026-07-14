"""Step 05: 截图去重。pHash 快速筛 → SSIM 精确确认。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash


class DedupStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "intermediate" / "candidates.json").exists():
            missing.append("intermediate/candidates.json")
        return missing

    def input_hashes(self) -> dict[str, str]:
        return {
            "candidates": file_hash(self.job_dir / "intermediate" / "candidates.json"),
            "config": json.dumps(self.config.get("domain", {}).get("dedup", {}), sort_keys=True),
        }

    def execute(self) -> dict | None:
        import imagehash
        from PIL import Image

        candidates = self.artifacts.load_json("intermediate/candidates.json")
        assets_dir = self.job_dir / "assets"

        dedup_cfg = self.config.get("domain", {}).get("dedup", {})
        hash_size = dedup_cfg.get("phash_hash_size", 8)
        phash_threshold = dedup_cfg.get("phash_threshold", 6)
        ssim_threshold = dedup_cfg.get("ssim_threshold", 0.92)
        ssim_resize = tuple(dedup_cfg.get("ssim_resize", [320, 180]))

        results = []
        seen_hashes: list[tuple[imagehash.ImageHash, int]] = []

        for i, cand in enumerate(candidates):
            self.progress.report(i, len(candidates), "deduplicating")
            img_path = assets_dir / cand["filename"]

            if not img_path.exists():
                results.append({**cand, "keep": False, "phash": "", "reason": "missing"})
                continue

            try:
                with Image.open(img_path) as img:
                    ph = imagehash.phash(img, hash_size=hash_size)
            except Exception as e:
                self.log.warning("phash_error", path=str(img_path), error=str(e))
                results.append({**cand, "keep": False, "phash": "", "reason": "error"})
                continue

            # 三段式带宽:明显不同直接跳过;足够近直接判重;灰区才上 SSIM 复核。省算力且更准。
            duplicate = False
            for prev_hash, prev_idx in seen_hashes:
                band = self._phash_band(ph - prev_hash, phash_threshold)
                if band == "different":
                    continue
                if band == "duplicate":
                    duplicate = True
                    break
                if self._ssim_check(img_path, assets_dir / candidates[prev_idx]["filename"],
                                    ssim_threshold, ssim_resize):
                    duplicate = True
                    break

            results.append({**cand, "keep": not duplicate, "phash": str(ph)})
            if not duplicate:
                seen_hashes.append((ph, i))

        self.progress.report(len(candidates), len(candidates), "done")
        self.artifacts.write("intermediate/dedup.json", results)
        kept = sum(1 for r in results if r["keep"])
        return {"total": len(results), "kept": kept, "removed": len(results) - kept}

    @staticmethod
    def _phash_band(dist: int, threshold: int) -> str:
        """pHash 距离分三段:>阈值+4 为 'different' 直接跳过;≤阈值为 'duplicate';其间为 'gray',需 SSIM 复核。"""
        if dist > threshold + 4:
            return "different"
        if dist <= threshold:
            return "duplicate"
        return "gray"

    def _ssim_check(self, path_a: Path, path_b: Path, threshold: float, resize: tuple) -> bool:
        try:
            import numpy as np
            from PIL import Image
            from skimage.metrics import structural_similarity as ssim

            with Image.open(path_a) as ia, Image.open(path_b) as ib:
                img_a = np.array(ia.convert("L").resize(resize))
                img_b = np.array(ib.convert("L").resize(resize))
            score = ssim(img_a, img_b, data_range=255)  # 与 step_04 一致显式传,避免依赖 dtype 推断
            return score >= threshold
        except Exception as e:
            # SSIM 复核失败降级为"非重复",仅记 warning。偏保留:宁可多留一帧也不误删。
            self.log.warning("ssim_error", path_a=str(path_a), path_b=str(path_b), error=str(e))
            return False


if __name__ == "__main__":
    DedupStep.cli_main("05_dedup")
