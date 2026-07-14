# StepBase 统一基类

> 所有 `steps/*.py` 继承此基类。StepBase 只编排输入校验、幂等检查、执行和最终发布,执行能力由显式组件提供。

## 1. 基类接口

`StepExecutionServices` 是不可变聚合,由构造函数中的 factory 创建。测试或扩展步可以注入自己的 factory,运行时没有模块级可变全局。

| 属性 | 组件 | 责任 |
|------|------|------|
| `artifacts` | `ArtifactIO` | JSON/文本/二进制产物及 done/meta/error 文件 |
| `progress` | `StepProgressReporter` | 步骤进度文件和结构化日志门 |
| `commands` | `SubprocessExecutor` | 外部命令、timeout、stdout/stderr 和失败类型 |
| `structured` | `StructuredOutputParser` | JSON 抽取、fallback 和分数 salvage |
| `ai` | `AIInvocation` | PromptResolver、provider 路由、usage、AI logs 和 transcript |
| `review` | `ReviewExecution` | 智能笔记净化、评审输入留痕和可靠性解析 |

```python
class StepBase:
    def __init__(self, step_name, job_dir, config, *, service_factory=None):
        self.log = structlog.get_logger(step=step_name, job_dir=str(job_dir))
        context = StepExecutionContext(
            step_name=step_name,
            job_dir=job_dir,
            config=config,
            log=self.log,
            input_hashes=self.input_hashes,
        )
        services = (service_factory or StepExecutionServices.create)(context)
        self.artifacts = services.artifacts
        self.progress = services.progress
        self.commands = services.commands
        self.structured = services.structured
        self.ai = services.ai
        self.review = services.review

    def run(self):
        """校验 provider 和输入,执行幂等步骤,最后发布 done/meta 或 error。"""

    def execute(self) -> dict | None:
        raise NotImplementedError

    def validate_inputs(self) -> list[str]:
        return []

    def input_hashes(self) -> dict[str, str]:
        return {}
```

步骤不得绕回 StepBase 添加新的执行工具方法。新增通用能力应进入职责对应的组件,并通过 `StepExecutionServices` 装配。

## 2. 幂等机制

核心原则：**步骤只看自己的输入文件内容，不关心"谁跑过"或"跑了几次"**。

### 2.1 指纹计算

每步通过 `input_hashes()` 定义自己的指纹——hash 的是实际输入文件的内容和相关配置：

```python
# CPU 步骤示例（确定性）
class OcrStep(StepBase):
    def input_hashes(self):
        return {
            "dedup": file_hash(self.job_dir / "intermediate/dedup.json"),
            "config": json.dumps(self.config.get("ocr", {}), sort_keys=True),
        }

# LLM 步骤示例（非确定性）
class SmartStep(StepBase):
    def input_hashes(self):
        return {
            "mechanical": file_hash(self.job_dir / "output/notes_mechanical.md"),
            "prompt": file_hash(Path("/data/prompts/smart_notes.md")),
            "profile": json.dumps(self.load_profile(), sort_keys=True),
        }
```

### 2.2 .done 标记

步骤完成时写 `.{step}.done`，存储当时的指纹：

```json
{
  "step": "06_ocr",
  "input_hashes": {
    "dedup": "sha256:abc123...",
    "config": "{\"confidence_threshold\": 0.6}"
  },
  "finished_at": "2026-05-16T20:15:00"
}
```

### 2.3 should_run() 逻辑

```python
def should_run(self) -> bool:
    done_file = self.job_dir / f".{self.step_name}.done"
    if not done_file.exists():
        return True   # 从未跑过
    stored = json.loads(done_file.read_text())
    current = self.input_hashes()
    return stored.get("input_hashes") != current  # 指纹变了就重跑
```

### 2.4 级联失效（自动）

上游步骤重跑 → 输出文件内容变了 → 下游步骤的 `input_hashes()` 返回不同值 → 自动重跑。

```
场景：修改 06_ocr 的 confidence_threshold 0.6 → 0.5

06_ocr  → config hash 变了 → 重跑 → ocr.json 内容变了
09_mechanical → input_hashes() 中 ocr.json hash 变了 → 重跑 → mechanical.md 变了
10_smart → input_hashes() 中 mechanical.md hash 变了 → 重跑
11_review → input_hashes() 中 smart.md hash 变了 → 重跑

01-05, 07, 08 → 输入没变 → 跳过 ✓
```

