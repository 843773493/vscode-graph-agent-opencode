$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# TODO: 以下 Windows 兼容实现需在真实 VMware Windows + OpenSSH 环境验证路径转义、ACL 和后台进程继承行为。

function Fail-Target([string]$Message) {
    throw "Windows 开发目标动作失败: $Message"
}

function Assert-NativeCommand([string]$Label) {
    if ($LASTEXITCODE -ne 0) { Fail-Target "$Label 失败，exit_code=$LASTEXITCODE" }
}

function Require-AbsolutePath([string]$Value) {
    if (-not [System.IO.Path]::IsPathFullyQualified($Value)) {
        Fail-Target "必须使用绝对路径: $Value"
    }
}

function Require-Repository([string]$Repository) {
    Require-AbsolutePath $Repository
    if (-not (Test-Path -LiteralPath (Join-Path $Repository ".git") -PathType Container)) {
        Fail-Target "目标不是完整 Git 仓库: $Repository"
    }
}

function Resolve-ProfileHome(
    [string]$TargetHome,
    [string]$Profile,
    [string]$Override
) {
    if ($Override) {
        Require-AbsolutePath $Override
        return $Override
    }
    if ($Profile -eq "development") { return (Join-Path $TargetHome ".boxteams-dev") }
    if ($Profile -eq "installed") { return (Join-Path $TargetHome ".boxteams") }
    Fail-Target "未知运行 profile: $Profile"
}

function Stop-DevelopmentPorts([string]$Profile) {
    $Ports = if ($Profile -eq "development") {
        @(8002, 8010, 8011, 8012, 8013, 8014, 8015, 8016)
    } elseif ($Profile -eq "installed") {
        @(8010, 8012, 8014, 8015)
    } else {
        Fail-Target "未知运行 profile: $Profile"
    }
    $Processes = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $Ports -contains $_.LocalPort } |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($ProcessId in $Processes) {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    }
}

