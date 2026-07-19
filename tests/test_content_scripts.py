"""三个便携仓库出货入口(content-backup/import/gc)的 shell 层覆盖。

这三个 .sh 此前零自动化覆盖:没有 test、没有 shellcheck、连 bash -n 都没跑过。
四次评审里有三次的缺陷落在 shell 层(把关只看 flag、帮助泄漏源码行、早退不写
result JSON、退出码与 python 侧不一致),因为没有任何东西在看它们。

做法与 tests/test_test_script.py 同构:PATH 上打桩 docker,记录 argv,再对每个
flag 组合断言实际发出的命令行。不起容器,因此可以进普通测试轮。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

from shared.content_backup import build_parser as backup_parser
from shared.content_gc import build_parser as gc_parser
from shared.content_import import build_parser as import_parser

REPO = Path(__file__).parents[1]
BACKUP = REPO / "scripts/content-backup.sh"
IMPORT = REPO / "scripts/content-import.sh"
GC = REPO / "scripts/content-gc.sh"
SCRIPTS = (BACKUP, IMPORT, GC)

_FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  volume) exit "${FAKE_VOLUME_RC:-0}" ;;
  ps) printf '%s' "${FAKE_RUNNING_CONTAINERS:-}"; exit "${FAKE_PS_RC:-0}" ;;
esac
if [ -n "${FAKE_MOVE_RESULT_PARENT:-}" ]; then
  mv "$FAKE_MOVE_RESULT_PARENT" "$FAKE_MOVED_RESULT_PARENT"
  mkdir "$FAKE_MOVE_RESULT_PARENT"
  printf '{"ok":true}\n' > "$FAKE_MOVED_RESULT_PARENT/$FAKE_RESULT_LEAF"
fi
if [ -n "${FAKE_MOVE_SOURCE_ROOT:-}" ]; then
  mv "$FAKE_MOVE_SOURCE_ROOT" "$FAKE_MOVED_SOURCE_ROOT"
  mkdir "$FAKE_MOVE_SOURCE_ROOT"
  [ -z "${FAKE_SOURCE_RESULT_FILE:-}" ] || \
    printf '{"ok":true}\n' > "$FAKE_SOURCE_RESULT_FILE"
fi
exit 0
"""


