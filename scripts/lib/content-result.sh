#!/usr/bin/env bash
# 便携内容工具的宿主侧 result JSON 编码、边界校验与原子发布。

declare -a CONTENT_RESULT_PROTECTED_ROOTS=()

content_json_escape() {
  local value="$1" char ordinal index
  for ((index = 0; index < ${#value}; index++)); do
    char="${value:index:1}"
    case "$char" in
      '"') printf '\\"' ;;
      $'\\') printf '\\\\' ;;
      $'\b') printf '\\b' ;;
      $'\f') printf '\\f' ;;
      $'\n') printf '\\n' ;;
      $'\r') printf '\\r' ;;
      $'\t') printf '\\t' ;;
      [[:cntrl:]])
        printf -v ordinal '%d' "'$char"
        printf '\\u%04x' "$ordinal"
        ;;
      *) printf '%s' "$char" ;;
    esac
  done
}

content_write_failure_result() {
  local mode="$1" message="$2" directory leaf temporary escaped escaped_mode
  local directory_fd opened_identity current_identity repository temporary_identity
  [ -n "${RESULT_FILE:-}" ] || return 0
  directory="$(dirname "$RESULT_FILE")"
  leaf="$(basename "$RESULT_FILE")"
  repository="${PRESCAN_REPO:-}"
  if [ "$leaf" = "." ] || [ "$leaf" = ".." ] || [ ! -d "$directory" ] \
      || content_path_has_symlink "$directory"; then
    echo "警告: result-file父目录必须预先存在且不能含符号链接: $directory" >&2
    return 0
  fi
  # 早退发生在容器启动前，只能依赖宿主bash/coreutils。打开父目录FD后所有写入都经
  # /proc/self/fd 固定到该实体；路径随后被换成symlink也不会把JSON重定向进仓库。
  if ! exec {directory_fd}<"$directory"; then
    echo "警告: 无法打开result-file父目录: $directory" >&2
    return 0
  fi
  opened_identity="$(stat -Lc '%d:%i' -- "/proc/self/fd/$directory_fd")" || {
    exec {directory_fd}<&-
    return 0
  }
  current_identity="$(stat -Lc '%d:%i' -- "$directory")" || true
  if [ "$opened_identity" != "$current_identity" ] \
      || content_path_is_in_repository_tree "$repository" "/proc/self/fd/$directory_fd" \
      || content_fd_overlaps_protected_roots "$directory_fd" 1 \
          "${CONTENT_RESULT_PROTECTED_ROOTS[@]}"; then
    echo "警告: result-file父目录实体已变化或与仓库重叠: $directory" >&2
    exec {directory_fd}<&-
    return 0
  fi
  temporary="$(mktemp "/proc/self/fd/$directory_fd/.flori-result.XXXXXX")" || {
    exec {directory_fd}<&-
    return 0
  }
  escaped="$(content_json_escape "$message")"
  escaped_mode="$(content_json_escape "$mode")"
  if [ -n "$mode" ]; then
    printf '{\n  "error": "%s",\n  "mode": "%s",\n  "ok": false\n}\n' \
      "$escaped" "$escaped_mode" > "$temporary"
  else
    printf '{\n  "error": "%s",\n  "ok": false\n}\n' "$escaped" > "$temporary"
  fi
  chmod 600 "$temporary"
  sync -f "$temporary" 2>/dev/null || sync
  temporary_identity="$(stat -Lc '%d:%i' -- "$temporary")" || {
    rm -f -- "$temporary"
    exec {directory_fd}<&-
    return 0
  }
  current_identity="$(stat -Lc '%d:%i' -- "$directory")" || true
  if [ "$opened_identity" != "$current_identity" ] \
      || content_path_is_in_repository_tree "$repository" "/proc/self/fd/$directory_fd" \
      || content_fd_overlaps_protected_roots "$directory_fd" 0 \
          "${CONTENT_RESULT_PROTECTED_ROOTS[@]}"; then
    echo "警告: result-file父目录在发布前已变化或移入仓库: $directory" >&2
    rm -f -- "$temporary"
    exec {directory_fd}<&-
    return 0
  fi
  if ! mv -fT -- "$temporary" "/proc/self/fd/$directory_fd/$leaf"; then
    rm -f -- "$temporary"
    exec {directory_fd}<&-
    return 0
  fi
  sync -f "/proc/self/fd/$directory_fd/$leaf" 2>/dev/null || sync
  sync -f "/proc/self/fd/$directory_fd" 2>/dev/null || sync
  current_identity="$(stat -Lc '%d:%i' -- "$directory")" || true
  if [ "$opened_identity" != "$current_identity" ] \
      || content_path_is_in_repository_tree "$repository" "/proc/self/fd/$directory_fd" \
      || content_fd_overlaps_protected_roots "$directory_fd" 0 \
          "${CONTENT_RESULT_PROTECTED_ROOTS[@]}"; then
    if [ "$(stat -Lc '%d:%i' -- "/proc/self/fd/$directory_fd/$leaf" 2>/dev/null || true)" \
        = "$temporary_identity" ]; then
      rm -f -- "/proc/self/fd/$directory_fd/$leaf"
      sync -f "/proc/self/fd/$directory_fd" 2>/dev/null || sync
    fi
    echo "警告: result-file发布期间父目录已变化或移入仓库,已撤销结果: $directory" >&2
  fi
  exec {directory_fd}<&-
  return 0
}

