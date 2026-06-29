#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=""
OLD_HEAD=""
UPDATE=false
RESOURCES_ONLY=false
PI_FORGE_HOME="${PI_FORGE_HOME:-}"
BIN_DIR="${PI_FORGE_BIN_DIR:-}"
AGENT_DIR="${PI_FORGE_AGENT_DIR:-}"
NPM_CACHE_DIR="${PI_FORGE_NPM_CACHE:-}"
PLAYWRIGHT_BROWSERS_DIR="${PI_FORGE_PLAYWRIGHT_BROWSERS:-}"

usage() {
	cat <<'EOF'
Usage: scripts/pi-forge-install.sh --source-dir <path> [options]

Options:
  --bin-dir <path>       Launcher directory (default: $PI_FORGE_HOME/bin)
  --agent-dir <path>     Isolated pi-forge state directory (default: $PI_FORGE_HOME/agent)
  --update               Update an existing installation
  --old-head <commit>    Previous revision used to detect core changes
  --resources-only       Update the profile without dependencies or a CLI rebuild
EOF
}

canonical_existing() {
	local path="$1"
	if [[ -e "$path" ]]; then
		(cd "$path" 2>/dev/null && pwd) || printf '%s' "$path"
	else
		printf '%s' "$path"
	fi
}

remove_legacy_launcher() {
	local launcher="$1"
	local old_source_dir="$2"
	[[ -L "$launcher" ]] || return 0
	local target
	target="$(readlink "$launcher" 2>/dev/null || true)"
	[[ -n "$target" ]] || return 0
	if [[ "$target" != /* ]]; then
		target="$(dirname "$launcher")/$target"
	fi
	case "$target" in
		"$old_source_dir"/scripts/pi-forge-run.sh | "$old_source_dir"/scripts/pi-forge-mcp-run.sh | "$old_source_dir"/update.sh)
			rm -f "$launcher"
			;;
	esac
}

migrate_legacy_default_install() {
	[[ -z "${PI_FORGE_HOME:-}" && -z "${PI_FORGE_INSTALL_DIR:-}" ]] || return 0
	local data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
	local old_home="$data_home/pi-forge"
	local new_home="$data_home/pi-vault"
	local old_source="$old_home/repository"
	[[ -d "$old_source" ]] || return 0
	[[ "$(canonical_existing "$SOURCE_DIR")" == "$(canonical_existing "$old_source")" ]] || return 0
	if [[ -e "$new_home" ]]; then
		echo "Cannot migrate pi-forge install: target already exists: $new_home" >&2
		echo "Move or remove it, or set PI_FORGE_HOME to choose a different install home." >&2
		exit 1
	fi
	mkdir -p "$(dirname "$new_home")"
	mv "$old_home" "$new_home"
	SOURCE_DIR="$new_home/repository"
	PI_FORGE_HOME="$new_home"

	local old_state="$HOME/.pi-forge"
	if [[ -d "$old_state" ]]; then
		for entry in agent transcription; do
			if [[ -e "$old_state/$entry" ]]; then
				if [[ -e "$new_home/$entry" ]]; then
					echo "Warning: leaving legacy state in place because target exists: $old_state/$entry" >&2
				else
					mv "$old_state/$entry" "$new_home/$entry"
				fi
			fi
		done
		rmdir "$old_state" 2>/dev/null || true
	fi

	local legacy_bin="$HOME/.local/bin"
	remove_legacy_launcher "$legacy_bin/pi-forge" "$old_source"
	remove_legacy_launcher "$legacy_bin/pi-forge-mcp" "$old_source"
	remove_legacy_launcher "$legacy_bin/pi-forge-update" "$old_source"
}

while (($#)); do
	case "$1" in
		--source-dir) SOURCE_DIR="${2:-}"; shift ;;
		--bin-dir) BIN_DIR="${2:-}"; shift ;;
		--agent-dir) AGENT_DIR="${2:-}"; shift ;;
		--old-head) OLD_HEAD="${2:-}"; shift ;;
		--update) UPDATE=true ;;
		--resources-only) RESOURCES_ONLY=true ;;
		--help|-h) usage; exit 0 ;;
		*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

if [[ -z "$SOURCE_DIR" || ! -f "$SOURCE_DIR/package.json" ]]; then
	echo "A valid --source-dir is required." >&2
	exit 1
fi

SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
migrate_legacy_default_install
if [[ -z "$PI_FORGE_HOME" ]]; then
	if [[ "$(basename "$SOURCE_DIR")" == "repository" ]]; then
		PI_FORGE_HOME="$(cd "$SOURCE_DIR/.." && pwd)"
	else
		PI_FORGE_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/pi-vault"
	fi
fi
BIN_DIR="${BIN_DIR:-$PI_FORGE_HOME/bin}"
AGENT_DIR="${AGENT_DIR:-$PI_FORGE_HOME/agent}"
NPM_CACHE_DIR="${NPM_CACHE_DIR:-$AGENT_DIR/npm-cache}"
PLAYWRIGHT_BROWSERS_DIR="${PLAYWRIGHT_BROWSERS_DIR:-$AGENT_DIR/playwright-browsers}"

command -v node >/dev/null 2>&1 || { echo "pi-forge requires Node.js 22.19 or newer." >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "pi-forge requires npm." >&2; exit 1; }

node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 19)) process.exit(1)' || {
	echo "pi-forge requires Node.js 22.19 or newer; found $(node --version)." >&2
	exit 1
}

NEEDS_BUILD=true
NEEDS_INSTALL=true
BUILD_REVISION_FILE="$AGENT_DIR/.pi-forge-build-revision"
COMPARE_REVISION="$OLD_HEAD"
if [[ -f "$BUILD_REVISION_FILE" ]]; then
	COMPARE_REVISION="$(<"$BUILD_REVISION_FILE")"
fi

if [[ "$UPDATE" == true && -n "$COMPARE_REVISION" && -d "$SOURCE_DIR/.git" ]]; then
	CHANGED_FILES="$(git -C "$SOURCE_DIR" diff --name-only "$COMPARE_REVISION" HEAD)"
	CORE_FILES="$(grep -E '^(packages/|package(-lock)?\.json$|tsconfig|scripts/)' <<<"$CHANGED_FILES" | grep -Ev '^scripts/(configure-pi-forge\.mjs|pi-forge-(install|run)\.sh)$' || true)"
	if [[ -z "$CORE_FILES" ]]; then
		NEEDS_BUILD=false
		NEEDS_INSTALL=false
	elif ! grep -Eq '(^|/)package(-lock)?\.json$' <<<"$CORE_FILES"; then
		NEEDS_INSTALL=false
	fi
fi

if [[ "$RESOURCES_ONLY" == true ]]; then
	NEEDS_BUILD=false
	NEEDS_INSTALL=false
fi

mkdir -p "$BIN_DIR" "$AGENT_DIR"

if [[ "$NEEDS_INSTALL" == true ]]; then
	npm_config_cache="$NPM_CACHE_DIR" npm --prefix "$SOURCE_DIR" ci --ignore-scripts
	# npm ci runs with --ignore-scripts, so Playwright's browser download is
	# skipped. Fetch Chromium explicitly for the web-collection skill's rendered
	# capture. No system packages are installed (--with-deps is intentionally
	# omitted); the skill's doctor reports remediation if the browser is missing.
	if [[ -x "$SOURCE_DIR/node_modules/.bin/playwright" ]]; then
		PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_DIR" "$SOURCE_DIR/node_modules/.bin/playwright" install chromium ||
			echo "Warning: Chromium download failed; web-collection rendered capture will be unavailable until 'playwright install chromium' succeeds." >&2
	fi
fi

if [[ "$NEEDS_BUILD" == true ]]; then
	# Installation builds consume the committed generated model registries. The
	# normal development/release build refreshes them from upstream APIs, which
	# would dirty an installed Git checkout and make pi-forge-update refuse it.
	npm --prefix "$SOURCE_DIR" run build:install
	if [[ -d "$SOURCE_DIR/.git" ]]; then
		git -C "$SOURCE_DIR" rev-parse HEAD >"$BUILD_REVISION_FILE"
	fi
elif [[ ! -f "$SOURCE_DIR/packages/coding-agent/dist/cli.js" ]]; then
	echo "The pi-forge CLI is not built. Run install.sh without --resources-only." >&2
	exit 1
fi
node "$SOURCE_DIR/scripts/configure-pi-forge.mjs" "$AGENT_DIR" "$SOURCE_DIR/forge"
ln -sfn "$SOURCE_DIR/scripts/pi-forge-run.sh" "$BIN_DIR/pi-forge"
ln -sfn "$SOURCE_DIR/scripts/pi-forge-mcp-run.sh" "$BIN_DIR/pi-forge-mcp"
ln -sfn "$SOURCE_DIR/update.sh" "$BIN_DIR/pi-forge-update"

echo "pi-forge is installed."
echo "  CLI: $BIN_DIR/pi-forge"
echo "  MCP: $BIN_DIR/pi-forge-mcp"
echo "  Updater: $BIN_DIR/pi-forge-update"
echo "  State: $AGENT_DIR"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
	echo "Add $BIN_DIR to PATH before running pi-forge."
fi
