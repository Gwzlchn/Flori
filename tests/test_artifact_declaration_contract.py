"""产物声明对账:凡是运行期真会落在 Job 产物树里的路径,必须有人认领。

这条不变量守的是一整类缺陷,不是某个文件:某个产物没被任何 step 的 outputs 声明
- 备份看不见它(manifest 驱动选择),恢复后依赖它的复算永久失败;
- 或者它变成 unknown 残留,让每次备份 fail-closed 或被批量放行而静默丢弃。

已经踩过两次:intermediate/pdf_page_support.json(D5)和 output/ai_logs/*.jsonl。
两次都是"只覆盖 document/03_structure 一个步、一类字段"的定向测试拦不住的。
因此这里按路径的来源分三条泛化断言,而不是逐个文件写死:
- 契约回读路径 -> 必须被每个可能产出它的步声明;
- 编排面直写路径 -> 必须登记在备份的 claimed 集合里;
- 声明本身的自洽 -> outputs 参与语义摘要,不许写出跨步互相覆盖的 glob。
"""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

import pytest

from shared.config import load_pipelines
from shared.content_backup import ORCHESTRATION_CLAIMED_PATHS
from shared.provenance import SEMANTIC_AI_LOG_PREFIX

REPO = Path(__file__).parents[1]
PIPELINES = load_pipelines(REPO / "configs" / "pipelines.yaml")

# Job 产物树内的相对路径长这样;编排面扫描用它筛掉普通字符串。
_ARTIFACT_PATH = re.compile(r"^(?:input|output|intermediate|assets)/[A-Za-z0-9_./*-]+$")
_STORAGE_WRITE_CALLS = frozenset({"write_file", "write_stream"})


def _all_steps() -> list[tuple[str, dict]]:
    return [
        (pipeline, step)
        for pipeline, config in sorted(PIPELINES.items())
        for step in config["steps"]
    ]


def _declared(step: dict) -> list[str]:
    return list(step.get("outputs") or [])


def _is_declared(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in globs)


def _ai_steps() -> list[tuple[str, dict]]:
    return [(pipeline, step) for pipeline, step in _all_steps() if step.get("ai")]


# 证据/存证契约按固定路径回读产物,缺失即抛。这些路径是"必须有人声明"的需求源头,
# 逐个手写会重蹈 D5 覆辙(只钉了 document/03_structure 一个步),因此从这两个文件
# 自动导出。
_CONTRACT_SOURCES = ("shared/evidence_contract.py", "shared/provenance.py")


def _parse(rel: str) -> ast.Module:
    return ast.parse((REPO / rel).read_text(encoding="utf-8"))


def _path_literals(node: ast.AST) -> set[str]:
    """节点子树里所有产物路径字面量;f-string 的插值段折成 *,当 glob 比。"""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if _ARTIFACT_PATH.fullmatch(child.value):
                found.add(child.value)
        elif isinstance(child, ast.JoinedStr):
            rendered = "".join(
                part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "*"
                for part in child.values
            )
            if _ARTIFACT_PATH.fullmatch(rendered):
                found.add(rendered)
    return found


def _looks_like_a_file(path: str) -> bool:
    """只要求文件路径被声明;"output/ai_logs/" 这类前缀契约由别的用例覆盖。"""
    return "." in path.rsplit("/", 1)[-1]


def _contract_paths() -> dict[str, str]:
    """{产物路径: 定义处 source:line};裸字面量、f-string 骨架与 ALL_CAPS 常量都算。"""
    paths: dict[str, str] = {}
    for rel in _CONTRACT_SOURCES:
        for node in ast.walk(_parse(rel)):
            if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                continue
            for literal in _path_literals(node):
                if _looks_like_a_file(literal):
                    paths.setdefault(literal, f"{rel}:{node.lineno}")
    return paths


def _contract_constants() -> dict[str, str]:
    """{常量名: 路径};供按名字引用(跨模块 import)的场景反查。"""
    constants: dict[str, str] = {}
    for rel in _CONTRACT_SOURCES:
        for node in ast.walk(_parse(rel)):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            value = node.value
            if (
                isinstance(target, ast.Name) and target.id.isupper()
                and isinstance(value, ast.Constant) and isinstance(value.value, str)
                and _ARTIFACT_PATH.fullmatch(value.value)
                and _looks_like_a_file(value.value)
            ):
                constants[target.id] = value.value
    return constants


CONTRACT_PATHS = _contract_paths()
CONTRACT_CONSTANTS = _contract_constants()


