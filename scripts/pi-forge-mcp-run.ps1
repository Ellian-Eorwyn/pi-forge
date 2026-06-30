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

$mcpPath = Join-Path $SourceDir "scripts\pi-forge-mcp-server.mjs"
& node $mcpPath @args
exit $LASTEXITCODE
