$ErrorActionPreference = "Stop"

$ScriptDir = ""
if ($PSScriptRoot) {
    $ScriptDir = $PSScriptRoot
}

$ResourcesOnly = $false
$ArgsList = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq "--resources-only") {
        $ResourcesOnly = $true
    }
    else {
        $ArgsList += $args[$i]
    }
}

if (-not (Test-Path (Join-Path $ScriptDir ".git"))) {
    Write-Error "This legacy updater requires a git checkout: $ScriptDir. Use the installed pi-forge-update package command instead."
    exit 1
}

$gitStatus = git -C $ScriptDir status --porcelain --untracked-files=no
if (-not [string]::IsNullOrWhiteSpace($gitStatus)) {
    Write-Error "pi-forge has local tracked changes; update aborted."
    exit 1
}

$OldHead = git -C $ScriptDir rev-parse HEAD
git -C $ScriptDir pull --ff-only

$installArgs = @("-SourceDir", $ScriptDir, "-Update", "-OldHead", $OldHead)
if ($ResourcesOnly) {
    $installArgs += "-ResourcesOnly"
}

$installScript = Join-Path $ScriptDir "scripts\pi-forge-install.ps1"
& $installScript @installArgs @ArgsList
exit $LASTEXITCODE
