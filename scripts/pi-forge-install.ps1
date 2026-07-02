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

$PackageName = "@ellian-eorwyn/pi-forge"
$DefaultPackageSpec = "$PackageName@latest"
$PiPackageName = "@earendil-works/pi-coding-agent"
$DefaultPiPackageSpec = "$PiPackageName@latest"

function Invoke-Checked {
    param(
        [string]$Command,
        [string[]]$Arguments,
        [string]$WorkingDirectory = ""
    )
    if ([string]::IsNullOrEmpty($WorkingDirectory)) {
        & $Command @Arguments
    } else {
        & $Command @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Command failed: $Command $($Arguments -join ' ')"
        exit $LASTEXITCODE
    }
}

function Write-Launcher {
    param(
        [string]$CommandName,
        [string]$BinDir,
        [string]$TargetDir
    )
    $cmdPath = Join-Path $BinDir "$CommandName.cmd"
    $psPath = Join-Path $BinDir "$CommandName.ps1"
    $targetCmd = Join-Path $TargetDir "$CommandName.cmd"
    $targetPs = Join-Path $TargetDir "$CommandName.ps1"
    "@ECHO off`r`n`"$targetCmd`" %*`r`n" | Out-File -FilePath $cmdPath -Encoding ASCII
    "& `"$targetPs`" @args`n" | Out-File -FilePath $psPath -Encoding UTF8
}

function Write-ScriptLauncher {
    param(
        [string]$CommandName,
        [string]$BinDir,
        [string]$ScriptPath
    )
    $cmdPath = Join-Path $BinDir "$CommandName.cmd"
    $psPath = Join-Path $BinDir "$CommandName.ps1"
    "@powershell -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" %*`r`n" | Out-File -FilePath $cmdPath -Encoding ASCII
    "& `"$ScriptPath`" @args`n" | Out-File -FilePath $psPath -Encoding UTF8
}

function Resolve-PackageRoot {
    param([string]$AppDir)
    $script = 'const { createRequire } = require("node:module"); const { dirname } = require("node:path"); const req = createRequire(process.cwd() + "/package.json"); console.log(dirname(req.resolve("@ellian-eorwyn/pi-forge/package.json")));'
    Push-Location $AppDir
    try {
        $packageRoot = node -e $script
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($packageRoot)) {
            Write-Error "Could not resolve installed pi-forge package."
            exit 1
        }
        return $packageRoot.Trim()
    } finally {
        Pop-Location
    }
}

function New-LocalPackageSpec {
    param(
        [string]$PackageRoot,
        [string]$AppDir,
        [string]$NpmCacheDir
    )
    $packageCacheDir = Join-Path $AppDir "package-cache"
    New-Item -ItemType Directory -Force -Path $packageCacheDir | Out-Null
    New-Item -ItemType Directory -Force -Path $NpmCacheDir | Out-Null
    $env:npm_config_cache = $NpmCacheDir
    Push-Location $PackageRoot
    try {
        $packOutput = npm pack --json --pack-destination $packageCacheDir
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Could not pack local pi-forge package."
            exit 1
        }
        $packed = $packOutput | ConvertFrom-Json
        return "file:" + (Join-Path $packageCacheDir $packed[0].filename)
    } finally {
        Pop-Location
    }
}

$PiForgeHome = $env:PI_FORGE_HOME
if ([string]::IsNullOrEmpty($PiForgeHome)) {
    $PiForgeHome = Join-Path $HOME ".pi-forge"
}

$AppDir = $env:PI_FORGE_INSTALL_DIR
if ([string]::IsNullOrEmpty($AppDir)) { $AppDir = Join-Path $PiForgeHome "app" }

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

$PackageSpec = $env:PI_FORGE_PACKAGE_SPEC
$PackageSpecExplicit = -not [string]::IsNullOrEmpty($PackageSpec)
if (-not $PackageSpecExplicit) { $PackageSpec = $DefaultPackageSpec }
$PiPackageSpec = $env:PI_FORGE_PI_PACKAGE_SPEC
if ([string]::IsNullOrEmpty($PiPackageSpec)) { $PiPackageSpec = $DefaultPiPackageSpec }

if (-not (Get-Command "node" -ErrorAction SilentlyContinue)) {
    Write-Error "pi-forge requires Node.js 22.19 or newer."
    exit 1
}
if (-not (Get-Command "npm" -ErrorAction SilentlyContinue)) {
    Write-Error "pi-forge requires npm."
    exit 1
}

$nodeVerCheck = 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 19)) process.exit(1);'
node -e $nodeVerCheck
if ($LASTEXITCODE -ne 0) {
    Write-Error "pi-forge requires Node.js 22.19 or newer."
    exit 1
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
New-Item -ItemType Directory -Force -Path $NpmCacheDir | Out-Null

if ($DevLink) {
    if ([string]::IsNullOrEmpty($SourceDir) -or -not (Test-Path (Join-Path $SourceDir "package.json"))) {
        Write-Error "A valid -SourceDir is required with -DevLink."
        exit 1
    }
    $SourceDir = (Resolve-Path $SourceDir).ProviderPath
    if (-not $ResourcesOnly) {
        $env:npm_config_cache = $NpmCacheDir
        Push-Location $SourceDir
        try {
            Invoke-Checked -Command "npm" -Arguments @("ci", "--ignore-scripts")
            Invoke-Checked -Command "npm" -Arguments @("run", "build:install")
        } finally {
            Pop-Location
        }
    }
    if (-not (Test-Path (Join-Path $SourceDir "packages\coding-agent\dist\cli.js"))) {
        Write-Error "The pi-forge CLI is not built. Run install.ps1 -DevLink without -ResourcesOnly."
        exit 1
    }
    Invoke-Checked -Command "node" -Arguments @((Join-Path $SourceDir "forge\scripts\configure-pi-forge.mjs"), $AgentDir, (Join-Path $SourceDir "forge"))
    Write-ScriptLauncher "pi-forge" $BinDir (Join-Path $SourceDir "scripts\pi-forge-run.ps1")
    Write-ScriptLauncher "pi-forge-mcp" $BinDir (Join-Path $SourceDir "scripts\pi-forge-mcp-run.ps1")
    Write-ScriptLauncher "pi-forge-update" $BinDir (Join-Path $SourceDir "update.ps1")
    $PackageRoot = Join-Path $SourceDir "forge"
} else {
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    $appPackageJson = Join-Path $AppDir "package.json"
    if (-not (Test-Path $appPackageJson)) {
        (@{ private = $true; dependencies = @{} } | ConvertTo-Json -Depth 4) + "`n" | Out-File -FilePath $appPackageJson -Encoding UTF8
    }
    if (-not $PackageSpecExplicit -and -not [string]::IsNullOrEmpty($SourceDir) -and (Test-Path (Join-Path $SourceDir "forge\package.json"))) {
        $PackageSpec = New-LocalPackageSpec -PackageRoot (Join-Path $SourceDir "forge") -AppDir $AppDir -NpmCacheDir $NpmCacheDir
    }
    $env:npm_config_cache = $NpmCacheDir
    Invoke-Checked -Command "npm" -Arguments @("--prefix", $AppDir, "install", "--omit=dev", "--ignore-scripts", $PackageSpec)
    Invoke-Checked -Command "npm" -Arguments @("--prefix", $AppDir, "install", "--omit=dev", "--ignore-scripts", $PiPackageSpec)
	$PackageRoot = Resolve-PackageRoot $AppDir
	Invoke-Checked -Command "node" -Arguments @((Join-Path $PackageRoot "scripts\configure-pi-forge.mjs"), $AgentDir, $PackageRoot)
    Write-Launcher "pi-forge" $BinDir (Join-Path $AppDir "node_modules\.bin")
    Write-Launcher "pi-forge-mcp" $BinDir (Join-Path $AppDir "node_modules\.bin")
    Write-Launcher "pi-forge-update" $BinDir (Join-Path $AppDir "node_modules\.bin")

    if (-not [string]::IsNullOrEmpty($SourceDir)) {
        $managedRepository = Join-Path $PiForgeHome "repository"
        if ((Test-Path $managedRepository) -and ((Resolve-Path $managedRepository).ProviderPath -eq (Resolve-Path $SourceDir).ProviderPath)) {
            Remove-Item -Path $managedRepository -Recurse -Force
        }
    }
}

$PathUpdated = $false
try {
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not ($UserPath -split ';' | Where-Object { $_ -eq $BinDir })) {
        $NewPath = if ([string]::IsNullOrEmpty($UserPath)) { $BinDir } else { $UserPath + ";" + $BinDir }
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
Write-Host "  Package: $PackageRoot"

if ($PathUpdated) {
    Write-Host "Added $BinDir to your User PATH. Open a new shell before running pi-forge."
} else {
    Write-Host "Ensure $BinDir is in your PATH before running pi-forge."
}
