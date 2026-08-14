param(
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Set-Location -LiteralPath "Z:\"
$windowsRuntimeRoot = Join-Path $env:LOCALAPPDATA "boxteam-windows"
$localRoot = Join-Path $windowsRuntimeRoot "source"
New-Item -ItemType Directory -Force -Path $windowsRuntimeRoot, $localRoot | Out-Null
$partialReferenceRepo = Join-Path $localRoot "reference_repo"
if (Test-Path -LiteralPath $partialReferenceRepo) {
    Remove-Item -LiteralPath $partialReferenceRepo -Recurse -Force
}
$robocopyArguments = @(
    "Z:\",
    $localRoot,
    "/MIR",
    "/FFT",
    "/Z",
    "/R:2",
    "/W:1",
    "/XD",
    "Z:\.git",
    "Z:\.venv",
    "Z:\out",
    "Z:\node_modules",
    "Z:\src\clients\web\node_modules",
    "Z:\src\webview-ui\node_modules",
    "Z:\tools\ssh",
    "Z:\tools\windows-vm\runtime-cache",
    "Z:\reference_repo",
    "node_modules"
)
Write-Output ("[windows-source] mirroring source to {0}" -f $localRoot)
& robocopy.exe @robocopyArguments
if ($LASTEXITCODE -gt 7) {
    throw "Windows local source mirror failed: exit_code=$LASTEXITCODE"
}
Set-Location -LiteralPath $localRoot

if (-not $InstallDependencies) {
    Write-Output "[windows-source] source mirror is ready"
    exit 0
}

function Invoke-Bun([string[]]$Arguments) {
    Write-Output ("[windows-bun] bun {0}" -f ($Arguments -join " "))
    & bun @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Bun install failed: arguments=$($Arguments -join ' ') exit_code=$LASTEXITCODE"
    }
}

Invoke-Bun @("install", "--frozen-lockfile", "--backend=copyfile")
Invoke-Bun @("install", "--cwd", "src/clients/web", "--frozen-lockfile", "--backend=copyfile")
Invoke-Bun @("install", "--cwd", "src/webview-ui", "--frozen-lockfile", "--backend=copyfile")

foreach ($dependency in @(
    (Join-Path $localRoot "node_modules\ajv"),
    (Join-Path $localRoot "src\clients\web\node_modules\vite"),
    (Join-Path $localRoot "src\webview-ui\node_modules\vite")
)) {
    if (-not (Test-Path -LiteralPath $dependency)) {
        throw "Expected Bun dependency was not installed: $dependency"
    }
}
Write-Output "[windows-bun] all dependency roots are ready"
