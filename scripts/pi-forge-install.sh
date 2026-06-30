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
PATH_PROFILE_UPDATED=""
PATH_PROFILE_PATH=""

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

move_without_overwrite() {
	local source="$1"
	local target="$2"
	if [[ ! -e "$source" ]]; then
		return 0
	fi
	if [[ "$(canonical_existing "$source")" == "$(canonical_existing "$target")" ]]; then
		return 0
	fi
	if [[ ! -e "$target" ]]; then
		mkdir -p "$(dirname "$target")"
		mv "$source" "$target"
		return 0
	fi
	if [[ -d "$source" && -d "$target" ]]; then
		local entry
		for entry in "$source"/* "$source"/.[!.]* "$source"/..?*; do
			[[ -e "$entry" ]] || continue
			local name
			name="$(basename "$entry")"
			if [[ -e "$target/$name" ]]; then
				echo "Warning: leaving legacy path in place because target exists: $entry" >&2
			else
				mv "$entry" "$target/$name"
			fi
		done
		rmdir "$source" 2>/dev/null || true
		return 0
	fi
	echo "Warning: leaving legacy path in place because target exists: $source" >&2
}

sudo_user_home() {
	local user="${SUDO_USER:-}"
	[[ -n "$user" && "$user" != "root" ]] || return 1
	case "$user" in
		*[!A-Za-z0-9._-]*) return 1 ;;
	esac
	local home
	home="$(eval "printf '%s' ~$user" 2>/dev/null || true)"
	[[ -n "$home" && -d "$home" ]] || return 1
	printf '%s' "$home"
}

shell_quote() {
	local value="$1"
	printf "'%s'" "${value//\'/\'\\\'\'}"
}

profile_file_for_home() {
	local home="$1"
	local shell_name
	shell_name="$(basename "${SHELL:-}")"
	if [[ "$shell_name" == "zsh" || -f "$home/.zprofile" || -f "$home/.zshrc" ]]; then
		printf '%s' "$home/.zprofile"
	elif [[ "$shell_name" == "bash" || -f "$home/.bash_profile" || -f "$home/.bashrc" ]]; then
		printf '%s' "$home/.bash_profile"
	else
		printf '%s' "$home/.profile"
	fi
}

ensure_path_profile() {
	local bin_dir="$1"
	local profile_home="${HOME:-}"
	local sudo_home
	if sudo_home="$(sudo_user_home)"; then
		profile_home="$sudo_home"
	fi
	if [[ -z "$profile_home" || ! -d "$profile_home" ]]; then
		echo "Warning: cannot update PATH automatically; no writable home directory found." >&2
		return 0
	fi
	local candidates=("$profile_home/.zprofile" "$profile_home/.zshrc" "$profile_home/.bash_profile" "$profile_home/.bashrc" "$profile_home/.profile")
	local candidate
	for candidate in "${candidates[@]}"; do
		if [[ -f "$candidate" ]] && grep -Fq "$bin_dir" "$candidate"; then
			PATH_PROFILE_PATH="$candidate"
			return 0
		fi
	done
	local profile_file
	profile_file="$(profile_file_for_home "$profile_home")"
	mkdir -p "$(dirname "$profile_file")"
	local quoted_bin
	quoted_bin="$(shell_quote "$bin_dir")"
	{
		printf '\n'
		printf '# Added by pi-forge. Keep pi-forge launchers on PATH.\n'
		printf 'case ":$PATH:" in\n'
		printf '\t*:%s:*) ;;\n' "$bin_dir"
		printf '\t*) export PATH=%s:$PATH ;;\n' "$quoted_bin"
		printf 'esac\n'
	} >>"$profile_file"
	if [[ -n "${SUDO_USER:-}" && "${SUDO_USER:-}" != "root" ]]; then
		chown "$SUDO_USER" "$profile_file" 2>/dev/null || true
	fi
	PATH_PROFILE_UPDATED="true"
	PATH_PROFILE_PATH="$profile_file"
}

migrate_default_install_home() {
	[[ -z "${PI_FORGE_HOME:-}" && -z "${PI_FORGE_INSTALL_DIR:-}" ]] || return 0
	local data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
	local install_home="$HOME/.pi-forge"
	local previous_homes=("$data_home/pi-forge" "$data_home/pi-vault")
	local moved_homes=()
	local moved_sources=()
	local previous_home

	for previous_home in "${previous_homes[@]}"; do
		local previous_source="$previous_home/repository"
		[[ "$(canonical_existing "$SOURCE_DIR")" == "$(canonical_existing "$previous_source")" ]] || continue
		if [[ -e "$install_home" && ! -d "$install_home" ]]; then
			echo "Cannot migrate pi-forge install: target exists and is not a directory: $install_home" >&2
			echo "Move it, or set PI_FORGE_HOME to choose a different install home." >&2
			exit 1
		fi
		if [[ -e "$install_home/repository" ]]; then
			echo "Cannot migrate pi-forge install: target repository already exists: $install_home/repository" >&2
			echo "Move it, or set PI_FORGE_HOME to choose a different install home." >&2
			exit 1
		fi
		mkdir -p "$install_home"
		mv "$previous_source" "$install_home/repository"
		SOURCE_DIR="$install_home/repository"
		moved_homes+=("$previous_home")
		moved_sources+=("$previous_source")
	done

	if [[ "$(canonical_existing "$SOURCE_DIR")" != "$(canonical_existing "$install_home/repository")" ]]; then
		return 0
	fi
	PI_FORGE_HOME="$install_home"

	if ((${#moved_homes[@]} > 0)); then
		for previous_home in "${moved_homes[@]}"; do
			if [[ "$(canonical_existing "$previous_home")" == "$(canonical_existing "$install_home")" ]]; then
				continue
			fi
			if [[ -e "$previous_home" && ! -d "$previous_home" ]]; then
				echo "Cannot migrate pi-forge install: previous install path exists and is not a directory: $previous_home" >&2
				echo "Move it, or set PI_FORGE_HOME to choose a different install home." >&2
				exit 1
			fi
			move_without_overwrite "$previous_home/agent" "$install_home/agent"
			move_without_overwrite "$previous_home/transcription" "$install_home/transcription"
			for old_source in "${moved_sources[@]}" "$previous_home/repository"; do
				[[ -n "$old_source" ]] || continue
				remove_legacy_launcher "$previous_home/bin/pi-forge" "$old_source"
				remove_legacy_launcher "$previous_home/bin/pi-forge-mcp" "$old_source"
				remove_legacy_launcher "$previous_home/bin/pi-forge-update" "$old_source"
			done
			rmdir "$previous_home/bin" 2>/dev/null || true
			rmdir "$previous_home" 2>/dev/null || true
		done
	fi

	local legacy_bin="$HOME/.local/bin"
	if ((${#moved_sources[@]} > 0)); then
		for old_source in "${moved_sources[@]}"; do
			remove_legacy_launcher "$legacy_bin/pi-forge" "$old_source"
			remove_legacy_launcher "$legacy_bin/pi-forge-mcp" "$old_source"
			remove_legacy_launcher "$legacy_bin/pi-forge-update" "$old_source"
		done
	fi
	for old_source in "$install_home/repository"; do
		remove_legacy_launcher "$legacy_bin/pi-forge" "$old_source"
		remove_legacy_launcher "$legacy_bin/pi-forge-mcp" "$old_source"
		remove_legacy_launcher "$legacy_bin/pi-forge-update" "$old_source"
	done
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
migrate_default_install_home
if [[ -z "$PI_FORGE_HOME" ]]; then
	if [[ "$(basename "$SOURCE_DIR")" == "repository" ]]; then
		PI_FORGE_HOME="$(cd "$SOURCE_DIR/.." && pwd)"
	else
		PI_FORGE_HOME="$HOME/.pi-forge"
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
ensure_path_profile "$BIN_DIR"

echo "pi-forge is installed."
echo "  CLI: $BIN_DIR/pi-forge"
echo "  MCP: $BIN_DIR/pi-forge-mcp"
echo "  Updater: $BIN_DIR/pi-forge-update"
echo "  State: $AGENT_DIR"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
	if [[ "$PATH_PROFILE_UPDATED" == "true" ]]; then
		echo "Added $BIN_DIR to PATH in $PATH_PROFILE_PATH. Open a new shell before running pi-forge."
	else
		echo "Add $BIN_DIR to PATH before running pi-forge."
	fi
fi
