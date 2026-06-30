#!/usr/bin/env bash
set -euo pipefail

SOURCE_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE_PATH" ]]; do
	SOURCE_PATH_DIR="$(cd -P "$(dirname "$SOURCE_PATH")" && pwd)"
	SOURCE_PATH="$(readlink "$SOURCE_PATH")"
	if [[ "$SOURCE_PATH" != /* ]]; then
		SOURCE_PATH="$SOURCE_PATH_DIR/$SOURCE_PATH"
	fi
done
SOURCE_DIR="$(cd -P "$(dirname "$SOURCE_PATH")/.." && pwd)"
if [[ -z "${PI_FORGE_HOME:-}" ]]; then
	if [[ "$(basename "$SOURCE_DIR")" == "repository" ]]; then
		PI_FORGE_HOME="$(cd "$SOURCE_DIR/.." && pwd)"
	else
		PI_FORGE_HOME="$HOME/.pi-forge"
	fi
fi
export PI_CODING_AGENT_DIR="${PI_FORGE_AGENT_DIR:-$PI_FORGE_HOME/agent}"

exec node "$SOURCE_DIR/scripts/pi-forge-mcp-server.mjs" "$@"
