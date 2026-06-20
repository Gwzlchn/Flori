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
from .errors import ProcessingError, StepError
from .models import AIUsage, LLMRequest


def file_hash(path: Path) -> str:
    """计算文件 SHA-256，返回 'sha256:{hex}' 格式。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


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

    # ── 统一入口 ──

    def run(self) -> None:
        try:
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
            sys.exit(1)
        except Exception as e:
            self.write_error("unknown", str(e), traceback.format_exc())
            sys.exit(1)

    @classmethod
    def cli_main(cls, step_name: str) -> None:
        """步骤脚本统一入口：解析 --job-dir/--step-config，实例化并 run。"""
        import argparse

        from shared.logging_setup import setup_logging
        setup_logging()  # 步骤子进程日志也输出结构化 JSON,与 scheduler/worker 一致

        parser = argparse.ArgumentParser()
        parser.add_argument("--job-dir", required=True)
        parser.add_argument("--step-config", required=True)
        args = parser.parse_args()
        config = json.loads(Path(args.step_config).read_text())
        cls(step_name, Path(args.job_dir), config).run()

    # ── 子类实现 ──

    def execute(self) -> dict | None:
        raise NotImplementedError

    def validate_inputs(self) -> list[str]:
        return []

    def input_hashes(self) -> dict[str, str]:
        return {}

    # ── 幂等 ──

    def should_run(self) -> bool:
        done_file = self.job_dir / f".{self.step_name}.done"
        if not done_file.exists():
            return True
        stored = json.loads(done_file.read_text())
        return stored.get("input_hashes") != self.input_hashes()

    def mark_done(self) -> None:
        data = {
            "step": self.step_name,
            "input_hashes": self.input_hashes(),
            "finished_at": datetime.now().isoformat(),
        }
        (self.job_dir / f".{self.step_name}.done").write_text(
            json.dumps(data, ensure_ascii=False, indent=2)
        )

    # ── IO 工具 ──

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
        """最近一次 AI 调用的 (provider, model)。claude-cli 订阅默认 Opus 4.8——config 里是
        占位 "subscription",此处换成真实模型名,供笔记/评审统一标注。"""
        prov = self.last_ai_provider or "unknown"
        model = self.last_ai_model or "unknown"
        if prov == "claude-cli" and model in ("subscription", "unknown", ""):
            model = "claude-opus-4-8"
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
    _API_PROVIDERS = ("anthropic", "deepseek", "kimi", "openai", "ollama")

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
        return s

    def write_smart_note(self, content: str) -> str:
        """智能笔记按版本落盘:output/versions/notes_smart_{provider}_{model}_{时间}.md,
        开头加一行说明(生成时间 / 方式 / 模型)。不再写规范 notes_smart.md——前端取最新版本。
        落盘前净化 claude agentic 口水(_sanitize_smart_note)。
        返回相对路径,供评审步在 review.json 里标明评的是哪一版。"""
        prov, model = self.ai_provider_model()
        content = self._sanitize_smart_note(content, prov)
        # 字段内只允许字母数字与 . - (把 _ 也归一为 -),保证文件名按 "_" 切分无歧义。
        safe = lambda s: __import__("re").sub(r"[^0-9A-Za-z.-]+", "-", s).strip("-") or "x"
        now = datetime.now()
        rel = f"output/versions/notes_smart_{safe(prov)}_{safe(model)}_{now.strftime('%Y%m%d-%H%M%S')}.md"
        header = f"> 生成于 {now.strftime('%Y/%m/%d %H:%M:%S')} · 方式 {prov} · 模型 {model}\n\n"
        self.write_output(rel, header + content)
        return rel

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
            from shared.notes_versions import review_path_for_note
            vrel = review_path_for_note(note_file)
            if vrel:
                self.write_output(vrel, review)

    def latest_smart_note(self) -> Path | None:
        """工作目录里最新的智能笔记版本文件(供评审步读取并标注评的是哪一版)。"""
        from shared.notes_versions import latest_smart
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

    # ── 进度 ──

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

    # ── AI 调用 ──

    def override_provider(self) -> str:
        """读 job.json 里本步的 provider 覆盖(无则空串)。供 input_hashes 纳入,
        使"换 provider 重跑"改变指纹、绕过幂等跳过。"""
        try:
            job = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return (job.get("ai_overrides") or {}).get(self.step_name, "") or ""

    def _apply_provider_override(self) -> None:
        """按 job.json 的 ai_overrides[step] 覆盖本步 provider(供"选 provider 重跑")。
        只用所选 provider(去掉 fallback),避免失败时静默回退到别的 provider,
        保证版本化笔记的 provider 标记如实。"""
        try:
            job = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        provider = (job.get("ai_overrides") or {}).get(self.step_name)
        if not provider:
            return
        pcfg = self.config.get("providers", {}).get("providers", {}).get(provider, {})
        models = pcfg.get("models", [])
        model = models[0] if models else "subscription"
        self.config["ai"] = {"primary": {"provider": provider, "model": model}}
        self._gateway = None  # 强制按新 ai 配置重建

    def call_ai(self, prompt: str, images: list[Path] | None = None, **kwargs) -> str:
        self._apply_provider_override()
        if self._gateway is None:
            self._gateway = AIGateway(
                self.config.get("providers", {}),
                {"steps": [{"name": self.step_name, "ai": self.config.get("ai", {})}]},
            )

        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            images=images or [],
            system=self._load_system_prompt(),
            **kwargs,
        )

        import asyncio
        response = asyncio.run(
            self._gateway.call(self.step_name, request, job_id=self.job_dir.name)
        )
        self.last_ai_provider = response.provider
        self.last_ai_model = response.model

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
                cost_usd=response.cost_usd,
                duration_sec=response.duration_sec,
                cached=response.cached,
            ),
            log_dir,
        )
        self._call_index += 1
        return response.content

    def call_ai_json(
        self,
        prompt: str,
        fallback: dict,
        score_keys: list[str] | None = None,
        images: list[Path] | None = None,
        **kwargs,
    ) -> tuple[dict, bool]:
        """调用 AI 并解析 JSON。解析失败时回退到 fallback（附 raw_response/parse_failed）。
        若给 score_keys 且结果缺 overall，按维度均值自动补 overall。
        返回 (result, parse_failed)。"""
        kwargs.setdefault("response_format", "json")
        # 评分/抽取类要确定性:默认低温,幂等重跑/retry 拿到稳定分数(claude-cli 无视此项无害)。
        kwargs.setdefault("temperature", 0)
        raw = self.call_ai(prompt, images=images, **kwargs)
        parse_failed = False
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
                result = {**fallback, **salvaged, "raw_response": raw[:500]}
                result.pop("overall", None)
            else:
                self.log.warn("ai_json_parse_failed", raw=raw[:200])
                result = {**fallback, "raw_response": raw[:500], "parse_failed": True}
                parse_failed = True
        if score_keys and "overall" not in result:
            scores = [result.get(k, 3) for k in score_keys]
            result["overall"] = round(sum(scores) / max(len(scores), 1), 1)
        return result, parse_failed

    @staticmethod
    def _salvage_scores(raw: str, score_keys: list[str] | None) -> dict | None:
        """JSON 整体解析失败时的兜底:按 `"维度": 数字` 正则逐项抢救 1-5 分。
        rationale 里的同名键值是字符串("维度": "..."),数字正则不会误命中。
        至少命中半数维度才返回(部分命中按已命中均值补齐缺的,round),否则 None → 走 fallback。
        放宽自"必须全维度命中":少一个维度就整体落 fallback 全 3 是 overall 恒 3.0 的残留口子。"""
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

    # ── 外部命令 ──

    def run_subprocess(
        self, cmd: list[str], timeout: int = 600, **kwargs
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True, **kwargs
        )

    # ── Private ──

    def _setup_logger(self):
        return structlog.get_logger(step=self.step_name, job_dir=str(self.job_dir))

    def _load_system_prompt(self) -> str | None:
        prompts_dir = self.config.get("paths", {}).get("prompts_dir")
        if not prompts_dir:
            return None
        path = Path(prompts_dir) / f"{self.step_name}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None
