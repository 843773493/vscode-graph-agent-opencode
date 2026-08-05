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
        "full-python"
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

$windowsRuntimeRoot = Join-Path $env:LOCALAPPDATA "boxteam-windows"
New-Item -ItemType Directory -Force -Path $windowsRuntimeRoot | Out-Null
$env:UV_PROJECT_ENVIRONMENT = Join-Path $windowsRuntimeRoot "venv"
$env:UV_LINK_MODE = "copy"
$localModules = @("runtime", "python-unit", "python-static", "gateway-unit", "terminal-powershell", "dev-windows", "js-platform", "backend-js", "web-build", "webview-build", "extension", "full-python")
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
    "backend-js" { Invoke-Native "bun" @("test", "src/shared", "src/browser", "src/terminal") }
    "web-build" { Invoke-Native "bun" @("run", "--cwd", "src/web", "build") }
    "webview-build" { Invoke-Native "bun" @("run", "--cwd", "src/webview-ui", "build") }
    "extension" { Invoke-Native "bun" @("run", "test:extension") }
    "full-python" { Invoke-Native "uv" @("run", "pytest") }
    default { throw "Unimplemented Windows module: $Module" }
}
Copy-TerminalArtifacts
Copy-DevArtifacts
