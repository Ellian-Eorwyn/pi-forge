#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]-}" ]]; then
	SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/scripts/pi-forge-install.sh" ]]; then
	exec "$SCRIPT_DIR/scripts/pi-forge-install.sh" --source-dir "$SCRIPT_DIR" "$@"
fi

INSTALL_DIR="${PI_FORGE_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/pi-forge}"
REPOSITORY="${PI_FORGE_REPOSITORY:-https://github.com/Ellian-Eorwyn/pi-forge.git}"
SOURCE_DIR="$INSTALL_DIR/repository"

command -v git >/dev/null 2>&1 || { echo "pi-forge requires git." >&2; exit 1; }

if [[ -e "$SOURCE_DIR" ]]; then
	echo "Install checkout already exists: $SOURCE_DIR" >&2
	echo "Run pi-forge-update, or remove the checkout before reinstalling." >&2
	exit 1
fi

mkdir -p "$INSTALL_DIR"
git clone "$REPOSITORY" "$SOURCE_DIR"
exec "$SOURCE_DIR/scripts/pi-forge-install.sh" --source-dir "$SOURCE_DIR" "$@"