def _module_file(dotted: str) -> Path | None:
    candidate = REPO / (dotted.replace(".", "/") + ".py")
    return candidate if candidate.is_file() else None


def _step_module(step: dict) -> str:
    """normalize_pipeline 把 YAML 的 run 归一成 module,按归一后的键取。"""
    return step.get("module") or ""


def _written_paths(step: dict) -> dict[str, int]:
    """该步实现里 artifacts.write() 的目标路径:{路径: 行号}。

    只认写调用。读同一个文件的步骤没有声明义务,把读也算进来会把
    "谁产出它"和"谁消费它"混成一锅,噪声淹掉真信号。
    """
    source = _module_file(_step_module(step))
    if source is None:
        return {}
    written: dict[str, int] = {}
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "write"
            or not node.args
        ):
            continue
        for literal in _path_literals(node.args[0]):
            written.setdefault(literal, node.lineno)
    return written


def test_pipeline_fixture_actually_has_ai_steps() -> None:
    """本文件多条断言以 AI 步集合为基础;集合空了要立刻暴露,而不是全部空转通过。"""
    assert len(_ai_steps()) >= 10


@pytest.mark.parametrize(
    "pipeline,step",
    [(pipeline, step) for pipeline, step in _ai_steps()],
    ids=[f"{pipeline}/{step['name']}" for pipeline, step in _ai_steps()],
)
def test_ai_steps_declare_the_ai_log_the_evidence_contract_reads_back(
    pipeline: str, step: dict,
) -> None:
    """每个 AI 步都必须声明自己的 ai_log。

    恢复侧 shared/evidence_contract.py 会按 semantic 绑定里的路径重读这个文件,
    缺失即抛 CanonicalEvidenceError 且该 Job 每拍重试、永不收敛。文件由
    shared/step_ai.py 在首次调用发起前就落盘,所以只要该步真跑过就存在;
    没跑到调用的执行只是 glob 零命中,不构成失败(见 shared/step_output_commit.py)。
    """
    name = step["name"]
    required = f"{SEMANTIC_AI_LOG_PREFIX}{name}.jsonl"
    globs = _declared(step)
    assert _is_declared(required, globs), (
        f"{pipeline}/{name} 是 AI 步但没声明 {required};"
        f"恢复后 canonical_evidence 无法复算。declared={globs}"
    )


@pytest.mark.parametrize(
    "pipeline,step",
    [(pipeline, step) for pipeline, step in _ai_steps()],
    ids=[f"{pipeline}/{step['name']}" for pipeline, step in _ai_steps()],
)
def test_ai_log_globs_do_not_capture_the_atomic_write_temp_file(
    pipeline: str, step: dict,
) -> None:
    """ai_log 的 glob 不能顺手把 .jsonl.tmp 收进 manifest。

    shared/step_ai.py 用 tmp + replace 原子落盘;崩溃会留下半截 tmp,一旦被声明
    就会进快照并冒充有效证据。所以声明必须精确到 .jsonl,不能写 output/ai_logs/*。
    """
    name = step["name"]
    globs = _declared(step)
    assert not _is_declared(f"{SEMANTIC_AI_LOG_PREFIX}{name}.jsonl.tmp", globs), (
        f"{pipeline}/{name} 的 ai_log glob 过宽,会把原子写 tmp 收进 manifest: {globs}"
    )


