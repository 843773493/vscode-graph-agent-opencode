param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

& fsutil.exe behavior set SymlinkEvaluation L2L:1 L2R:1 R2L:1 R2R:1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to enable Windows symbolic-link evaluation: exit_code=$LASTEXITCODE"
}

$userProfile = [Environment]::GetFolderPath("UserProfile")
$uvBin = Join-Path $userProfile ".local\bin"
$bunBin = Join-Path $userProfile ".bun\bin"
$tempRoot = Join-Path $env:TEMP "boxteam-windows-runtime"
New-Item -ItemType Directory -Force -Path $tempRoot, $uvBin, $bunBin | Out-Null

function Install-OfficialScript([string]$Name, [string]$Uri, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        Write-Output ("[windows-runtime] {0} already exists: {1}" -f $Name, $Destination)
        return
    }
    $installer = Join-Path $tempRoot "$Name-install.ps1"
    Write-Output ("[windows-runtime] downloading {0} from {1}" -f $Name, $Uri)
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $installer
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installer
    if ($LASTEXITCODE -ne 0) {
        throw "Windows runtime installer failed: name=$Name exit_code=$LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath $Destination)) {
        throw "Windows runtime installer did not create expected executable: name=$Name path=$Destination"
    }
}

function Install-BunFromCache([string]$Destination) {
    $archive = "Z:\tools\windows-vm\runtime-cache\bun-windows-x64.zip"
    if (-not (Test-Path -LiteralPath $archive)) {
        return $false
    }
    $extractRoot = Join-Path $tempRoot "bun-cache"
    Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output ("[windows-runtime] using cached Bun archive sha256={0}" -f $hash)
    Expand-Archive -LiteralPath $archive -DestinationPath $extractRoot -Force
    $candidate = Get-ChildItem -LiteralPath $extractRoot -Filter "bun.exe" -File -Recurse | Select-Object -First 1
    if ($null -eq $candidate) {
        throw "cached Bun archive does not contain bun.exe: $archive"
    }
    Copy-Item -LiteralPath $candidate.FullName -Destination $Destination -Force
    return $true
}

Install-OfficialScript "uv" "https://astral.sh/uv/install.ps1" (Join-Path $uvBin "uv.exe")
$bunExecutable = Join-Path $bunBin "bun.exe"
if (-not (Test-Path -LiteralPath $bunExecutable)) {
    if (-not (Install-BunFromCache $bunExecutable)) {
        Install-OfficialScript "bun" "https://bun.sh/install.ps1" $bunExecutable
    }
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ([string]::IsNullOrWhiteSpace($userPath)) {
    $userPath = ""
}
$pathEntries = @($userPath -split ";" | Where-Object { $_ })
foreach ($entry in @($uvBin, $bunBin)) {
    if (-not ($pathEntries | Where-Object { $_ -ieq $entry })) {
        $pathEntries += $entry
    }
}
$newUserPath = $pathEntries -join ";"
[Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
$env:Path = "$uvBin;$bunBin;$env:Path"

& (Join-Path $uvBin "uv.exe") --version
if ($LASTEXITCODE -ne 0) {
    throw "uv version check failed: exit_code=$LASTEXITCODE"
}
& (Join-Path $bunBin "bun.exe") --version
if ($LASTEXITCODE -ne 0) {
    throw "bun version check failed: exit_code=$LASTEXITCODE"
}
