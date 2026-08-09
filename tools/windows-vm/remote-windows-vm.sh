#!/usr/bin/env bash
# shellcheck disable=SC2016
set -euo pipefail

REPO_ROOT=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null) || {
  printf '[windows-vm stage=bootstrap] 当前目录不在 Git 仓库内\n' >&2
  exit 78
}
LOCAL_CONFIG=${WINDOWS_VM_REMOTE_CONFIG:-$REPO_ROOT/tools/ssh/windows-vm-remote.env}

if [[ -f "$LOCAL_CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$LOCAL_CONFIG"
fi

REMOTE_HOST=${WINDOWS_VM_REMOTE_HOST:-}
REMOTE_PORT=${WINDOWS_VM_REMOTE_PORT:-22}
REMOTE_ROOT=${WINDOWS_VM_REMOTE_ROOT:-}
REMOTE_REPO=${WINDOWS_VM_REMOTE_REPO:-${REMOTE_ROOT:+$REMOTE_ROOT/repository}}
REMOTE_STATE_DIR=${WINDOWS_VM_REMOTE_STATE_DIR:-${REMOTE_ROOT:+$REMOTE_ROOT/state}}
REMOTE_VM_TOOL=${WINDOWS_VM_REMOTE_VM_TOOL:-${REMOTE_ROOT:+$REMOTE_ROOT/Kunlun-Code/tools/windows-vm/windows-vm.sh}}
REMOTE_SSH_KEY=${WINDOWS_VM_REMOTE_SSH_KEY:-}
REMOTE_KNOWN_HOSTS=${WINDOWS_VM_REMOTE_KNOWN_HOSTS:-$REPO_ROOT/tools/ssh/windows_vm_known_hosts}
SYNC_ATTEMPTS=${WINDOWS_VM_SYNC_ATTEMPTS:-4}

die() {
  printf '[windows-vm stage=%s] error: %s\n' "${2:-runtime}" "$1" >&2
  exit "${3:-1}"
}

require_positive_integer() {
  [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "$1 必须是正整数: $2" config 78
}

require_config() {
  [[ -n "$REMOTE_HOST" ]] || die "WINDOWS_VM_REMOTE_HOST 缺失: $LOCAL_CONFIG" config 78
  [[ -n "$REMOTE_ROOT" ]] || die "WINDOWS_VM_REMOTE_ROOT 缺失" config 78
  [[ -n "$REMOTE_REPO" ]] || die "WINDOWS_VM_REMOTE_REPO 缺失" config 78
  [[ -n "$REMOTE_STATE_DIR" ]] || die "WINDOWS_VM_REMOTE_STATE_DIR 缺失" config 78
  [[ -n "$REMOTE_VM_TOOL" ]] || die "WINDOWS_VM_REMOTE_VM_TOOL 缺失" config 78
  [[ -n "$REMOTE_SSH_KEY" && -f "$REMOTE_SSH_KEY" ]] || die "SSH 私钥不存在: $REMOTE_SSH_KEY" config 78
  require_positive_integer WINDOWS_VM_REMOTE_PORT "$REMOTE_PORT"
  require_positive_integer WINDOWS_VM_SYNC_ATTEMPTS "$SYNC_ATTEMPTS"
  mkdir -p "$(dirname "$REMOTE_KNOWN_HOSTS")"
  chmod 600 "$REMOTE_SSH_KEY"
  local mode
  mode=$(stat -c '%a' "$REMOTE_SSH_KEY")
  [[ "$mode" == 600 ]] || die "SSH 私钥权限必须为 600: path=$REMOTE_SSH_KEY mode=$mode" config 78
}

ssh_args() {
  printf '%s\n' \
    -i "$REMOTE_SSH_KEY" \
    -p "$REMOTE_PORT" \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=4 \
    -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=$REMOTE_KNOWN_HOSTS"
}

remote_bash() {
  require_config
  local script=${1:?remote bash script is required}
  shift
  local -a args
  local remote_command='bash -s --'
  local argument quoted
  for argument in "$@"; do
    printf -v quoted '%q' "$argument"
    remote_command+=" $quoted"
  done
  mapfile -t args < <(ssh_args)
  ssh "${args[@]}" "$REMOTE_HOST" "$remote_command" <<<"$script"
}

rsync_transport() {
  local key known
  printf -v key '%q' "$REMOTE_SSH_KEY"
  printf -v known '%q' "$REMOTE_KNOWN_HOSTS"
  printf 'ssh -i %s -p %q -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=%s' \
    "$key" "$REMOTE_PORT" "$known"
}

ensure_remote_directories() {
  remote_bash '
set -euo pipefail
root=$1
repo=$2
state=$3
mkdir -p -- "$root" "$repo" "$state"
[[ "$repo" == "$root"/* ]] || { printf "remote repository must be below remote root: %s\n" "$repo" >&2; exit 78; }
' "$REMOTE_ROOT" "$REMOTE_REPO" "$REMOTE_STATE_DIR"
}

sync_checkout() {
  require_config
  ensure_remote_directories
  local transport changes attempt
  transport=$(rsync_transport)
  local -a filters=(
    --exclude /tools/ssh/
    --exclude /reference_repo/
    --exclude /.venv/
    --exclude '**/.venv/'
    --exclude /node_modules/
    --exclude '**/node_modules/'
    --exclude /out/
    --exclude /.boxteam/
    --exclude '**/__pycache__/'
    --exclude /.pytest_cache/
  )
  for ((attempt = 1; attempt <= SYNC_ATTEMPTS; attempt++)); do
    rsync -a --checksum --delete --delete-excluded --no-times --omit-dir-times \
      --human-readable --info=stats1 "${filters[@]}" -e "$transport" \
      "$REPO_ROOT/" "$REMOTE_HOST:$REMOTE_REPO/"
    changes=$(rsync -a --dry-run --checksum --delete --delete-excluded --no-times --omit-dir-times \
      --itemize-changes --out-format='%i %n%L' "${filters[@]}" -e "$transport" \
      "$REPO_ROOT/" "$REMOTE_HOST:$REMOTE_REPO/")
    if [[ -z "$changes" ]]; then
      printf '[windows-vm stage=sync] 同步完成: repo=%s\n' "$REMOTE_REPO"
      return 0
    fi
    if ((attempt < SYNC_ATTEMPTS)); then
      printf '[windows-vm stage=sync] 本地工作区在同步期间发生变化，重试 %d/%d\n' "$attempt" "$SYNC_ATTEMPTS" >&2
    fi
  done
  printf '%s\n' "$changes" >&2
  die "工作区在 $SYNC_ATTEMPTS 次同步期间持续变化" sync 75
}

run_vm_tool() {
  require_config
  (($# > 0)) || die '缺少 VM 动作' dispatch 64
remote_bash '
set -euo pipefail
tool=$1
state=$2
repo=$3
shift 3
[[ -x "$tool" ]] || { printf "VM tool is not executable: %s\n" "$tool" >&2; exit 78; }
export WINDOWS_VM_STATE_DIR="$state"
export WINDOWS_VM_REPO_ROOT_OVERRIDE="$repo"
exec "$tool" "$@"
' "$REMOTE_VM_TOOL" "$REMOTE_STATE_DIR" "$REMOTE_REPO" "$@"
}

switch_share() {
  remote_bash '
set -euo pipefail
state=$1
repo=$2
config=$state/config.env
backup=$state/config.env.boxteam-backup
mkdir -p -- "$state"
if [[ -f "$config" && ! -f "$backup" ]]; then
  cp -p -- "$config" "$backup"
  chmod 600 "$backup"
fi
tmp="$config.tmp.$$"
if [[ -f "$config" ]]; then
  awk -F= '\''$1 != "WINDOWS_VM_REPO_ROOT" { print }'\'' "$config" >"$tmp"
fi
printf "WINDOWS_VM_REPO_ROOT=%q\n" "$repo" >>"$tmp"
chmod 600 "$tmp"
mv -- "$tmp" "$config"
printf "share_repo=%s\n" "$repo"
' "$REMOTE_STATE_DIR" "$REMOTE_REPO"
}

restore_share() {
  run_vm_tool stop
  remote_bash '
set -euo pipefail
state=$1
config=$state/config.env
backup=$state/config.env.boxteam-backup
[[ -f "$backup" ]] || { printf "share backup is missing: %s\n" "$backup" >&2; exit 78; }
cp -p -- "$backup" "$config"
chmod 600 "$config"
printf "restored=%s\n" "$config"
' "$REMOTE_STATE_DIR"
}

activate_share() {
  sync_checkout
  run_vm_tool stop
  switch_share
  run_vm_tool start
  run_vm_tool status --json
}

guest_script_path() {
  printf 'Z:\\tools\\windows-vm\\guest\\%s' "$1"
}

guest_repo_exec() {
  local command=${1:?guest repository command is required}
  local invocation='$ErrorActionPreference = "Stop"; '
  invocation+='& cmd.exe /c "net use Z: /delete /y >nul 2>&1" | Out-Null; '
  invocation+='& net.exe use Z: '\''\\10.0.2.4\qemu'\'' /persistent:no | Out-Null; '
  invocation+='if ($LASTEXITCODE -ne 0) { throw "Failed to map Windows project share to Z:" }; '
  invocation+="$command"
  run_vm_tool exec "$invocation"
}

prepare_runtime_cache() {
  local cache_dir="$REPO_ROOT/tools/windows-vm/runtime-cache"
  local archive="$cache_dir/bun-windows-x64.zip"
  local checksum="$cache_dir/bun-windows-x64.zip.sha256"
  local temporary="$archive.part"
  mkdir -p "$cache_dir"
  if [[ -s "$archive" ]] && unzip -t "$archive" >/dev/null 2>&1; then
    :
  else
    printf '[windows-vm stage=bootstrap] 下载官方 Bun Windows x64 archive\n'
    rm -f "$archive"
    rm -f "$temporary"
    curl --fail --location --retry 3 --retry-all-errors --connect-timeout 20 \
      --output "$temporary" \
      https://github.com/oven-sh/bun/releases/latest/download/bun-windows-x64.zip || {
        rm -f "$temporary"
        die 'Bun archive 下载失败，临时文件已清理' bootstrap 75
      }
    mv -- "$temporary" "$archive"
  fi
  sha256sum "$archive" | tee "$checksum"
}

bootstrap_runtime() {
  prepare_runtime_cache
  sync_checkout
  guest_repo_exec "& '$(guest_script_path bootstrap-runtime.ps1)'"
}

mirror_source() {
  sync_checkout
  guest_repo_exec "& '$(guest_script_path bootstrap-js.ps1)'"
}

bootstrap_js() {
  sync_checkout
  guest_repo_exec "& '$(guest_script_path bootstrap-js.ps1)' -InstallDependencies"
}

run_guest_module() {
  local module=${1:-}
  [[ -n "$module" ]] || die 'run-module 需要模块名' dispatch 64
  shift || true
  (($# == 0)) || die 'run-module 不接受额外参数，模块必须保持结构化' dispatch 64
  local script
  script="& '$(guest_script_path run-module.ps1)' -Module '$module'"
  mkdir -p "$REPO_ROOT/out/tests/temp/windows-vm/artifacts"
  local stamp log status
  stamp=$(date -u +%Y%m%d-%H%M%S)
  log="$REPO_ROOT/out/tests/temp/windows-vm/artifacts/${module}-${stamp}.log"
  set +e
  guest_repo_exec "$script" 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  set -e
  if ((status != 0)); then
    die "模块 $module 失败，日志: $log" module "$status"
  fi
  printf '[windows-vm stage=module] module=%s log=%s\n' "$module" "$log"
}

run_module() {
  local module=${1:-}
  [[ -n "$module" ]] || die 'run-module 需要模块名' dispatch 64
  shift || true
  (($# == 0)) || die 'run-module 不接受额外参数，模块必须保持结构化' dispatch 64
  case "$module" in
    js-platform|backend-js|web-build|webview-build|extension|package-windows-x64)
      bootstrap_js
      ;;
    *)
      mirror_source
      ;;
  esac
  run_guest_module "$module"
}

push_packaging_artifacts() {
  require_config
  local source="$REPO_ROOT/out/packaging/windows-x64"
  local destination="$REMOTE_REPO/out/packaging/windows-x64"
  [[ -f "$source/standalone/boxteam-windows-x64-0.1.0.zip" ]] || {
    die "Linux 交叉打包便携 ZIP 不存在: $source/standalone" push-packaging 78
  }
  [[ -d "$source/tarballs" && -d "$source/release-assets" ]] || {
    die "Linux 交叉打包 npm 产物目录不存在: $source" push-packaging 78
  }
  [[ -d "$source/installer" ]] || {
    die "Windows 安装器目录不存在: $source/installer" push-packaging 78
  }
  remote_bash '
set -euo pipefail
destination=$1
mkdir -p -- "$destination/tarballs" "$destination/release-assets" "$destination/standalone" "$destination/installer"
' "$destination"
  local transport
  transport=$(rsync_transport)
  rsync -a --delete --human-readable -e "$transport" \
    "$source/tarballs/" "$REMOTE_HOST:$destination/tarballs/"
  rsync -a --delete --human-readable -e "$transport" \
    "$source/release-assets/" "$REMOTE_HOST:$destination/release-assets/"
  rsync -a --delete --human-readable -e "$transport" \
    "$source/standalone/" "$REMOTE_HOST:$destination/standalone/"
  rsync -a --delete --human-readable -e "$transport" \
    "$source/installer/" "$REMOTE_HOST:$destination/installer/"
  rsync -a --human-readable -e "$transport" \
    "$source/build-result.json" "$source/size-report.json" \
    "$REMOTE_HOST:$destination/"
  printf '[windows-vm stage=push-packaging] artifacts=%s\n' "$destination"
}

collect_cross_verification() {
  require_config
  local destination=${1:-$REPO_ROOT/out/windows-vm/verify-windows-x64-cross}
  mkdir -p "$destination"
  local transport
  transport=$(rsync_transport)
  rsync -a --human-readable -e "$transport" \
    "$REMOTE_HOST:$REMOTE_REPO/out/windows-vm/verify-windows-x64-cross/" \
    "$destination/" || {
      die "远端 Windows VM 交叉产物验证结果收集失败" collect-cross-verification 75
    }
  printf '[windows-vm stage=collect-cross-verification] artifacts=%s\n' "$destination"
}

collect_installer_verification() {
  require_config
  local destination=${1:-$REPO_ROOT/out/windows-vm/verify-windows-installer}
  mkdir -p "$destination"
  local transport
  transport=$(rsync_transport)
  rsync -a --human-readable -e "$transport" \
    "$REMOTE_HOST:$REMOTE_REPO/out/windows-vm/verify-windows-installer/" \
    "$destination/" || {
      die "远端 Windows VM 安装器验证结果收集失败" collect-installer-verification 75
    }
  printf '[windows-vm stage=collect-installer-verification] artifacts=%s\n' "$destination"
}

collect_artifacts() {
  require_config
  local destination=${1:-$REPO_ROOT/out/tests/temp/windows-vm/artifacts/remote}
  mkdir -p "$destination"
  local transport
  transport=$(rsync_transport)
  rsync -a --human-readable -e "$transport" \
    "$REMOTE_HOST:$REMOTE_REPO/out/windows-vm/" "$destination/" || {
      die "远端 Windows VM 产物收集失败: $REMOTE_REPO/out/windows-vm" collect 75
    }
  printf '[windows-vm stage=collect] artifacts=%s\n' "$destination"
}

usage() {
  cat <<'EOF'
Usage: tools/windows-vm/windows-vm.sh <command> [arguments]

Commands:
  config                         打印已解析的非秘密连接配置
  check                          检查 A4500、VM 工具和 guest 状态
  host-info                     检查 A4500 主机和代理监听端口
  sync                           将当前工作区同步到 A4500 独立目录
  activate                       切换 QEMU SMB share 到当前项目并启动 VM
  restore-share                  停止 VM 并恢复切换前的 share 配置
  start | stop | restart         管理对应 Windows VM 生命周期
  status                         获取 Windows VM JSON 状态
  guest-info                     获取 Windows、Git、uv、Bun、Node 信息
  bootstrap-runtime              安装并验证 uv 与 Bun
  bootstrap-js                   在三个项目目录安装 Bun 依赖
  exec <PowerShell>              执行显式 guest PowerShell 诊断命令
  run-module <module>            同步并运行一个固定 Windows 模块
  push-packaging                 推送本地 Linux 交叉打包产物到 Windows VM 共享仓库
  verify-cross-package           在 Windows VM 验证本地 Linux 交叉打包产物
  verify-windows-installer       在 Windows VM 安装并验证 setup.exe
  collect [output-dir]           收集远端 out/windows-vm 产物
EOF
}

command=${1:-help}
if (($#)); then shift; fi
case "$command" in
  config)
    require_config
    printf 'host=%s\nport=%s\nremote_root=%s\nremote_repo=%s\nstate_dir=%s\nvm_tool=%s\nknown_hosts=%s\nidentity_basename=%s\n' \
      "$REMOTE_HOST" "$REMOTE_PORT" "$REMOTE_ROOT" "$REMOTE_REPO" "$REMOTE_STATE_DIR" "$REMOTE_VM_TOOL" \
      "$REMOTE_KNOWN_HOSTS" "$(basename "$REMOTE_SSH_KEY")"
    ;;
  check)
    require_config
    ensure_remote_directories
    remote_bash '
set -euo pipefail
tool=$1
state=$2
repo=$3
[[ -x "$tool" ]] || { printf "VM tool is not executable: %s\n" "$tool" >&2; exit 78; }
[[ -d "$repo" ]] || { printf "remote repository is missing: %s\n" "$repo" >&2; exit 78; }
[[ -f "$state/config.env" ]] || { printf "VM config is missing: %s/config.env\n" "$state" >&2; exit 78; }
printf "remote_ready=true\nrepo=%s\nstate=%s\n" "$repo" "$state"
' "$REMOTE_VM_TOOL" "$REMOTE_STATE_DIR" "$REMOTE_REPO"
    run_vm_tool status --json
    ;;
  host-info)
    remote_bash '
set -euo pipefail
printf "host=%s\n" "$(hostname)"
printf "kernel=%s\n" "$(uname -sr)"
printf "proxy_listeners=\n"
ss -ltn 2>/dev/null | awk "NR == 1 || /:10809|:7890|:8080|:3128/" || true
printf "vm_tool=%s\n" "$1"
test -x "$1"
' "$REMOTE_VM_TOOL"
    ;;
  sync) sync_checkout ;;
  activate) activate_share ;;
  restore-share) restore_share ;;
  start) switch_share; run_vm_tool start ;;
  stop) run_vm_tool stop ;;
  restart) run_vm_tool stop; switch_share; run_vm_tool start ;;
  status) run_vm_tool status --json ;;
  guest-info)
    run_vm_tool exec "Get-ComputerInfo -Property WindowsProductName,WindowsVersion,OsArchitecture | ConvertTo-Json -Compress; Get-Command git,uv,bun,node,python | Select-Object Name,Source | ConvertTo-Json -Compress"
    ;;
  bootstrap-runtime) bootstrap_runtime ;;
  bootstrap-js) bootstrap_js ;;
  exec)
    (($# > 0)) || die 'exec 需要 PowerShell 命令' dispatch 64
    run_vm_tool exec "$*"
    ;;
  run-module) run_module "$@" ;;
  push-packaging) push_packaging_artifacts ;;
  verify-cross-package)
    sync_checkout
    # 交叉产物位于被同步过滤的 out/ 下，必须在同步完成后再推送到共享目录。
    push_packaging_artifacts
    # 验证模块运行在本地 Windows checkout；先从 Z: 刷新脚本，避免使用旧缓存。
    guest_repo_exec "& '$(guest_script_path bootstrap-js.ps1)'"
    run_guest_module verify-windows-x64-cross
    collect_cross_verification
    ;;
  verify-windows-installer)
    sync_checkout
    # 交叉产物位于被同步过滤的 out/下，必须在同步完成后再推送到共享目录。
    push_packaging_artifacts
    guest_repo_exec "& '$(guest_script_path bootstrap-js.ps1)'"
    run_guest_module verify-windows-installer
    collect_installer_verification
    ;;
  collect) collect_artifacts "$@" ;;
  help|-h|--help) usage ;;
  *) die "未知命令: $command" dispatch 64 ;;
esac
