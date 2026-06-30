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

PI_FORGE_HOME="${PI_FORGE_HOME:-$HOME/.pi-forge}"
INSTALL_DIR="${PI_FORGE_INSTALL_DIR:-$PI_FORGE_HOME}"
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/scripts/pi-forge-install.sh" && "$DEV_LINK" == true ]]; then
	if ((${#ARGS[@]} > 0)); then
		exec "$SCRIPT_DIR/scripts/pi-forge-install.sh" --source-dir "$SCRIPT_DIR" --dev-link "${ARGS[@]}"
	fi
	exec "$SCRIPT_DIR/scripts/pi-forge-install.sh" --source-dir "$SCRIPT_DIR" --dev-link
fi
SOURCE_DIR="$INSTALL_DIR/repository"

command -v git >/dev/null 2>&1 || { echo "pi-forge requires git." >&2; exit 1; }

if [[ -n "${PI_FORGE_REPOSITORY:-}" ]]; then
	REPOSITORY="$PI_FORGE_REPOSITORY"
elif [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/scripts/pi-forge-install.sh" ]]; then
	REPOSITORY="$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || printf '%s' "$SCRIPT_DIR")"
else
	REPOSITORY="https://github.com/Ellian-Eorwyn/pi-forge.git"
fi

if [[ -e "$SOURCE_DIR" ]]; then
	echo "Install checkout already exists: $SOURCE_DIR" >&2
	echo "Run $INSTALL_DIR/bin/pi-forge-update, or remove the checkout before reinstalling." >&2
	exit 1
fi

mkdir -p "$INSTALL_DIR"
git clone "$REPOSITORY" "$SOURCE_DIR"
if ((${#ARGS[@]} > 0)); then
	exec "$SOURCE_DIR/scripts/pi-forge-install.sh" --source-dir "$SOURCE_DIR" "${ARGS[@]}"
fi
exec "$SOURCE_DIR/scripts/pi-forge-install.sh" --source-dir "$SOURCE_DIR"
