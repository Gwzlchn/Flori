#!/usr/bin/env bash
# 输出当前交付单元需要的紧凑 Git 基线或收尾事实.

set -euo pipefail

MODE="${1:-start}"
TASK_BRANCH="${2:-}"
EXTRA_SPECS=()
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${REPO:-}" ]; then
  REPO="$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || (cd "$SCRIPT_DIR/../../../.." && pwd))"
fi

usage() {
  echo "用法: $0 start | $0 close <task-branch> [--extra <label=path>]..." >&2
  exit 2
}

case "$MODE" in
  start) [ "$#" -eq 1 ] || usage ;;
  close)
    [ "$#" -ge 2 ] && [ -n "$TASK_BRANCH" ] || usage
    shift 2
    while [ "$#" -gt 0 ]; do
      [ "$1" = "--extra" ] && [ "$#" -ge 2 ] || usage
      EXTRA_SPECS+=("$2")
      shift 2
    done
    ;;
  *) usage ;;
esac

git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null

find_branch_worktree() {
  local wanted="refs/heads/$1" path="" line
  while IFS= read -r line; do
    case "$line" in
      worktree\ *) path="${line#worktree }" ;;
      branch\ "$wanted") printf '%s\n' "$path"; return 0 ;;
    esac
  done < <(git -C "$REPO" worktree list --porcelain)
  return 1
}

TARGET_REPO="$REPO"
if [ "$MODE" = "close" ]; then
  git -C "$REPO" show-ref --verify --quiet "refs/heads/$TASK_BRANCH" || {
    echo "错误: 本地任务分支不存在: $TASK_BRANCH" >&2
    exit 1
  }
  TARGET_REPO="$(find_branch_worktree "$TASK_BRANCH")" || {
    echo "错误: 任务分支没有活跃 worktree: $TASK_BRANCH" >&2
    exit 1
  }
fi

HEAD_SHA="$(git -C "$TARGET_REPO" rev-parse HEAD)"
BRANCH="$(git -C "$TARGET_REPO" symbolic-ref --quiet --short HEAD || echo detached)"
STATUS="$(git -C "$TARGET_REPO" status --short)"
WORKTREE_COUNT="$(git -C "$TARGET_REPO" worktree list --porcelain | grep -c '^worktree ')"

if [ "$MODE" = "close" ] && [ "$BRANCH" != "$TASK_BRANCH" ]; then
  echo "错误: worktree 分支不匹配: expected=$TASK_BRANCH actual=$BRANCH" >&2
  exit 1
fi

extra_evidence_records() {
  local spec label path full_path mode hash
  for spec in "${EXTRA_SPECS[@]}"; do
    [[ "$spec" == *=* ]] || {
      echo "错误: --extra 必须使用 label=path: $spec" >&2
      return 1
    }
    label="${spec%%=*}"
    path="${spec#*=}"
    [[ "$label" =~ ^[A-Za-z0-9._-]+$ ]] && [ -n "$path" ] || {
      echo "错误: --extra label 只允许字母、数字、点、下划线和短横线: $label" >&2
      return 1
    }
    if [[ "$path" = /* ]]; then
      full_path="$path"
    else
      full_path="$REPO/$path"
    fi
    [ -f "$full_path" ] || {
      echo "错误: --extra 只接受现有文件: $path" >&2
      return 1
    }
    mode="$(stat -c '%a' "$full_path")"
    hash="$(sha256sum "$full_path" | awk '{print $1}')"
    printf 'extra %s %s %s\n' "$label" "$mode" "$hash"
  done
}

EXTRA_RECORDS="$(extra_evidence_records)"

candidate_digest() {
  {
    printf 'head %s\n' "$HEAD_SHA"
    git -C "$TARGET_REPO" diff --binary HEAD --
    git -C "$TARGET_REPO" ls-files --others --exclude-standard -z \
      | while IFS= read -r -d '' path; do
          printf 'untracked %s %s %s\n' \
            "$(stat -c '%a' "$TARGET_REPO/$path")" "$path" \
            "$(git -C "$TARGET_REPO" hash-object -- "$path")"
        done
    if [ -n "$EXTRA_RECORDS" ]; then
      printf '%s\n' "$EXTRA_RECORDS"
    fi
  } | sha256sum | awk '{print $1}'
}

printf 'mode=%s\n' "$MODE"
printf 'worktree=%s\n' "$TARGET_REPO"
printf 'branch=%s\n' "$BRANCH"
printf 'head=%s\n' "$HEAD_SHA"
printf 'dirty=%s\n' "$([ -n "$STATUS" ] && echo yes || echo no)"
printf 'worktrees=%s\n' "$WORKTREE_COUNT"

if [ -n "$STATUS" ]; then
  printf '%s\n' 'status:'
  printf '%s\n' "$STATUS"
fi

if [ "$MODE" = "close" ]; then
  TASK_SHA="$(git -C "$TARGET_REPO" rev-parse "$TASK_BRANCH")"
  if git -C "$TARGET_REPO" merge-base --is-ancestor "$TASK_BRANCH" main; then
    MERGED_MAIN=yes
  else
    MERGED_MAIN=no
  fi
  printf 'task_branch=%s\n' "$TASK_BRANCH"
  printf 'task_head=%s\n' "$TASK_SHA"
  printf 'branch_head_merged_main=%s\n' "$MERGED_MAIN"
  if [ -n "$STATUS" ] || [ "${#EXTRA_SPECS[@]}" -gt 0 ]; then
    if [ "${#EXTRA_SPECS[@]}" -gt 0 ]; then
      printf 'candidate_kind=composite\n'
    else
      printf 'candidate_kind=working-tree\n'
    fi
    printf 'candidate_digest=sha256:%s\n' "$(candidate_digest)"
    printf 'candidate_merged_main=no\n'
  else
    printf 'candidate_kind=branch\n'
    printf 'candidate_digest=git:%s\n' "$TASK_SHA"
    printf 'candidate_merged_main=%s\n' "$MERGED_MAIN"
  fi
  if [ -n "$EXTRA_RECORDS" ]; then
    printf 'extra_evidence_count=%s\n' "${#EXTRA_SPECS[@]}"
    while read -r _ label mode hash; do
      printf 'extra_evidence=%s:mode-%s:sha256-%s\n' "$label" "$mode" "$hash"
    done <<< "$EXTRA_RECORDS"
  fi

  if git -C "$TARGET_REPO" rev-parse --abbrev-ref "$TASK_BRANCH@{upstream}" >/dev/null 2>&1; then
    UPSTREAM="$(git -C "$TARGET_REPO" rev-parse --abbrev-ref "$TASK_BRANCH@{upstream}")"
    COUNTS="$(git -C "$TARGET_REPO" rev-list --left-right --count "$TASK_BRANCH...$UPSTREAM")"
    printf 'upstream=%s\n' "$UPSTREAM"
    printf 'ahead_behind=%s\n' "$COUNTS"
  else
    printf 'upstream=none\n'
  fi
fi
