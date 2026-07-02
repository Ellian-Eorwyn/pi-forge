#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]-}" ]]; then
	SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/scripts/pi-forge-uninstall.sh" ]]; then
	exec "$SCRIPT_DIR/scripts/pi-forge-uninstall.sh" --source-dir "$SCRIPT_DIR" "$@"
fi

UNINSTALLER_URL="${PI_FORGE_UNINSTALLER_URL:-https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/scripts/pi-forge-uninstall.sh}"
UNINSTALLER_PATH="$(mktemp "${TMPDIR:-/tmp}/pi-forge-uninstall.XXXXXX")"
cleanup() {
	rm -f "$UNINSTALLER_PATH"
}
trap cleanup EXIT

if command -v curl >/dev/null 2>&1; then
	curl -fsSL "$UNINSTALLER_URL" -o "$UNINSTALLER_PATH"
elif command -v wget >/dev/null 2>&1; then
	wget -qO "$UNINSTALLER_PATH" "$UNINSTALLER_URL"
else
	echo "pi-forge uninstall requires curl or wget to fetch the uninstaller." >&2
	exit 1
fi

chmod +x "$UNINSTALLER_PATH"
exec "$UNINSTALLER_PATH" "$@"
