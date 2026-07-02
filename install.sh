#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]-}" ]]; then
	SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
fi

DEV_LINK=false
ARGS=()
while (($#)); do
	case "$1" in
		--dev-link) DEV_LINK=true ;;
		*) ARGS+=("$1") ;;
	esac
	shift
done

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/scripts/pi-forge-install.sh" ]]; then
	if [[ "$DEV_LINK" == true ]]; then
		if ((${#ARGS[@]})); then
			exec "$SCRIPT_DIR/scripts/pi-forge-install.sh" --source-dir "$SCRIPT_DIR" --dev-link "${ARGS[@]}"
		fi
		exec "$SCRIPT_DIR/scripts/pi-forge-install.sh" --source-dir "$SCRIPT_DIR" --dev-link
	fi
	if ((${#ARGS[@]})); then
		exec "$SCRIPT_DIR/scripts/pi-forge-install.sh" "${ARGS[@]}"
	fi
	exec "$SCRIPT_DIR/scripts/pi-forge-install.sh"
fi

INSTALLER_URL="${PI_FORGE_INSTALLER_URL:-https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/scripts/pi-forge-install.sh}"
INSTALLER_PATH="$(mktemp "${TMPDIR:-/tmp}/pi-forge-install.XXXXXX")"
cleanup() {
	rm -f "$INSTALLER_PATH"
}
trap cleanup EXIT

if command -v curl >/dev/null 2>&1; then
	curl -fsSL "$INSTALLER_URL" -o "$INSTALLER_PATH"
elif command -v wget >/dev/null 2>&1; then
	wget -qO "$INSTALLER_PATH" "$INSTALLER_URL"
else
	echo "pi-forge install requires curl or wget to fetch the installer." >&2
	exit 1
fi

chmod +x "$INSTALLER_PATH"
if [[ "$DEV_LINK" == true ]]; then
	echo "--dev-link requires running install.sh from a checkout." >&2
	exit 1
fi
if ((${#ARGS[@]})); then
	exec "$INSTALLER_PATH" "${ARGS[@]}"
fi
exec "$INSTALLER_PATH"
