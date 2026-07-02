#!/usr/bin/env bash
set -euo pipefail

PACKAGE_NAME="@ellian-eorwyn/pi-forge"
DEFAULT_PACKAGE_SPEC="$PACKAGE_NAME@latest"
PI_PACKAGE_NAME="@earendil-works/pi-coding-agent"
DEFAULT_PI_PACKAGE_SPEC="$PI_PACKAGE_NAME@latest"
DEFAULT_SOURCE_ARCHIVE_URL="https://github.com/Ellian-Eorwyn/pi-forge/archive/refs/heads/main.tar.gz"

SOURCE_DIR=""
OLD_HEAD=""
UPDATE=false
RESOURCES_ONLY=false
DEV_LINK=false
PI_FORGE_HOME="${PI_FORGE_HOME:-}"
APP_DIR="${PI_FORGE_INSTALL_DIR:-}"
BIN_DIR="${PI_FORGE_BIN_DIR:-}"
AGENT_DIR="${PI_FORGE_AGENT_DIR:-}"
NPM_CACHE_DIR="${PI_FORGE_NPM_CACHE:-}"
PLAYWRIGHT_BROWSERS_DIR="${PI_FORGE_PLAYWRIGHT_BROWSERS:-}"
SOURCE_ARCHIVE_URL="${PI_FORGE_SOURCE_ARCHIVE_URL:-$DEFAULT_SOURCE_ARCHIVE_URL}"
PACKAGE_SPEC_EXPLICIT=false
if [[ -n "${PI_FORGE_PACKAGE_SPEC:-}" ]]; then
	PACKAGE_SPEC="$PI_FORGE_PACKAGE_SPEC"
	PACKAGE_SPEC_EXPLICIT=true
else
	PACKAGE_SPEC="$DEFAULT_PACKAGE_SPEC"
fi
PI_PACKAGE_SPEC="${PI_FORGE_PI_PACKAGE_SPEC:-$DEFAULT_PI_PACKAGE_SPEC}"
PATH_PROFILE_UPDATED=""
PATH_PROFILE_PATH=""
TEMP_DIRS=()

