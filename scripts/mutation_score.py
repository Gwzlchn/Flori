#!/usr/bin/env python3
"""真实变异分数驱动(绕过 mutmut 3.6 自动 runner 的 killed=0 缺陷)。

为什么需要它:mutmut 3.6 的 `mutmut run` 自带的"跑测试判生死"那一步在本仓库布局下恒报
killed=0(它没把被测代码从 mutants/ 副本里激活——见 mutation.yml 注释)。但底层机制是好的:
设 MUTANT_UNDER_TEST={module}.{mutant_func} 后从 mutants/ 跑 pytest,trampoline 会真正路由到
该变异体(实测 assert_public_url 的逻辑变异确实被 test_net 杀掉)。本脚本就把这条"手动 recipe"
做成可重复的循环,产出**真实**的 killed/survived。

用法(在测试容器内,cwd=/app):
  python3 scripts/mutation_score.py [目标前缀过滤]
  - 不带参数:跑下面 TARGETS 里的全部核心模块。
  - 带参数:只跑 key 含该子串的目标(如 `ai_gateway` 只跑计费模块)。

慢是变异测试的固有属性(每个变异体都要跑一遍相关测试),故只对"计费/正确性关键"模块开,
且每个目标只跑它相关的测试(不是全套),把时间压到可接受。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

# 目标模块前缀 → 该模块"相关"的测试文件(变异体只用这些测试判生死)。
# 选的都是 docs/09-testing.md 反复强调的钱/并发/状态机安全面。
# 生成阶段的基线测试:只为让 mutmut 的(无效)自动 run 快速跑完以产出 mutants/——
# 变异体只取决于 source_paths,与跑什么测试无关,故固定用一个最快的测试当基线。
GEN_BASELINE = ["tests/test_net.py"]

TARGETS: dict[str, list[str]] = {
    "shared.ai_gateway": ["tests/test_ai_gateway.py"],
    "shared.db": ["tests/test_db.py"],
    "scheduler": ["tests/test_scheduler.py", "tests/test_runner_ops.py",
                  "tests/test_pipeline_config.py"],
    "worker": ["tests/test_worker.py", "tests/test_transport.py"],
}

_MUTANT_DEF = re.compile(r"^def (x_[A-Za-z0-9_]+__mutmut_\d+)\(", re.M)
_SEL = re.compile(r"pytest_add_cli_args_test_selection = \[[^\]]*\]")


def _set_generation_selection() -> str:
    """把 [tool.mutmut] 的测试选择临时改成最快基线,让 mutmut 的(无效)生成跑得快;
    返回原始 pyproject 文本以便还原。生成出的变异体只取决于 source_paths,与选择无关。"""
    p = pathlib.Path("pyproject.toml")
    orig = p.read_text()
    sel = ", ".join(repr(t) for t in [*GEN_BASELINE, "-m", "not fuzz"])
    p.write_text(_SEL.sub(f"pytest_add_cli_args_test_selection = [{sel}]", orig))
    return orig


def _enumerate_mutants(prefix: str) -> list[str]:
    """从生成好的 mutants/ 源码里枚举全部变异体 id = {module}.{mutant_func}。
    直接扫源码比 `mutmut results` 可靠(后者会漏列未"check"的逻辑变异)。"""
    ids: list[str] = []
    for f in sorted(pathlib.Path("mutants").rglob("*.py")):
        rel = f.relative_to("mutants")
        if rel.parts[0] == "tests":
            continue
        module = ".".join(rel.with_suffix("").parts)
        if not (module == prefix or module.startswith(prefix + ".")):
            continue
        for m in _MUTANT_DEF.finditer(f.read_text()):
            ids.append(f"{module}.{m.group(1)}")
    return ids


def _score(ids: list[str], tests: list[str]) -> tuple[int, int]:
    killed = survived = 0
    for mid in ids:
        env = {**os.environ, "MUTANT_UNDER_TEST": mid}
        r = subprocess.run(
            [sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"],
            cwd="mutants", env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # 退出码 0 = 全过 = 变异体存活(测试盲区);非 0 = 有测试挂 = 变异体被杀。
        if r.returncode == 0:
            survived += 1
        else:
            killed += 1
    return killed, survived


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    g_k = g_s = 0
    results: list[tuple[str, int, int]] = []   # (prefix, killed, survived)
    for prefix, tests in TARGETS.items():
        if only and only not in prefix:
            continue
        print(f"mutation scoring: {prefix}  (tests: {' '.join(tests)})", flush=True)
        orig = _set_generation_selection()
        try:
            subprocess.run(["mutmut", "run", prefix + "*"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            pathlib.Path("pyproject.toml").write_text(orig)  # 还原,勿污染仓库
        ids = _enumerate_mutants(prefix)
        killed, survived = _score(ids, tests)
        results.append((prefix, killed, survived))
        total = killed + survived
        pct = 100.0 * killed / total if total else 0.0
        print(f"  {prefix:22s} killed={killed:4d} survived={survived:4d}"
              f"  total={total:4d}  score={pct:5.1f}%", flush=True)
        g_k += killed
        g_s += survived
    gt = g_k + g_s

    print("\n变异分数汇总(真实,非 mutmut 自报的 killed=0)")
    for prefix, k, s in results:
        t = k + s
        print(f"  {prefix:22s} killed={k:4d} survived={s:4d}  total={t:4d}"
              f"  score={100.0 * k / t if t else 0:5.1f}%")
    print(f"  {'TOTAL':22s} killed={g_k:4d} survived={g_s:4d}"
          f"  total={gt:4d}  score={100.0 * g_k / gt if gt else 0:.1f}%")

    # GitHub Actions job summary 友好的 markdown 块(工作流抽这段 >> $GITHUB_STEP_SUMMARY)。
    print("\n<!--MUTATION-SUMMARY-->")
    print("### 🧬 变异分数(killed/总数;存活=测试盲区,非阻塞)")
    print("| 模块 | killed | survived | total | score |")
    print("|---|---:|---:|---:|---:|")
    for prefix, k, s in results:
        t = k + s
        print(f"| `{prefix}` | {k} | {s} | {t} | {100.0 * k / t if t else 0:.1f}% |")
    print(f"| **TOTAL** | **{g_k}** | **{g_s}** | **{gt}** | **{100.0 * g_k / gt if gt else 0:.1f}%** |")
    print("<!--/MUTATION-SUMMARY-->")

    # 机器可读块:工作流抽这段、加日期后追加到 mutation-data 分支的 history.csv(趋势源数据)。
    # 格式:module,killed,survived(分数由 mutation_report.py 算,避免重复定义)。
    print("<!--MUTATION-CSV-->")
    for prefix, k, s in results:
        print(f"{prefix},{k},{s}")
    print("<!--/MUTATION-CSV-->")
    # 存活变异 = 断言盲区,但初期必有(等价变异 / 误差信息变异)→ 不让本步变红,供人工裁定。
    return 0


if __name__ == "__main__":
    sys.exit(main())
