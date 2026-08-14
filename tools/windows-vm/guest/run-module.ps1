param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "runtime",
        "python-unit",
        "python-static",
        "gateway-unit",
        "terminal-powershell",
        "dev-windows",
        "js-platform",
        "backend-js",
        "web-build",
        "webview-build",
        "extension",
        "full-python",
        "package-windows-x64",
        "verify-windows-x64-cross",
        "verify-windows-installer"
    )]
    [string]$Module
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Native([string]$Executable, [string[]]$Arguments) {
    Write-Output ("[windows-module={0}] {1} {2}" -f $Module, $Executable, ($Arguments -join " "))
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Windows module failed: module=$Module executable=$Executable exit_code=$LASTEXITCODE"
    }
}

function Copy-TerminalArtifacts {
    if ($Module -ne "terminal-powershell") {
        return
    }
    $source = Join-Path $projectRoot "out\tests\e2e\windows\test_terminal_powershell"
    $destination = "Z:\out\windows-vm\terminal-powershell"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Windows terminal E2E artifacts are missing: $source"
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    & robocopy.exe $source $destination "/MIR" "/FFT" "/Z" "/R:2" "/W:1"
    if ($LASTEXITCODE -gt 7) {
        throw "Windows terminal E2E artifact copy failed: exit_code=$LASTEXITCODE"
    }
    Write-Output ("[windows-artifacts] copied={0}" -f $destination)
}

function Copy-DevArtifacts {
    if ($Module -ne "dev-windows") {
        return
    }
    $source = Join-Path $projectRoot "out\windows-vm\dev-windows"
    $destination = "Z:\out\windows-vm\dev-windows"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Windows dev lifecycle artifacts are missing: $source"
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    & robocopy.exe $source $destination "/MIR" "/FFT" "/Z" "/R:2" "/W:1"
    if ($LASTEXITCODE -gt 7) {
        throw "Windows dev lifecycle artifact copy failed: exit_code=$LASTEXITCODE"
    }
    Write-Output ("[windows-artifacts] copied={0}" -f $destination)
}

function Copy-PackagingArtifacts {
    if ($Module -ne "package-windows-x64") {
        return
    }
    $source = Join-Path $projectRoot "out\packaging\windows-x64"
    $destination = "Z:\out\windows-vm\package-windows-x64"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Windows npm packaging artifacts are missing: $source"
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    & robocopy.exe $source $destination "/MIR" "/FFT" "/Z" "/R:2" "/W:1"
    if ($LASTEXITCODE -gt 7) {
        throw "Windows npm packaging artifact copy failed: exit_code=$LASTEXITCODE"
    }
    Write-Output ("[windows-artifacts] copied={0}" -f $destination)
}

function Copy-CrossVerificationArtifacts {
    if ($Module -ne "verify-windows-x64-cross") {
        return
    }
    $source = Join-Path $projectRoot "out\windows-vm\verify-windows-x64-cross"
    $destination = "Z:\out\windows-vm\verify-windows-x64-cross"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Windows cross-package verification artifacts are missing: $source"
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    & robocopy.exe $source $destination "/MIR" "/FFT" "/Z" "/R:2" "/W:1"
    if ($LASTEXITCODE -gt 7) {
        throw "Windows cross-package verification artifact copy failed: exit_code=$LASTEXITCODE"
    }
    Write-Output ("[windows-artifacts] copied={0}" -f $destination)
}

function Copy-InstallerVerificationArtifacts {
    if ($Module -ne "verify-windows-installer") {
        return
    }
    $source = Join-Path $projectRoot "out\windows-vm\verify-windows-installer"
    $destination = "Z:\out\windows-vm\verify-windows-installer"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Windows installer verification artifacts are missing: $source"
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    & robocopy.exe $source $destination "/MIR" "/FFT" "/Z" "/R:2" "/W:1"
    if ($LASTEXITCODE -gt 7) {
        throw "Windows installer verification artifact copy failed: exit_code=$LASTEXITCODE"
    }
    Write-Output ("[windows-artifacts] copied={0}" -f $destination)
}

