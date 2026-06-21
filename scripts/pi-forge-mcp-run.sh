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
export PI_CODING_AGENT_DIR="${PI_FORGE_AGENT_DIR:-$HOME/.pi-forge/agent}"

exec node "$SOURCE_DIR/scripts/pi-forge-mcp-server.mjs" "$@"
