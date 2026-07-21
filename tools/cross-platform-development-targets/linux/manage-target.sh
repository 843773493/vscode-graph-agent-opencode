#!/bin/sh
set -eu

fail() {
  printf '%s\n' "Linux 开发目标动作失败: $*" >&2
  exit 1
}

require_absolute_path() {
  case "$1" in
    /*) ;;
    *) fail "必须使用绝对路径: $1" ;;
  esac
}

require_repository() {
  require_absolute_path "$1"
  test -d "$1/.git" || fail "目标不是完整 Git 仓库: $1"
}

profile_home() {
  target_home=$1
  profile=$2
  override=$3
  if test -n "$override"; then
    require_absolute_path "$override"
    printf '%s\n' "$override"
  elif test "$profile" = development; then
    printf '%s\n' "$target_home/.boxteams-dev"
  elif test "$profile" = installed; then
    printf '%s\n' "$target_home/.boxteams"
  else
    fail "未知运行 profile: $profile"
  fi
}

profile_workspace() {
  boxteam_home=$1
  override=$2
  if test -n "$override"; then
    require_absolute_path "$override"
    printf '%s\n' "$override"
  else
    printf '%s\n' "$boxteam_home/boxteam_workspace"
  fi
}

listener_pids() {
  for port in "$@"; do
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
  done | sort -u
}

stop_ports() {
  pids=$(listener_pids "$@")
  if test -z "$pids"; then
    return
  fi
  # shellcheck disable=SC2086
  kill $pids
  attempts=0
  while test "$attempts" -lt 20; do
    remaining=""
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then remaining="$remaining $pid"; fi
    done
    test -z "$remaining" && return
    sleep 0.25
    attempts=$((attempts + 1))
  done
  # shellcheck disable=SC2086
  kill -KILL $remaining
}

wait_http() {
  url=$1
  label=$2
  pid=$3
  attempts=0
  while test "$attempts" -lt 180; do
    if curl -fsS "$url" >/dev/null 2>&1; then return; fi
    if ! kill -0 "$pid" 2>/dev/null; then
      fail "$label 启动进程已经退出: pid=$pid"
    fi
    sleep 0.5
    attempts=$((attempts + 1))
  done
  fail "$label 在 90 秒内未就绪: $url"
}

action=${1:-}
test -n "$action" || fail "缺少动作"
shift

case "$action" in
  init-repository)
    repository=$1
    artifacts=$2
    target_home=$3
    require_absolute_path "$repository"
    require_absolute_path "$artifacts"
    require_absolute_path "$target_home"
    mkdir -p "$repository" "$artifacts" "$target_home/.boxteams-dev/boxteam_workspace" "$target_home/.boxteams/boxteam_workspace"
    if test ! -d "$repository/.git"; then
      git -C "$repository" init
    fi
    git -C "$repository" config receive.denyCurrentBranch refuse
    printf '.env\n.env.uploading-*\n' > "$repository/.git/info/exclude"
    printf '%s\n' "$repository"
    ;;
  repository-status)
    repository=$1
    require_repository "$repository"
    git -C "$repository" status --porcelain --untracked-files=all
    ;;
  activate)
    repository=$1
    snapshot_ref=$2
    require_repository "$repository"
    dirty=$(git -C "$repository" status --porcelain --untracked-files=all)
    test -z "$dirty" || fail "目标工作区包含本地修改，拒绝激活:\n$dirty"
    git -C "$repository" show-ref --verify --quiet "$snapshot_ref" || fail "快照引用不存在: $snapshot_ref"
    git -C "$repository" checkout -B boxteam-host-snapshot "$snapshot_ref"
    git -C "$repository" rev-parse HEAD
    ;;
  latest-snapshot)
    repository=$1
    require_repository "$repository"
    snapshot_ref=$(git -C "$repository" for-each-ref --sort=-creatordate --format='%(refname)' refs/boxteam/snapshots | head -n 1)
    test -n "$snapshot_ref" || fail "目标仓库没有已推送快照"
    printf '%s\n' "$snapshot_ref"
    ;;
  hash-file)
    file_path=$1
    require_absolute_path "$file_path"
    test -f "$file_path" || fail "待校验文件不存在: $file_path"
    sha256sum "$file_path" | awk '{print $1}'
    ;;
  remove-upload)
    upload_path=$1
    require_absolute_path "$upload_path"
    case "$(basename "$upload_path")" in
      .env.uploading-*) rm -f -- "$upload_path" ;;
      *) fail "拒绝删除非 .env 上传临时文件: $upload_path" ;;
    esac
    ;;
  install-env)
    upload_path=$1
    destination=$2
    require_absolute_path "$upload_path"
    require_absolute_path "$destination"
    case "$(basename "$upload_path")" in
      .env.uploading-*) ;;
      *) fail "无效 .env 上传临时文件: $upload_path" ;;
    esac
    test "$(dirname "$upload_path")" = "$(dirname "$destination")" || fail ".env 临时文件必须与目标文件位于同一目录"
    test "$(basename "$destination")" = .env || fail "目标文件必须命名为 .env"
    chmod 600 "$upload_path"
    mv -f -- "$upload_path" "$destination"
    ;;
  bootstrap)
    repository=$1
    initialize_submodules=$2
    require_repository "$repository"
    command -v uv >/dev/null 2>&1 || fail "目标缺少 uv"
    command -v bun >/dev/null 2>&1 || fail "目标缺少 bun"
    command -v python3 >/dev/null 2>&1 || fail "目标缺少 python3"
    if test "$initialize_submodules" = 1; then
      git -C "$repository" submodule update --init --recursive
    fi
    system_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if test -x "$repository/.venv/bin/python"; then
      venv_version=$($repository/.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
      if test "$venv_version" != "$system_version"; then
        test "$repository/.venv" = "${repository%/}/.venv" || fail "虚拟环境删除路径校验失败"
        rm -rf -- "$repository/.venv"
      fi
    elif test -e "$repository/.venv"; then
      test "$repository/.venv" = "${repository%/}/.venv" || fail "虚拟环境删除路径校验失败"
      rm -rf -- "$repository/.venv"
    fi
    (
      cd "$repository"
      UV_PROJECT_ENVIRONMENT="$repository/.venv" uv sync --frozen
      bun install --frozen-lockfile
      if ! node -e 'require("node-pty")' >/dev/null 2>&1; then
        bun install --force --frozen-lockfile
      fi
      node -e 'require("node-pty")' >/dev/null
      bun install --cwd src/web --frozen-lockfile
      bun install --cwd src/webview-ui --frozen-lockfile
      lock_hash=$(sha256sum uv.lock bun.lock src/web/bun.lock src/webview-ui/bun.lock | sha256sum | awk '{print $1}')
      printf 'platform=linux\npython=%s\nlocks=%s\n' "$system_version" "$lock_hash" > .venv/.boxteam-target-metadata
    )
    printf '%s\n' "$repository/.venv/bin/python"
    ;;
  start)
    repository=$1
    target_home=$2
    artifacts=$3
    profile=$4
    home_override=${5:-}
    workspace_override=${6:-}
    require_repository "$repository"
    require_absolute_path "$target_home"
    require_absolute_path "$artifacts"
    boxteam_home=$(profile_home "$target_home" "$profile" "$home_override")
    workspace=$(profile_workspace "$boxteam_home" "$workspace_override")
    mkdir -p "$boxteam_home" "$workspace" "$artifacts/runtime/$profile"
    if test -n "$(listener_pids 8010 8011 8012 8013 8014 8015 8016 8002)"; then
      fail "开发服务端口已被占用，请先执行 stop 或使用另一目标"
    fi
    log_file="$artifacts/runtime/$profile/services.log"
    if test "$profile" = development; then
      test -x "$repository/.venv/bin/python" || fail "目标 .venv 尚未初始化，请先执行 bootstrap"
      (
        cd "$repository"
        nohup env \
          BOXTEAM_PROJECT_ROOT="$repository" \
          BOXTEAM_HOME="$boxteam_home" \
          BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT="$workspace" \
          BOXTEAM_PYTHON_BIN="$repository/.venv/bin/python" \
          UV_PROJECT_ENVIRONMENT="$repository/.venv" \
          PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium \
          BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE=0 \
          bun run scripts/dev.mjs --only-launch >"$log_file" 2>&1 </dev/null &
        printf '%s\n' "$!" > "$artifacts/runtime/$profile/start.pid"
      )
      start_pid=$(cat "$artifacts/runtime/$profile/start.pid")
      wait_http http://127.0.0.1:8014/api/gateway/health "development Gateway" "$start_pid"
      wait_http http://127.0.0.1:8011/health "development Web" "$start_pid"
    else
      command -v boxteam >/dev/null 2>&1 || fail "目标尚未安装 boxteam 命令"
      nohup env BOXTEAM_HOME="$boxteam_home" BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT="$workspace" \
        boxteam start --no-open >"$log_file" 2>&1 </dev/null &
      start_pid=$!
      printf '%s\n' "$start_pid" > "$artifacts/runtime/$profile/start.pid"
      wait_http http://127.0.0.1:8014/api/gateway/health "installed Gateway" "$start_pid"
    fi
    printf '{"profile":"%s","boxteam_home":"%s","workspace":"%s"}\n' "$profile" "$boxteam_home" "$workspace"
    ;;
  stop)
    profile=$1
    case "$profile" in
      development) stop_ports 8002 8010 8011 8012 8013 8014 8015 8016 ;;
      installed) stop_ports 8010 8012 8014 8015 ;;
      *) fail "未知运行 profile: $profile" ;;
    esac
    ;;
  status)
    profile=$1
    if curl -fsS http://127.0.0.1:8014/api/gateway/health >/dev/null 2>&1; then
      gateway=true
    else
      gateway=false
    fi
    if test "$profile" = development && curl -fsS http://127.0.0.1:8011/health >/dev/null 2>&1; then
      web=true
    elif test "$profile" = installed; then
      web=null
    else
      web=false
    fi
    printf '{"profile":"%s","gateway":%s,"web":%s}\n' "$profile" "$gateway" "$web"
    ;;
  test)
    repository=$1
    target_home=$2
    profile=$3
    home_override=${4:-}
    workspace_override=${5:-}
    shift 5
    require_repository "$repository"
    boxteam_home=$(profile_home "$target_home" "$profile" "$home_override")
    workspace=$(profile_workspace "$boxteam_home" "$workspace_override")
    test -x "$repository/.venv/bin/python" || fail "目标 .venv 尚未初始化"
    cd "$repository"
    BOXTEAM_HOME="$boxteam_home" WORKSPACE_ROOT="$workspace" "$repository/.venv/bin/python" -m pytest "$@"
    ;;
  prepare-collect)
    artifacts=$1
    archive=$2
    require_absolute_path "$artifacts"
    require_absolute_path "$archive"
    test "$(dirname "$archive")" = "$artifacts" || fail "产物压缩包必须位于目标 artifacts 根目录"
    temporary_archive=$(mktemp /tmp/boxteam-target-artifacts-XXXXXX.tar.gz)
    trap 'rm -f -- "$temporary_archive"' EXIT
    tar -C "$artifacts" --exclude="$(basename "$archive")" -czf "$temporary_archive" .
    mv -f -- "$temporary_archive" "$archive"
    trap - EXIT
    printf '%s\n' "$archive"
    ;;
  *) fail "未知 Linux 目标动作: $action" ;;
esac
