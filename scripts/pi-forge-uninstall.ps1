$ErrorActionPreference = "Stop"

param(
    [string]$BinDir = "",
    [string]$AgentDir = "",
    [string]$InstallDir = "",
    [string]$SourceDir = "",
    [switch]$PurgeState,
    [switch]$DryRun,
    [switch]$Yes
)

if (-not [string]::IsNullOrEmpty($SourceDir)) {
    $SourceDir = (Resolve-Path $SourceDir -ErrorAction SilentlyContinue).ProviderPath
}

$PiForgeHome = $env:PI_FORGE_HOME
if ([string]::IsNullOrEmpty($PiForgeHome)) {
    if (-not [string]::IsNullOrEmpty($SourceDir) -and (Split-Path $SourceDir -Leaf) -eq "repository") {
        $PiForgeHome = Split-Path $SourceDir -Parent
    } else {
        $PiForgeHome = Join-Path $HOME ".pi-forge"
    }
}

if ([string]::IsNullOrEmpty($BinDir)) {
    $BinDir = $env:PI_FORGE_BIN_DIR
    if ([string]::IsNullOrEmpty($BinDir)) { $BinDir = Join-Path $PiForgeHome "bin" }
}
if ([string]::IsNullOrEmpty($AgentDir)) {
    $AgentDir = $env:PI_FORGE_AGENT_DIR
    if ([string]::IsNullOrEmpty($AgentDir)) { $AgentDir = Join-Path $PiForgeHome "agent" }
}
if ([string]::IsNullOrEmpty($InstallDir)) {
    $InstallDir = $env:PI_FORGE_INSTALL_DIR
    if ([string]::IsNullOrEmpty($InstallDir)) { $InstallDir = Join-Path $PiForgeHome "repository" }
}

$Launchers = @("pi-forge.cmd", "pi-forge-mcp.cmd", "pi-forge-update.cmd")
$Planned = @()
$Warnings = @()

# Evaluate Launchers
$LaunchersToRemove = @()
foreach ($name in $Launchers) {
    $launcher = Join-Path $BinDir $name
    if (Test-Path $launcher) {
        $LaunchersToRemove += $launcher
        $Planned += "Remove launcher: $launcher"
    }
}

# Evaluate Install Dir
$RemoveInstallDir = $false
if (Test-Path $InstallDir) {
    if (-not [string]::IsNullOrEmpty($SourceDir) -and (Resolve-Path $InstallDir).ProviderPath -eq $SourceDir) {
        $Warnings += "Skipping managed checkout: it contains this checkout ($SourceDir)."
    } elseif (Test-Path (Join-Path $InstallDir "scripts\pi-forge-install.ps1")) {
        $RemoveInstallDir = $true
        $Planned += "Remove managed checkout: $InstallDir"
    } else {
        $Warnings += "Skipping $InstallDir: does not look like a managed pi-forge checkout."
    }
}

# Evaluate Agent Dir
$RemoveAgentDir = $false
if ($PurgeState) {
    if (-not (Test-Path $AgentDir)) {
        $Warnings += "Agent directory not found (nothing to purge): $AgentDir"
    } else {
        $RemoveAgentDir = $true
        $Planned += "Purge agent state: $AgentDir"
    }
} elseif (Test-Path $AgentDir) {
    $Planned += "Preserve agent state: $AgentDir"
}

if ($LaunchersToRemove.Count -eq 0 -and -not $RemoveInstallDir -and -not $RemoveAgentDir) {
    Write-Host "Nothing to uninstall."
    foreach ($w in $Warnings) { Write-Host "  - $w" }
    exit 0
}

Write-Host "pi-forge uninstall plan:"
foreach ($item in $Planned) { Write-Host "  - $item" }
if ($Warnings.Count -gt 0) {
    Write-Host "Notes:"
    foreach ($w in $Warnings) { Write-Host "  - $w" }
}

if ($DryRun) {
    Write-Host "Dry run: no changes made."
    exit 0
}

if (-not $Yes) {
    $prompt = "Proceed?"
    if ($RemoveAgentDir) { $prompt = "Proceed? This permanently deletes credentials and sessions." }
    $reply = Read-Host "$prompt [y/N]"
    if ($reply -notmatch "^[yY]([eE][sS])?$") {
        Write-Host "Aborted."
        exit 1
    }
}

foreach ($launcher in $LaunchersToRemove) {
    Remove-Item -Path $launcher -Force -ErrorAction SilentlyContinue
    Write-Host "Removed $launcher"
}

if ($RemoveInstallDir) {
    Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Removed $InstallDir"
}

if ($RemoveAgentDir) {
    Remove-Item -Path $AgentDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Removed $AgentDir"
}

Write-Host "`npi-forge uninstalled."
if (-not $RemoveAgentDir -and (Test-Path $AgentDir)) {
    Write-Host "  Agent state kept at: $AgentDir (re-run with -PurgeState to remove it)."
}