def _orchestration_written_paths() -> set[tuple[str, int, str]]:
    """编排面(scheduler/api)直接写进 Job 产物树的字面量路径。

    只认 storage.write_file/write_stream 的字面量参数:编排写入统一走这个门,
    因此扫描噪音低,不像在 steps/ 里扫字符串那样读写混在一起分不开。
    """
    found: set[tuple[str, int, str]] = set()
    for root in ("scheduler", "api"):
        for source in sorted((REPO / root).rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in _STORAGE_WRITE_CALLS:
                    continue
                for argument in node.args:
                    if isinstance(argument, ast.Constant) \
                            and isinstance(argument.value, str) \
                            and _ARTIFACT_PATH.match(argument.value):
                        found.add((
                            source.relative_to(REPO).as_posix(), node.lineno, argument.value,
                        ))
    return found


def test_orchestration_written_job_files_are_claimed_by_backup() -> None:
    """scheduler/api 写进产物树的文件必须在备份里被认领。

    没有任何 step manifest 能认领它们(写的时候一个 step 都还没跑),于是它们会被
    归成 unknown 残留:要么每次备份硬失败,要么被 --allow-unknown 批量放行而丢掉。
    新增一条编排写入就必须同步登记 ORCHESTRATION_CLAIMED_PATHS 并说明所有权。
    """
    written = _orchestration_written_paths()
    assert written, "扫描没找到任何编排写入,说明扫描口径失效了(写入 API 改名?)"
    unclaimed = sorted(
        f"{path} ({source}:{line})"
        for source, line, path in written
        if path not in ORCHESTRATION_CLAIMED_PATHS
    )
    assert not unclaimed, (
        f"编排面写了产物树却没登记: {unclaimed}; "
        f"claimed={sorted(ORCHESTRATION_CLAIMED_PATHS)}"
    )


def test_term_map_is_claimed_and_deliberately_not_snapshotted() -> None:
    """term_map.json 的所有权划分要显式钉住,防止后人"顺手"把它收进快照。

    它是 glossary + 集合 terms.json 的纯派生物,scheduler 在提交与 rerun 时各重算
    一次;两个输入都进快照(glossary 是 DB 行,terms.json 走 user_config),所以
    恢复后重新导出即可。备份一份可能过期的副本只会在恢复后误导人——与 notes_fts5
    同一条划分。
    """
    assert "input/term_map.json" in ORCHESTRATION_CLAIMED_PATHS
    effects = (REPO / "scheduler" / "effects.py").read_text(encoding="utf-8")
    assert "input/term_map.json" in effects
    assert '"term_map"' not in (
        REPO / "shared" / "content_backup.py"
    ).read_text(encoding="utf-8"), "term_map 不应成为备份 record 字段"


def test_contract_path_scan_still_finds_the_known_readback_paths() -> None:
    """扫描口径的金丝雀:契约常量改写法(改成拼接/换模块)会让下面几条静默失效。"""
    assert CONTRACT_CONSTANTS.get("SEMANTIC_BATCH_COMMIT_PATH") == (
        "output/provenance/semantic_batch.json"
    )
    for expected in (
        "output/provenance/semantic_batch.json",
        "output/notes_mechanical.md",
        "intermediate/ocr.json",
        "intermediate/segments.json",
        "intermediate/pdf_page_support.json",
    ):
        assert expected in CONTRACT_PATHS, f"契约路径扫描漏了 {expected}"


@pytest.mark.parametrize("path", sorted(CONTRACT_PATHS), ids=sorted(CONTRACT_PATHS))
def test_every_contract_readback_path_is_produced_by_some_step(path: str) -> None:
    """契约按固定路径回读、缺失即抛,那就必须有步骤声明产出它。

    pdf_page_support.json(D5)正是死在这里:evidence_contract 认死它存在,
    却没有任何 outputs 声明它,于是备份看不见、恢复后复算永久失败。
    """
    producers = [
        f"{pipeline}/{step['name']}"
        for pipeline, step in _all_steps()
        if _is_declared(path, _declared(step))
    ]
    assert producers, (
        f"契约回读 {path}(定义于 {CONTRACT_PATHS[path]}),但没有任何 step 声明产出它"
    )


@pytest.mark.parametrize("path", sorted(CONTRACT_PATHS), ids=sorted(CONTRACT_PATHS))
def test_steps_sharing_an_implementation_declare_the_same_contract_paths(path: str) -> None:
    """同一实现模块的步骤必须给出同一份契约路径声明。

    三个 *_semantic_attestation 步跑的是同一个 steps.utils.step_semantic_attestation,
    却各自维护一份 outputs。删掉其中一条声明,另外两条仍然存在,任何"至少有人声明"
    式的检查都还是绿的——D5 就是这么漏过去的。按实现模块对账才抓得住单点删除。
    """
    by_module: dict[str, list[tuple[str, bool]]] = {}
    for pipeline, step in _all_steps():
        module = _step_module(step)
        if not module:
            continue
        by_module.setdefault(module, []).append(
            (f"{pipeline}/{step['name']}", _is_declared(path, _declared(step)))
        )
    for module, entries in sorted(by_module.items()):
        declaring = sorted(name for name, ok in entries if ok)
        missing = sorted(name for name, ok in entries if not ok)
        assert not (declaring and missing), (
            f"{module} 的步骤对 {path} 声明不一致:{declaring} 声明了,{missing} 没有。"
            f"同一份实现产出同一份产物,漏声明的那个在备份里就是残缺的"
        )


def _steps_by_module() -> dict[str, list[tuple[str, dict]]]:
    grouped: dict[str, list[tuple[str, dict]]] = {}
    for pipeline, step in _all_steps():
        module = _step_module(step)
        if module:
            grouped.setdefault(module, []).append((pipeline, step))
    return grouped


@pytest.mark.parametrize("module", sorted(_steps_by_module()), ids=sorted(_steps_by_module()))
def test_artifacts_written_by_a_step_are_declared_by_its_pipeline(module: str) -> None:
    """实现里 artifacts.write() 的每个目标,都要有跑该实现的步骤声明它。

    新增一次写入而忘了改 YAML,产物就成了 unknown 残留:要么每次备份 fail-closed,
    要么被 --allow-unknown 批量放行而静默丢掉。这条把"改代码"和"改声明"绑在一起。

    口径按实现模块而非单个步骤:steps.common.step_01_download 被三条 pipeline 共用,
    其中 arXiv HTML 分支只有 document 走得到,静态分析判不出可达性。因此只要求
    "跑这个实现的步骤里有人声明",不强迫每条 pipeline 声明自己到不了的分支。
    单实现单步骤(绝大多数)时两者等价。
    """
    entries = _steps_by_module()[module]
    declared_anywhere = [glob for _, step in entries for glob in _declared(step)]
    undeclared = sorted(
        f"{path}(:{line})"
        for path, line in _written_paths(entries[0][1]).items()
        if not _is_declared(path, declared_anywhere)
    )
    owners = [f"{pipeline}/{step['name']}" for pipeline, step in entries]
    assert not undeclared, (
        f"{module} 写了未声明产物: {undeclared}; 该实现的步骤={owners}"
    )


def test_write_scan_is_not_silently_empty() -> None:
    """金丝雀:artifacts.write 改名会让上面那条对每个步骤都变成空断言。"""
    total = sum(len(_written_paths(step)) for _, step in _all_steps())
    assert total >= 20, f"写入扫描只找到 {total} 条,口径大概率失效了"


@pytest.mark.parametrize(
    "pipeline,step",
    _all_steps(),
    ids=[f"{pipeline}/{step['name']}" for pipeline, step in _all_steps()],
)
def test_declared_globs_stay_inside_the_job_artifact_tree(
    pipeline: str, step: dict,
) -> None:
    """声明只能指向 Job 产物树内的相对路径。

    outputs 参与 step 语义摘要,写错一条不会当场报错,只会在恢复投影时把整步判
    stale;因此形状问题必须在配置层拦下。
    """
    for pattern in _declared(step):
        assert _ARTIFACT_PATH.match(pattern), (
            f"{pipeline}/{step['name']} 的 outputs 含非产物树路径: {pattern!r}"
        )


def test_download_steps_declare_every_source_extension_they_can_produce() -> None:
    """01_download 落盘的原件扩展名必须全部被声明。

    yt-dlp 的 -o input/source.%(ext)s 与本地导入的 shutil.copyfile 都按来源的真实
    扩展名命名,所以支持一种新容器格式就等于多一种产物。audio 曾只声明
    mp3/m4a/mp4,.wav/.aac/.flac 上传件因此永远不进快照。
    """
    download = (REPO / "steps" / "common" / "step_01_download.py").read_text(encoding="utf-8")
    audio = next(
        step for pipeline, step in _all_steps()
        if pipeline == "audio" and step["name"] == "01_download"
    )
    globs = _declared(audio)
    for suffix in (".wav", ".aac", ".flac", ".mp3", ".m4a"):
        if suffix not in download:
            continue
        path = f"input/source{suffix}"
        assert _is_declared(path, globs), (
            f"audio/01_download 能产出 {path} 却没声明它;declared={globs}"
        )


def test_merge_parts_declares_the_assets_it_copies_to_the_job_root() -> None:
    """合并分段会把各 part 的帧图复制到 job 根 assets/,那是新产物不是引用。

    合并后的 dedup/ocr JSON 按新文件名指回这些图;只声明 JSON 不声明图,恢复后
    每张图都是坏链。
    """
    merge = next(
        step for pipeline, step in _all_steps()
        if pipeline == "video" and step["name"] == "09_merge_parts"
    )
    source = (REPO / "steps" / "video" / "step_09_merge_parts.py").read_text(encoding="utf-8")
    assert 'assets' in source
    assert _is_declared("assets/frame_001.png", _declared(merge)), (
        f"video/09_merge_parts 复制帧图到 job 根 assets/ 却没声明;"
        f"declared={_declared(merge)}"
    )