@pytest.fixture
def shell(tmp_path):
    """打桩 docker + 一套可用的默认参数;返回 (run, log_path, paths)。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "docker.log"

    repo_dir = tmp_path / "content-repo"
    repo_dir.mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "jobs").mkdir(parents=True)

    def run(script: Path, *args: str, env_overrides: dict | None = None):
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            "FLORI_DATA_DIR": str(data_dir),
        })
        for key in ("MINIO_URL", "MINIO_BUCKET", "FLORI_DR_RECEIPT",
                    "FLORI_REMOTE_WORKERS_QUIESCED",
                    "FLORI_ACCEPT_INCOMPLETE_PORTABLE", "FLORI_LIVE_CONFIG_ROOT",
                    "IMAGE_TAG"):
            environment.pop(key, None)
        environment.update(env_overrides or {})
        return subprocess.run(
            ["bash", str(script), *args],
            cwd=REPO, env=environment, capture_output=True, text=True, check=False,
        )

    return {
        "run": run, "log": log, "repo": repo_dir, "data": data_dir, "tmp": tmp_path,
    }


def _calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _last_run(log: Path) -> str:
    runs = [call for call in _calls(log) if call.startswith("run ")]
    assert runs, f"没有发出 docker run;实际调用: {_calls(log)}"
    return runs[-1]


# shell 发出的 argv 与容器内 argparse 之间原本只有"两边各写一遍同样的字面量"这一条
# 纽带:协同改一处就能同时骗过两套测试,而 shell 发出一个 argparse 根本没定义的 flag
# 会一路绿灯,直到生产上容器以 exit 2 收场。下面把真解析器接进来做机械对账。
_PARSERS = {
    "shared.content_backup": backup_parser,
    "shared.content_import": import_parser,
    "shared.content_gc": gc_parser,
}


def _emitted_argv(log: Path) -> tuple[str, list[str]]:
    """从 fake docker 的 run 行里截出 `python -m <模块> ...` 之后的真实 argv。"""
    tokens = _last_run(log).split()
    assert "python" in tokens, f"docker run 里没有 python 调用: {tokens}"
    index = tokens.index("python")
    assert tokens[index + 1] == "-m", f"预期 python -m,实际: {tokens[index:index + 3]}"
    module = tokens[index + 2]
    assert module in _PARSERS, f"未知模块 {module};新增入口要同步登记 _PARSERS"
    return module, tokens[index + 3:]


def assert_argv_parses(log: Path) -> argparse.Namespace:
    """脚本发出的 argv 必须被对应模块的真 argparse 完整接受,不留 unrecognized。"""
    module, argv = _emitted_argv(log)
    parser = _PARSERS[module]()
    namespace, extras = parser.parse_known_args(argv)
    assert not extras, (
        f"{module} 的 argparse 不认识 shell 发出的参数: {extras}(完整 argv: {argv})"
    )
    return namespace


class TestStaticHygiene:
    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_parses_under_bash(self, script: Path) -> None:
        completed = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False,
        )
        assert completed.returncode == 0, completed.stderr

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_help_prints_only_the_comment_header(self, shell, script: Path) -> None:
        """帮助不得泄漏源码行。写死行号的旧实现会把 set -euo pipefail / IMAGE= printf 出来。"""
        completed = shell["run"](script, "--help")
        assert completed.returncode == 0, completed.stderr
        assert "set -euo pipefail" not in completed.stdout
        assert "IMAGE=" not in completed.stdout
        assert not _calls(shell["log"]), "--help 不该启动容器"

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_help_is_not_truncated(self, shell, script: Path) -> None:
        """帮助必须完整覆盖注释块,不能像旧版那样把最后一行说明截掉。"""
        source_header = []
        for line in script.read_text(encoding="utf-8").splitlines()[1:]:
            if not line.startswith("#"):
                break
            source_header.append(line[1:].lstrip())
        last = next(line for line in reversed(source_header) if line)
        assert last in shell["run"](script, "--help").stdout, \
            f"帮助最后一行被截断: {last!r}"

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_unknown_flag_exits_two(self, shell, script: Path) -> None:
        """参数错与 python 侧 argparse 对齐用 2,不能各说各话。"""
        assert shell["run"](script, "--no-such-flag").returncode == 2


class TestBackupScript:
    def test_default_backup_targets_live_db_and_jobs(self, shell) -> None:
        completed = shell["run"](BACKUP, "--repo", str(shell["repo"]))
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert "python -m shared.content_backup backup" in call
        assert "--db /data/db/analyzer.db" in call
        assert "--jobs-dir /data/jobs" in call
        assert "--ref latest" in call
        assert f"{shell['repo']}:/content-repo" in call

    def test_result_cannot_replace_source_database(self, shell) -> None:
        database = shell["data"] / "db" / "analyzer.db"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"sqlite-sentinel")
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]),
            "--result-file", str(database),
        )
        assert completed.returncode == 2
        assert database.read_bytes() == b"sqlite-sentinel"
        assert not [call for call in _calls(shell["log"]) if call.startswith("run ")]

    def test_verify_subcommand_skips_the_data_mount_and_backup_args(self, shell) -> None:
        completed = shell["run"](BACKUP, "--repo", str(shell["repo"]), "--verify")
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert "content_backup verify" in call
        assert "--jobs-dir" not in call

    def test_partial_snapshot_refuses_to_overwrite_latest(self, shell, tmp_path) -> None:
        result_file = tmp_path / "out.json"
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--job", "jobs_x",
            "--result-file", str(result_file),
        )
        assert completed.returncode == 2
        # 早退也要留机器可读结果:自动化按 result JSON 判生死。
        assert json.loads(result_file.read_text(encoding="utf-8"))["ok"] is False

    def test_named_ref_allows_partial_snapshot(self, shell) -> None:
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--job", "jobs_x", "--ref", "probe",
        )
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert "--ref probe" in call and "--job jobs_x" in call

    def test_full_rehash_and_run_id_reach_the_container(self, shell) -> None:
        shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--full-rehash", "--run-id", "run-abc",
        )
        call = _last_run(shell["log"])
        assert "--full-rehash" in call and "--run-id run-abc" in call

    def test_vendor_media_mounts_explicit_source_root_read_only(self, shell, tmp_path) -> None:
        source = tmp_path / "nas-media"
        source.mkdir()
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--vendor-media",
            "--source-root", f"media-a={source}",
        )
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert f"{source}:/source-roots/media-a:ro" in call
        assert 'FLORI_SOURCE_ROOTS_JSON={"media-a":"/source-roots/media-a"}' in call
        assert "--vendor-media" in call
        args = assert_argv_parses(shell["log"])
        assert args.vendor_media is True

    def test_invalid_run_id_is_rejected_before_starting_a_container(self, shell) -> None:
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--run-id", "bad id!",
        )
        assert completed.returncode == 2
        assert not [call for call in _calls(shell["log"]) if call.startswith("run ")]

    def test_work_dir_is_mounted_and_passed(self, shell, tmp_path) -> None:
        work = tmp_path / "work"
        shell["run"](BACKUP, "--repo", str(shell["repo"]), "--work-dir", str(work))
        call = _last_run(shell["log"])
        assert f"{work}:/work" in call and "--work-dir /work" in call

    def test_explicit_user_config_root_is_mounted_read_only(self, shell, tmp_path) -> None:
        prompts = tmp_path / "runtime-prompts"
        prompts.mkdir()
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]),
            "--user-config-dir", str(prompts),
        )
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert f"{prompts}:/user-config:ro" in call
        assert "--user-config-dir /user-config" in call
        assert f"--user-config-source-id {prompts}" in call
        args = assert_argv_parses(shell["log"])
        assert args.user_config_dir == "/user-config"

    def test_allow_unknown_file_is_mounted_read_only(self, shell, tmp_path) -> None:
        allowlist = tmp_path / "allow.txt"
        allowlist.write_text("jobs_x:input/weird.bin\n", encoding="utf-8")
        shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--allow-unknown-file", str(allowlist),
        )
        call = _last_run(shell["log"])
        assert f"{tmp_path}:/allowlist:ro" in call
        assert "--allow-unknown-file /allowlist/allow.txt" in call

    def test_secret_blob_allowlist_is_mounted_alongside_unknown_allowlist(
        self, shell, tmp_path,
    ) -> None:
        """两张清单语义不同,必须能同时给,不能互相覆盖。"""
        unknown = tmp_path / "allow.txt"
        unknown.write_text("jobs_x:input/weird.bin\n", encoding="utf-8")
        secrets = tmp_path / "secrets.txt"
        secrets.write_text("jobs_x:input/metadata.json\n", encoding="utf-8")
        shell["run"](
            BACKUP, "--repo", str(shell["repo"]),
            "--allow-unknown-file", str(unknown),
            "--allow-secret-blob-file", str(secrets),
        )
        call = _last_run(shell["log"])
        assert "--allow-unknown-file /allowlist/allow.txt" in call
        assert "--allow-secret-blob-file /secret-allowlist/secrets.txt" in call

    def test_minio_env_is_forwarded_and_joins_the_network(self, shell) -> None:
        shell["run"](
            BACKUP, "--repo", str(shell["repo"]),
            env_overrides={"MINIO_URL": "minio:9000", "MINIO_BUCKET": "flori"},
        )
        call = _last_run(shell["log"])
        assert "-e MINIO_URL=minio:9000" in call
        assert "--network flori_default" in call


class TestGcScript:
    def test_missing_subcommand_exits_two(self, shell, tmp_path) -> None:
        result_file = tmp_path / "gc.json"
        completed = shell["run"](
            GC, "--repo", str(shell["repo"]), "--result-file", str(result_file),
        )
        assert completed.returncode == 2
        assert json.loads(result_file.read_text(encoding="utf-8"))["ok"] is False

    def test_mark_mounts_the_repository_read_only(self, shell) -> None:
        shell["run"](GC, "--repo", str(shell["repo"]), "--mark")
        call = _last_run(shell["log"])
        assert f"{shell['repo']}:/content-repo:ro" in call
        assert "--mark" in call

    def test_sweep_dry_run_stays_read_only(self, shell) -> None:
        """默认预演不写:只读挂载的仓库上取写锁会直接 OSError。"""
        shell["run"](GC, "--repo", str(shell["repo"]), "--sweep")
        call = _last_run(shell["log"])
        assert ":/content-repo:ro" in call
        assert "--apply" not in call

    def test_sweep_apply_mounts_writable_and_passes_apply(self, shell) -> None:
        """真删路径此前从未被 main() 进入过:唯一的 --apply 用例断言的是锁冲突。"""
        completed = shell["run"](GC, "--repo", str(shell["repo"]), "--sweep", "--apply")
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert f"{shell['repo']}:/content-repo " in call + " "
        assert ":/content-repo:ro" not in call
        assert "--sweep" in call and "--apply" in call

    def test_sweep_apply_forwards_allow_no_anchor(self, shell) -> None:
        shell["run"](
            GC, "--repo", str(shell["repo"]), "--sweep", "--apply", "--allow-no-anchor",
        )
        assert "--allow-no-anchor" in _last_run(shell["log"])

    def test_break_lock_needs_a_writable_mount(self, shell) -> None:
        shell["run"](GC, "--repo", str(shell["repo"]), "--break-lock")
        call = _last_run(shell["log"])
        assert ":/content-repo:ro" not in call
        assert "--break-lock" in call

    def test_scrub_and_keep_receipts_reach_the_container(self, shell) -> None:
        shell["run"](
            GC, "--repo", str(shell["repo"]), "--scrub", "--keep-receipts", "5",
        )
        call = _last_run(shell["log"])
        assert "--scrub" in call and "--keep-receipts 5" in call

    def test_grace_days_must_be_a_non_negative_integer(self, shell) -> None:
        completed = shell["run"](
            GC, "--repo", str(shell["repo"]), "--sweep", "--grace-days", "-1",
        )
        assert completed.returncode == 2


class TestImportScript:
    def _base(self, shell) -> list[str]:
        return ["--repo", str(shell["repo"]), "--db", "/data/import/new.db"]

    def test_default_import_writes_isolated_staging(self, shell) -> None:
        completed = shell["run"](IMPORT, *self._base(shell))
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert "--jobs-dir /data/import-staging/jobs" in call
        assert "--config-root /data/import-staging/prompts" in call
        assert "--into-live" not in call
        assert f"{shell['repo']}:/content-repo:ro" in call

    def test_config_root_and_source_root_reach_explicit_write_namespaces(
        self, shell, tmp_path,
    ) -> None:
        source = tmp_path / "restored-media"
        source.mkdir()
        completed = shell["run"](
            IMPORT, *self._base(shell),
            "--config-root", "/data/import-staging/custom-prompts",
            "--source-root", f"nas-main={source}",
        )
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert "--config-root /data/import-staging/custom-prompts" in call
        assert f"{source}:/source-targets/nas-main-" in call
        assert ":rw" in call
        assert "--source-root nas-main=/source-targets/nas-main-" in call
        args = assert_argv_parses(shell["log"])
        assert args.config_root == "/data/import-staging/custom-prompts"
        assert len(args.source_root) == 1
        assert args.source_root[0].startswith("nas-main=/source-targets/nas-main-")
        assert len(args.source_root_identity) == 1
        assert args.source_root_identity[0].startswith("nas-main=")

    def test_live_config_root_without_into_live_is_refused(self, shell) -> None:
        completed = shell["run"](
            IMPORT, *self._base(shell), "--config-root", "/data/prompts",
        )
        assert completed.returncode == 2
        assert "--into-live" in completed.stderr

    def test_source_root_symlink_is_refused_before_container_start(
        self, shell, tmp_path,
    ) -> None:
        real = tmp_path / "real-media"
        real.mkdir()
        alias = tmp_path / "alias-media"
        alias.symlink_to(real, target_is_directory=True)
        completed = shell["run"](
            IMPORT, *self._base(shell), "--source-root", f"nas-main={alias}",
        )
        assert completed.returncode == 2
        assert "符号链接" in completed.stderr
        assert not [call for call in _calls(shell["log"]) if call.startswith("run ")]

    def test_source_root_overlapping_repository_is_refused_before_container_start(
        self, shell,
    ) -> None:
        source = shell["repo"] / "media"
        source.mkdir()
        completed = shell["run"](
            IMPORT, *self._base(shell), "--source-root", f"nas-main={source}",
        )
        assert completed.returncode == 2
        assert "不得与便携仓库重叠" in completed.stderr
        assert not [call for call in _calls(shell["log"]) if call.startswith("run ")]

    def test_result_cannot_replace_source_root_blob(self, shell, tmp_path) -> None:
        source = tmp_path / "source-media"
        source.mkdir()
        media = source / "course.mp4"
        media.write_bytes(b"media-sentinel")
        completed = shell["run"](
            IMPORT, *self._base(shell),
            "--source-root", f"nas-main={source}",
            "--result-file", str(media),
        )
        assert completed.returncode == 2
        assert media.read_bytes() == b"media-sentinel"
        assert not [call for call in _calls(shell["log"]) if call.startswith("run ")]

    def test_source_root_replaced_during_container_run_revokes_success(
        self, shell, tmp_path,
    ) -> None:
        source = tmp_path / "restored-media"
        source.mkdir()
        moved = tmp_path / "restored-media-old"
        result_file = tmp_path / "import-result.json"
        completed = shell["run"](
            IMPORT, *self._base(shell),
            "--source-root", f"nas-main={source}",
            "--result-file", str(result_file),
            env_overrides={
                "FAKE_MOVE_SOURCE_ROOT": str(source),
                "FAKE_MOVED_SOURCE_ROOT": str(moved),
                "FAKE_SOURCE_RESULT_FILE": str(result_file),
            },
        )
        assert completed.returncode == 1
        assert source.is_dir() and moved.is_dir()
        result = json.loads(result_file.read_text(encoding="utf-8"))
        assert result["ok"] is False
        assert "宿主路径实体" in result["error"]

    def test_plan_and_verify_only_are_forwarded(self, shell) -> None:
        shell["run"](IMPORT, *self._base(shell), "--plan")
        assert "--plan" in _last_run(shell["log"])
        shell["run"](IMPORT, *self._base(shell), "--verify-only")
        assert "--verify-only" in _last_run(shell["log"])

    def test_non_latest_snapshot_and_generation_are_forwarded(self, shell) -> None:
        shell["run"](
            IMPORT, *self._base(shell), "--snapshot", "sha256:" + "a" * 64,
            "--target-generation", "gen-x1", "--target", "merge", "--apply-user-state",
        )
        call = _last_run(shell["log"])
        assert "--snapshot sha256:" + "a" * 64 in call
        assert "--target-generation gen-x1" in call
        assert "--target merge" in call and "--apply-user-state" in call

    def test_journal_inside_target_database_directory_exits_two(
        self, shell, tmp_path,
    ) -> None:
        """与 python 侧同因同码:旧 shell 在这里 exit 1 而 python 返回 2。"""
        result_file = tmp_path / "import.json"
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/import/new.db",
            "--journal", "/data/import/journal.sqlite3",
            "--result-file", str(result_file),
        )
        assert completed.returncode == 2
        assert json.loads(result_file.read_text(encoding="utf-8"))["ok"] is False

    def test_journal_outside_data_volume_exits_two(self, shell) -> None:
        completed = shell["run"](
            IMPORT, *self._base(shell), "--journal", "/tmp/journal.sqlite3",
        )
        assert completed.returncode == 2

    def test_live_database_without_into_live_is_refused(self, shell, tmp_path) -> None:
        """P0-2:显式 --db 指到线上库时,把关不能因为没传 --into-live 就跳过。"""
        result_file = tmp_path / "import.json"
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db",
            "--result-file", str(result_file),
        )
        assert completed.returncode == 2
        assert "--into-live" in completed.stderr
        assert not [call for call in _calls(shell["log"]) if call.startswith("run ")]
        assert json.loads(result_file.read_text(encoding="utf-8"))["ok"] is False

    def test_live_jobs_dir_without_into_live_is_refused(self, shell) -> None:
        """旧实现只让 --into-live 挑默认 jobs-dir,显式传同一路径就绕过了全部门。"""
        completed = shell["run"](
            IMPORT, *self._base(shell), "--jobs-dir", "/data/jobs",
        )
        assert completed.returncode == 2
        assert "--into-live" in completed.stderr

    def test_object_store_without_isolated_bucket_is_refused(self, shell) -> None:
        completed = shell["run"](
            IMPORT, *self._base(shell),
            env_overrides={"MINIO_URL": "minio:9000", "MINIO_BUCKET": "flori"},
        )
        assert completed.returncode == 2
        assert "--object-bucket" in completed.stderr

    def test_object_store_with_isolated_bucket_is_allowed(self, shell) -> None:
        completed = shell["run"](
            IMPORT, *self._base(shell), "--object-bucket", "flori-staging",
            env_overrides={"MINIO_URL": "minio:9000", "MINIO_BUCKET": "flori"},
        )
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert "--object-bucket flori-staging" in call
        assert "--network flori_default" in call

    def test_plan_against_live_database_is_not_blocked(self, shell) -> None:
        """恢复流程第 1 步就是对着线上库出计划;只读路径不过写入门。"""
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db", "--plan",
        )
        assert completed.returncode == 0, completed.stderr
        assert "--plan" in _last_run(shell["log"])

    def test_into_live_does_not_guess_service_state_from_container_names(
        self, shell, tmp_path,
    ) -> None:
        receipt = tmp_path / "dr.json"
        receipt.write_text("{}", encoding="utf-8")
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db",
            "--into-live",
            env_overrides={
                "FAKE_RUNNING_CONTAINERS": "flori-scheduler\n",
                "FLORI_DR_RECEIPT": str(receipt),
                "FLORI_REMOTE_WORKERS_QUIESCED": "1",
            },
        )
        assert completed.returncode == 0, completed.stderr
        assert not [call for call in _calls(shell["log"]) if call.startswith("ps ")]
        assert "python -m shared.content_import" in _last_run(shell["log"])

    def test_into_live_service_gate_is_the_python_maintenance_lock(
        self, shell, tmp_path,
    ) -> None:
        receipt = tmp_path / "dr.json"
        receipt.write_text("{}", encoding="utf-8")
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db",
            "--into-live", env_overrides={
                "FAKE_PS_RC": "1",
                "FLORI_REMOTE_WORKERS_QUIESCED": "1",
                "FLORI_DR_RECEIPT": str(receipt),
            },
        )
        assert completed.returncode == 0, completed.stderr
        assert not [call for call in _calls(shell["log"]) if call.startswith("ps ")]

    def test_into_live_requires_remote_worker_attestation(self, shell) -> None:
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db",
            "--into-live",
        )
        assert completed.returncode == 1
        assert "FLORI_REMOTE_WORKERS_QUIESCED" in completed.stderr

    def test_into_live_requires_a_dr_receipt(self, shell) -> None:
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db",
            "--into-live",
            env_overrides={"FLORI_REMOTE_WORKERS_QUIESCED": "1"},
        )
        assert completed.returncode == 1
        assert "FLORI_DR_RECEIPT" in completed.stderr

    def test_into_live_mounts_the_receipt_for_container_side_parsing(
        self, shell, tmp_path,
    ) -> None:
        """新鲜度判定必须在容器内解析 receipt 内容,shell 只负责把文件递进去。"""
        receipt = tmp_path / "dr.json"
        receipt.write_text("{}", encoding="utf-8")
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db",
            "--into-live",
            env_overrides={
                "FLORI_REMOTE_WORKERS_QUIESCED": "1",
                "FLORI_DR_RECEIPT": str(receipt),
            },
        )
        assert completed.returncode == 0, completed.stderr
        call = _last_run(shell["log"])
        assert f"{tmp_path}:/dr-receipt:ro" in call
        assert f"{REPO / 'scripts/dr_snapshot.py'}:/tool/dr_snapshot.py:ro" in call
        assert f"{REPO / 'shared/migrations'}:/tool/migrations:ro" in call
        assert "FLORI_DR_VALIDATOR=/tool/dr_snapshot.py" in call
        assert "--dr-receipt /dr-receipt/dr.json" in call
        assert "--into-live" in call
        assert "--jobs-dir /data/jobs" in call


class TestShellArgvMatchesArgparse:
    """出货入口的机械对账:shell 发出什么,容器内 argparse 就得认什么。

    原来两侧各写一遍字面量,协同改一处就能同时骗过两套测试;shell 发一个 argparse
    根本没定义的 flag 会一路绿灯,直到生产上容器以 exit 2 收场。这里不再比字符串,
    直接把发出的 argv 喂进真解析器。
    """

    def test_backup_default_argv_parses(self, shell) -> None:
        completed = shell["run"](BACKUP, "--repo", str(shell["repo"]))
        assert completed.returncode == 0, completed.stderr
        args = assert_argv_parses(shell["log"])
        assert args.command == "backup"

    def test_backup_verify_argv_parses(self, shell, tmp_path) -> None:
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--verify",
            "--result-file", str(tmp_path / "r.json"),
        )
        assert completed.returncode == 0, completed.stderr
        assert assert_argv_parses(shell["log"]).command == "verify"

    def test_backup_every_optional_flag_parses(self, shell, tmp_path) -> None:
        """把脚本能发的可选 flag 一次全打开,漏掉任何一个定义都会在这里炸。"""
        allow = tmp_path / "allow.txt"
        allow.write_text("job_x:notes/a.md\n", encoding="utf-8")
        secrets = tmp_path / "secrets.txt"
        secrets.write_text("job_x:input/metadata.json\n", encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]),
            "--ref", "probe", "--job", "job_x", "--job", "job_y",
            "--run-id", "run-abc", "--full-rehash", "--allow-unknown",
            "--allow-unknown-file", str(allow),
            "--allow-secret-blob-file", str(secrets),
            "--work-dir", str(work),
            "--result-file", str(tmp_path / "r.json"),
            "--db", "/data/db/other.db", "--jobs-dir", "/data/jobs",
        )
        assert completed.returncode == 0, completed.stderr
        args = assert_argv_parses(shell["log"])
        assert args.ref == "probe" and args.jobs == ["job_x", "job_y"]
        assert args.full_rehash and args.allow_unknown

    def test_import_default_argv_parses(self, shell) -> None:
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/import/new.db",
        )
        assert completed.returncode == 0, completed.stderr
        assert assert_argv_parses(shell["log"]).target == "empty"

    def test_import_every_optional_flag_parses(self, shell, tmp_path) -> None:
        receipt = tmp_path / "dr.json"
        receipt.write_text("{}", encoding="utf-8")
        source = tmp_path / "source-target"
        source.mkdir()
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/db/analyzer.db",
            "--into-live", "--target", "merge", "--target-generation", "gen-x1",
            "--snapshot", "sha256:" + "a" * 64, "--apply-user-state",
            "--skip-index-rebuild", "--allow-partial",
            "--config-root", "/data/prompts",
            "--source-root", f"nas-main={source}",
            "--allow-incomplete-portable-snapshot",
            "--result-file", str(tmp_path / "r.json"),
            env_overrides={
                "FLORI_REMOTE_WORKERS_QUIESCED": "1",
                "FLORI_DR_RECEIPT": str(receipt),
                "FLORI_ACCEPT_INCOMPLETE_PORTABLE": "1",
            },
        )
        assert completed.returncode == 0, completed.stderr
        args = assert_argv_parses(shell["log"])
        assert args.into_live and args.target == "merge"
        assert args.apply_user_state and args.skip_index_rebuild and args.allow_partial
        assert args.config_root == "/data/prompts"
        assert len(args.source_root) == 1
        assert args.source_root[0].startswith("nas-main=/source-targets/nas-main-")
        assert args.allow_incomplete_portable_snapshot

    def test_import_plan_argv_parses(self, shell) -> None:
        completed = shell["run"](
            IMPORT, "--repo", str(shell["repo"]), "--db", "/data/import/new.db", "--plan",
        )
        assert completed.returncode == 0, completed.stderr
        assert assert_argv_parses(shell["log"]).plan

    @pytest.mark.parametrize("mode", ["--mark", "--sweep", "--scrub", "--break-lock"])
    def test_gc_每个子命令的_argv_parses(self, shell, mode: str) -> None:
        completed = shell["run"](GC, "--repo", str(shell["repo"]), mode)
        assert completed.returncode == 0, completed.stderr
        assert getattr(assert_argv_parses(shell["log"]), mode.lstrip("-").replace("-", "_"))

    def test_gc_every_optional_flag_parses(self, shell, tmp_path) -> None:
        completed = shell["run"](
            GC, "--repo", str(shell["repo"]), "--sweep", "--apply",
            "--allow-no-anchor", "--keep-receipts", "5", "--grace-days", "3",
            "--result-file", str(tmp_path / "r.json"),
        )
        assert completed.returncode == 0, completed.stderr
        args = assert_argv_parses(shell["log"])
        assert args.apply and args.allow_no_anchor
        assert args.keep_receipts == 5 and args.grace_days == 3


class TestUsageEarlyExitWritesResult:
    """usage 早退同样受 fail() 上方那条不变量约束:自动化按 result JSON 判生死。

    原来 28 处 `usage 2` 绕过 fail(),调用方读到的是上一次的陈旧 JSON 或什么都没有,
    而注释就写在它们违反的那条不变量上方。
    """

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_unknown_flag_writes_failure_result(self, shell, tmp_path, script) -> None:
        result = tmp_path / "r.json"
        completed = shell["run"](
            script, "--repo", str(shell["repo"]),
            "--result-file", str(result), "--no-such-flag",
        )
        assert completed.returncode == 2
        assert json.loads(result.read_text(encoding="utf-8"))["ok"] is False

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_missing_flag_value_writes_failure_result(self, shell, tmp_path, script) -> None:
        """--result-file 排在出错的 flag 之前也必须生效(预扫描,不靠解析顺序)。"""
        result = tmp_path / "r.json"
        completed = shell["run"](
            script, "--result-file", str(result), "--repo",
        )
        assert completed.returncode == 2
        assert json.loads(result.read_text(encoding="utf-8"))["ok"] is False

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_result_file_after_the_bad_flag_still_written(self, shell, tmp_path, script) -> None:
        """预扫描的意义:出错点在 --result-file 之前时,旧实现根本没机会写。"""
        result = tmp_path / "r.json"
        completed = shell["run"](
            script, "--no-such-flag", "--result-file", str(result),
        )
        assert completed.returncode == 2
        assert json.loads(result.read_text(encoding="utf-8"))["ok"] is False

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_help_does_not_write_a_failure_result(self, shell, tmp_path, script) -> None:
        """--help 是成功路径,不该留下 ok=false 的残骸。"""
        result = tmp_path / "r.json"
        completed = shell["run"](script, "--result-file", str(result), "--help")
        assert completed.returncode == 0
        assert not result.exists()

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_result_parent_must_be_precreated(self, shell, tmp_path, script) -> None:
        result = tmp_path / "missing-parent" / "r.json"
        if script == BACKUP:
            args = ["--repo", str(shell["repo"]), "--result-file", str(result)]
        elif script == IMPORT:
            args = [
                "--repo", str(shell["repo"]), "--db", "/data/import/new.db",
                "--result-file", str(result),
            ]
        else:
            args = [
                "--repo", str(shell["repo"]), "--mark", "--result-file", str(result),
            ]
        completed = shell["run"](script, *args)
        assert completed.returncode != 0
        assert "预先创建" in completed.stderr
        assert not result.exists()
        assert not [call for call in _calls(shell["log"]) if call.startswith("run ")]

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_result_file_inside_repository_is_rejected_on_host(
        self, shell, script,
    ) -> None:
        result = shell["repo"] / "result.json"
        completed = shell["run"](
            script, "--repo", str(shell["repo"]),
            "--result-file", str(result), "--no-such-flag",
        )
        assert completed.returncode == 2
        assert not result.exists()
        assert "仓库之外" in completed.stderr
        assert not _calls(shell["log"])

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_result_directory_symlink_alias_to_repository_is_rejected(
        self, shell, tmp_path, script,
    ) -> None:
        alias = tmp_path / "repo-alias"
        alias.symlink_to(shell["repo"], target_is_directory=True)
        completed = shell["run"](
            script, "--repo", str(shell["repo"]),
            "--result-file", str(alias / "result.json"), "--no-such-flag",
        )
        assert completed.returncode == 2
        assert not (shell["repo"] / "result.json").exists()
        assert not _calls(shell["log"])

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_result_directory_moved_into_repository_during_container_is_cleaned(
        self, shell, tmp_path, script,
    ) -> None:
        result_parent = tmp_path / f"{script.stem}-race"
        result_parent.mkdir()
        moved = shell["repo"] / f"stolen-{script.stem}"
        result = result_parent / "result.json"
        environment = {
            "FAKE_MOVE_RESULT_PARENT": str(result_parent),
            "FAKE_MOVED_RESULT_PARENT": str(moved),
            "FAKE_RESULT_LEAF": result.name,
        }
        if script == BACKUP:
            args = ["--repo", str(shell["repo"]), "--result-file", str(result)]
        elif script == IMPORT:
            args = [
                "--repo", str(shell["repo"]), "--db", "/data/import/new.db",
                "--result-file", str(result),
            ]
        else:
            args = [
                "--repo", str(shell["repo"]), "--mark", "--result-file", str(result),
            ]
        completed = shell["run"](script, *args, env_overrides=environment)
        assert completed.returncode == 74
        assert "已撤销结果" in completed.stderr
        assert not (moved / result.name).exists()
        assert not result.exists()

    def test_backup_result_directory_moved_into_data_during_container_is_cleaned(
        self, shell, tmp_path,
    ) -> None:
        result_parent = tmp_path / "backup-data-race"
        result_parent.mkdir()
        moved = shell["data"] / "stolen-results"
        result = result_parent / "result.json"
        completed = shell["run"](
            BACKUP, "--repo", str(shell["repo"]), "--result-file", str(result),
            env_overrides={
                "FAKE_MOVE_RESULT_PARENT": str(result_parent),
                "FAKE_MOVED_RESULT_PARENT": str(moved),
                "FAKE_RESULT_LEAF": result.name,
            },
        )
        assert completed.returncode == 74
        assert "受保护数据树" in completed.stderr
        assert not (moved / result.name).exists()

    def test_import_result_directory_moved_into_source_during_container_is_cleaned(
        self, shell, tmp_path,
    ) -> None:
        source = tmp_path / "source-target"
        source.mkdir()
        result_parent = tmp_path / "import-source-race"
        result_parent.mkdir()
        moved = source / "stolen-results"
        result = result_parent / "result.json"
        completed = shell["run"](
            IMPORT,
            "--repo", str(shell["repo"]),
            "--db", "/data/import/new.db",
            "--source-root", f"nas-main={source}",
            "--result-file", str(result),
            env_overrides={
                "FAKE_MOVE_RESULT_PARENT": str(result_parent),
                "FAKE_MOVED_RESULT_PARENT": str(moved),
                "FAKE_RESULT_LEAF": result.name,
            },
        )
        assert completed.returncode == 74
        assert "受保护数据树" in completed.stderr
        assert not (moved / result.name).exists()

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
    def test_multiline_special_characters_are_valid_json(
        self, shell, tmp_path, script,
    ) -> None:
        result = tmp_path / f"{script.stem}.json"
        missing = tmp_path / 'missing\n"quoted"\\path\tend'
        if script == BACKUP:
            args = [
                "--repo", str(shell["repo"]), "--data-dir", str(missing),
                "--result-file", str(result),
            ]
        elif script == IMPORT:
            args = [
                "--repo", str(missing), "--db", "/data/import/new.db",
                "--result-file", str(result),
            ]
        else:
            args = [
                "--repo", str(missing), "--mark", "--result-file", str(result),
            ]
        completed = shell["run"](script, *args)
        assert completed.returncode != 0
        payload = json.loads(result.read_text(encoding="utf-8"))
        assert payload["ok"] is False
        assert "missing\n" in payload["error"]
