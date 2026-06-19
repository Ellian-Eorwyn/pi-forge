#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PI_CODING_AGENT_DIR="${PI_FORGE_AGENT_DIR:-$HOME/.pi-forge/agent}"
export PI_SKIP_VERSION_CHECK="${PI_SKIP_VERSION_CHECK:-1}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${PI_FORGE_PLAYWRIGHT_BROWSERS:-$PI_CODING_AGENT_DIR/playwright-browsers}}"

exec node "$SOURCE_DIR/packages/coding-agent/dist/cli.js" "$@"
