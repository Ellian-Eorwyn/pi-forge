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

$uninstallerUrl = $env:PI_FORGE_UNINSTALLER_URL
if ([string]::IsNullOrEmpty($uninstallerUrl)) {
    $uninstallerUrl = "https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/scripts/pi-forge-uninstall.ps1"
}
$uninstallerPath = Join-Path ([System.IO.Path]::GetTempPath()) ("pi-forge-uninstall-" + [System.Guid]::NewGuid().ToString("N") + ".ps1")

try {
    Invoke-WebRequest -UseBasicParsing -Uri $uninstallerUrl -OutFile $uninstallerPath
    & $uninstallerPath @args
    exit $LASTEXITCODE
} finally {
    Remove-Item -Path $uninstallerPath -Force -ErrorAction SilentlyContinue
}