function Invoke-TargetAction([string[]]$Arguments) {
    if ($Arguments.Count -eq 0) { Fail-Target "缺少动作" }
    $Action = $Arguments[0]
    $Values = @($Arguments | Select-Object -Skip 1)
    switch ($Action) {
        "init-repository" {
            $Repository, $Artifacts, $TargetHome = $Values
            Require-AbsolutePath $Repository
            Require-AbsolutePath $Artifacts
            Require-AbsolutePath $TargetHome
            New-Item -ItemType Directory -Force -Path $Repository, $Artifacts | Out-Null
            New-Item -ItemType Directory -Force -Path (Join-Path $TargetHome ".boxteams-dev\boxteam_workspace") | Out-Null
            New-Item -ItemType Directory -Force -Path (Join-Path $TargetHome ".boxteams\boxteam_workspace") | Out-Null
            if (-not (Test-Path -LiteralPath (Join-Path $Repository ".git"))) {
                git -C $Repository init
                Assert-NativeCommand "git init"
            }
            git -C $Repository config receive.denyCurrentBranch refuse
            Assert-NativeCommand "git config"
            ".env`n.env.uploading-*" | Set-Content -LiteralPath (Join-Path $Repository ".git\info\exclude") -Encoding UTF8
            Write-Output $Repository
        }
        "repository-status" {
            $Repository = $Values[0]
            Require-Repository $Repository
            git -C $Repository status --porcelain --untracked-files=all
        }
        "activate" {
            $Repository, $SnapshotRef = $Values
            Require-Repository $Repository
            $Dirty = @(git -C $Repository status --porcelain --untracked-files=all)
            if ($Dirty.Count -gt 0) { Fail-Target "目标工作区包含本地修改，拒绝激活: $($Dirty -join '; ')" }
            git -C $Repository show-ref --verify --quiet $SnapshotRef
            if ($LASTEXITCODE -ne 0) { Fail-Target "快照引用不存在: $SnapshotRef" }
            git -C $Repository checkout -B boxteam-host-snapshot $SnapshotRef
            git -C $Repository rev-parse HEAD
        }
        "latest-snapshot" {
            $Repository = $Values[0]
            Require-Repository $Repository
            $SnapshotRef = @(git -C $Repository for-each-ref --sort=-creatordate --format="%(refname)" refs/boxteam/snapshots | Select-Object -First 1)
            if (-not $SnapshotRef) { Fail-Target "目标仓库没有已推送快照" }
            Write-Output $SnapshotRef
        }
        "hash-file" {
            $FilePath = $Values[0]
            Require-AbsolutePath $FilePath
            (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        "remove-upload" {
            $UploadPath = $Values[0]
            Require-AbsolutePath $UploadPath
            if (-not ([System.IO.Path]::GetFileName($UploadPath).StartsWith(".env.uploading-"))) {
                Fail-Target "拒绝删除非 .env 上传临时文件: $UploadPath"
            }
            Remove-Item -LiteralPath $UploadPath -Force -ErrorAction SilentlyContinue
        }
        "install-env" {
            $UploadPath, $Destination = $Values
            Require-AbsolutePath $UploadPath
            Require-AbsolutePath $Destination
            if ([System.IO.Path]::GetDirectoryName($UploadPath) -ne [System.IO.Path]::GetDirectoryName($Destination)) {
                Fail-Target ".env 临时文件必须与目标文件位于同一目录"
            }
            if ([System.IO.Path]::GetFileName($Destination) -ne ".env") { Fail-Target "目标文件必须命名为 .env" }
            Move-Item -LiteralPath $UploadPath -Destination $Destination -Force
            icacls $Destination /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
        }
        "bootstrap" {
            $Repository, $InitializeSubmodules = $Values
            Require-Repository $Repository
            if ($InitializeSubmodules -eq "1") {
                git -C $Repository submodule update --init --recursive
                Assert-NativeCommand "git submodule update"
            }
            Push-Location $Repository
            try {
                $env:UV_PROJECT_ENVIRONMENT = Join-Path $Repository ".venv"
                uv sync --frozen
                Assert-NativeCommand "uv sync"
                bun install --frozen-lockfile
                Assert-NativeCommand "bun install"
                # TODO: 获得 VMware Windows 资源后验证 node-pty 原生模块重建路径。
                node -e 'require("node-pty")'
                if ($LASTEXITCODE -ne 0) {
                    bun install --force --frozen-lockfile
                    Assert-NativeCommand "bun install --force"
                    node -e 'require("node-pty")'
                }
                if ($LASTEXITCODE -ne 0) { Fail-Target "node-pty Windows 原生模块不可用" }
                bun install --cwd src/web --frozen-lockfile
                Assert-NativeCommand "Web bun install"
                bun install --cwd src/webview-ui --frozen-lockfile
                Assert-NativeCommand "Webview bun install"
                bun x playwright install chromium
                Assert-NativeCommand "Playwright Chromium install"
                $Python = Join-Path $Repository ".venv\Scripts\python.exe"
                if (-not (Test-Path -LiteralPath $Python)) { Fail-Target "uv 未生成 Windows .venv Python: $Python" }
                $LockHash = (Get-FileHash uv.lock -Algorithm SHA256).Hash.ToLowerInvariant()
                "platform=windows`nlocks=$LockHash" | Set-Content -LiteralPath (Join-Path $Repository ".venv\.boxteam-target-metadata") -Encoding UTF8
                Write-Output $Python
            } finally {
                Pop-Location
            }
        }
        "start" {
            $Repository, $TargetHome, $Artifacts, $Profile, $HomeOverride, $WorkspaceOverride = $Values
            Require-Repository $Repository
            $BoxTeamHome = Resolve-ProfileHome $TargetHome $Profile $HomeOverride
            $Workspace = if ($WorkspaceOverride) { $WorkspaceOverride } else { Join-Path $BoxTeamHome "boxteam_workspace" }
            New-Item -ItemType Directory -Force -Path $BoxTeamHome, $Workspace, (Join-Path $Artifacts "runtime\$Profile") | Out-Null
            $PortsInUse = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { @(8002,8010,8011,8012,8013,8014,8015,8016) -contains $_.LocalPort }
            if ($PortsInUse) { Fail-Target "开发服务端口已被占用" }
            $Log = Join-Path $Artifacts "runtime\$Profile\services.log"
            if ($Profile -eq "development") {
                $Python = Join-Path $Repository ".venv\Scripts\python.exe"
                if (-not (Test-Path $Python)) { Fail-Target "目标 .venv 尚未初始化" }
                $Environment = @{
                    BOXTEAM_PROJECT_ROOT = $Repository
                    BOXTEAM_HOME = $BoxTeamHome
                    BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT = $Workspace
                    BOXTEAM_PYTHON_BIN = $Python
                    UV_PROJECT_ENVIRONMENT = (Join-Path $Repository ".venv")
                    BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE = "0"
                }
                foreach ($Pair in $Environment.GetEnumerator()) { [Environment]::SetEnvironmentVariable($Pair.Key, $Pair.Value, "Process") }
                Start-Process -FilePath "bun.exe" -ArgumentList @("run", "scripts/dev.mjs", "--only-launch") -WorkingDirectory $Repository -RedirectStandardOutput $Log -RedirectStandardError "$Log.stderr" -WindowStyle Hidden
            } else {
                $env:BOXTEAM_HOME = $BoxTeamHome
                $env:BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT = $Workspace
                Start-Process -FilePath "boxteam.cmd" -ArgumentList @("start", "--no-open") -RedirectStandardOutput $Log -RedirectStandardError "$Log.stderr" -WindowStyle Hidden
            }
            Write-Output (@{ profile = $Profile; boxteam_home = $BoxTeamHome; workspace = $Workspace } | ConvertTo-Json -Compress)
        }
        "stop" { Stop-DevelopmentPorts $Values[0] }
        "status" {
            $Profile = $Values[0]
            $Gateway = $false
            $Web = if ($Profile -eq "installed") { $null } else { $false }
            try { Invoke-WebRequest http://127.0.0.1:8014/api/gateway/health -UseBasicParsing | Out-Null; $Gateway = $true } catch {}
            if ($Profile -eq "development") { try { Invoke-WebRequest http://127.0.0.1:8011/health -UseBasicParsing | Out-Null; $Web = $true } catch {} }
            Write-Output (@{ profile = $Profile; gateway = $Gateway; web = $Web } | ConvertTo-Json -Compress)
        }
        "test" {
            $Repository, $TargetHome, $Profile, $HomeOverride, $WorkspaceOverride = $Values[0..4]
            $TestArgs = @($Values | Select-Object -Skip 5)
            $env:BOXTEAM_HOME = Resolve-ProfileHome $TargetHome $Profile $HomeOverride
            $env:WORKSPACE_ROOT = if ($WorkspaceOverride) { $WorkspaceOverride } else { Join-Path $env:BOXTEAM_HOME "boxteam_workspace" }
            & (Join-Path $Repository ".venv\Scripts\python.exe") -m pytest @TestArgs
            Assert-NativeCommand "pytest"
        }
        "prepare-collect" {
            $Artifacts, $Archive = $Values
            Require-AbsolutePath $Artifacts
            Require-AbsolutePath $Archive
            $TemporaryArchive = Join-Path ([System.IO.Path]::GetTempPath()) ("boxteam-target-artifacts-{0}.zip" -f [guid]::NewGuid())
            try {
                Compress-Archive -Path (Join-Path $Artifacts "*") -DestinationPath $TemporaryArchive -Force
                Move-Item -LiteralPath $TemporaryArchive -Destination $Archive -Force
            } finally {
                Remove-Item -LiteralPath $TemporaryArchive -Force -ErrorAction SilentlyContinue
            }
            Write-Output $Archive
        }
        default { Fail-Target "未知 Windows 目标动作: $Action" }
    }
}

if (-not $env:BOXTEAM_TARGET_ARGUMENTS_BASE64) { Fail-Target "缺少结构化动作参数" }
$Decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:BOXTEAM_TARGET_ARGUMENTS_BASE64)) | ConvertFrom-Json
Invoke-TargetAction @($Decoded)