$windowsRuntimeRoot = Join-Path $env:LOCALAPPDATA "boxteam-windows"
New-Item -ItemType Directory -Force -Path $windowsRuntimeRoot | Out-Null
$env:UV_PROJECT_ENVIRONMENT = Join-Path $windowsRuntimeRoot "venv"
$env:UV_LINK_MODE = "copy"
$localModules = @("runtime", "python-unit", "python-static", "gateway-unit", "terminal-powershell", "dev-windows", "js-platform", "backend-js", "web-build", "webview-build", "extension", "full-python", "package-windows-x64", "verify-windows-x64-cross", "verify-windows-installer")
$projectRoot = if ($localModules -contains $Module) {
    $localRoot = Join-Path $windowsRuntimeRoot "source"
    if (-not (Test-Path -LiteralPath $localRoot)) {
        throw "Local Windows JS checkout is missing; run bootstrap-js first: $localRoot"
    }
    $localRoot
} else {
    "Z:\"
}
Set-Location -LiteralPath $projectRoot

switch ($Module) {
    "runtime" {
        $commands = @("git", "uv", "bun", "node", "python")
        foreach ($command in $commands) {
            $resolved = Get-Command $command -ErrorAction Stop
            Write-Output ("[windows-runtime] {0}={1}" -f $command, $resolved.Source)
        }
        Write-Output (Get-ComputerInfo -Property WindowsProductName, WindowsVersion, OsArchitecture | ConvertTo-Json -Compress)
    }
    "python-unit" { Invoke-Native "uv" @("run", "pytest", "tests/unit") }
    "python-static" { Invoke-Native "uv" @("run", "ruff", "check", "app", "tests") }
    "gateway-unit" { Invoke-Native "uv" @("run", "pytest", "tests/unit/gateway", "tests/unit/api") }
    "terminal-powershell" { Invoke-Native "uv" @("run", "pytest", "tests/e2e/windows/test_terminal_powershell.py") }
    "dev-windows" {
        $artifactRoot = Join-Path $projectRoot "out\windows-vm\dev-windows"
        New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
        $pythonBin = Join-Path $windowsRuntimeRoot "venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $pythonBin)) {
            throw "Windows venv Python is missing: $pythonBin"
        }
        $env:BOXTEAM_PYTHON_BIN = $pythonBin
        $env:BOXTEAM_PROJECT_ROOT = $projectRoot
        $stdoutPath = Join-Path $artifactRoot "bun-dev.stdout.log"
        $stderrPath = Join-Path $artifactRoot "bun-dev.stderr.log"
        $lifecyclePath = Join-Path $artifactRoot "lifecycle.json"
        $devPorts = @(8002, 8010, 8011, 8012, 8013, 8014, 8015, 8016)

        function Stop-ProcessTree([int]$ProcessId) {
            for ($attempt = 0; $attempt -lt 3; $attempt++) {
                if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
                    return
                }
                & taskkill.exe /T /F /PID $ProcessId | Out-Null
                if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
                    return
                }
                Start-Sleep -Milliseconds 500
            }
            throw "Failed to stop process tree: pid=$ProcessId exit_code=$LASTEXITCODE"
        }

        function Stop-DevPorts {
            $owners = @(
                Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                    Where-Object { $devPorts -contains $_.LocalPort } |
                    Select-Object -ExpandProperty OwningProcess -Unique
            )
            foreach ($owner in $owners) {
                if ([int]$owner -ne $PID) {
                    Stop-ProcessTree -ProcessId ([int]$owner)
                }
            }
        }

        function Assert-DevPortsClear {
            $remaining = @(
                Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                    Where-Object { $devPorts -contains $_.LocalPort }
            )
            if ($remaining.Count -gt 0) {
                $details = $remaining | ForEach-Object { "$($_.LocalPort):$($_.OwningProcess)" }
                throw "Development ports are still listening: $($details -join ',')"
            }
        }

        function Wait-DevHttp(
            [string]$Url,
            [System.Diagnostics.Process]$Process,
            [hashtable]$Headers = @{}
        ) {
            for ($attempt = 0; $attempt -lt 240; $attempt++) {
                $Process.Refresh()
                if ($Process.HasExited) {
                    throw "Development process exited before endpoint became healthy: pid=$($Process.Id) exit_code=$($Process.ExitCode) url=$Url"
                }
                try {
                    $requestParameters = @{
                        UseBasicParsing = $true
                        Uri = $Url
                        TimeoutSec = 3
                    }
                    if ($Headers.Count -gt 0) {
                        $requestParameters.Headers = $Headers
                    }
                    $response = Invoke-WebRequest @requestParameters
                    if ($response.StatusCode -eq 200) {
                        return
                    }
                } catch {
                    Start-Sleep -Milliseconds 500
                }
            }
            throw "Development endpoint did not become healthy: $Url"
        }

        function Start-DevProcess([string]$OutputPath, [string]$ErrorPath) {
            $bun = (Get-Command bun -ErrorAction Stop).Source
            return Start-Process -FilePath $bun -ArgumentList @("run", "dev") `
                -WorkingDirectory $projectRoot -RedirectStandardOutput $OutputPath `
                -RedirectStandardError $ErrorPath -WindowStyle Hidden -PassThru
        }

        Stop-DevPorts
        Assert-DevPortsClear
        $firstProcess = $null
        $secondProcess = $null
        $gatewayHeaders = @{}
        try {
            $firstProcess = Start-DevProcess -OutputPath $stdoutPath -ErrorPath $stderrPath
            Wait-DevHttp "http://127.0.0.1:8014/api/gateway/health" $firstProcess
            $gatewayCredential = Invoke-RestMethod -UseBasicParsing -Uri "http://127.0.0.1:8014/api/gateway/auth/local-credential" -TimeoutSec 3
            $gatewayHeaders = @{ "X-Local-Token" = [string]$gatewayCredential.data.token }
            Wait-DevHttp "http://127.0.0.1:8013/health" $firstProcess
            Wait-DevHttp "http://127.0.0.1:8016/health" $firstProcess
            Wait-DevHttp "http://127.0.0.1:8011/health" $firstProcess
            Wait-DevHttp "http://127.0.0.1:8011/api/gateway/health" $firstProcess
            Wait-DevHttp "http://127.0.0.1:8011/api/gateway/workspaces" $firstProcess
            Wait-DevHttp "http://127.0.0.1:8011/api/v1/workspace" $firstProcess $gatewayHeaders

            Stop-ProcessTree -ProcessId $firstProcess.Id
            $firstProcess = $null
            Start-Sleep -Seconds 2
            Assert-DevPortsClear

            $secondProcess = Start-DevProcess -OutputPath $stdoutPath -ErrorPath $stderrPath
            Wait-DevHttp "http://127.0.0.1:8014/api/gateway/health" $secondProcess
            Wait-DevHttp "http://127.0.0.1:8011/api/gateway/workspaces" $secondProcess
            Wait-DevHttp "http://127.0.0.1:8011/api/v1/workspace" $secondProcess $gatewayHeaders

            @{
                first_start = "passed"
                first_tree_stop = "passed"
                second_start = "passed"
                endpoint_validation = "passed"
            } | ConvertTo-Json | Set-Content -LiteralPath $lifecyclePath -Encoding utf8
        } finally {
            if ($null -ne $firstProcess) {
                Stop-ProcessTree -ProcessId $firstProcess.Id
            }
            if ($null -ne $secondProcess) {
                Stop-ProcessTree -ProcessId $secondProcess.Id
            }
            Stop-DevPorts
            Assert-DevPortsClear
        }
    }
    "js-platform" { Invoke-Native "bun" @("test", "scripts/cross-platform-development-target.test.mjs") }
    "backend-js" { Invoke-Native "bun" @("test", "src/shared", "src/workspace-services/browser", "src/workspace-services/terminal") }
    "web-build" { Invoke-Native "bun" @("run", "--cwd", "src/clients/web", "build") }
    "webview-build" { Invoke-Native "bun" @("run", "--cwd", "src/webview-ui", "build") }
    "extension" { Invoke-Native "bun" @("run", "test:extension") }
    "full-python" { Invoke-Native "uv" @("run", "pytest") }
    "package-windows-x64" {
        $pythonCacheRoot = Join-Path $env:LOCALAPPDATA "boxteam-windows\packaging-cache\python"
        New-Item -ItemType Directory -Force -Path $pythonCacheRoot | Out-Null
        $env:BOXTEAM_PYTHON_DOWNLOAD_ROOT = $pythonCacheRoot
        $nodeCacheRoot = Join-Path $env:LOCALAPPDATA "boxteam-windows\packaging-cache\node"
        New-Item -ItemType Directory -Force -Path $nodeCacheRoot | Out-Null
        $env:BOXTEAM_NODE_DOWNLOAD_ROOT = $nodeCacheRoot
        $env:BOXTEAM_PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:LOCALAPPDATA "ms-playwright"
        Invoke-Native "bun" @("run", "package:windows-x64")
    }
    "verify-windows-x64-cross" {
        $sharedOutput = "Z:\out\packaging\windows-x64"
        $localOutput = Join-Path $projectRoot "out\packaging\windows-x64"
        if (-not (Test-Path -LiteralPath "$sharedOutput\standalone\boxteam-windows-x64-0.1.0.zip")) {
            throw "Linux cross-package artifact is missing: $sharedOutput"
        }
        Remove-Item -LiteralPath $localOutput -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $localOutput | Out-Null
        & robocopy.exe $sharedOutput $localOutput "/MIR" "/FFT" "/Z" "/R:2" "/W:1"
        if ($LASTEXITCODE -gt 7) {
            throw "Linux cross-package artifact copy failed: exit_code=$LASTEXITCODE"
        }
        $env:BOXTEAM_PROJECT_ROOT = $projectRoot
        $verifier = Join-Path $projectRoot "packaging\runtime\verify-windows-x64.mjs"
        Invoke-Native "node" @($verifier)
        $artifactRoot = Join-Path $projectRoot "out\windows-vm\verify-windows-x64-cross"
        New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
        @{
            status = "passed"
            package_source = "linux-cross-build"
            verifier = $verifier
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $artifactRoot "result.json") -Encoding utf8
    }
    "verify-windows-installer" {
        $sharedOutput = "Z:\out\packaging\windows-x64"
        $localOutput = Join-Path $projectRoot "out\packaging\windows-x64"
        $installerCandidates = @(Get-ChildItem -LiteralPath (Join-Path $sharedOutput "installer") -Filter "*-setup.exe" -File)
        if ($installerCandidates.Count -ne 1) {
            throw "Expected one Windows setup.exe, found $($installerCandidates.Count): $sharedOutput\installer"
        }
        Remove-Item -LiteralPath $localOutput -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $localOutput | Out-Null
        & robocopy.exe $sharedOutput $localOutput "/MIR" "/FFT" "/Z" "/R:2" "/W:1"
        if ($LASTEXITCODE -gt 7) {
            throw "Windows installer artifact copy failed: exit_code=$LASTEXITCODE"
        }

        $installer = Join-Path $localOutput "installer\$($installerCandidates[0].Name)"
        $defaultInstallRoot = "C:\Program Files\BoxTeam"
        $customInstallRoot = Join-Path $env:LOCALAPPDATA "boxteam-windows\installer-test\BoxTeam"
        $artifactRoot = Join-Path $projectRoot "out\windows-vm\verify-windows-installer"
        New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null

        function Remove-InstalledBoxTeam([string]$InstallRoot) {
            $uninstaller = Join-Path $InstallRoot "Uninstall.exe"
            if (Test-Path -LiteralPath $uninstaller) {
                $uninstallerProcess = Start-Process -FilePath $uninstaller `
                    -ArgumentList @("/S", "/NCRC") `
                    -WorkingDirectory $InstallRoot -PassThru -Wait
                if ($uninstallerProcess.ExitCode -ne 0) {
                    throw "BoxTeam uninstall failed: path=$InstallRoot exit_code=$($uninstallerProcess.ExitCode)"
                }
                Start-Sleep -Seconds 2
            }
            if (Test-Path -LiteralPath $InstallRoot) {
                Remove-Item -LiteralPath $InstallRoot -Recurse -Force
            }
        }

        function Install-BoxTeam([string]$InstallRoot) {
            $installerProcess = Start-Process -FilePath $installer `
                -ArgumentList @("/S", "/NCRC", "/D=$InstallRoot") `
                -WorkingDirectory (Split-Path -Parent $installer) -PassThru -Wait
            if ($installerProcess.ExitCode -ne 0) {
                throw "BoxTeam installer failed: path=$InstallRoot exit_code=$($installerProcess.ExitCode)"
            }
            if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot "BoxTeam.exe"))) {
                throw "BoxTeam.exe is missing after installation: $InstallRoot"
            }
        }

        function Stop-BoxTeamProcessTree([int]$ProcessId) {
            if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
                return
            }
            & taskkill.exe /T /F /PID $ProcessId | Out-Null
            if ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
                throw "Failed to stop installed BoxTeam process tree: pid=$ProcessId exit_code=$LASTEXITCODE"
            }
        }

        function Test-InstalledBoxTeam([string]$InstallRoot, [string]$HomeRoot) {
            $previousHome = $env:BOXTEAM_HOME
            $previousNoPause = $env:BOXTEAM_NO_PAUSE
            $env:BOXTEAM_HOME = $HomeRoot
            $env:BOXTEAM_NO_PAUSE = "1"
            try {
                $doctorOutput = (& (Join-Path $InstallRoot "BoxTeamDoctor.exe") "--json" 2>&1 | Out-String)
                if ($LASTEXITCODE -ne 0) {
                    throw "Installed BoxTeamDoctor.exe failed: exit_code=$LASTEXITCODE output=$doctorOutput"
                }
                $doctorPayload = $doctorOutput | ConvertFrom-Json
                if ($doctorPayload.distribution -ne "standalone") {
                    throw "Installed doctor did not report standalone distribution: $doctorOutput"
                }

                $process = Start-Process -FilePath (Join-Path $InstallRoot "BoxTeam.exe") `
                    -ArgumentList @("--no-open") -WorkingDirectory $InstallRoot -PassThru
                try {
                    $healthy = $false
                    for ($attempt = 0; $attempt -lt 360; $attempt++) {
                        $process.Refresh()
                        if ($process.HasExited) {
                            throw "Installed BoxTeam.exe exited before Gateway became healthy: exit_code=$($process.ExitCode)"
                        }
                        try {
                            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8114/api/gateway/health" -TimeoutSec 3
                            if ($response.StatusCode -eq 200) {
                                $healthy = $true
                                break
                            }
                        } catch {
                            Start-Sleep -Milliseconds 500
                        }
                    }
                    if (-not $healthy) {
                        throw "Installed BoxTeam.exe Gateway did not become healthy"
                    }
                } finally {
                    $process.Refresh()
                    if (-not $process.HasExited) {
                        Stop-BoxTeamProcessTree -ProcessId $process.Id
                    }
                }
            } finally {
                $env:BOXTEAM_HOME = $previousHome
                $env:BOXTEAM_NO_PAUSE = $previousNoPause
            }
        }

        Remove-InstalledBoxTeam -InstallRoot $defaultInstallRoot
        Remove-InstalledBoxTeam -InstallRoot $customInstallRoot
        Install-BoxTeam -InstallRoot $defaultInstallRoot
        $defaultShortcut = Join-Path $env:PUBLIC "Desktop\BoxTeam.lnk"
        $startMenuShortcut = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\BoxTeam\BoxTeam.lnk"
        if (-not (Test-Path -LiteralPath $defaultShortcut) -or -not (Test-Path -LiteralPath $startMenuShortcut)) {
            throw "BoxTeam shortcuts were not created in the default installation"
        }
        $defaultKey = Get-ItemProperty -LiteralPath "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam"
        if ($defaultKey.InstallLocation -ne $defaultInstallRoot) {
            throw "Default install location is unexpected: $($defaultKey.InstallLocation)"
        }
        Test-InstalledBoxTeam -InstallRoot $defaultInstallRoot -HomeRoot (Join-Path $artifactRoot "default-home")
        Remove-InstalledBoxTeam -InstallRoot $defaultInstallRoot

        Install-BoxTeam -InstallRoot $customInstallRoot
        Test-InstalledBoxTeam -InstallRoot $customInstallRoot -HomeRoot (Join-Path $artifactRoot "custom-home")
        Remove-InstalledBoxTeam -InstallRoot $customInstallRoot
        if (Test-Path -LiteralPath $customInstallRoot) {
            throw "Custom installation directory was not removed by uninstall: $customInstallRoot"
        }
        @{
            status = "passed"
            installer = $installer
            default_install_path = $defaultInstallRoot
            custom_install_path = $customInstallRoot
            shortcuts = "passed"
            install_uninstall = "passed"
            launcher = "passed"
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $artifactRoot "result.json") -Encoding utf8
    }
    default { throw "Unimplemented Windows module: $Module" }
}
Copy-TerminalArtifacts
Copy-DevArtifacts
Copy-PackagingArtifacts
Copy-CrossVerificationArtifacts
Copy-InstallerVerificationArtifacts