**不需要手动标记哪些步骤需要重跑**——级联是基于实际文件内容的。

### 2.5 CPU 步骤 vs LLM 步骤

| | CPU 步骤 | LLM 步骤 |
|--|---------|---------|
| 确定性 | 同输入 → 同输出（字节相同） | 同输入 → 语义等价但不字节相同 |
| 指纹包含 | hash(输入文件) + hash(config) | hash(输入文件) + hash(prompt) + hash(profile) |
| 跳过安全性 | 完全安全 | 安全（节省 Claude 调用，输入没变不必重跑） |
| 强制重跑 | 几乎不需要 | 用户不满意时需要 |

LLM 步骤虽然非确定性，但**输入和 prompt 都没变就不该浪费 Claude 调用**。质量不满意时通过 force rerun 显式触发。

### 2.6 强制重跑

删除 `.done` 标记即可让步骤重跑：

```python
def force_rerun(job_dir: Path, from_step: str, pipeline_steps: list):
    """从指定步骤开始，清除它及所有下游步骤的 .done 标记"""
    downstream = get_downstream_steps(from_step, pipeline_steps)
    for step in [from_step] + downstream:
        done_file = job_dir / f".{step}.done"
        done_file.unlink(missing_ok=True)
```

API: `POST /api/jobs/{id}/rerun  Body: {"from_step": "10_smart"}`

效果：清除 10_smart 和 11_review 的 `.done` → 调度器重新提交 → Worker 执行时 `should_run()` 返回 True。

## 3. 进度上报（两层）

### 第一层：Worker 心跳进度（所有步骤都有）

Worker 在 subprocess 运行期间，每 10 秒写一次心跳进度文件。步骤不需要做任何事——Worker 自动搞定：

```python
# Worker 层：subprocess 运行期间的心跳
async def execute_with_heartbeat(self, job_id, step, job_dir, cmd, timeout):
    progress_file = job_dir / f".{step}.progress"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start = time.time()

    while proc.poll() is None:
        elapsed = time.time() - start
        progress_file.write_text(json.dumps({
            "source": "worker_heartbeat",
            "elapsed_sec": round(elapsed),
            "message": f"running {elapsed:.0f}s",
            "updated_at": time.time(),
        }))
        await asyncio.sleep(10)

    # subprocess 结束后再读 stdout/stderr
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr
```

效果：即使 10_smart 跑 5 分钟没有内部进度，心跳进度文件也每 10 秒更新一次。卡住检测能看到 `updated_at` 停止更新。

### 第二层：步骤内细粒度进度（步骤自己报）

步骤通过 `self.progress.report()` 覆盖 Worker 心跳,提供更细的进度信息：

```python
def report(self, current: int, total: int, message: str = ""):
    pct = round(100 * current / max(total, 1))

    progress_file = self.job_dir / f".{self.step_name}.progress"
    progress_file.write_text(json.dumps({
        "source": "step",
        "current": current, "total": total, "pct": pct,
        "message": message, "updated_at": time.time(),
    }))

    if pct % 10 == 0 or current == total:
        self.log.info(f"progress: {current}/{total} ({pct}%) {message}")
```

### 各步骤进度来源

| 步骤 | 进度来源 | 前端显示 |
|------|---------|---------|
| 01_download | Worker 心跳 | `running 15s` |
| 02_whisper | Worker 心跳 | `running 60s` |
| 03_scene | step: 帧/总帧 | `15000/40080 (37%)` |
| 04_frames | step: 帧/场景数 | `50/76 (66%)` |
| 05_dedup | step: 帧/总帧 | `30/76 (39%)` |
| 06_ocr | step: 帧/待识别 | `85/162 (52%)` |
| 07_danmaku | Worker 心跳 | `running 1s` |
| 08_punctuate | step: 块/总块 | `2/3 (67%)` |
| 09_mechanical | Worker 心跳 | `running 2s` |
| 10_smart | Worker 心跳 | `running 180s` |
| 11_review | Worker 心跳 | `running 45s` |

前端根据 `source` 字段决定显示方式：
- `source: "step"` → 显示 `████████░░ 52%  85/162 帧`
- `source: "worker_heartbeat"` → 显示 `⏳ 运行中 3m0s`

### Worker 转发到 WebSocket

Worker 每 5 秒轮询 `.progress` 文件，内容有变化时 publish 到 Redis → API → WebSocket → 前端：

