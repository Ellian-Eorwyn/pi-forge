$ErrorActionPreference = "Stop"

$ScriptDir = ""
if ($PSScriptRoot) {
    $ScriptDir = $PSScriptRoot
}

if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "scripts\pi-forge-uninstall.ps1"))) {
    $uninstallScript = Join-Path $ScriptDir "scripts\pi-forge-uninstall.ps1"
    & $uninstallScript -SourceDir $ScriptDir @args
    exit $LASTEXITCODE
}

$PiForgeHome = $env:PI_FORGE_HOME
if ([string]::IsNullOrEmpty($PiForgeHome)) {
    $PiForgeHome = Join-Path $HOME ".pi-forge"
}

$InstallDir = $env:PI_FORGE_INSTALL_DIR
if ([string]::IsNullOrEmpty($InstallDir)) {
    $InstallDir = $PiForgeHome
}

$SourceDir = Join-Path $InstallDir "repository"

if (Test-Path (Join-Path $SourceDir "scripts\pi-forge-uninstall.ps1")) {
    $uninstallScript = Join-Path $SourceDir "scripts\pi-forge-uninstall.ps1"
    & $uninstallScript -SourceDir $SourceDir @args
    exit $LASTEXITCODE
}

Write-Error "Cannot locate pi-forge-uninstall.ps1. Run it from a pi-forge checkout:`n  .\uninstall.ps1"
exit 1
