$ErrorActionPreference = "Stop"

$SourceDir = (Split-Path $PSScriptRoot -Parent)
$PiForgeHome = $env:PI_FORGE_HOME

if ([string]::IsNullOrEmpty($PiForgeHome)) {
    if ((Split-Path $SourceDir -Leaf) -eq "repository") {
        $PiForgeHome = Split-Path $SourceDir -Parent
    } else {
        $PiForgeHome = Join-Path $HOME ".pi-forge"
    }
}

$env:PI_CODING_AGENT_DIR = $env:PI_FORGE_AGENT_DIR
if ([string]::IsNullOrEmpty($env:PI_CODING_AGENT_DIR)) {
    $env:PI_CODING_AGENT_DIR = Join-Path $PiForgeHome "agent"
}

if ([string]::IsNullOrEmpty($env:PI_SKIP_VERSION_CHECK)) {
    $env:PI_SKIP_VERSION_CHECK = "1"
}

if ([string]::IsNullOrEmpty($env:PLAYWRIGHT_BROWSERS_PATH)) {
    if (-not [string]::IsNullOrEmpty($env:PI_FORGE_PLAYWRIGHT_BROWSERS)) {
        $env:PLAYWRIGHT_BROWSERS_PATH = $env:PI_FORGE_PLAYWRIGHT_BROWSERS
    } else {
        $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:PI_CODING_AGENT_DIR "playwright-browsers"
    }
}

if ([string]::IsNullOrEmpty($env:FORGE_SEARXNG_URL)) {
    $env:FORGE_SEARXNG_URL = "http://llms/searxng"
}

$cliPath = Join-Path $SourceDir "packages\coding-agent\dist\cli.js"
& node $cliPath @args
exit $LASTEXITCODE
