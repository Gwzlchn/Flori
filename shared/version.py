"""构建版本单一事实源。

FLORI_VERSION = 语义版本 + 构建短 sha,形如 ``0.2.0+f1d86f0``:
- 语义版本:单一源 = ``pyproject.toml`` 的 ``[project].version``,经 ``importlib.metadata``
  读取已安装的 ``flori`` 包(镜像内 pip 装了)。**每次改系统/worker 行为时按语义递增此值**。
- 构建短 sha:构建期经 env ``FLORI_BUILD_SHA`` 注入(CI=github.sha,本地=git short),取前 7 位;
  用于"哪台 worker 跑哪份代码"的漂移排查。未注入(纯源码/开发)则只剩语义版本。

显式 env ``FLORI_VERSION`` 仍可整体覆盖(测试/特殊场景)。
api / scheduler / worker 三处共用本常量(单一事实源),避免各自读 env 而版本显示漂移。

注:CI/build 构建期把 pyproject ``version`` 抹成占位 ``0.0.0``(稳 pip 缓存层),故镜像内
``importlib.metadata`` 读到的语义版本是 ``0.0.0``;真实版本靠 ``FLORI_VERSION`` build-arg → env
覆盖(上面的分支),故运行时显示仍准。见 docker/base.Dockerfile / ci.yml「稳定 pip 缓存」步。
"""

from __future__ import annotations

import os


def _semver() -> str:
    """读已安装 flori 包的版本(= pyproject [project].version)。未安装则兜底。"""
    try:
        from importlib.metadata import version as _pkgver

        return _pkgver("flori")
    except Exception:
        return "0.0.0"


_BUILD = (os.environ.get("FLORI_BUILD_SHA") or "").strip()[:7]
_SEMVER = _semver()

FLORI_VERSION: str = (
    os.environ["FLORI_VERSION"]
    if os.environ.get("FLORI_VERSION")
    else (f"{_SEMVER}+{_BUILD}" if _BUILD else _SEMVER)
)