cleanup_temp_dirs() {
	((${#TEMP_DIRS[@]})) || return 0
	local dir
	for dir in "${TEMP_DIRS[@]}"; do
		rm -rf "$dir"
	done
}
trap cleanup_temp_dirs EXIT

usage() {
	cat <<'EOF'
Usage: scripts/pi-forge-install.sh [options]

Installs pi-forge into ~/.pi-forge/app without cloning the repository.

Options:
  --source-dir <path>    Existing checkout used only for migration or --dev-link
  --bin-dir <path>       Launcher directory (default: $PI_FORGE_HOME/bin)
  --agent-dir <path>     Isolated pi-forge state directory (default: $PI_FORGE_HOME/agent)
  --dev-link             Link launchers and skills to the given source checkout
  --update               Update an existing installation
  --old-head <commit>    Accepted for legacy migration compatibility
  --resources-only       Accepted for update compatibility

Environment:
  PI_FORGE_PACKAGE_SPEC      pi-forge package spec (default: @ellian-eorwyn/pi-forge@latest)
  PI_FORGE_PI_PACKAGE_SPEC   Pi CLI package spec (default: @earendil-works/pi-coding-agent@latest)
  PI_FORGE_SOURCE_ARCHIVE_URL source archive fallback when the default package is unavailable
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

move_legacy_state_to_default_home() {
	[[ -z "${PI_FORGE_INSTALL_DIR:-}" ]] || return 0
	local data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
	local install_home="$HOME/.pi-forge"
	[[ "$(canonical_existing "$PI_FORGE_HOME")" == "$(canonical_existing "$install_home")" ]] || return 0
	local source_canon=""
	if [[ -n "$SOURCE_DIR" ]]; then
		source_canon="$(canonical_existing "$SOURCE_DIR")"
	fi
	local legacy_home
	for legacy_home in "$data_home/pi-forge" "$data_home/pi-vault"; do
		[[ "$(canonical_existing "$legacy_home")" != "$(canonical_existing "$install_home")" ]] || continue
		[[ -d "$legacy_home" ]] || continue
		local legacy_repository="$legacy_home/repository"
		local legacy_is_source=false
		if [[ -n "$source_canon" && "$source_canon" == "$(canonical_existing "$legacy_repository")" ]]; then
			legacy_is_source=true
		fi
		if [[ "$legacy_is_source" != true && -d "$install_home/agent" ]]; then
			remove_legacy_launcher "$legacy_home/bin/pi-forge" "$legacy_repository"
			remove_legacy_launcher "$legacy_home/bin/pi-forge-mcp" "$legacy_repository"
			remove_legacy_launcher "$legacy_home/bin/pi-forge-update" "$legacy_repository"
			remove_legacy_launcher "$HOME/.local/bin/pi-forge" "$legacy_repository"
			remove_legacy_launcher "$HOME/.local/bin/pi-forge-mcp" "$legacy_repository"
			remove_legacy_launcher "$HOME/.local/bin/pi-forge-update" "$legacy_repository"
			rmdir "$legacy_home/bin" 2>/dev/null || true
			rmdir "$legacy_home" 2>/dev/null || true
			continue
		fi
		move_without_overwrite "$legacy_home/agent" "$install_home/agent"
		move_without_overwrite "$legacy_home/transcription" "$install_home/transcription"
		remove_legacy_launcher "$legacy_home/bin/pi-forge" "$legacy_repository"
		remove_legacy_launcher "$legacy_home/bin/pi-forge-mcp" "$legacy_repository"
		remove_legacy_launcher "$legacy_home/bin/pi-forge-update" "$legacy_repository"
		remove_legacy_launcher "$HOME/.local/bin/pi-forge" "$legacy_repository"
		remove_legacy_launcher "$HOME/.local/bin/pi-forge-mcp" "$legacy_repository"
		remove_legacy_launcher "$HOME/.local/bin/pi-forge-update" "$legacy_repository"
		rmdir "$legacy_home/bin" 2>/dev/null || true
		rmdir "$legacy_home" 2>/dev/null || true
	done
}

require_node_runtime() {
	command -v node >/dev/null 2>&1 || { echo "pi-forge requires Node.js 22.19 or newer." >&2; exit 1; }
	command -v npm >/dev/null 2>&1 || { echo "pi-forge requires npm." >&2; exit 1; }
	node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 19)) process.exit(1)' || {
		echo "pi-forge requires Node.js 22.19 or newer; found $(node --version)." >&2
		exit 1
	}
}

ensure_app_project() {
	mkdir -p "$APP_DIR"
	if [[ ! -f "$APP_DIR/package.json" ]]; then
		printf '{\n\t"private": true,\n\t"dependencies": {}\n}\n' >"$APP_DIR/package.json"
	fi
}

resolve_installed_package_root() {
	(cd "$APP_DIR" && node -e 'const { createRequire } = require("node:module"); const { dirname } = require("node:path"); const req = createRequire(process.cwd() + "/package.json"); console.log(dirname(req.resolve("@ellian-eorwyn/pi-forge/package.json")));')
}

pack_local_package_spec() {
	local package_root="$1"
	local package_cache_dir="$APP_DIR/package-cache"
	mkdir -p "$package_cache_dir" "$NPM_CACHE_DIR"
	local pack_output
	pack_output="$(cd "$package_root" && npm_config_cache="$NPM_CACHE_DIR" npm pack --json --pack-destination "$package_cache_dir")"
	local filename
	filename="$(printf '%s' "$pack_output" | node -e 'let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", (chunk) => { input += chunk; }); process.stdin.on("end", () => { const packed = JSON.parse(input)[0]; process.stdout.write(packed.filename); });')"
	printf 'file:%s\n' "$package_cache_dir/$filename"
}

download_file() {
	local url="$1"
	local output="$2"
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL "$url" -o "$output"
	elif command -v wget >/dev/null 2>&1; then
		wget -qO "$output" "$url"
	else
		echo "pi-forge install requires curl or wget to fetch the source archive." >&2
		return 1
	fi
}

