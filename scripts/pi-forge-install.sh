#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=""
OLD_HEAD=""
UPDATE=false
RESOURCES_ONLY=false
BIN_DIR="${PI_FORGE_BIN_DIR:-$HOME/.local/bin}"
AGENT_DIR="${PI_FORGE_AGENT_DIR:-$HOME/.pi-forge/agent}"
NPM_CACHE_DIR="${PI_FORGE_NPM_CACHE:-$AGENT_DIR/npm-cache}"
PLAYWRIGHT_BROWSERS_DIR="${PI_FORGE_PLAYWRIGHT_BROWSERS:-$AGENT_DIR/playwright-browsers}"

usage() {
	cat <<'EOF'
Usage: scripts/pi-forge-install.sh --source-dir <path> [options]

Options:
  --bin-dir <path>       Launcher directory (default: ~/.local/bin)
  --agent-dir <path>     Isolated pi-forge state directory
  --update               Update an existing installation
  --old-head <commit>    Previous revision used to detect core changes
  --resources-only       Update the profile without dependencies or a CLI rebuild
EOF
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
	npm --prefix "$SOURCE_DIR" run build
	if [[ -d "$SOURCE_DIR/.git" ]]; then
		git -C "$SOURCE_DIR" rev-parse HEAD >"$BUILD_REVISION_FILE"
	fi
elif [[ ! -f "$SOURCE_DIR/packages/coding-agent/dist/cli.js" ]]; then
	echo "The pi-forge CLI is not built. Run install.sh without --resources-only." >&2
	exit 1
fi
node "$SOURCE_DIR/scripts/configure-pi-forge.mjs" "$AGENT_DIR" "$SOURCE_DIR/forge"
ln -sfn "$SOURCE_DIR/scripts/pi-forge-run.sh" "$BIN_DIR/pi-forge"
ln -sfn "$SOURCE_DIR/update.sh" "$BIN_DIR/pi-forge-update"

echo "pi-forge is installed."
echo "  CLI: $BIN_DIR/pi-forge"
echo "  Updater: $BIN_DIR/pi-forge-update"
echo "  State: $AGENT_DIR"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
	echo "Add $BIN_DIR to PATH before running pi-forge."
fi
