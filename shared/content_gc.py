"""便携仓库 GC 入口:mark / sweep / scrub(设计稿 05 号 §2.14)。

纯编排:可达性、清扫与校验的判定都在 shared.content_repository(P1 契约),
这里只负责持写锁、串起三个阶段并输出机器可读结果。

锁的边界:mark 与 scrub 只读,不取锁(已发布对象不可变,读者天然安全);
sweep 会删对象,必须与 backup 互斥,因此在写锁内执行。import 只读仓库,
任何时候都不被 GC 阻塞。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .content_policy import PolicyError
from .content_repository import ContentRepository, RepositoryError
from .content_result import (
    ResultDestination,
    ResultFileError,
    emit_result,
    prepare_result_destination,
)

# 默认保留最近多少条 receipt 引用的 snapshot(§2.14-2 保留集合的一部分)。
DEFAULT_KEEP_RECEIPTS = 20
DEFAULT_GRACE_DAYS = 7


def _emit(payload: dict, result_file: ResultDestination | None) -> None:
    emit_result(payload, result_file)


def _has_monthly_anchor(repository: ContentRepository) -> bool:
    return any(name.startswith("monthly-") for name in repository.list_refs())


def _plan_payload(plan) -> dict:
    return {
        "reachable": {
            "snapshots": len(plan.reachable_snapshots),
            "records": len(plan.reachable_records),
            "blobs": len(plan.reachable_blobs),
        },
        "unreachable": {
            "snapshots": list(plan.unreachable_snapshots),
            "records": [f"{kind}/{digest}" for kind, digest in plan.unreachable_records],
            "blobs": list(plan.unreachable_blobs),
        },
        "warnings": list(plan.warnings),
    }


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器;独立出来是为了让脚本发出的 argv 能喂进真解析器对账。"""
    parser = argparse.ArgumentParser(prog="content-gc", description=__doc__)
    parser.add_argument("--repo", required=True)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--mark", action="store_true", help="只算可达集合与待清扫清单")
    group.add_argument("--sweep", action="store_true", help="清扫不可达且过 grace 的对象")
    group.add_argument("--scrub", action="store_true", help="全量重算并核对完整性")
    parser.add_argument("--apply", action="store_true", help="sweep 真删(默认 dry-run)")
    parser.add_argument(
        "--allow-no-anchor", action="store_true",
        help="没有月度锚点也允许真删(明知会失去较早恢复点时才用)",
    )
    parser.add_argument("--keep-receipts", type=int, default=DEFAULT_KEEP_RECEIPTS)
    parser.add_argument("--grace-days", type=int, default=DEFAULT_GRACE_DAYS)
    parser.add_argument(
        "--break-lock", action="store_true",
        help="打印当前持锁者后强制破锁(仅在确认持有者已死时使用)",
    )
    parser.add_argument("--result-file", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.result_file = prepare_result_destination(args.result_file, args.repo)
    except ResultFileError as exc:
        emit_result({"ok": False, "error": str(exc)}, None)
        return 2

    if not (args.mark or args.sweep or args.scrub or args.break_lock):
        _emit({"ok": False, "error": "one of --mark/--sweep/--scrub/--break-lock is required"},
              args.result_file)
        return 2
    if args.keep_receipts < 0 or args.grace_days < 0:
        _emit({"ok": False, "error": "keep-receipts/grace-days must be >= 0"},
              args.result_file)
        return 2
    try:
        repository = ContentRepository.open(Path(args.repo))
    except (RepositoryError, PolicyError) as exc:
        _emit({"ok": False, "error": str(exc)}, args.result_file)
        return 1

    if args.break_lock:
        holder = repository.write_lock_holder()
        if holder is None:
            _emit({"ok": True, "mode": "break-lock", "held": False}, args.result_file)
            return 0
        try:
            repository.break_write_lock()
        except (RepositoryError, OSError) as exc:
            _emit({"ok": False, "mode": "break-lock", "holder": holder,
                   "error": str(exc)}, args.result_file)
            return 1
        # 先把持有者信息打出来:破锁是人工判断"那个进程真的死了"之后的动作。
        _emit({"ok": True, "mode": "break-lock", "held": True, "holder": holder},
              args.result_file)
        return 0

    try:
        if args.scrub:
            report = repository.scrub()
            _emit({
                "ok": report.ok,
                "mode": "scrub",
                "checked": {
                    "blobs": report.checked_blobs,
                    "records": report.checked_records,
                    "snapshots": report.checked_snapshots,
                    "refs": report.checked_refs,
                    "receipts": report.checked_receipts,
                },
                "issues": [
                    {"kind": item.kind, "path": item.path, "detail": item.detail}
                    for item in report.issues
                ],
            }, args.result_file)
            return 0 if report.ok else 1

        if args.mark:
            plan = repository.gc_mark(receipt_root_limit=args.keep_receipts)
            _emit({"ok": True, "mode": "mark", **_plan_payload(plan)}, args.result_file)
            return 0

        # dry-run 不删任何东西,与 mark 一样只读,因此不取写锁——取锁会在
        # 只读挂载的仓库上直接抛 OSError,让默认(预演)路径彻底不可用。
        # 真删才与 backup 互斥,且 mark 必须与清扫在同一把锁内:否则会清掉
        # 一次并发备份刚写入、尚未被 ref 指向的对象。
        # 锚点告警必须在删除之前给出:删完再说"你本来该先建锚点"没有任何用,
        # 被扫掉的恢复点已经没了。
        anchor_warning = None
        if not _has_monthly_anchor(repository):
            anchor_warning = (
                "no monthly anchor refs found; retention currently rests on 'latest' "
                "plus the receipt window, so older restore points may be swept. "
                "Run a backup with anchor creation enabled before --apply."
            )
        if args.apply and anchor_warning is not None and not args.allow_no_anchor:
            _emit({"ok": False, "mode": "sweep", "warning": anchor_warning,
                   "error": "refusing to sweep without a monthly anchor ref; "
                            "create one (or re-run with --allow-no-anchor) first"},
                  args.result_file)
            return 1
        if not args.apply:
            plan = repository.gc_mark(receipt_root_limit=args.keep_receipts)
            outcome = repository.gc_sweep(
                plan, grace_seconds=args.grace_days * 86_400, dry_run=True,
            )
        else:
            with repository.write_lock("gc-sweep"):
                plan = repository.gc_mark(receipt_root_limit=args.keep_receipts)
                outcome = repository.gc_sweep(
                    plan, grace_seconds=args.grace_days * 86_400, dry_run=False,
                )
        payload = {"ok": True, "mode": "sweep", **_plan_payload(plan), "sweep": outcome}
        if anchor_warning is not None:
            payload["warning"] = anchor_warning
        _emit(payload, args.result_file)
        return 0
    except (RepositoryError, PolicyError, OSError) as exc:
        # 任何失败都要出机器可读 JSON:只读挂载、锁冲突、磁盘错误都算。
        _emit({"ok": False, "mode": "sweep" if args.sweep else "mark",
               "error": str(exc)}, args.result_file)
        return 1


if __name__ == "__main__":
    sys.exit(main())
