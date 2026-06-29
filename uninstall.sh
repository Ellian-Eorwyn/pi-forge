#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]-}" ]]; then
	SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/scripts/pi-forge-uninstall.sh" ]]; then
	exec "$SCRIPT_DIR/scripts/pi-forge-uninstall.sh" --source-dir "$SCRIPT_DIR" "$@"
fi

# When run detached from a checkout (for example piped from curl), fall back to
# the standalone uninstaller logic with no source directory. It removes the
# installed launchers and managed checkout and, with --purge-state, the agent
# state. It never deletes a development checkout.
PI_FORGE_HOME="${PI_FORGE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/pi-forge}"
INSTALL_DIR="${PI_FORGE_INSTALL_DIR:-$PI_FORGE_HOME}"
SOURCE_DIR="$INSTALL_DIR/repository"

if [[ -f "$SOURCE_DIR/scripts/pi-forge-uninstall.sh" ]]; then
	exec "$SOURCE_DIR/scripts/pi-forge-uninstall.sh" --source-dir "$SOURCE_DIR" "$@"
fi

echo "Cannot locate pi-forge-uninstall.sh. Run it from a pi-forge checkout:" >&2
echo "  ./uninstall.sh" >&2
exit 1
