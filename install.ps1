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
    }
    else {
        $ArgsList += $args[$i]
    }
}

$PiForgeHome = $env:PI_FORGE_HOME
if ([string]::IsNullOrEmpty($PiForgeHome)) {
    $PiForgeHome = Join-Path $HOME ".pi-forge"
}

$InstallDir = $env:PI_FORGE_INSTALL_DIR
if ([string]::IsNullOrEmpty($InstallDir)) {
    $InstallDir = $PiForgeHome
}

if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "scripts\pi-forge-install.ps1")) -and $DevLink) {
    $installScript = Join-Path $ScriptDir "scripts\pi-forge-install.ps1"
    & $installScript -SourceDir $ScriptDir -DevLink @ArgsList
    exit $LASTEXITCODE
}

$SourceDir = Join-Path $InstallDir "repository"

if (-not (Get-Command "git" -ErrorAction SilentlyContinue)) {
    Write-Error "pi-forge requires git."
    exit 1
}

$Repository = $env:PI_FORGE_REPOSITORY
if ([string]::IsNullOrEmpty($Repository)) {
    if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "scripts\pi-forge-install.ps1"))) {
        $Repository = git -C $ScriptDir remote get-url origin 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($Repository)) {
            $Repository = $ScriptDir
        }
    } else {
        $Repository = "https://github.com/Ellian-Eorwyn/pi-forge.git"
    }
}

if (Test-Path $SourceDir) {
    Write-Error "Install checkout already exists: $SourceDir`nRun pi-forge-update, or remove the checkout before reinstalling."
    exit 1
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
git clone $Repository $SourceDir

$installScript = Join-Path $SourceDir "scripts\pi-forge-install.ps1"
& $installScript -SourceDir $SourceDir @ArgsList
exit $LASTEXITCODE
