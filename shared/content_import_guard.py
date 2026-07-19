"""导入侧的线上面隔离门:目标身份判定、DR receipt 校验、storage 构造。

存在的理由是一个真实的生产缺口:``shared.storage.create_storage`` 在设了
``MINIO_URL`` 时完全忽略 jobs_dir 参数,直接返回生产桶。于是"默认写隔离
staging"这条安全属性在生产后端上根本不成立,而全部演练与单测都跑在
LocalStorage 上,永远看不见。

因此隔离与放行都不看 flag,只看目标的实际身份:线上库路径、线上产物根、
生产桶。判定为线上就必须显式确认,并附带 quiesce 与够新的 exact DR receipt。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# 容器内视图由 docker-compose 固定(docs/08-deployment.md §8.1);
# 非标准部署与测试用 env 覆盖,不改代码。
LIVE_DB_PATH_ENV = "FLORI_LIVE_DB_PATH"
LIVE_JOBS_DIR_ENV = "FLORI_LIVE_JOBS_DIR"
DEFAULT_LIVE_DB_PATH = "/data/db/analyzer.db"
DEFAULT_LIVE_JOBS_DIR = "/data/jobs"
DEFAULT_PRODUCTION_BUCKET = "flori"

DR_MAX_AGE_ENV = "FLORI_DR_MAX_AGE_SEC"
DEFAULT_DR_MAX_AGE_SEC = 86_400
# 覆盖必须有上限:无上限的 env 等于把这道门交给调用者关掉。
DR_MAX_AGE_CEILING_SEC = 7 * 86_400
DR_FORMAT_NAME = "flori-disaster-recovery"

REMOTE_QUIESCE_ENV = "FLORI_REMOTE_WORKERS_QUIESCED"

TARGET_DATABASE = "database"
TARGET_ARTIFACT_ROOT = "artifact-root"
TARGET_OBJECT_STORE = "object-store"


class LiveTargetError(Exception):
    """目标解析为线上面但未取得显式授权,或 DR/quiesce 前置不成立。"""


def _normalized(path: str | Path) -> Path:
    # 目标可能尚不存在(空库导入),不能用 resolve(strict=True);
    # 同时要吃掉 ``//``、``.`` 与末尾斜杠这些纯书写差异。
    return Path(os.path.normpath(str(Path(path)))).absolute()


def live_db_path() -> Path:
    return _normalized(os.environ.get(LIVE_DB_PATH_ENV) or DEFAULT_LIVE_DB_PATH)


def live_jobs_dir() -> Path:
    return _normalized(os.environ.get(LIVE_JOBS_DIR_ENV) or DEFAULT_LIVE_JOBS_DIR)


def production_bucket() -> str:
    """生产桶 = 部署 env 里的那个;备份入口读的也是它,两侧必须同源。"""
    return os.environ.get("MINIO_BUCKET") or DEFAULT_PRODUCTION_BUCKET


def object_mode() -> bool:
    return bool(os.environ.get("MINIO_URL"))


def resolve_object_bucket(requested: str | None) -> str:
    return requested or production_bucket()


def is_live_db(db_path: str | Path) -> bool:
    return _normalized(db_path) == live_db_path()


def is_live_artifact_root(jobs_dir: str | Path) -> bool:
    """产物根落在线上根之内也算线上:``/data/jobs/x`` 写的仍是线上对象树。"""
    resolved = _normalized(jobs_dir)
    live = live_jobs_dir()
    return resolved == live or live in resolved.parents


def is_live_object_bucket(bucket: str | None) -> bool:
    return resolve_object_bucket(bucket) == production_bucket()


def resolve_live_targets(
    *, db_path: str | Path, jobs_dir: str | Path, object_bucket: str | None,
) -> list[str]:
    """本次导入会写到的线上面清单;空 = 全部落在隔离区。

    对象存储没有本地路径,隔离只能靠显式的、与生产桶不同的桶表达;
    没给出显式桶就等于写生产桶,必须按线上处理而不是按 staging 处理。
    """
    targets: list[str] = []
    if is_live_db(db_path):
        targets.append(TARGET_DATABASE)
    if object_mode():
        if is_live_object_bucket(object_bucket):
            targets.append(TARGET_OBJECT_STORE)
    elif is_live_artifact_root(jobs_dir):
        targets.append(TARGET_ARTIFACT_ROOT)
    return targets


def describe_targets(targets: list[str]) -> str:
    labels = {
        TARGET_DATABASE: f"数据库 {live_db_path()}",
        TARGET_ARTIFACT_ROOT: f"产物根 {live_jobs_dir()}",
        TARGET_OBJECT_STORE: f"对象存储桶 {production_bucket()}",
    }
    return ", ".join(labels.get(item, item) for item in targets)


def isolation_hint() -> str:
    if object_mode():
        return (
            "对象存储模式下隔离必须显式:传 --object-bucket <隔离桶>(须不同于生产桶 "
            f"{production_bucket()});确实要写生产面时加 --into-live"
        )
    return (
        f"传 --db/--jobs-dir 指向隔离目录(默认 staging 即可),"
        f"确实要写线上面({live_db_path()} / {live_jobs_dir()})时加 --into-live"
    )


def dr_max_age_seconds() -> int:
    raw = os.environ.get(DR_MAX_AGE_ENV)
    if not raw:
        return DEFAULT_DR_MAX_AGE_SEC
    try:
        value = int(raw)
    except ValueError as exc:
        raise LiveTargetError(f"{DR_MAX_AGE_ENV} 必须是整数秒,收到 {raw!r}") from exc
    if value <= 0:
        raise LiveTargetError(f"{DR_MAX_AGE_ENV} 必须为正整数秒,收到 {value}")
    if value > DR_MAX_AGE_CEILING_SEC:
        raise LiveTargetError(
            f"{DR_MAX_AGE_ENV}={value} 超过硬上限 {DR_MAX_AGE_CEILING_SEC}s;"
            "这道门不接受把自己关掉的覆盖值"
        )
    return value


def _parse_timestamp(value: object, field: str) -> datetime:
    if type(value) is not str or not value:
        raise LiveTargetError(f"DR receipt 的 {field} 缺失或不是字符串")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LiveTargetError(f"DR receipt 的 {field} 不是 ISO8601 时间: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def verify_dr_receipt(receipt_path: str | Path, *, now: datetime | None = None) -> dict:
    """解析并校验 exact DR receipt,返回摘要;任何不成立都抛。

    旧实现只查文件存在 + mtime,``FLORI_DR_RECEIPT=/etc/hostname`` 都能过,
    而 mtime 是 ``touch`` 一下就能伪造的。新鲜度必须取自 receipt 内部由
    scripts/dr_snapshot.py 写入的 ``manifest.created_at``。
    """
    path = Path(receipt_path)
    if not path.is_file():
        raise LiveTargetError(f"DR receipt 不存在或不是普通文件: {path}")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveTargetError(f"DR receipt 不是可读 JSON: {path}({exc})") from exc
    if type(body) is not dict:
        raise LiveTargetError(f"DR receipt 顶层必须是 JSON 对象: {path}")
    if body.get("status") != "success" or body.get("operation") != "backup":
        raise LiveTargetError(
            f"DR receipt 不是一次成功的 exact DR 备份结果: {path}"
            f"(status={body.get('status')!r}, operation={body.get('operation')!r})"
        )
    digest = body.get("archive_sha256")
    if type(digest) is not str or len(digest) != 64 or \
            any(ch not in "0123456789abcdef" for ch in digest):
        raise LiveTargetError(f"DR receipt 缺少合法 archive_sha256: {path}")
    manifest = body.get("manifest")
    if type(manifest) is not dict:
        raise LiveTargetError(f"DR receipt 缺少 manifest 块: {path}")
    if manifest.get("format") != DR_FORMAT_NAME:
        raise LiveTargetError(
            f"DR receipt 的 manifest.format 不是 {DR_FORMAT_NAME}: {path}"
        )
    created_at = _parse_timestamp(manifest.get("created_at"), "manifest.created_at")
    moment = now or datetime.now(timezone.utc)
    age = int((moment - created_at).total_seconds())
    if age < 0:
        raise LiveTargetError(
            f"DR receipt 的 manifest.created_at 在未来({created_at.isoformat()});"
            "时钟错误或 receipt 被伪造"
        )
    limit = dr_max_age_seconds()
    if age > limit:
        raise LiveTargetError(
            f"exact DR receipt 已过期({age}s > {limit}s),先重跑 scripts/backup.sh"
        )
    return {
        "path": str(path),
        "generation": body.get("generation"),
        "archive_sha256": digest,
        "created_at": created_at.isoformat(),
        "age_seconds": age,
        "max_age_seconds": limit,
    }


def assert_write_authorized(
    *,
    db_path: str | Path,
    jobs_dir: str | Path,
    object_bucket: str | None,
    into_live: bool,
    dr_receipt: str | Path | None,
    now: datetime | None = None,
) -> dict:
    """写入前的唯一授权入口;返回写进结果 JSON 的审计块。

    ``--into-live`` 只是操作者的确认,不是判定依据:判定全部来自目标身份,
    因此显式 ``--jobs-dir /data/jobs`` 这类绕过默认分支的写法照样被拦。
    """
    targets = resolve_live_targets(
        db_path=db_path, jobs_dir=jobs_dir, object_bucket=object_bucket,
    )
    if not targets:
        if into_live:
            raise LiveTargetError(
                "--into-live 但解析出的目标全在隔离区;"
                "要么去掉该开关,要么把目标指向线上面"
            )
        return {"live_targets": [], "into_live": False}
    if not into_live:
        raise LiveTargetError(
            f"目标解析为线上面({describe_targets(targets)}),但没有 --into-live。"
            f"{isolation_hint()}"
        )
    if os.environ.get(REMOTE_QUIESCE_ENV) != "1":
        raise LiveTargetError(
            f"写线上面还需要确认远程 worker 已停:docker ps 只看得到本机容器,"
            f"跨机 worker 仍可能在写同一个桶。确认后设 {REMOTE_QUIESCE_ENV}=1"
        )
    if dr_receipt is None:
        raise LiveTargetError(
            "写线上面必须提供 exact DR receipt(--dr-receipt / FLORI_DR_RECEIPT);"
            "portable 仓库不是回滚手段"
        )
    return {
        "live_targets": targets,
        "into_live": True,
        "dr_receipt": verify_dr_receipt(dr_receipt, now=now),
    }


def create_import_storage(jobs_dir: str | Path, *, object_bucket: str | None):
    """导入侧唯一的 storage 构造口。

    绝不裸调 ``create_storage``:那条路径在 MINIO_URL 存在时会丢掉 jobs_dir 并
    绑到生产桶,让"隔离 staging"变成静默写生产。桶必须由本模块显式解析。
    """
    from .storage import create_storage

    return create_storage(Path(jobs_dir), bucket=resolve_object_bucket(object_bucket))