content_path_has_symlink() {
  local candidate
  candidate="$(realpath -ms -- "$1")" || return 0
  while [ "$candidate" != "/" ]; do
    [ ! -L "$candidate" ] || return 0
    candidate="$(dirname "$candidate")"
  done
  return 1
}

content_paths_overlap() {
  local protected="$1" candidate="$2" protected_real candidate_real
  [ -n "$protected" ] && [ -n "$candidate" ] || return 1
  protected_real="$(realpath -m -- "$protected")" || return 0
  candidate_real="$(realpath -m -- "$candidate")" || return 0
  case "$candidate_real" in
    "$protected_real"|"$protected_real"/*) return 0 ;;
  esac
  case "$protected_real" in
    "$candidate_real"/*) return 0 ;;
  esac
  if [ -e "$protected_real" ] && [ -e "$candidate_real" ] \
      && [ "$protected_real" -ef "$candidate_real" ]; then
    return 0
  fi
  # bind alias到仓库子目录时realpath与根inode都不同；受保护仓库目录树很小，
  # 反向按samefile查目录实体可精确识别，不扫描可能很大的媒体目标树。
  if [ -d "$protected_real" ] && [ -d "$candidate_real" ] \
      && find -P "$protected_real" -xdev -type d -samefile "$candidate_real" \
          -print -quit 2>/dev/null | grep -q .; then
    return 0
  fi
  return 1
}

content_path_is_in_repository_tree() {
  local repository="$1" candidate="$2" repository_real candidate_real
  [ -n "$repository" ] && [ -n "$candidate" ] || return 1
  repository_real="$(realpath -m -- "$repository")" || return 0
  candidate_real="$(realpath -m -- "$candidate")" || return 0
  case "$candidate_real" in
    "$repository_real"|"$repository_real"/*) return 0 ;;
  esac
  if [ -d "$repository_real" ] && [ -d "$candidate_real" ] \
      && find -P "$repository_real" -xdev -type d -samefile "$candidate_real" \
          -print -quit 2>/dev/null | grep -q .; then
    return 0
  fi
  return 1
}

content_fd_path_within_root() {
  local root="$1" fd_path="$2" root_real fd_real
  [ -n "$root" ] || return 1
  root_real="$(realpath -m -- "$root")" || return 0
  fd_real="$(realpath -m -- "$fd_path")" || return 0
  case "$fd_real" in
    "$root_real"|"$root_real"/*) return 0 ;;
  esac
  [ -e "$root_real" ] && [ "$root_real" -ef "$fd_path" ] && return 0
  return 1
}

content_fd_overlaps_protected_roots() {
  local directory_fd="$1" full_identity_check="$2" root
  shift 2
  for root in "$@"; do
    [ -n "$root" ] || continue
    if content_fd_path_within_root "$root" "/proc/self/fd/$directory_fd"; then
      return 0
    fi
    if [ "$full_identity_check" -eq 1 ] \
        && content_path_is_in_repository_tree \
            "$root" "/proc/self/fd/$directory_fd"; then
      return 0
    fi
  done
  return 1
}

content_validate_result_boundary() {
  local repository="$1" result="$2" repository_real result_real candidate
  [ -n "$repository" ] && [ -n "$result" ] || return 0
  if content_path_has_symlink "$repository" || content_path_has_symlink "$result"; then
    return 1
  fi
  repository_real="$(realpath -m -- "$repository")" || return 1
  result_real="$(realpath -m -- "$result")" || return 1
  content_path_is_in_repository_tree "$repository_real" "$result_real" && return 1
  # realpath 看不穿 bind mount。沿 result 父目录向上用 samefile(-ef) 比较,
  # 防止宿主同一目录以 /result 和 /content-repo 两个 mount 名进入容器。
  candidate="$(dirname "$result_real")"
  while [ "$candidate" != "/" ]; do
    if content_path_is_in_repository_tree "$repository_real" "$candidate"; then
      return 1
    fi
    candidate="$(dirname "$candidate")"
  done
  return 0
}

content_run_with_result_guard() {
  local repository="$1" result="$2" expected_identity="$3" root_count="$4"
  shift 4
  local -a protected_roots=()
  local index
  for ((index = 0; index < root_count; index++)); do
    protected_roots+=("$1")
    shift
  done
  if [ -z "$result" ]; then
    "$@"
    return $?
  fi

  local directory leaf directory_fd opened_identity current_identity status
  directory="$(dirname "$result")"
  leaf="$(basename "$result")"
  if ! exec {directory_fd}<"$directory"; then
    echo "错误: 无法打开result-file父目录: $directory" >&2
    return 2
  fi
  opened_identity="$(stat -Lc '%d:%i' -- "/proc/self/fd/$directory_fd")" || {
    exec {directory_fd}<&-
    return 2
  }
  current_identity="$(stat -Lc '%d:%i' -- "$directory")" || true
  if [ "$opened_identity" != "$expected_identity" ] \
      || [ "$opened_identity" != "$current_identity" ] \
      || content_path_is_in_repository_tree "$repository" "/proc/self/fd/$directory_fd" \
      || content_fd_overlaps_protected_roots "$directory_fd" 1 \
          "${protected_roots[@]}"; then
    echo "错误: result-file父目录在容器启动前已变化或移入受保护数据树: $directory" >&2
    exec {directory_fd}<&-
    return 2
  fi

  # 先移除陈旧结果。后续无论容器成功或失败,调用方都不会把上一轮 JSON 当成本轮证据。
  rm -f -- "/proc/self/fd/$directory_fd/$leaf"
  sync -f "/proc/self/fd/$directory_fd" 2>/dev/null || sync
  if "$@"; then
    status=0
  else
    status=$?
  fi

  current_identity="$(stat -Lc '%d:%i' -- "$directory")" || true
  if [ "$opened_identity" != "$current_identity" ] \
      || content_path_is_in_repository_tree "$repository" "/proc/self/fd/$directory_fd" \
      || content_fd_overlaps_protected_roots "$directory_fd" 0 \
          "${protected_roots[@]}"; then
    rm -f -- "/proc/self/fd/$directory_fd/$leaf"
    sync -f "/proc/self/fd/$directory_fd" 2>/dev/null || sync
    echo "错误: result-file父目录在容器运行期间已变化或移入受保护数据树,已撤销结果" >&2
    status=74
  fi
  exec {directory_fd}<&-
  return "$status"
}
