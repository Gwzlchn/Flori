"""Step 04: 播客智能笔记。AI 把口语转写重组为中文结构化笔记。

转写超过单次阈值时走 map-reduce(分段提炼要点 → 合并成完整笔记),覆盖全集不丢正文;
硬截断会让 90min 长集只总结开头十几分钟。
"""

from __future__ import annotations

from shared.note_text import markdown_to_index_text
from shared.step_base import StepBase, file_hash
from steps.audio.provenance import (
    extract_smart_markers,
    load_audio_source_manifest,
    persist_audio_note_provenance,
    smart_provenance_segments,
    smart_reference_block,
)

# 单次喂 AI 的转写正文上限:不超过它就一次成稿(短集保持原质量);超过则分段 map-reduce。
# 现代模型上下文足够,阈值取宽,常见集仍走单次,只有超长集才分段。
SINGLE_PASS_CHAR_LIMIT = 24000
# map 阶段每段的字符预算(按 segment 边界切,尽量不破句)。
MAP_CHUNK_CHARS = 16000


class SmartPodcastStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "transcript.json").exists():
            return ["intermediate/transcript.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {
            "transcript": file_hash(self.job_dir / "intermediate" / "transcript.json"),
        }
        source_manifest = self.job_dir / "intermediate" / "source_segments.json"
        if source_manifest.exists():
            hashes["source_segments"] = file_hash(source_manifest)
        hashes.update(self.ai.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        return hashes

    def execute(self) -> dict | None:
        transcript = self.artifacts.load_json("intermediate/transcript.json")
        full_text = self._full_text(transcript)

        if len(full_text) <= SINGLE_PASS_CHAR_LIMIT:
            # 短集:一次成稿,full_text 全量喂入不截断。
            result = self.ai.call(self._build_prompt(transcript), max_tokens=8192)
            mode, chunks_n = "single", 1
        else:
            # 长集:map-reduce 覆盖全文。
            result, chunks_n = self._map_reduce(transcript)
            mode = "map_reduce"

        # 先撤掉上一版清单,随后任何解析/校验失败都保持 fail-closed。
        (self.job_dir / "output" / "provenance" / "smart.json").unlink(missing_ok=True)
        source_manifest = load_audio_source_manifest(self.job_dir)
        candidates = []
        if source_manifest is not None:
            result, candidates = extract_smart_markers(result, source_manifest)
        elif "[[source:" in result:
            raise ValueError("audio smart note contains a source marker without a source manifest")

        rel = self.review.write_smart_note(result)   # 版本化落盘(含生成时间/方式/模型)
        mappings = []
        if source_manifest is not None:
            note_bytes = (self.job_dir / rel).read_bytes()
            mappings = smart_provenance_segments(
                markdown_to_index_text(note_bytes.decode("utf-8")), candidates,
            )
        provenance = persist_audio_note_provenance(
            self.job_dir,
            note_type="smart",
            note_artifact=rel,
            provenance_segments=mappings,
        )
        return {"chars": len(result), "mode": mode, "chunks": chunks_n,
                "provider": self.ai.last_provider, "model": self.ai.last_model,
                "note_file": rel,
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"]}

    # 单次成稿

    def _build_prompt(self, transcript: dict) -> str:
        profile = self.ai.load_domain_prompt_profile()

        parts = [self.ai.load_prompt_template("04_smart_podcast")]
        parts.append(self.ai.terminology_block(profile))  # 注入已沉淀的标准概念,各 smart 步共用
        parts.append(self._duration_line(transcript))
        source_block = smart_reference_block(
            transcript, load_audio_source_manifest(self.job_dir),
        )
        if source_block:
            parts.append(source_block)
        else:
            parts.append("\n--- 转写正文 ---\n")
            parts.append(self._full_text(transcript))      # 全量,不截断
        return "".join(parts)

    # 长集 map-reduce

    def _map_reduce(self, transcript: dict) -> tuple[str, int]:
        profile = self.ai.load_domain_prompt_profile()
        source_block = smart_reference_block(
            transcript, load_audio_source_manifest(self.job_dir),
        )
        chunks = (
            self._chunk_text(source_block, MAP_CHUNK_CHARS)
            if source_block else self._chunk_segments(transcript, MAP_CHUNK_CHARS)
        )
        total = len(chunks) + 1  # +1 = reduce 合并步

        summaries: list[str] = []
        for i, chunk in enumerate(chunks):
            self.progress.report(i, total, f"summarizing part {i + 1}/{len(chunks)}")
            summaries.append(self.ai.call(self._map_prompt(chunk, i, len(chunks)),
                                          max_tokens=4096).strip())

        self.progress.report(len(chunks), total, "merging")
        result = self.ai.call(self._reduce_prompt(summaries, transcript, profile),
                              max_tokens=8192)
        self.progress.report(total, total, "done")
        return result, len(chunks)

    def _map_prompt(self, chunk: str, idx: int, n: int) -> str:
        return (
            _MAP_HEADER.format(i=idx + 1, n=n)
            + "\n--- 转写片段 ---\n" + chunk
        )

    def _reduce_prompt(self, summaries: list[str], transcript: dict, profile: dict) -> str:
        parts = [self.ai.load_prompt_template("04_smart_podcast")]
        parts.append(self.ai.terminology_block(profile))
        parts.append(self._duration_line(transcript))
        parts.append(
            "\n以下是该音频【按顺序分段提炼的要点】(非完整转写)。请据此合并、去重、"
            "重组为一篇完整的中文结构化学习笔记,覆盖全部分段、不要遗漏任何要点。"
            "其中 [[source:segment_id]] 是来源坐标,只能原样保留且每个最多出现一次:\n"
        )
        for i, s in enumerate(summaries):
            parts.append(f"\n--- 第 {i + 1}/{len(summaries)} 部分要点 ---\n{s}\n")
        return "".join(parts)

    # 工具

    @staticmethod
    def _full_text(transcript: dict) -> str:
        full_text = transcript.get("full_text", "")
        if not full_text:
            full_text = "".join(s.get("text", "") for s in transcript.get("segments", []))
        return full_text

    @staticmethod
    def _duration_line(transcript: dict) -> str:
        return f"\n时长：约 {int(transcript.get('duration_sec', 0)) // 60} 分钟\n"

    @staticmethod
    def _chunk_segments(transcript: dict, max_chars: int) -> list[str]:
        """按 segment 边界把转写切成 ≤max_chars 的若干段(段内用换行分隔条目,避免粘连)。
        无 segments(只有 full_text)时回退按字符窗切。至少返回一段。"""
        texts = [s.get("text", "") for s in (transcript.get("segments") or []) if s.get("text")]
        if not texts:
            full = SmartPodcastStep._full_text(transcript)
            return [full[i:i + max_chars] for i in range(0, len(full), max_chars)] or [""]

        chunks: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for t in texts:
            if cur and cur_len + len(t) + 1 > max_chars:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            cur.append(t)
            cur_len += len(t) + 1
        if cur:
            chunks.append("\n".join(cur))
        return chunks

    @staticmethod
    def _chunk_text(text: str, max_chars: int) -> list[str]:
        """按行切带来源标记的文本,不拆断 segment_id。"""
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines():
            if current and current_len + len(line) + 1 > max_chars:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks or [""]


# map 阶段:对长集的每个分段提炼要点(中间结果,不写总起/结语)。
_MAP_HEADER = (
    "下面是一段较长播客/音频转写的第 {i}/{n} 部分(口语逐字稿)。\n"
    "请提炼这一部分的要点：保留关键信息、论点、事实、例子与专业术语(术语保留英文)，"
    "不要遗漏；用简洁中文条目输出。[[source:segment_id]] 是不可改写的来源坐标,"
    "必须跟随对应要点且每个最多保留一次。这是中间结果，不要写开场白或结语。\n\n"
)


if __name__ == "__main__":
    SmartPodcastStep.cli_main("04_smart_podcast")