```python
# Worker 进度转发循环（和 subprocess 心跳并行）
async def progress_relay(self, job_id, step, job_dir):
    progress_file = job_dir / f".{step}.progress"
    last_content = ""
    while True:
        if progress_file.exists():
            content = progress_file.read_text()
            if content != last_content:
                last_content = content
                await self.redis.publish(f"events:{job_id}", json.dumps({
                    "event": "step_progress", "step": step,
                    **json.loads(content),
                }))
        await asyncio.sleep(5)
```

## 4. 原子写文件

```python
def write(self, filename: str, data):
    target = self.job_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(target) + ".tmp"
    if isinstance(data, (dict, list)):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    elif isinstance(data, str):
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
    elif isinstance(data, bytes):
        with open(tmp, "wb") as f:
            f.write(data)
    os.rename(tmp, str(target))
```

先写 `.tmp` 再 rename，防止写到一半崩溃产生残缺文件。

## 5. AI 调用（通过 Gateway）

步骤以 subprocess 运行,`AIInvocation` 在步骤进程内调用 Gateway。初始化只读 provider 配置和环境变量中的凭证,不连接 DB。

AI 调用的计费记录先写到 `logs/.{step}.usage.json`,Worker 在步骤完成后统一收集写入 SQLite。步骤进程不需要 DB 连接。

```python
def execute(self):
    result = self.ai.call(prompt, images=images, max_tokens=8192)
    return {
        "provider": self.ai.last_provider,
        "model": self.ai.last_model,
        "chars": len(result),
    }
```

每次调用在发起前写 `pending` AI log,完成后以相同 `call_index` 原位替换为 `final`。usage 的 `exec_id` 保持 `{STEP_EXEC_ID}:{call_index}`,CLI transcript 保存到 `output/ai_logs/{step}.turns.{call_index}.jsonl`。

详见 [AI 网关模块设计](ai-gateway.md)。

## 6. 错误层级

```python
class StepError(Exception):
    error_type = "unknown"

class InputMissingError(StepError):
    error_type = "input_missing"

class InputInvalidError(StepError):
    error_type = "input_invalid"

class ProcessingError(StepError):
    error_type = "processing"

class AIProviderError(StepError):
    error_type = "ai"

class AIRateLimitError(AIProviderError):
    error_type = "ai_rate_limit"
```

## 7. 步骤脚本入口模板

```python
# steps/video/step_06_ocr.py
import json

from shared.step_base import StepBase, file_hash

class OcrStep(StepBase):
    def validate_inputs(self):
        if not (self.job_dir / "intermediate" / "dedup.json").exists():
            return ["intermediate/dedup.json"]
        return []

    def input_hashes(self):
        return {
            "dedup": file_hash(self.job_dir / "intermediate" / "dedup.json"),
            "config": json.dumps(self.config.get("ocr", {}), sort_keys=True),
        }

    def execute(self):
        results = run_ocr(self.artifacts.load_json("intermediate/dedup.json"))
        nonempty = sum(bool(item.get("text")) for item in results)
        self.progress.report(len(results), len(results), "done")
        self.artifacts.write("intermediate/ocr.json", results)
        return {"total": len(results), "nonempty": nonempty}


if __name__ == "__main__":
    OcrStep.cli_main("06_ocr")
```

## 8. 输入校验规范

| 步骤 | 必须存在 | 校验 |
|------|---------|------|
| 01_download | job.json (url 或 upload) | URL 格式合法 |
| 02_whisper | input/*.mp4 或音频 | 有音轨；存在 input/*.srt 则跳过 |
| 03_scene | input/*.mp4 | 文件 >1MB |
| 04_frames | intermediate/scenes.json + input/*.mp4 | scenes 非空 |
| 05_dedup | intermediate/frames.json + assets/*.jpg | 至少 1 张 jpg |
| 06_ocr | intermediate/dedup.json | 至少 1 个 keep=true |
| 07_danmaku | input/*.ass | 无则跳过（条件步骤） |
| 08_punctuate | input/*.srt | 无则跳过（条件步骤） |
| 09_mechanical | intermediate/{ocr,dedup,danmaku}.json + output/transcript.md | 全部可读 |
| 10_smart | output/notes_mechanical.md | 大小 >100 字节 |
| 11_review | output/notes_smart.md + notes_mechanical.md | 两个都存在 |
