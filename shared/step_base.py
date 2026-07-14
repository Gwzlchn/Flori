"""StepBase 统一基类。所有 steps/*.py 继承此类。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import structlog

from .ai_gateway import AIGateway, record_usage_to_file
from .ai_routing import (
    InvalidAIOverrideError,
    READ_TOOL_TAG,
    ai_required_tags,
    parse_ai_override,
    step_required_capability_tags_sync,
)
from .errors import ProcessingError, StepError
from .models import AIUsage, DEFAULT_AI_MODEL, LLMRequest, LLMResponse


def file_hash(path: Path) -> str:
    """计算文件 SHA-256,返回 'sha256:{hex}' 格式。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def def_digest_for(version: str | int | None, ai: dict | None) -> str:
    """本步 pipeline 定义指纹 = sha256(version + ai)。单一来源:StepBase._def_digest 与
    "重建过期"判断(api 侧 is_job_expired)都调它,防公式漂移。版本来自 pipelines.yaml(使用者维护),
    不取代码/git;prompt 内容定制走 {step}.md/profiles/styles(经 input_hashes 纳入,与此正交)。"""
    defn = {"version": str(version if version is not None else "1"), "ai": ai or {}}
    blob = json.dumps(defn, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pipeline_digest_for(steps: list[dict]) -> str:
    """整条 pipeline 的定义指纹聚合 = sha256(各步 name→def_digest 排序后)。
    任一步 version/ai 变 → 聚合变;落 job.pipeline_digest 供"过期"批量快查(免逐 .done 比对)。"""
    per = {s.get("name", ""): def_digest_for(s.get("version"), s.get("ai")) for s in steps}
    blob = json.dumps(per, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SubprocessFailed(subprocess.CalledProcessError):
    """str() 附带 stderr 尾部,便于诊断:基类 CalledProcessError 的 str() 只有退出码,丢 capture 的 stderr。
    仍是 CalledProcessError 子类,既有 `except CalledProcessError` 的调用方不受影响。"""

    def __str__(self) -> str:
        base = super().__str__()
        tail = self.stderr or ""
        if isinstance(tail, (bytes, bytearray)):
            tail = tail.decode(errors="replace")
        tail = tail.strip()[-1500:]
        return f"{base}\nstderr(tail):\n{tail}" if tail else base


class StepBase:
    def __init__(self, step_name: str, job_dir: Path, config: dict):
        self.step_name = step_name
        self.job_dir = job_dir
        self.config = config
        self.log = self._setup_logger()
        self._gateway: AIGateway | None = None
        self._call_index = 0
        # 最近一次 AI 调用实际命中的 provider / model(供版本化笔记标记)。
        self.last_ai_provider: str | None = None
        self.last_ai_model: str | None = None
        # 消费方只读最近一次响应元数据,正文仍由 call_ai 返回,不扩大各 step 接口。
        self.last_ai_response: LLMResponse | None = None
        self._resolved_prompts: dict[str, object] = {}
        self._prompt_overrides_snapshot: dict | None = None
        self._active_prompt_name: str | None = None
        # AI 审计日志(prompt 白盒化):本步每次 LLM 调用一条,内存累积后落 output/ai_logs/{step}.jsonl。
        # 留内存副本是为 call_ai_json 解析后能回填 output_processed(amend 最后一条)。
        self._ai_log_records: list[dict] = []
        # workdir 复用(STORAGE_WORKDIR_REUSE)下重试是新进程:装载已有 jsonl 续写,call_index 续增。
        # 否则首次 _flush_ai_logs 整文件重写会吞掉上一次(被外杀)执行留下的 pending 记录。
        self._load_existing_ai_logs()

    # 统一入口

    def run(self) -> None:
        try:
            # job.json 是任务控制输入。执行前先验证 override,使非法形状稳定归类 input_invalid,
            # 不能等到某次 AI 调用才偶然触发或被非 AI 路径跳过。
            self._read_override()
            missing = self.validate_inputs()
            if missing:
                from .errors import InputMissingError
                raise InputMissingError(f"Missing: {missing}")

            if not self.should_run():
                self.log.info("skip: up-to-date")
                return

            start = time.time()
            result = self.execute()
            duration = time.time() - start

            self.mark_done()
            self.write_meta({
                "status": "done",
                "duration_sec": round(duration, 1),
                **(result or {}),
            })
        except StepError as e:
            self.write_error(e.error_type, str(e))
            # 同时打到 stderr:step_runner 会把它 tee 进 logs/{step}.log(失败也推存储)并 capture
            # 为 stderr_tail,worker 据此记真实错误,而非只写 error.json 导致 worker 记 "unknown error"。
            print(f"[{e.error_type}] {e}", file=sys.stderr, flush=True)
            sys.exit(1)
        except Exception as e:
            self.write_error("unknown", str(e), traceback.format_exc())
            traceback.print_exc()  # 完整栈到 stderr → logs/{step}.log,前端可见、worker 记真因
            sys.exit(1)

    @classmethod
    def cli_main(cls, step_name: str) -> None:
        """步骤脚本统一入口,运行身份以 step config 为准."""
        import argparse

        from .logging_setup import setup_logging
        setup_logging()  # 步骤子进程日志也输出结构化 JSON,与 scheduler/worker 一致

        parser = argparse.ArgumentParser()
        parser.add_argument("--job-dir", required=True)
        parser.add_argument("--step-config", required=True)
        args = parser.parse_args()
        config = json.loads(Path(args.step_config).read_text())
        configured_name = (config.get("step") or {}).get("name")
        if not isinstance(configured_name, str) or not configured_name:
            from .errors import InputInvalidError
            raise InputInvalidError("step config is missing runtime name")
        cls(configured_name, Path(args.job_dir), config).run()

    # 子类实现

    def execute(self) -> dict | None:
        raise NotImplementedError

    def validate_inputs(self) -> list[str]:
        return []

    def input_hashes(self) -> dict[str, str]:
        return {}

    def _def_digest(self) -> str:
        """本步 pipeline 定义指纹——版本来自 pipelines.yaml(经 build_step_config 进 self.config),不取代码/git。
        纳入:step.version(使用者在 YAML 维护的版本号)+ ai(provider/model)。
        改 YAML 的 version 或 ai 模型会改变该指纹,should_run 即判需重跑(该步+下游;上游指纹未变仍跳过)。
        prompt 内容定制走 {step}.md/profiles/styles(已在各步 input_hashes 经 prompt_profile_style_hashes 纳入)。"""
        step = self.config.get("step", {}) if isinstance(self.config, dict) else {}
        ai = self.config.get("ai", {}) if isinstance(self.config, dict) else {}
        return def_digest_for(step.get("version", "1"), ai)

    # 幂等:指纹机制见 docs/04-module-design/step-base.md §2

    def should_run(self) -> bool:
        done_file = self.job_dir / f".{self.step_name}.done"
        if not done_file.exists():
            return True
        stored = json.loads(done_file.read_text())
        if stored.get("input_hashes") != self.input_hashes():
            return True
        # pipeline 定义版本(def_digest):仅当旧 .done 已记录该键才比对。
        # 没有此键的老 .done 不因新增字段而强制重跑,避免发版时全量重跑。
        # mark_done 会记录该键;此后改 YAML version / ai 模型即触发该步重跑。
        stored_def = stored.get("def_digest")
        if stored_def is not None and stored_def != self._def_digest():
            return True
        return False

    def mark_done(self) -> None:
        data = {
            "step": self.step_name,
            "input_hashes": self.input_hashes(),
            "def_digest": self._def_digest(),
            "finished_at": datetime.now().isoformat(),
        }
        (self.job_dir / f".{self.step_name}.done").write_text(
            json.dumps(data, ensure_ascii=False, indent=2)
        )

    # IO 工具

    def write_output(self, filename: str, data) -> None:
        target = self.job_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        if isinstance(data, (dict, list)):
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        elif isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        elif isinstance(data, bytes):
            tmp.write_bytes(data)
        tmp.rename(target)

    def ai_provider_model(self) -> tuple[str, str]:
        """最近一次 AI 调用的 (provider, model),供笔记/评审统一标注。"""
        prov = self.last_ai_provider or "unknown"
        model = self.last_ai_model or "unknown"
        if prov == "claude-cli" and model in ("unknown", ""):
            model = DEFAULT_AI_MODEL
        return prov, model

    # claude-cli 视觉笔记走 --allowedTools Read 多轮,常 agentic 化:开头插"已完成/我做了什么/
    # I've reviewed…"过程汇报、结尾追加"要不要我再…"提议,个别甚至只回一段"已保存到 xx.md"的
    # 元汇报而正文整段丢失。系统 prompt 已明令禁止仍被无视,故在落盘前做结构化净化。
    _PREAMBLE_MARK = (
        "已完成", "我做了什么", "我做的", "我的处理", "处理思路", "重组思路", "笔记结构一览",
        "结构化学习笔记", "保存在", "保存到", "已生成并保存", "思路如下",
        "I've ", "I have ", "I now ", "Here'", "Here is", "Let me ", "I'll ",
    )
    _OFFER_MARK = (
        "要不要我", "需要我", "如需", "需要的话", "如果需要", "我可以再", "我还可以",
        "是否需要", "可以帮你", "如有需要", "Let me know", "Would you like", "If you",
    )
    # 结尾第一人称过程自述(展示型笔记是第三人称,不该出现"我已…重组/标注/内嵌…"的收尾签名)。
    _TRAIL_META = (
        "我已", "我把", "我按", "已按", "我对", "我将", "我用", "我把视频", "我已经",
        "I've ", "I have ", "I've reorganized", "I restructured",
    )
    # 抢救失败的退化标志:正文自称把笔记存进了文件(实际 --allowedTools 只放 Read,根本没写,
    # 即正文未被输出),或首个标题就是"我做了什么"之类元小节。
    _META_HEAD = (
        "我做了什么", "我做的", "我的处理", "处理说明", "处理思路", "重组思路",
        "笔记结构一览", "改动说明", "What I did", "Summary",
    )

    # 单轮纯文本 API provider:不会"只回过程汇报而丢正文",短笔记(短文章/短播客)也合法,
    # 故只对它们做去壳、不做"过短/元标题"判废——判废是 claude-cli 视觉多轮 agentic 退化的专治。
    # 单轮 API provider 名(与 providers.yaml 的 provider 键一致)。注:本地 ollama 后端的 provider
    # 键是 'local'(providers.yaml),'ollama' 只是其 api_key 字面值——故含 'local';'ollama' 暂留兼容。
    _API_PROVIDERS = ("anthropic", "deepseek", "kimi", "openai", "ollama", "local")

    @classmethod
    def _sanitize_smart_note(cls, content: str, provider: str | None = None) -> str:
        """剥离 claude agentic 口水(开头过程汇报 / 结尾后续提议),并判废退化输出。
        正文存在时只去壳;若剥完不像笔记(过短 / 首标题是元小节)则抛 ProcessingError 触发
        重试——宁可重跑也不存废稿。判废仅对 claude-cli/未知 provider 生效(见 _API_PROVIDERS);
        provider 缺省按严格处理(兼容直调与视频两段式 claude-cli 路径)。"""
        import re
        s = (content or "").strip()
        if os.environ.get("DRY_RUN") == "1":   # 干跑产物是合成占位,不做净化/判废(与 gateway 同判定;"0"=关)
            return s
        strict = (provider or "") not in cls._API_PROVIDERS   # None/""/claude-cli/unknown → 严格判废
        # 1) 去开头元描述:仅当前缀命中 agentic 标记时,砍到首个 markdown 标题(正文应以标题起)。
        if any(m in s[:160] for m in cls._PREAMBLE_MARK):
            m = re.search(r"(?m)^#{1,6} ", s)
            if m:
                s = s[m.start():].strip()
        # 2) 去结尾口水:从尾部逐段砍掉对话式提议("要不要我…")与第一人称过程签名("我已…重组")。
        #    提议限短段(<200,marker 在段首附近);过程签名限段首命中且 <500(章节清单可能较长)。
        paras = s.split("\n\n")
        while paras:
            tail = paras[-1].strip().lstrip("-*># ").strip()
            is_offer = len(tail) < 200 and any(o in tail[:24] for o in cls._OFFER_MARK)
            is_meta = len(tail) < 500 and any(tail.startswith(o) for o in cls._TRAIL_META)
            if tail and (is_offer or is_meta):
                paras.pop()
                while paras and paras[-1].strip() in ("---", "***", "___"):
                    paras.pop()
            else:
                break
        s = "\n\n".join(paras).strip()
        # 3) 退化判废:正文整段丢失,只剩"我做了什么/已保存到 xx.md"式元汇报。
        #    仅 claude-cli/未知 provider 才判废;API provider 单轮纯输出的短笔记是正常的,不误杀。
        first_head = next((ln for ln in s.splitlines() if ln.lstrip().startswith("#")), "")
        head_is_meta = any(mk in first_head for mk in cls._META_HEAD)
        if strict and (len(s) < 500 or head_is_meta):
            raise ProcessingError(
                f"智能笔记疑似 agentic 退化(len={len(s)}, 首标题={first_head[:40]!r}):"
                "claude 可能只回了过程汇报而非笔记正文,触发重试。"
            )
        # 4) 归一图片路径:smart 的 prompt 让 AI 写文件名,它有时给裸名(无 assets/ 前缀);
        #    前端按 assets/ 解析本地资源,缺前缀就图裂。给缺前缀的本地图片补 assets/
        #    (放过 http(s)/绝对路径/已带 assets/ 的)。
        s = re.sub(
            r"(!\[[^\]]*\]\()(?!https?:|/|assets/)([^)\s]+\.(?:jpg|jpeg|png|webp|gif))(\))",
            r"\1assets/\2\3", s,
        )
        return s

    @staticmethod
    def _backfill_image_refs(content: str, image_map: dict) -> str:
        """把 AI 写的 ![描述](img:N) 占位符按资产清单回填成 ![描述](assets/<filename>)。
        N=资产清单序号(index)。AI 全程不碰路径/文件名,不会漏 assets/ 前缀导致图裂;
        未命中的 N(AI 编的/越界/无内嵌位图)整条图片删掉,避免前端渲染出裸占位符文本。"""
        import re as _re
        def _sub(m):
            fn = image_map.get(int(m.group(2)))
            return f"{m.group(1)}assets/{fn}{m.group(3)}" if fn else ""
        return _re.sub(r"(!\[[^\]]*\]\()\s*img:(\d+)\s*(\))", _sub, content or "")

    def write_smart_note(self, content: str, image_assets: list | None = None) -> str:
        """智能笔记按版本落盘:output/versions/notes_smart_{provider}_{model}_{时间}.md,
        开头加一行说明(生成时间 / 方式 / 模型)。不写规范 notes_smart.md,前端取最新版本。
        落盘前先按清单把 ![..](img:N) 占位符回填成真实 assets/ 路径(image_assets 给 N→filename),
        再净化 agentic 口水并兜底补 assets/ 前缀(_sanitize_smart_note)。
        返回相对路径,供评审步在 review.json 里标明评的是哪一版。"""
        prov, model = self.ai_provider_model()
        if image_assets:
            image_map = {int(a["n"]): a["filename"] for a in image_assets if a.get("filename")}
            content = self._backfill_image_refs(content, image_map)
        content = self._sanitize_smart_note(content, prov)
        # 字段内只允许字母数字与 . - (把 _ 也归一为 -),保证文件名按 "_" 切分无歧义。
        safe = lambda s: __import__("re").sub(r"[^0-9A-Za-z.-]+", "-", s).strip("-") or "x"
        now = datetime.now()
        rel = f"output/versions/notes_smart_{safe(prov)}_{safe(model)}_{now.strftime('%Y%m%d-%H%M%S')}.md"
        header = f"> 生成于 {now.strftime('%Y/%m/%d %H:%M:%S')} · 方式 {prov} · 模型 {model}\n\n"
        self.write_output(rel, header + content)
        return rel

    @staticmethod
    def clip_note_for_review(smart: str) -> tuple[str, dict]:
        """兼容旧调用名,评审 v2 始终返回完整笔记并记录未截断。"""
        cov = {
            "note_chars": len(smart),
            "reviewed_chars": len(smart),
            "truncated": False,
        }
        return smart, cov

    def write_review(self, review: dict, note_file: str | None) -> None:
        """评审结果落盘:补记 生成时间 / 方式 / 模型 + 评的是哪一版智能笔记(note_file)。
        写 review.json(最新,供术语采集/默认),并按所评笔记版本 1:1 落一份版本化评审。"""
        prov, model = self.ai_provider_model()
        review["note_file"] = note_file
        review["provider"] = prov
        review["model"] = model
        review["generated_at"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.write_output("output/review.json", review)
        if note_file:
            from .notes_versions import review_path_for_note
            vrel = review_path_for_note(note_file)
            if vrel:
                self.write_output(vrel, review)

    def build_review_prompt(
        self, *, intro: str, dimensions: list[tuple[str, str]], ref_block: str,
    ) -> str:
        """拼装评审 prompt:从 resolver 加载任务覆盖,热编辑或镜像 tracked 骨架,
        再把 {{intro}}/{{dimensions}}/{{score_example}}/{{ref_block}} 按本步实参注入(str.replace,
        prompt 含字面 {} 不可 format)。各评审步只传 intro / dimensions=[(维度键, 中文说明)] / ref_block。
        score_keys 解析始终从步内 dimensions 取(见各步 execute),绝不解析模板文本,故覆盖文本被
        改坏也不破坏评分 JSON 解析。维度展示在 prompt 里只为让 AI 知道评什么,解析靠代码键。"""
        dim_lines = "".join(
            f"{i}. {key}: {desc}\n" for i, (key, desc) in enumerate(dimensions, 1)
        )
        example_scores = ", ".join(f'"{key}": 4' for key, _ in dimensions)
        template = self._load_prompt_template(self.step_name)
        rendered = (
            template
            .replace("{{intro}}", intro)
            .replace("{{dimensions}}", dim_lines)
            .replace("{{score_example}}", example_scores)
            .replace("{{ref_block}}", ref_block)
        )
        # 安全:若覆盖/模板被改得缺了 {{ref_block}} 占位,被评笔记会整段丢失 → 兜底补在末尾,
        # 保证 AI 永远拿得到待评内容(参照块是评审的核心输入,不可丢)。
        if "{{ref_block}}" not in template:
            rendered = rendered.rstrip() + "\n\n" + ref_block
        return rendered

    def review_fallback(self, score_keys: list[str]) -> dict:
        """保留旧扩展的调用兼容;评审 v2 不消费返回值,不得据此生成可靠分数。"""
        fallback = {key: 3 for key in score_keys}
        fallback.update(
            overall=3.0, key_terms=[], missing_concepts=[],
            top3_improvements=["AI 返回的不是有效 JSON"],
        )
        return fallback

    def prepare_smart_for_review(self) -> tuple[str, dict, str, dict]:
        """读取完整的最新智能笔记并返回正文、覆盖信息与稳定相对路径。"""
        from .review_contract import source_record

        smart_path = self.latest_smart_note()
        if smart_path is None:
            raise ValueError("review source has no smart note")
        note_file = str(smart_path.relative_to(self.job_dir))
        smart, record = source_record(self.job_dir, note_file, label="smart")
        smart_clip, coverage = self.clip_note_for_review(smart)
        return smart_clip, coverage, note_file, record

    def run_dimension_review(
        self, prompt, fallback, score_keys, note_file, coverage,
        *, review_sources: list[dict] | None = None,
        review_source_texts: dict[str, str] | None = None,
        citation_validation: dict | None = None,
        evidence_manifest_record: dict | None = None,
    ):
        """评审 v2:完整输入留痕、结束原因归一、严格 JSON 与引用门禁。"""
        del fallback  # v2 不再用虚构的全 3 分冒充评分。
        from .review_contract import (
            MAX_REVIEW_SOURCE_AGGREGATE_BYTES,
            MAX_REVIEW_SOURCE_BYTES,
            MAX_REVIEW_SOURCES,
            parse_review,
            sha256_bytes,
        )

        if type(prompt) is not str:
            raise ValueError("review input must be a string")
        try:
            prompt_data = prompt.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("review input must be UTF-8") from exc
        if len(prompt_data) > MAX_REVIEW_SOURCE_BYTES:
            raise ValueError(
                f"review input exceeds {MAX_REVIEW_SOURCE_BYTES} bytes",
            )
        validate_sources = review_sources is not None or review_source_texts is not None
        sources = review_sources or []
        if validate_sources and (not sources or len(sources) > MAX_REVIEW_SOURCES):
            raise ValueError("review sources count is invalid")
        labels: set[str] = set()
        declared_total = 0
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("review source record is invalid")
            label = source.get("label")
            size = source.get("bytes")
            if type(label) is not str or not label or label in labels:
                raise ValueError("review source label is invalid")
            if type(size) is not int or size < 0 or size > MAX_REVIEW_SOURCE_BYTES:
                raise ValueError("review source size is invalid")
            labels.add(label)
            declared_total += size
        if declared_total > MAX_REVIEW_SOURCE_AGGREGATE_BYTES:
            raise ValueError("review sources exceed aggregate byte limit")
        source_texts = review_source_texts or {}
        actual_total = 0
        for label in labels:
            text = source_texts.get(label)
            if type(text) is not str:
                raise ValueError("review source text is missing")
            actual_total += len(text.encode("utf-8"))
        if validate_sources and (
            actual_total != declared_total
            or actual_total > MAX_REVIEW_SOURCE_AGGREGATE_BYTES
        ):
            raise ValueError("review source bytes do not match records")
        self.write_output("output/review_input.md", prompt)
        prompt_rel = "output/review_input.md"
        if note_file:
            name = Path(note_file).name.replace("notes_smart_", "review_input_", 1)
            prompt_rel = f"output/versions/{name}"
            self.write_output(prompt_rel, prompt)
        review_input = {
            "artifact": prompt_rel,
            "sha256": sha256_bytes(prompt_data),
            "bytes": len(prompt_data),
            "chars": len(prompt),
            "truncated": bool(coverage.get("truncated")),
            "sources": sources,
        }
        if evidence_manifest_record is not None:
            review_input["evidence_manifest"] = evidence_manifest_record
        raw = self.call_ai(prompt, response_format="json", temperature=0)
        response = self.last_ai_response or LLMResponse(
            content=raw, model=self.last_ai_model or "unknown",
            provider=self.last_ai_provider or "unknown", finish_reason=None,
        )
        review, parse_failed = parse_review(
            raw, score_keys, response, review_input=review_input,
            review_source_texts=source_texts,
            citation_validation=citation_validation,
        )
        review["review_coverage"] = coverage
        self.write_review(review, note_file)
        return review, parse_failed

    def latest_smart_note(self) -> Path | None:
        """工作目录里最新的智能笔记版本文件(供评审步读取并标注评的是哪一版)。"""
        from .notes_versions import latest_smart
        vdir = self.job_dir / "output" / "versions"
        if not vdir.is_dir():
            return None
        rels = [f"output/versions/{p.name}" for p in vdir.glob("notes_smart_*.md")]
        latest = latest_smart(rels)
        return (self.job_dir / latest) if latest else None

    def load_json(self, filename: str) -> dict | list:
        return json.loads((self.job_dir / filename).read_text(encoding="utf-8"))

    def write_meta(self, meta: dict) -> None:
        path = self.job_dir / f".{self.step_name}.meta.json"
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    def write_error(self, error_type: str, message: str, trace: str = "") -> None:
        path = self.job_dir / f".{self.step_name}.error.json"
        path.write_text(json.dumps({
            "step": self.step_name,
            "error_type": error_type,
            "message": message,
            "trace": trace,
            "timestamp": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2))

    # 进度

    def report_progress(self, current: int, total: int, message: str = "") -> None:
        pct = round(100 * current / max(total, 1))
        path = self.job_dir / f".{self.step_name}.progress"
        path.write_text(json.dumps({
            "source": "step",
            "current": current,
            "total": total,
            "pct": pct,
            "message": message,
            "updated_at": time.time(),
        }))
        if pct % 10 == 0 or current == total:
            self.log.info("progress", current=current, total=total, pct=pct)

    # AI 调用

    def _read_override(self) -> str:
        """读 job.json 里本步的 provider 覆盖(文件不存在则空串)。
        override_provider 与 _apply_provider_override 共用这份读盘+解析,防口径漂移。"""
        try:
            job = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except OSError as exc:
            self.log.warning("ai_override_read_failed", reason="job_json_unreadable")
            raise InvalidAIOverrideError("invalid AI override: job_json_unreadable") from exc
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self.log.warning("ai_override_read_failed", reason="job_json_invalid")
            raise InvalidAIOverrideError("invalid AI override: job_json_invalid") from exc
        override, shape_error = parse_ai_override(
            job, self.step_name, self.config.get("providers", {}),
        )
        if shape_error:
            self.log.warning("ai_override_invalid", reason=shape_error)
            raise InvalidAIOverrideError(f"invalid AI override: {shape_error}")
        return override or ""

    def override_provider(self) -> str:
        """本步的 provider 覆盖(无则空串)。供 input_hashes 纳入,
        使"换 provider 重跑"改变指纹、绕过幂等跳过。"""
        return self._read_override()

    def _apply_provider_override(
        self, *, required_capabilities=(), actual_capabilities: bool = False,
    ) -> None:
        """按 job.json 的 ai_overrides[step] 覆盖本步 provider(供"选 provider 重跑")。
        只用所选 provider(去掉 fallback),避免失败时静默回退到别的 provider,
        保证版本化笔记的 provider 标记如实。"""
        provider = self._read_override()
        selected_ai = self.config.get("ai")
        if provider:
            pcfg = self.config.get("providers", {}).get("providers", {}).get(provider, {})
            models = pcfg.get("models", [])
            model = models[0] if models else (pcfg.get("model") or "unknown")
            selected_ai = {"primary": {"provider": provider, "model": model}}

        try:
            if actual_capabilities:
                capability_tags = sorted(set(required_capabilities))
            else:
                capability_tags = step_required_capability_tags_sync(
                    self.config.get("step", {}),
                    lambda rel: (self.job_dir / rel).is_file()
                    and (self.job_dir / rel).stat().st_size > 0,
                )
                capability_tags = sorted({*capability_tags, *required_capabilities})
            ai_required_tags(
                selected_ai, self.config.get("providers", {}),
                required_tags=capability_tags,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise InvalidAIOverrideError(
                f"invalid AI capability: {exc}",
            ) from exc
        if provider:
            self.config["ai"] = selected_ai
            self._gateway = None  # 强制按新 ai 配置重建

    def call_ai(self, prompt: str, images: list[Path] | None = None, **kwargs) -> str:
        allowed_tools = kwargs.get("allowed_tools")
        required_capabilities = {
            READ_TOOL_TAG
            for tool in (allowed_tools if isinstance(allowed_tools, (list, tuple)) else [])
            if type(tool) is str and tool.strip().lower() == "read"
        }
        self._apply_provider_override(
            required_capabilities=required_capabilities,
            actual_capabilities=True,
        )
        if self._gateway is None:
            self._gateway = AIGateway(
                self.config.get("providers", {}),
                {"steps": [{"name": self.step_name, "ai": self.config.get("ai", {})}]},
            )

        system = self._load_system_prompt()
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            images=images or [],
            system=system,
            **kwargs,
        )

        import asyncio
        ts_start = datetime.now()
        # 发起前先落 pending(输入侧留痕):步被外杀(超时 SIGKILL)时本次调用不再零审计。
        pend_pos = self._write_ai_log_pending(prompt, system, images, request, ts_start)
        try:
            response = asyncio.run(self._gateway.call(self.step_name, request))
        except Exception as e:
            # 失败也整条记审计(含尝试链 + 当时 prompt),诊断"喂了啥/哪个 provider 挂了"。
            self._write_ai_log_safe(prompt, system, images, request, None,
                                    ts_start, datetime.now(), error=e,
                                    replace_pos=pend_pos)
            self._call_index += 1
            raise
        ts_end = datetime.now()
        self.last_ai_provider = response.provider
        self.last_ai_model = response.model
        self.last_ai_response = response

        self.log.info(
            "ai_call",
            provider=response.provider,
            model=response.model,
            cost_usd=response.cost_usd,
            tokens=f"{response.input_tokens}+{response.output_tokens}",
        )

        step_exec_id = os.environ.get("STEP_EXEC_ID", f"{self.job_dir.name}:{self.step_name}")
        log_dir = self.job_dir / "logs"
        record_usage_to_file(
            AIUsage(
                exec_id=f"{step_exec_id}:{self._call_index}",
                provider=response.provider,
                model=response.model,
                job_id=self.job_dir.name,
                step=self.step_name,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_creation_input_tokens=response.cache_creation_input_tokens,
                cache_read_input_tokens=response.cache_read_input_tokens,
                cost_usd=response.cost_usd,
                duration_sec=response.duration_sec,
                num_turns=response.num_turns,
                cached=response.cached,
            ),
            log_dir,
        )
        self._write_ai_log_safe(prompt, system, images, request, response,
                                ts_start, ts_end, error=None, replace_pos=pend_pos)
        self._call_index += 1
        return response.content

    # AI 审计日志(prompt 白盒化:每次 LLM 调用一条 → output/ai_logs/{step}.jsonl)

    def _ai_log_path(self) -> Path:
        return self.job_dir / "output" / "ai_logs" / f"{self.step_name}.jsonl"

    def _load_existing_ai_logs(self) -> None:
        """装载 workdir 里已有的本步审计 jsonl(重试/复用续写):历史记录保留、call_index 从最大值续增。
        best-effort:损坏的历史审计不该挡步执行(此时从 0 开始,首次 flush 会重写整文件)。"""
        try:
            path = self._ai_log_path()
            if not path.exists():
                return
            records = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
            self._ai_log_records = records
            self._call_index = max(
                (int(r.get("call_index", -1)) for r in records), default=-1,
            ) + 1
        except Exception:
            self.log.warn("ai_log_load_existing_failed", step=self.step_name)

    def _flush_ai_logs(self) -> None:
        path = self._ai_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                    for r in self._ai_log_records),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _write_ai_log_pending(self, prompt, system, images, request, ts_start) -> int | None:
        """call 发起【前】先落一条 phase=pending 记录(输入侧全量:渲染后 prompt/system、模板来源、
        input_hashes、call_meta)并即刻 flush 落盘:步被外杀(超时 SIGKILL)时磁盘仍留本次调用的输入
        与 ts_start,可与 worker 家目录 transcript 按时间窗对上。返回记录位置供完成后原位替换;
        best-effort,失败返 None(final 走 append,不影响主流程)。"""
        try:
            rec = self._build_ai_log_record(prompt, system, images, request,
                                            None, ts_start, ts_start, None)
            rec["phase"] = "pending"
            rec["ok"] = None        # 未知结局(区别于 final 的 True/False)
            rec["error"] = None
            self._ai_log_records.append(rec)
            self._flush_ai_logs()
            return len(self._ai_log_records) - 1
        except Exception:
            self.log.warn("ai_log_pending_write_failed", step=self.step_name)
            return None

    def _write_ai_log_safe(self, prompt, system, images, request, response,
                           ts_start, ts_end, error=None, replace_pos=None) -> None:
        """落一条 AI 审计记录(best-effort,绝不影响主流程)。replace_pos 指向本次调用的 pending
        记录 → 原位替换为 final(同 call_index 才换,防错位);否则 append。"""
        try:
            rec = self._build_ai_log_record(prompt, system, images, request,
                                            response, ts_start, ts_end, error)
            rec["phase"] = "final"
            if (replace_pos is not None
                    and 0 <= replace_pos < len(self._ai_log_records)
                    and self._ai_log_records[replace_pos].get("phase") == "pending"
                    and self._ai_log_records[replace_pos].get("call_index") == rec["call_index"]):
                self._ai_log_records[replace_pos] = rec
            else:
                self._ai_log_records.append(rec)
            self._flush_ai_logs()
        except Exception:
            self.log.warn("ai_log_write_failed", step=self.step_name)

    def _amend_last_ai_log(self, patch: dict) -> None:
        """call_ai_json 解析后回填 output_processed 到最后一条(best-effort)。"""
        try:
            if not self._ai_log_records:
                return
            self._ai_log_records[-1].update(patch)
            self._flush_ai_logs()
        except Exception:
            pass

    @staticmethod
    def _flori_meta() -> dict:
        return {
            "image_tag": os.environ.get("FLORI_IMAGE_TAG") or os.environ.get("IMAGE_TAG"),
            "version": os.environ.get("FLORI_VERSION"),
            "git_commit": os.environ.get("FLORI_GIT_COMMIT"),
        }

    def _collect_transcript(self, response, attempts) -> dict:
        """agentic 全轨迹白盒:claude CLI 的中间轮(WebSearch/Bash/逐图 Read)只在 CLI 自写的会话
        transcript 里(顶层 json 仅最终汇总)。把它拷为 job 产物 sidecar
        `output/ai_logs/{step}.turns.{call_index}.jsonl`(随产物推中心存储、删 job 一起删=陪葬设计),
        审计记录留引用。失败调用经尝试链的 transcript_path 同样回收。
        找不到(非 CLI provider / HOME 未挂 / 会话无档)→ file=None + reason,绝不影响主流程。"""
        src = getattr(response, "transcript_path", None) if response is not None else None
        if not src:
            for a in reversed(attempts or []):
                if a.get("transcript_path"):
                    src = a["transcript_path"]
                    break
        if not src:
            return {"file": None, "reason": "no transcript (non-CLI provider or session log unavailable)"}
        try:
            data = Path(src).read_bytes()
            rel = f"output/ai_logs/{self.step_name}.turns.{self._call_index}.jsonl"
            dst = self.job_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            return {"file": rel, "turns": data.count(b"\n"), "bytes": len(data), "source": str(src)}
        except Exception as e:
            return {"file": None, "reason": f"copy failed: {e}"[:200]}

    def _build_ai_log_record(self, prompt, system, images, request, response,
                             ts_start, ts_end, error) -> dict:
        import socket
        cfg = self.config or {}
        paths = cfg.get("paths") or {}
        prompts_dir = paths.get("prompts_dir")
        domain = (cfg.get("domain") or {}).get("name")

        profile: dict = {}
        try:
            profile = self.load_domain_prompt_profile() or {}
        except Exception:
            profile = {}
        profile_hash = None
        try:
            if prompts_dir and domain:
                pf = Path(prompts_dir) / "profiles" / f"{domain}.yaml"
                if pf.exists():
                    profile_hash = file_hash(pf)
        except Exception:
            pass

        resolved_templates = []
        for name, item in sorted(getattr(self, "_resolved_prompts", {}).items()):
            resolved_templates.append({
                "name": name,
                "source": item.source,
                "sha256": item.sha256,
                "bytes": len(item.raw),
                "version": item.version,
            })
        active_name = getattr(self, "_active_prompt_name", None)
        template_meta = next(
            (item for item in resolved_templates if item["name"] == active_name),
            None,
        ) or {
            "name": None, "source": None, "sha256": None, "bytes": 0, "version": None,
        }

        try:
            in_hashes = self.input_hashes()
        except Exception:
            in_hashes = {}

        job_meta: dict = {}
        try:
            job_meta = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except Exception:
            job_meta = {}

        images = images or []
        img_recs = []
        for p in images:
            d: dict = {"path": str(p)}
            try:
                pp = Path(p)
                if pp.exists():
                    d["hash"] = file_hash(pp)
                    d["bytes"] = pp.stat().st_size
            except Exception:
                pass
            img_recs.append(d)

        ok = error is None and response is not None
        if response is not None:
            attempts, tier_used = response.attempts, response.tier_used
        else:
            attempts, tier_used = (getattr(error, "attempts", []) or []), None

        ct = job_meta.get("content_type") or cfg.get("content_type")
        return {
            # 标识/归组
            "job_id": self.job_dir.name,
            "step": self.step_name,
            "content_type": ct,
            "pipeline": ct,                       # pipeline 名即 content_type
            "domain": domain,
            "call_index": self._call_index,
            "exec_id": f"{os.environ.get('STEP_EXEC_ID', self.job_dir.name + ':' + self.step_name)}:{self._call_index}",
            "session_id": getattr(response, "session_id", None),
            "ts_start": ts_start.isoformat(),
            "ts_end": ts_end.isoformat(),
            # 溯源/可复现
            "flori": self._flori_meta(),
            "config": {
                "step_config_resolved": {
                    "ai": cfg.get("ai"), "pool": cfg.get("pool"),
                    "tags": cfg.get("tags"), "style_tags": cfg.get("style_tags"),
                },
                "provider_override": self._read_override() or None,
            },
            "injected": {
                "domain_profile": {"name": domain, "hash": profile_hash},
                "style_tags": cfg.get("style_tags") or [],
                "terminology_snapshot": profile.get("terminology"),
            },
            "input_hashes": in_hashes,
            # 调用/性能
            "routing": {
                "requested_ai": cfg.get("ai"),
                "tier_used": tier_used,
                "provider": getattr(response, "provider", None),
                "model": getattr(response, "model", None),
                "attempts": attempts,
            },
            "latency": {
                "ttft_ms": getattr(response, "ttft_ms", None),
                "api_ms": getattr(response, "api_ms", None),
                "duration_total_sec": (
                    getattr(response, "duration_sec", None) if response is not None
                    else round((ts_end - ts_start).total_seconds(), 2)
                ),
            },
            "call_meta": {
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "response_format": request.response_format,
                "allowed_tools": request.allowed_tools,
                "max_turns": request.max_turns,
                "images_count": len(images),
            },
            # 输入溯源(模板+值=渲染)
            "prompt": {
                "rendered": {"system": system, "user": prompt},
                "template": template_meta,
                "templates": resolved_templates,
                "values": {
                    "domain_profile_name": domain,
                    "terminology_snapshot": profile.get("terminology"),
                    "style_tags": cfg.get("style_tags") or [],
                },
                "images": img_recs,
            },
            # 输出
            "output": {
                "content": getattr(response, "content", None),
                "num_turns": getattr(response, "num_turns", None),
                "finish_reason": getattr(response, "finish_reason", None),
            },
            # agentic 全轨迹(sidecar 引用):{"file": "output/ai_logs/{step}.turns.{n}.jsonl", "turns", "bytes"}
            # 或 {"file": None, "reason": …}。中间轮工具轨迹全在 sidecar,本记录仅存指针。
            "transcript": self._collect_transcript(response, attempts),
            "output_processed": None,             # call_ai_json 解析后回填
            # 用量/成本/raw
            "usage": {
                "input_tokens": getattr(response, "input_tokens", 0),
                "output_tokens": getattr(response, "output_tokens", 0),
                "cache_creation_input_tokens": getattr(response, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens": getattr(response, "cache_read_input_tokens", 0),
            },
            "cost": {
                "cost_usd": getattr(response, "cost_usd", 0.0),
                "basis": "cli-equiv" if getattr(response, "provider", None) == "claude-cli" else "priced",
            },
            "raw": getattr(response, "raw", None),
            # 关联(produced_artifact 此刻未知,留空)
            "links": {
                "source": {
                    "job_url": job_meta.get("url"),
                    "collection": job_meta.get("collection_id"),
                    "published_at": job_meta.get("published_at"),
                },
            },
            # 评估(前瞻,留空槽)
            "feedback": None,
            # 环境
            "env": {
                "worker_id": os.environ.get("WORKER_ID") or os.environ.get("FLORI_WORKER_ID"),
                "host": socket.gethostname(),
                "pool": cfg.get("pool"),
            },
            "ok": ok,
            "error": None if ok else (str(error)[:2000] if error else "unknown"),
        }

    def call_ai_json(
        self,
        prompt: str,
        fallback: dict,
        score_keys: list[str] | None = None,
        images: list[Path] | None = None,
        **kwargs,
    ) -> tuple[dict, bool]:
        """调用 AI 并解析 JSON。解析失败时回退到 fallback(附 raw_response/parse_failed)。
        若给 score_keys 且结果缺 overall,按维度均值自动补 overall。
        返回 (result, parse_failed)。"""
        kwargs.setdefault("response_format", "json")
        # 评分/抽取类要确定性:默认低温,幂等重跑/retry 拿到稳定分数(claude-cli 无视此项无害)。
        kwargs.setdefault("temperature", 0)
        raw = self.call_ai(prompt, images=images, **kwargs)
        parse_failed = False
        did_salvage = False
        try:
            result = json.loads(self._extract_json(raw))
            # claude 有时把分数包进 "scores" 子对象(+rationale),抬平到顶层再按维度取键,
            # 否则顶层取不到→维度全落默认 3。
            if score_keys and isinstance(result.get("scores"), dict):
                result = {**result.pop("scores"), **result}
        except (json.JSONDecodeError, ValueError):
            # 整体 JSON 非法——常因 claude 多塞了 rationale 长文本,其中换行/引号未转义
            # 或被单轮输出截断。但分数往往仍完好,按维度键正则抢救,救回则用真分数,
            # 避免误落 fallback 的全 3(线上 11_review 实测此因 overall 恒为 3.0)。
            salvaged = self._salvage_scores(raw, score_keys)
            if salvaged is not None:
                # 用救回的真分数;丢掉 fallback 的占位 overall,让其按真分重算(否则恒 3.0)。
                did_salvage = True
                result = {**fallback, **salvaged, "raw_response": raw[:500]}
                result.pop("overall", None)
            else:
                self.log.warn("ai_json_parse_failed", raw=raw[:200])
                result = {**fallback, "raw_response": raw[:500], "parse_failed": True}
                parse_failed = True
        if score_keys and "overall" not in result:
            scores = [result.get(k, 3) for k in score_keys]
            result["overall"] = round(sum(scores) / max(len(scores), 1), 1)
        # 回填审计记录的 output_processed:解析成功/抢救/失败 + 抽出的结构化结果(供 review 步看产了哪些概念)。
        self._amend_last_ai_log({"output_processed": {
            "json_parse": {"ok": not parse_failed, "salvaged": did_salvage},
            "parse_failed": parse_failed,
            "extracted": {k: v for k, v in result.items() if k != "raw_response"},
        }})
        return result, parse_failed

    @staticmethod
    def _salvage_scores(raw: str, score_keys: list[str] | None) -> dict | None:
        """JSON 整体解析失败时的兜底:按 `"维度": 数字` 正则逐项抢救 1-5 分。
        rationale 里的同名键值是字符串("维度": "..."),数字正则不会误命中。
        至少命中半数维度才返回(部分命中按已命中均值补齐缺的,round),否则 None → 走 fallback。
        阈值取半数而非全命中:少一个维度就整体落 fallback 全 3,overall 又会恒 3.0。"""
        if not score_keys:
            return None
        import re
        found: dict = {}
        for k in score_keys:
            m = re.search(rf'"{re.escape(k)}"\s*:\s*([1-5])\b', raw or "")
            if m:
                found[k] = int(m.group(1))
        if not found or len(found) * 2 < len(score_keys):
            return None  # 命中不足半数,不可信,落 fallback
        if len(found) < len(score_keys):
            avg = round(sum(found.values()) / len(found))
            for k in score_keys:
                found.setdefault(k, avg)   # 缺的维度按已命中均值补,避免误落全 3
        return found

    @staticmethod
    def _extract_json(raw: str) -> str:
        """从 AI 输出里抽出 JSON:claude-cli 常包 ```json 围栏或带前后说明文字。
        先剥代码围栏,再退化为取首个 { 到末个 } 的子串。"""
        s = (raw or "").strip()
        if s.startswith("```"):
            import re
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"\n?```\s*$", "", s).strip()
        if not s.startswith("{"):
            i, j = s.find("{"), s.rfind("}")
            if i != -1 and j > i:
                s = s[i:j + 1]
        return s

    # 外部命令

    def run_subprocess(
        self, cmd: list[str], timeout: int = 600, **kwargs
    ) -> subprocess.CompletedProcess:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=True, **kwargs
            )
        except subprocess.CalledProcessError as e:
            # 失败:把工具输出转发到本步 stdout/stderr(进 logs/{step}.log 供排错),再重抛带 stderr 尾部。
            if e.stdout:
                print(e.stdout, flush=True)
            if e.stderr:
                print(e.stderr, file=sys.stderr, flush=True)
            raise SubprocessFailed(
                e.returncode, e.cmd, output=e.output, stderr=e.stderr
            ) from e
        # 成功:把工具(yt-dlp/yutto/ffmpeg 等)输出转发到本步 stdout/stderr,使其进 logs/{step}.log。
        # 否则 capture_output 把输出吃掉、成功步(尤其 01_download)的日志为空,无从查看下载/处理详情。
        if r.stdout:
            print(r.stdout, flush=True)
        if r.stderr:
            print(r.stderr, file=sys.stderr, flush=True)
        return r

    # Private

    def _setup_logger(self):
        return structlog.get_logger(step=self.step_name, job_dir=str(self.job_dir))

    def _job_prompt_overrides(self):
        """读取任务固化的 Prompt 覆盖,坏 job 或坏映射不得静默使用镜像模板."""
        snapshot = getattr(self, "_prompt_overrides_snapshot", None)
        if snapshot is not None:
            return snapshot
        job_dir = getattr(self, "job_dir", None)
        if job_dir is None:
            self._prompt_overrides_snapshot = {}
            return {}
        try:
            job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._prompt_overrides_snapshot = {}
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            from .prompt_resolver import PromptResolutionError
            raise PromptResolutionError("prompt override job metadata is invalid") from exc
        if not isinstance(job, dict):
            from .prompt_resolver import PromptResolutionError
            raise PromptResolutionError("prompt override job metadata is invalid")
        if "prompt_overrides" not in job:
            overrides = {}
        else:
            overrides = job["prompt_overrides"]
            if not isinstance(overrides, dict):
                from .prompt_resolver import PromptResolutionError
                raise PromptResolutionError("prompt override map is invalid")
        self._prompt_overrides_snapshot = overrides
        return overrides

    def _injected_prompt_override(self) -> str:
        """返回本步固化覆盖正文,兼容存量字符串形态."""
        from .prompt_resolver import parse_prompt_override

        parsed = parse_prompt_override(self._job_prompt_overrides(), self.step_name)
        return parsed[0].decode("utf-8") if parsed is not None else ""

    def _primary_prompt_template(self) -> str:
        step = self.config.get("step") or {}
        name = step.get("prompt_template") or self.step_name
        if not isinstance(name, str) or not name:
            from .prompt_resolver import PromptResolutionError
            raise PromptResolutionError("prompt template mapping is invalid")
        return name

    def _prompt_resolver(self):
        from .prompt_resolver import PromptResolver

        paths = self.config.get("paths") or {}
        prompts_dir = Path(paths.get("prompts_dir", "/data/prompts"))
        config_dir = Path(paths.get("config_dir", "/app/configs"))
        return PromptResolver(
            hot_dir=prompts_dir / "templates",
            image_dir=config_dir / "prompts" / "templates",
        )

    def _resolve_prompt_template(self, name: str):
        cache = getattr(self, "_resolved_prompts", None)
        if cache is None:
            cache = {}
            self._resolved_prompts = cache
        if name not in cache:
            cache[name] = self._prompt_resolver().resolve(
                name,
                step_name=self.step_name,
                prompt_overrides=self._job_prompt_overrides(),
                primary_template=self._primary_prompt_template(),
            )
        return cache[name]

    def _has_step_template(self) -> bool:
        """该运行步是否映射到 tracked user-prompt 主模板."""
        from .prompt_resolver import TRACKED_TEMPLATE_NAMES

        primary = self._primary_prompt_template()
        if any(
            name == primary or name.startswith(primary + ".")
            for name in TRACKED_TEMPLATE_NAMES
        ):
            return True
        resolver = self._prompt_resolver()
        return resolver.template_exists(primary)

    def _load_system_prompt(self) -> str | None:
        """加载独立 system 钩子;tracked user template 始终由 PromptResolver 处理.

        旧的无模板扩展步仍可把任务覆盖当 system.当前 16 条 AI route 都有 tracked
        user template,因此不会双重套用覆盖.system 文件使用独立的 prompts/{step}.md 契约.
        """
        injected = self._injected_prompt_override()
        if injected and not self._has_step_template():
            return injected
        paths = self.config.get("paths") or {}
        candidates = (
            Path(paths.get("prompts_dir", "/data/prompts")) / f"{self.step_name}.md",
            Path(paths.get("config_dir", "/app/configs")) / "prompts" / f"{self.step_name}.md",
        )
        for path in candidates:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            except OSError as exc:
                from .prompt_resolver import PromptResolutionError
                raise PromptResolutionError("system prompt is unreadable") from exc
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                from .prompt_resolver import PromptResolutionError
                raise PromptResolutionError("system prompt is not UTF-8") from exc
        return None

    def load_domain_prompt_profile(self) -> dict:
        """加载 domain prompt profile(prompts_dir/profiles/{domain}.yaml),不存在返回 {}。四个 smart 步共用。
        注:与 shared.config.load_domain_profile(读 config_dir/domain/{domain}.yaml,build 期用)不同源,
        故命名区分避免跨文件误认。"""
        import yaml
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        profile_path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if profile_path.exists():
            return yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        return {}

    @staticmethod
    def terminology_block(profile: dict) -> str:
        """四 smart 步共用:把 profile.terminology 注入为"已沉淀标准概念"提示段(无 terminology 则空串)。
        命中的概念沿用统一措辞、不重复展开,只对未列出的新概念首次解释。
        回流机制见 docs/06-prompt-engineering.md §4。"""
        terms = (profile or {}).get("terminology")
        if not terms:
            return ""
        joined = "; ".join(terms[:30])
        return (
            "\n本领域已沉淀的标准概念（命中时沿用统一措辞、无需重新展开解释；"
            f"只对下列未涵盖的新概念做首次解释）：\n{joined}\n"
        )

    def prompt_profile_style_hashes(self) -> dict[str, str]:
        """smart 步共用的指纹块:独立 system 钩子 + resolver 实际模板字节
        + domain profile + style tags。改默认模板(外置可编辑)即变指纹 → should_run 重跑。
        profile/styles 的键名与取值口径不可轻改:变了即改变既有 .done 指纹,触发全量重跑。"""
        import json
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        hashes: dict[str, str] = {}
        prompt_path = prompts_dir / f"{self.step_name}.md"
        if prompt_path.exists():
            hashes["prompt"] = file_hash(prompt_path)
        tpl = self.template_hash(self._primary_prompt_template())
        if tpl:
            hashes["template"] = tpl
        profile_path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if profile_path.exists():
            hashes["profile"] = file_hash(profile_path)
        hashes["styles"] = json.dumps({
            tag: file_hash(prompts_dir / "styles" / f"{tag}.yaml")
            for tag in sorted(self.config.get("style_tags", []))
            if (prompts_dir / "styles" / f"{tag}.yaml").exists()
        }, sort_keys=True)
        return hashes

    def _load_prompt_template(self, name: str) -> str:
        """返回 resolver 固化的模板快照,正文不在步骤代码中保留副本."""
        resolved = self._resolve_prompt_template(name)
        self._active_prompt_name = name
        return resolved.text

    def template_hash(self, *names: str) -> str:
        """实际解析模板的精确字节指纹;缺失或损坏直接失败."""
        present = {
            n: self._resolve_prompt_template(n).sha256
            for n in sorted(names)
        }
        return json.dumps(present, sort_keys=True)
