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
SOURCE_DIR="$(cd -P "$(dirname "$SOURCE_PATH")" && pwd)"
RESOURCES_ONLY=false

usage() {
	cat <<'EOF'
Usage: pi-forge-update [--resources-only]

Updates the pi-forge checkout and installation. Profile-only mode updates
managed instructions, skills, extensions, prompts, and themes without
rebuilding the CLI.
EOF
}

while (($#)); do
	case "$1" in
		--resources-only) RESOURCES_ONLY=true ;;
		--help|-h) usage; exit 0 ;;
		*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
	echo "pi-forge update requires a git checkout: $SOURCE_DIR" >&2
	exit 1
fi

if [[ -n "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=no)" ]]; then
	echo "pi-forge has local tracked changes; update aborted." >&2
	exit 1
fi

OLD_HEAD="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
git -C "$SOURCE_DIR" pull --ff-only

ARGS=(--source-dir "$SOURCE_DIR" --update --old-head "$OLD_HEAD")
if [[ "$RESOURCES_ONLY" == true ]]; then
	ARGS+=(--resources-only)
fi

exec "$SOURCE_DIR/scripts/pi-forge-install.sh" "${ARGS[@]}"
