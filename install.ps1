$ErrorActionPreference = "Stop"

$ScriptDir = ""
if ($PSScriptRoot) {
    $ScriptDir = $PSScriptRoot
}

$DevLink = $false
$ArgsList = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq "--dev-link") {
        $DevLink = $true
    } else {
        $ArgsList += $args[$i]
    }
}

$localInstaller = ""
if ($ScriptDir) {
    $candidate = Join-Path $ScriptDir "scripts\pi-forge-install.ps1"
    if (Test-Path $candidate) {
        $localInstaller = $candidate
    }
}

if (-not [string]::IsNullOrEmpty($localInstaller)) {
    if ($DevLink) {
        & $localInstaller -SourceDir $ScriptDir -DevLink @ArgsList
    } else {
        & $localInstaller @ArgsList
    }
    exit $LASTEXITCODE
}

if ($DevLink) {
    Write-Error "--dev-link requires running install.ps1 from a checkout."
    exit 1
}

$installerUrl = $env:PI_FORGE_INSTALLER_URL
if ([string]::IsNullOrEmpty($installerUrl)) {
    $installerUrl = "https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/scripts/pi-forge-install.ps1"
}
$installerPath = Join-Path ([System.IO.Path]::GetTempPath()) ("pi-forge-install-" + [System.Guid]::NewGuid().ToString("N") + ".ps1")

try {
    Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $installerPath
    & $installerPath @ArgsList
    exit $LASTEXITCODE
} finally {
    Remove-Item -Path $installerPath -Force -ErrorAction SilentlyContinue
}