download_source_archive() {
	command -v tar >/dev/null 2>&1 || {
		echo "pi-forge install requires tar to unpack the source archive." >&2
		return 1
	}
	local temp_dir
	temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/pi-forge-source.XXXXXX")"
	TEMP_DIRS+=("$temp_dir")
	local archive="$temp_dir/pi-forge.tar.gz"
	local extract_dir="$temp_dir/source"
	mkdir -p "$extract_dir"
	download_file "$SOURCE_ARCHIVE_URL" "$archive"
	tar -xzf "$archive" -C "$extract_dir"
	local source=""
	local entry
	for entry in "$extract_dir"/*; do
		if [[ -d "$entry" && -f "$entry/forge/package.json" ]]; then
			source="$entry"
			break
		fi
	done
	if [[ -z "$source" ]]; then
		echo "Source archive did not contain a pi-forge checkout: $SOURCE_ARCHIVE_URL" >&2
		return 1
	fi
	printf '%s\n' "$source"
}

pack_source_archive_package_spec() {
	local source
	source="$(download_source_archive)"
	pack_local_package_spec "$source/forge"
}

install_launcher() {
	local target="$1"
	local launcher="$2"
	if [[ -e "$launcher" && ! -L "$launcher" ]]; then
		echo "Refusing to overwrite non-symlink launcher: $launcher" >&2
		exit 1
	fi
	ln -sfn "$target" "$launcher"
}

install_package() {
	mkdir -p "$BIN_DIR" "$AGENT_DIR" "$NPM_CACHE_DIR"
	ensure_app_project
	local install_output
	if ! install_output="$(npm_config_cache="$NPM_CACHE_DIR" npm --prefix "$APP_DIR" install --omit=dev --ignore-scripts "$PACKAGE_SPEC" 2>&1)"; then
		if [[ "$PACKAGE_SPEC_EXPLICIT" == true ]]; then
			printf '%s\n' "$install_output" >&2
			exit 1
		fi
		echo "pi-forge package $DEFAULT_PACKAGE_SPEC is unavailable; installing from $SOURCE_ARCHIVE_URL." >&2
		PACKAGE_SPEC="$(pack_source_archive_package_spec)"
		npm_config_cache="$NPM_CACHE_DIR" npm --prefix "$APP_DIR" install --omit=dev --ignore-scripts "$PACKAGE_SPEC"
	fi
	npm_config_cache="$NPM_CACHE_DIR" npm --prefix "$APP_DIR" install --omit=dev --ignore-scripts "$PI_PACKAGE_SPEC"
	local package_root
	package_root="$(resolve_installed_package_root)"
	node "$package_root/scripts/configure-pi-forge.mjs" "$AGENT_DIR" "$package_root"
	install_launcher "$APP_DIR/node_modules/.bin/pi-forge" "$BIN_DIR/pi-forge"
	install_launcher "$APP_DIR/node_modules/.bin/pi-forge-mcp" "$BIN_DIR/pi-forge-mcp"
	install_launcher "$APP_DIR/node_modules/.bin/pi-forge-update" "$BIN_DIR/pi-forge-update"
}

install_dev_link() {
	if [[ -z "$SOURCE_DIR" || ! -f "$SOURCE_DIR/package.json" ]]; then
		echo "A valid --source-dir is required with --dev-link." >&2
		exit 1
	fi
	SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
	mkdir -p "$BIN_DIR" "$AGENT_DIR" "$NPM_CACHE_DIR"
	if [[ "$RESOURCES_ONLY" != true ]]; then
		npm_config_cache="$NPM_CACHE_DIR" npm --prefix "$SOURCE_DIR" ci --ignore-scripts
		npm --prefix "$SOURCE_DIR" run build:install
	fi
	if [[ ! -f "$SOURCE_DIR/packages/coding-agent/dist/cli.js" ]]; then
		echo "The pi-forge CLI is not built. Run install.sh --dev-link without --resources-only." >&2
		exit 1
	fi
	node "$SOURCE_DIR/forge/scripts/configure-pi-forge.mjs" "$AGENT_DIR" "$SOURCE_DIR/forge"
	install_launcher "$SOURCE_DIR/scripts/pi-forge-run.sh" "$BIN_DIR/pi-forge"
	install_launcher "$SOURCE_DIR/scripts/pi-forge-mcp-run.sh" "$BIN_DIR/pi-forge-mcp"
	install_launcher "$SOURCE_DIR/update.sh" "$BIN_DIR/pi-forge-update"
}

retire_managed_repository() {
	[[ -n "$SOURCE_DIR" && "$DEV_LINK" != true ]] || return 0
	local source_canon
	source_canon="$(canonical_existing "$SOURCE_DIR")"
	local data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
	local managed_source
	for managed_source in "$PI_FORGE_HOME/repository" "$data_home/pi-forge/repository" "$data_home/pi-vault/repository"; do
		if [[ "$source_canon" == "$(canonical_existing "$managed_source")" && -d "$SOURCE_DIR" ]]; then
			rm -rf "$SOURCE_DIR"
			rmdir "$(dirname "$SOURCE_DIR")/bin" 2>/dev/null || true
			rmdir "$(dirname "$SOURCE_DIR")" 2>/dev/null || true
			return 0
		fi
	done
	local source_parent
	source_parent="$(dirname "$SOURCE_DIR")"
	case "$(basename "$SOURCE_DIR"):$(basename "$source_parent")" in
		repository:.pi-forge | repository:pi-forge | repository:pi-vault)
			rm -rf "$SOURCE_DIR"
			rmdir "$source_parent/bin" 2>/dev/null || true
			rmdir "$source_parent" 2>/dev/null || true
			;;
	esac
}

while (($#)); do
	case "$1" in
		--source-dir) SOURCE_DIR="${2:-}"; shift ;;
		--bin-dir) BIN_DIR="${2:-}"; shift ;;
		--agent-dir) AGENT_DIR="${2:-}"; shift ;;
		--old-head) OLD_HEAD="${2:-}"; shift ;;
		--dev-link) DEV_LINK=true ;;
		--update) UPDATE=true ;;
		--resources-only) RESOURCES_ONLY=true ;;
		--help|-h) usage; exit 0 ;;
		*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

if [[ -n "$SOURCE_DIR" ]]; then
	SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
fi
PI_FORGE_HOME="${PI_FORGE_HOME:-$HOME/.pi-forge}"
APP_DIR="${APP_DIR:-$PI_FORGE_HOME/app}"
BIN_DIR="${BIN_DIR:-$PI_FORGE_HOME/bin}"
AGENT_DIR="${AGENT_DIR:-$PI_FORGE_HOME/agent}"
NPM_CACHE_DIR="${NPM_CACHE_DIR:-$AGENT_DIR/npm-cache}"
PLAYWRIGHT_BROWSERS_DIR="${PLAYWRIGHT_BROWSERS_DIR:-$AGENT_DIR/playwright-browsers}"

require_node_runtime
move_legacy_state_to_default_home

if [[ "$DEV_LINK" != true && "$PACKAGE_SPEC_EXPLICIT" != true && -n "$SOURCE_DIR" && -f "$SOURCE_DIR/forge/package.json" ]]; then
	PACKAGE_SPEC="$(pack_local_package_spec "$SOURCE_DIR/forge")"
fi

if [[ "$DEV_LINK" == true ]]; then
	install_dev_link
else
	install_package
	retire_managed_repository
fi

ensure_path_profile "$BIN_DIR"

echo "pi-forge is installed."
echo "  CLI: $BIN_DIR/pi-forge"
echo "  MCP: $BIN_DIR/pi-forge-mcp"
echo "  Updater: $BIN_DIR/pi-forge-update"
echo "  State: $AGENT_DIR"
if [[ "$DEV_LINK" == true ]]; then
	echo "  Package: $SOURCE_DIR/forge"
else
	echo "  Package: $(resolve_installed_package_root)"
fi
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
	if [[ "$PATH_PROFILE_UPDATED" == "true" ]]; then
		echo "Added $BIN_DIR to PATH in $PATH_PROFILE_PATH. Open a new shell before running pi-forge."
	else
		echo "Add $BIN_DIR to PATH before running pi-forge."
	fi
fi
