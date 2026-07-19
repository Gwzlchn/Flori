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
import hashlib
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 容器内视图由 docker-compose 固定(docs/08-deployment.md §8.1);
# 非标准部署与测试用 env 覆盖,不改代码。
LIVE_DB_PATH_ENV = "FLORI_LIVE_DB_PATH"
LIVE_JOBS_DIR_ENV = "FLORI_LIVE_JOBS_DIR"
LIVE_CONFIG_ROOT_ENV = "FLORI_LIVE_CONFIG_ROOT"
LIVE_DATA_ROOT_ENV = "FLORI_LIVE_DATA_ROOT"
DEPLOYMENT_ID_ENV = "FLORI_DEPLOYMENT_ID"
DEFAULT_LIVE_DB_PATH = "/data/db/analyzer.db"
DEFAULT_LIVE_JOBS_DIR = "/data/jobs"
DEFAULT_LIVE_CONFIG_ROOT = "/data/prompts"
DEFAULT_LIVE_DATA_ROOT = "/data"
DEFAULT_PRODUCTION_BUCKET = "flori"

DR_MAX_AGE_ENV = "FLORI_DR_MAX_AGE_SEC"
DEFAULT_DR_MAX_AGE_SEC = 86_400
# 覆盖必须有上限:无上限的 env 等于把这道门交给调用者关掉。
DR_MAX_AGE_CEILING_SEC = 7 * 86_400
DR_FORMAT_NAME = "flori-disaster-recovery"
DR_SUPPORTED_FORMAT_VERSIONS = frozenset({1, 2})
DR_VALIDATOR_ENV = "FLORI_DR_VALIDATOR"
DR_SCHEMA_MANIFEST_ENV = "FLORI_SCHEMA_MANIFEST"

REMOTE_QUIESCE_ENV = "FLORI_REMOTE_WORKERS_QUIESCED"

TARGET_DATABASE = "database"
TARGET_ARTIFACT_ROOT = "artifact-root"
TARGET_OBJECT_STORE = "object-store"
TARGET_CONFIG_ROOT = "config-root"


class LiveTargetError(Exception):
    """目标解析为线上面但未取得显式授权,或 DR/quiesce 前置不成立。"""


