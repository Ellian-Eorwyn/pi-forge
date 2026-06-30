$ErrorActionPreference = "Stop"

param(
    [string]$SourceDir = "",
    [string]$BinDir = "",
    [string]$AgentDir = "",
    [string]$OldHead = "",
    [switch]$DevLink,
    [switch]$Update,
    [switch]$ResourcesOnly
)

# Use environmental variables if missing
$PiForgeHome = $env:PI_FORGE_HOME

if ([string]::IsNullOrEmpty($SourceDir) -or -not (Test-Path (Join-Path $SourceDir "package.json"))) {
    Write-Error "A valid -SourceDir is required and must contain package.json."
    exit 1
}

$SourceDir = (Resolve-Path $SourceDir).ProviderPath

if ([string]::IsNullOrEmpty($PiForgeHome)) {
    if ((Split-Path $SourceDir -Leaf) -eq "repository") {
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

$NpmCacheDir = $env:PI_FORGE_NPM_CACHE
if ([string]::IsNullOrEmpty($NpmCacheDir)) { $NpmCacheDir = Join-Path $AgentDir "npm-cache" }

$PlaywrightBrowsersDir = $env:PI_FORGE_PLAYWRIGHT_BROWSERS
if ([string]::IsNullOrEmpty($PlaywrightBrowsersDir)) { $PlaywrightBrowsersDir = Join-Path $AgentDir "playwright-browsers" }

if (-not (Get-Command "node" -ErrorAction SilentlyContinue)) {
    Write-Error "pi-forge requires Node.js 22.19 or newer."
    exit 1
}
if (-not (Get-Command "npm" -ErrorAction SilentlyContinue)) {
    Write-Error "pi-forge requires npm."
    exit 1
}

$nodeVerCheck = @'
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 19)) process.exit(1);
'@
node -e $nodeVerCheck
if ($LASTEXITCODE -ne 0) {
    Write-Error "pi-forge requires Node.js 22.19 or newer."
    exit 1
}

$NeedsBuild = $true
$NeedsInstall = $true
$BuildRevisionFile = Join-Path $AgentDir ".pi-forge-build-revision"
$CompareRevision = $OldHead

if (Test-Path $BuildRevisionFile) {
    $CompareRevision = Get-Content $BuildRevisionFile -Raw
}

if ($Update -and -not [string]::IsNullOrEmpty($CompareRevision) -and (Test-Path (Join-Path $SourceDir ".git"))) {
    $ChangedFiles = git -C $SourceDir diff --name-only $CompareRevision HEAD
    $CoreFiles = $ChangedFiles | Where-Object { $_ -match "^(packages/|package(-lock)?\.json$|tsconfig|scripts/)" -and $_ -notmatch "^scripts/(configure-pi-forge\.mjs|pi-forge-(install|run)\.ps1)$" }
    
    if (-not $CoreFiles) {
        $NeedsBuild = $false
        $NeedsInstall = $false
    } elseif (-not ($CoreFiles | Where-Object { $_ -match "(^|/)package(-lock)?\.json$" })) {
        $NeedsInstall = $false
    }
}

if ($ResourcesOnly) {
    $NeedsBuild = $false
    $NeedsInstall = $false
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null

if ($NeedsInstall) {
    $env:npm_config_cache = $NpmCacheDir
    $proc = Start-Process npm -ArgumentList "ci", "--ignore-scripts" -WorkingDirectory $SourceDir -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) { Write-Error "npm ci failed."; exit 1 }

    $playwrightBin = Join-Path $SourceDir "node_modules\.bin\playwright.cmd"
    if (Test-Path $playwrightBin) {
        $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir
        $proc = Start-Process $playwrightBin -ArgumentList "install", "chromium" -WorkingDirectory $SourceDir -NoNewWindow -Wait -PassThru
        if ($proc.ExitCode -ne 0) {
            Write-Warning "Chromium download failed; web-collection rendered capture will be unavailable until 'playwright install chromium' succeeds."
        }
    }
}

if ($NeedsBuild) {
    $proc = Start-Process npm -ArgumentList "run", "build:install" -WorkingDirectory $SourceDir -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) { Write-Error "Build failed."; exit 1 }

    if (Test-Path (Join-Path $SourceDir ".git")) {
        git -C $SourceDir rev-parse HEAD | Out-File -FilePath $BuildRevisionFile -Encoding utf8
    }
} elseif (-not (Test-Path (Join-Path $SourceDir "packages\coding-agent\dist\cli.js"))) {
    Write-Error "The pi-forge CLI is not built. Run install.ps1 without -ResourcesOnly."
    exit 1
}

$proc = Start-Process node -ArgumentList (Join-Path $SourceDir "scripts\configure-pi-forge.mjs"), $AgentDir, (Join-Path $SourceDir "forge") -WorkingDirectory $SourceDir -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) { Write-Error "Configuration failed."; exit 1 }

# Create launchers
$runSh = Join-Path $SourceDir "scripts\pi-forge-run.ps1"
$runMcpSh = Join-Path $SourceDir "scripts\pi-forge-mcp-run.ps1"
$updateSh = Join-Path $SourceDir "update.ps1"

# Generate wrapper .cmd scripts so users can just type `pi-forge` in cmd or powershell
"@powershell -NoProfile -ExecutionPolicy Bypass -File `"$runSh`" %*" | Out-File -FilePath (Join-Path $BinDir "pi-forge.cmd") -Encoding ASCII
"@powershell -NoProfile -ExecutionPolicy Bypass -File `"$runMcpSh`" %*" | Out-File -FilePath (Join-Path $BinDir "pi-forge-mcp.cmd") -Encoding ASCII
"@powershell -NoProfile -ExecutionPolicy Bypass -File `"$updateSh`" %*" | Out-File -FilePath (Join-Path $BinDir "pi-forge-update.cmd") -Encoding ASCII

# Modify Windows Path
$PathUpdated = $false
try {
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not ($UserPath -split ';' | Where-Object { $_ -eq $BinDir })) {
        $NewPath = $UserPath + ";" + $BinDir
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        $PathUpdated = $true
    }
} catch {
    Write-Warning "Could not update User Path automatically."
}

Write-Host "pi-forge is installed."
Write-Host "  CLI: $(Join-Path $BinDir 'pi-forge.cmd')"
Write-Host "  MCP: $(Join-Path $BinDir 'pi-forge-mcp.cmd')"
Write-Host "  Updater: $(Join-Path $BinDir 'pi-forge-update.cmd')"
Write-Host "  State: $AgentDir"

if ($PathUpdated) {
    Write-Host "Added $BinDir to your User PATH. Open a new shell before running pi-forge."
} else {
    Write-Host "Ensure $BinDir is in your PATH before running pi-forge."
}