def _reject_symlink_components(path: str | Path, *, label: str = "目标路径") -> Path:
    """拒绝任一已存在路径分量为 symlink,未存在尾部允许空库导入。"""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LiveTargetError(f"无法校验{label}分量 {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise LiveTargetError(f"{label}不允许符号链接分量: {current}")
    return absolute


def _normalized(path: str | Path, *, label: str = "目标路径") -> Path:
    # 先逐分量 lstat,再 resolve 未存在尾部。两步都不允许把线上面绕过词法比较。
    absolute = _reject_symlink_components(path, label=label)
    resolved = absolute.resolve(strict=False)
    if resolved != absolute:
        raise LiveTargetError(f"{label}解析后发生跳转,拒绝可变别名: {absolute} -> {resolved}")
    _reject_symlink_components(resolved, label=label)
    return resolved


def _same_existing_path(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError as exc:
        raise LiveTargetError(f"无法比较目标 inode: {left} / {right}: {exc}") from exc


def live_db_path() -> Path:
    return _normalized(
        os.environ.get(LIVE_DB_PATH_ENV) or DEFAULT_LIVE_DB_PATH,
        label="线上数据库路径",
    )


def live_jobs_dir() -> Path:
    return _normalized(
        os.environ.get(LIVE_JOBS_DIR_ENV) or DEFAULT_LIVE_JOBS_DIR,
        label="线上产物根",
    )


def live_config_root() -> Path:
    return _normalized(
        os.environ.get(LIVE_CONFIG_ROOT_ENV) or DEFAULT_LIVE_CONFIG_ROOT,
        label="线上配置根",
    )


def production_bucket() -> str:
    """生产桶 = 部署 env 里的那个;备份入口读的也是它,两侧必须同源。"""
    return os.environ.get("MINIO_BUCKET") or DEFAULT_PRODUCTION_BUCKET


def object_mode() -> bool:
    return bool(os.environ.get("MINIO_URL"))


def resolve_object_bucket(requested: str | None) -> str:
    return requested or production_bucket()


def is_live_db(db_path: str | Path) -> bool:
    resolved = _normalized(db_path, label="导入数据库路径")
    live = live_db_path()
    return resolved == live or _same_existing_path(resolved, live)


def is_live_artifact_root(jobs_dir: str | Path) -> bool:
    """产物根落在线上根之内也算线上:``/data/jobs/x`` 写的仍是线上对象树。"""
    resolved = _normalized(jobs_dir, label="导入产物根")
    live = live_jobs_dir()
    return (
        resolved == live
        or live in resolved.parents
        or _same_existing_path(resolved, live)
    )


def is_live_object_bucket(bucket: str | None) -> bool:
    return resolve_object_bucket(bucket) == production_bucket()


def is_live_config_root(config_root: str | Path) -> bool:
    resolved = _normalized(config_root, label="导入配置根")
    live = live_config_root()
    return resolved == live or _same_existing_path(resolved, live)


def _paths_overlap(left: str | Path, right: str | Path, *, left_label: str) -> bool:
    """角色无关比较两个物理目标;父子覆盖和现存inode别名都算重叠。"""
    first = _normalized(left, label=left_label)
    second = _normalized(right, label="线上目标")
    if first == second or first in second.parents or second in first.parents:
        return True
    return _same_existing_path(first, second)


def resolve_live_targets(
    *, db_path: str | Path, jobs_dir: str | Path, object_bucket: str | None,
    config_root: str | Path | None = None,
    source_roots: list[str | Path] | tuple[str | Path, ...] = (),
) -> list[str]:
    """本次导入会写到的线上面清单;空 = 全部落在隔离区。

    对象存储没有本地路径,隔离只能靠显式的、与生产桶不同的桶表达;
    没给出显式桶就等于写生产桶,必须按线上处理而不是按 staging 处理。
    """
    targets: list[str] = []
    candidates: list[tuple[str, str | Path]] = [("导入数据库路径", db_path)]
    if not object_mode():
        candidates.append(("导入产物根", jobs_dir))
    if config_root is not None:
        candidates.append(("导入配置根", config_root))
    candidates.extend(("导入来源根", root) for root in source_roots)
    live_paths = (
        (TARGET_DATABASE, live_db_path()),
        (TARGET_ARTIFACT_ROOT, live_jobs_dir()),
        (TARGET_CONFIG_ROOT, live_config_root()),
    )
    for label, candidate in candidates:
        for target, live_path in live_paths:
            if _paths_overlap(candidate, live_path, left_label=label) and target not in targets:
                targets.append(target)
    if object_mode():
        if is_live_object_bucket(object_bucket):
            targets.append(TARGET_OBJECT_STORE)
    order = {
        TARGET_DATABASE: 0,
        TARGET_ARTIFACT_ROOT: 1,
        TARGET_OBJECT_STORE: 2,
        TARGET_CONFIG_ROOT: 3,
    }
    return sorted(set(targets), key=order.__getitem__)


def describe_targets(targets: list[str]) -> str:
    labels = {
        TARGET_DATABASE: f"数据库 {live_db_path()}",
        TARGET_ARTIFACT_ROOT: f"产物根 {live_jobs_dir()}",
        TARGET_OBJECT_STORE: f"对象存储桶 {production_bucket()}",
        TARGET_CONFIG_ROOT: f"配置根 {live_config_root()}",
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
        f"确实要写线上面({live_db_path()} / {live_jobs_dir()} / "
        f"{live_config_root()})时加 --into-live"
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LiveTargetError(f"无法读取 exact DR 归档 {path}: {exc}") from exc
    return digest.hexdigest()


def _dr_validator_path() -> Path:
    configured = os.environ.get(DR_VALIDATOR_ENV)
    candidate = Path(configured) if configured else Path(__file__).parents[1] / "scripts" / "dr_snapshot.py"
    candidate = _normalized(candidate, label="DR validator")
    if not candidate.is_file():
        raise LiveTargetError(
            f"缺少 exact DR 全链校验器: {candidate};"
            f"请设 {DR_VALIDATOR_ENV} 指向 scripts/dr_snapshot.py"
        )
    return candidate


def _validate_dr_archive(archive: Path) -> dict:
    command = [sys.executable, str(_dr_validator_path()), "validate", "--archive", str(archive)]
    schema_manifest = os.environ.get(DR_SCHEMA_MANIFEST_ENV)
    if schema_manifest:
        schema = _normalized(schema_manifest, label="schema manifest")
        if not schema.is_file():
            raise LiveTargetError(f"schema manifest 不存在: {schema}")
        command.extend(["--schema-manifest", str(schema)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise LiveTargetError(f"exact DR 归档全链校验失败: {detail[:1000]}")
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise LiveTargetError("exact DR 校验器未返回可用 JSON") from exc
    if type(result) is not dict or result.get("status") != "success" or \
            result.get("operation") != "validate":
        raise LiveTargetError("exact DR 校验器未返回 success/validate")
    return result


def _dr_asset_coverage(manifest: dict, targets: list[str]) -> dict:
    assets = manifest.get("assets")
    if type(assets) is not dict:
        raise LiveTargetError("exact DR manifest 缺少 assets,不能证明线上写面可回滚")

    def included(name: str) -> dict:
        value = assets.get(name)
        if type(value) is not dict or value.get("included") is not True:
            raise LiveTargetError(f"exact DR 未包含本次线上写入需要的 {name} 资产")
        return value

    required_data_subtrees: dict[str, Path] = {}
    covered: list[str] = []
    if TARGET_DATABASE in targets:
        required_data_subtrees[TARGET_DATABASE] = live_db_path()
    if TARGET_ARTIFACT_ROOT in targets:
        required_data_subtrees[TARGET_ARTIFACT_ROOT] = live_jobs_dir()
    if TARGET_CONFIG_ROOT in targets:
        required_data_subtrees[TARGET_CONFIG_ROOT] = live_config_root()
    if required_data_subtrees:
        data_asset = included("data")
        data_root = _normalized(
            os.environ.get(LIVE_DATA_ROOT_ENV) or DEFAULT_LIVE_DATA_ROOT,
            label="线上数据根",
        )
        exclusions = data_asset.get("excluded_external_subtrees") or []
        if not isinstance(exclusions, list) or not all(isinstance(item, str) for item in exclusions):
            raise LiveTargetError("exact DR data 资产的排除清单非法")
        for target, root in required_data_subtrees.items():
            normalized = _normalized(root, label=f"{target}线上根")
            try:
                relative = normalized.relative_to(data_root).as_posix()
            except ValueError as exc:
                raise LiveTargetError(
                    f"exact DR data 根 {data_root} 不覆盖线上目标 {normalized}"
                ) from exc
            for excluded in exclusions:
                excluded_path = Path(excluded)
                required_path = Path(relative)
                if (
                    excluded_path == required_path
                    or excluded_path in required_path.parents
                    or required_path in excluded_path.parents
                ):
                    raise LiveTargetError(
                        f"exact DR 排除了本次线上目标 {target}: {excluded}"
                    )
            covered.append(target)
    if TARGET_OBJECT_STORE in targets:
        minio = included("minio")
        exclusions = minio.get("excluded_external_subtrees") or []
        if exclusions:
            raise LiveTargetError(
                "exact DR MinIO 含外部排除项,不能证明生产桶完整可回滚"
            )
        covered.append(TARGET_OBJECT_STORE)
    return {"covered_targets": sorted(set(covered)), "assets": assets}


def verify_dr_receipt(
    receipt_path: str | Path, *, now: datetime | None = None,
    expected_deployment_id: str | None = None,
    required_targets: list[str] | None = None,
) -> dict:
    """解析并校验 exact DR receipt,返回摘要;任何不成立都抛。

    旧实现只查文件存在 + mtime,``FLORI_DR_RECEIPT=/etc/hostname`` 都能过,
    而 mtime 是 ``touch`` 一下就能伪造的。新鲜度必须取自 receipt 内部由
    scripts/dr_snapshot.py 写入的 ``manifest.created_at``。
    """
    path = _normalized(receipt_path, label="DR receipt")
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
    format_version = manifest.get("format_version")
    if type(format_version) is not int or format_version not in DR_SUPPORTED_FORMAT_VERSIONS:
        raise LiveTargetError(f"DR receipt 的 manifest.format_version 不受支持: {format_version!r}")
    deployment = manifest.get("deployment")
    deployment_id = deployment.get("id") if isinstance(deployment, dict) else None
    if expected_deployment_id is not None and deployment_id != expected_deployment_id:
        raise LiveTargetError(
            "exact DR deployment id 与当前实例不一致: "
            f"{deployment_id!r} != {expected_deployment_id!r}"
        )
    generation = body.get("generation")
    if type(generation) is not str or not generation or manifest.get("generation") != generation:
        raise LiveTargetError("DR receipt 的 generation 与 manifest.generation 不一致")
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
    archive_value = body.get("archive")
    if type(archive_value) is not str or not archive_value:
        raise LiveTargetError(f"DR receipt 缺少 archive: {path}")
    archive_name = Path(archive_value).name
    if archive_name in {"", ".", ".."}:
        raise LiveTargetError(f"DR receipt 的 archive 名非法: {archive_value!r}")
    # backup.sh 会把 result 与 archive 发布在同一目录。只按 basename 定位,
    # 不信任 receipt 中的容器内 /output 路径,也不允许它越出 receipt 目录。
    archive = _normalized(path.parent / archive_name, label="exact DR 归档")
    if archive.parent != path.parent or not archive.is_file():
        raise LiveTargetError(f"exact DR 归档不存在或不在 receipt 同目录: {archive}")
    sidecar = _normalized(
        archive.with_suffix(archive.suffix + ".sha256"), label="exact DR sha256 sidecar",
    )
    if not sidecar.is_file():
        raise LiveTargetError(f"exact DR 归档缺少 sha256 sidecar: {sidecar}")
    actual_digest = _sha256(archive)
    if actual_digest != digest:
        raise LiveTargetError(
            f"exact DR 归档 SHA 与 receipt 不一致: {actual_digest} != {digest}"
        )
    try:
        sidecar_parts = sidecar.read_text(encoding="utf-8").strip().split()
    except (OSError, UnicodeDecodeError) as exc:
        raise LiveTargetError(f"exact DR sha256 sidecar 不可读: {sidecar}: {exc}") from exc
    if (
        len(sidecar_parts) != 2
        or sidecar_parts[1] != archive.name
        or sidecar_parts[0].lower() != actual_digest
        or sidecar_parts[0].lower() != digest
    ):
        raise LiveTargetError("exact DR sha256 sidecar 与归档/receipt 不一致")
    validation = _validate_dr_archive(archive)
    if validation.get("generation") != generation:
        raise LiveTargetError("exact DR 归档 generation 与 receipt 不一致")
    if validation.get("format") not in (None, DR_FORMAT_NAME):
        raise LiveTargetError("exact DR 归档 format 与 receipt 不一致")
    if validation.get("format_version") not in (None, format_version):
        raise LiveTargetError("exact DR 归档 format_version 与 receipt 不一致")
    if expected_deployment_id is not None and validation.get("deployment_id") != deployment_id:
        raise LiveTargetError("exact DR 归档 deployment id 与 receipt 不一致")
    if required_targets is not None and validation.get("assets") != manifest.get("assets"):
        raise LiveTargetError("exact DR 归档 assets 与 receipt 不一致")
    archive_created_at = _parse_timestamp(
        validation.get("created_at"), "archive manifest.created_at",
    )
    if archive_created_at != created_at:
        raise LiveTargetError("exact DR 归档 created_at 与 receipt 不一致")
    coverage = _dr_asset_coverage(manifest, required_targets or []) \
        if required_targets is not None else {"covered_targets": [], "assets": {}}
    return {
        "path": str(path),
        "generation": generation,
        "archive": str(archive),
        "archive_sha256": digest,
        "format_version": format_version,
        "created_at": created_at.isoformat(),
        "age_seconds": age,
        "max_age_seconds": limit,
        "validation": validation.get("checks", {}),
        "deployment_id": deployment_id,
        "coverage": coverage,
    }


def assert_write_authorized(
    *,
    db_path: str | Path,
    jobs_dir: str | Path,
    object_bucket: str | None,
    config_root: str | Path | None = None,
    source_roots: list[str | Path] | tuple[str | Path, ...] = (),
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
        config_root=config_root, source_roots=source_roots,
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
            f"写线上面还需要确认远程 worker 已停:本机 maintenance flock"
            f"无法覆盖跨机进程。确认后设 {REMOTE_QUIESCE_ENV}=1"
        )
    if dr_receipt is None:
        raise LiveTargetError(
            "写线上面必须提供 exact DR receipt(--dr-receipt / FLORI_DR_RECEIPT);"
            "portable 仓库不是回滚手段"
        )
    deployment_id = os.environ.get(DEPLOYMENT_ID_ENV)
    if (
        not deployment_id or deployment_id == "unbound" or len(deployment_id) > 128
        or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in deployment_id)
    ):
        raise LiveTargetError(
            f"写线上面必须设置稳定且非unbound的 {DEPLOYMENT_ID_ENV}"
            "([A-Za-z0-9_.-],最长128位)"
        )
    from .content_maintenance import live_import_resources

    resources = live_import_resources(
        targets=targets,
        live_db_path=live_db_path(),
        live_jobs_dir=live_jobs_dir(),
        production_bucket=production_bucket(),
        live_config_root=live_config_root(),
    )
    return {
        "live_targets": targets,
        "into_live": True,
        "dr_receipt": verify_dr_receipt(
            dr_receipt, now=now,
            expected_deployment_id=deployment_id,
            required_targets=targets,
        ),
        "maintenance_resources": list(resources),
    }


def create_import_storage(jobs_dir: str | Path, *, object_bucket: str | None):
    """导入侧唯一的 storage 构造口。

    绝不裸调 ``create_storage``:那条路径在 MINIO_URL 存在时会丢掉 jobs_dir 并
    绑到生产桶,让"隔离 staging"变成静默写生产。桶必须由本模块显式解析。
    """
    from .storage import create_storage

    return create_storage(Path(jobs_dir), bucket=resolve_object_bucket(object_bucket))
