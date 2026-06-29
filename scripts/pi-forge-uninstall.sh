#!/usr/bin/env bash
set -euo pipefail

# Remove a pi-forge installation: the launchers in the bin directory and the
# managed checkout created by a remote install. Agent state (credentials,
# sessions, settings) is preserved unless --purge-state is given. A development
# checkout is never deleted, including the one this script lives in.

SOURCE_DIR=""
PI_FORGE_HOME="${PI_FORGE_HOME:-}"
BIN_DIR="${PI_FORGE_BIN_DIR:-}"
AGENT_DIR="${PI_FORGE_AGENT_DIR:-}"
INSTALL_DIR="${PI_FORGE_INSTALL_DIR:-}"
PURGE_STATE=false
DRY_RUN=false
ASSUME_YES=false

LAUNCHERS=(pi-forge pi-forge-mcp pi-forge-update)
# Symlink targets the installer creates; a launcher is only removed when it
# points at one of these so an unrelated command of the same name is left alone.
EXPECTED_TARGETS=(pi-forge-run.sh pi-forge-mcp-run.sh update.sh)

usage() {
	cat <<'EOF'
Usage: scripts/pi-forge-uninstall.sh [options]

Removes the pi-forge launchers and the managed checkout. Agent state is kept
by default so a later reinstall reuses your credentials and sessions.

Options:
  --bin-dir <path>       Launcher directory (default: $PI_FORGE_HOME/bin)
  --agent-dir <path>     Agent state directory (default: $PI_FORGE_HOME/agent)
  --install-dir <path>   Managed checkout root (default: $PI_FORGE_HOME)
  --purge-state          Also delete the agent state directory (credentials,
                         sessions, settings). Irreversible.
  --dry-run              Print what would be removed without changing anything.
  --yes, -y              Do not prompt for confirmation.
  --source-dir <path>    The checkout this script runs from; protected from
                         deletion. Set automatically by uninstall.sh.
EOF
}

while (($#)); do
	case "$1" in
		--bin-dir) BIN_DIR="${2:-}"; shift ;;
		--agent-dir) AGENT_DIR="${2:-}"; shift ;;
		--install-dir) INSTALL_DIR="${2:-}"; shift ;;
		--source-dir) SOURCE_DIR="${2:-}"; shift ;;
		--purge-state) PURGE_STATE=true ;;
		--dry-run) DRY_RUN=true ;;
		--yes|-y) ASSUME_YES=true ;;
		--help|-h) usage; exit 0 ;;
		*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

# Resolve a path to an absolute, symlink-free form when it exists; otherwise
# echo it unchanged so comparisons still work on missing paths.
canonical() {
	local path="$1"
	if [[ -e "$path" ]]; then
		(cd "$path" 2>/dev/null && pwd) || printf '%s' "$path"
	else
		printf '%s' "$path"
	fi
}

# Refuse to delete a filesystem root, the home directory, or an empty path.
is_protected_path() {
	local path="$1"
	[[ -z "$path" ]] && return 0
	case "$path" in
		/ | /root | /home | /Users | "$HOME" | /usr | /bin | /etc | /var | /System | /Library | /opt) return 0 ;;
	esac
	return 1
}

if [[ -n "$SOURCE_DIR" ]]; then
	SOURCE_DIR="$(canonical "$SOURCE_DIR")"
fi
if [[ -z "$PI_FORGE_HOME" ]]; then
	if [[ -n "$SOURCE_DIR" && "$(basename "$SOURCE_DIR")" == "repository" ]]; then
		PI_FORGE_HOME="$(cd "$SOURCE_DIR/.." && pwd)"
	else
		PI_FORGE_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/pi-vault"
	fi
fi
BIN_DIR="${BIN_DIR:-$PI_FORGE_HOME/bin}"
AGENT_DIR="${AGENT_DIR:-$PI_FORGE_HOME/agent}"
INSTALL_DIR="${INSTALL_DIR:-$PI_FORGE_HOME}"

PLANNED=()
WARNINGS=()
SUDO_NEEDED=false

note_plan() { PLANNED+=("$1"); }
note_warn() { WARNINGS+=("$1"); }

# Remove a path, honoring --dry-run and reporting permission failures with a
# sudo hint instead of aborting the whole uninstall.
remove_path() {
	local path="$1"
	if [[ "$DRY_RUN" == true ]]; then
		return 0
	fi
	if rm -rf "$path" 2>/dev/null; then
		return 0
	fi
	if rm -rf "$path"; then
		return 0
	fi
	SUDO_NEEDED=true
	note_warn "Could not remove $path (permission denied). Retry with: sudo rm -rf '$path'"
	return 1
}

# --- Plan: launchers -------------------------------------------------------
LAUNCHERS_TO_REMOVE=()
for name in "${LAUNCHERS[@]}"; do
	launcher="$BIN_DIR/$name"
	[[ -L "$launcher" || -e "$launcher" ]] || continue
	if [[ ! -L "$launcher" ]]; then
		note_warn "Skipping $launcher: not a symlink (left untouched)."
		continue
	fi
	target="$(readlink "$launcher" 2>/dev/null || true)"
	base="$(basename "$target")"
	matched=false
	for expected in "${EXPECTED_TARGETS[@]}"; do
		[[ "$base" == "$expected" ]] && matched=true && break
	done
	if [[ "$matched" == true ]]; then
		LAUNCHERS_TO_REMOVE+=("$launcher")
		note_plan "Remove launcher: $launcher -> $target"
	else
		note_warn "Skipping $launcher: points at $target (not a pi-forge launcher)."
	fi
done

# --- Plan: managed checkout ------------------------------------------------
REMOVE_INSTALL_DIR=false
INSTALL_DIR_CANON="$(canonical "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR" ]]; then
	managed_repo="$INSTALL_DIR/repository"
	if is_protected_path "$INSTALL_DIR_CANON"; then
		note_warn "Refusing to remove managed checkout at protected path: $INSTALL_DIR"
	elif [[ -n "$SOURCE_DIR" && ( "$INSTALL_DIR_CANON" == "$SOURCE_DIR" || "$SOURCE_DIR" == "$INSTALL_DIR_CANON"/* ) ]]; then
		note_warn "Skipping managed checkout: it contains this checkout ($SOURCE_DIR)."
	elif [[ -f "$managed_repo/scripts/pi-forge-install.sh" ]]; then
		REMOVE_INSTALL_DIR=true
		note_plan "Remove managed checkout: $INSTALL_DIR"
	else
		note_warn "Skipping $INSTALL_DIR: does not look like a managed pi-forge checkout."
	fi
fi

# --- Plan: agent state -----------------------------------------------------
REMOVE_AGENT_DIR=false
AGENT_DIR_CANON="$(canonical "$AGENT_DIR")"
PROFILE_MARKER="$AGENT_DIR/.pi-forge-profile-path"
if [[ "$PURGE_STATE" == true ]]; then
	if [[ ! -d "$AGENT_DIR" ]]; then
		note_warn "Agent directory not found (nothing to purge): $AGENT_DIR"
	elif is_protected_path "$AGENT_DIR_CANON"; then
		note_warn "Refusing to purge agent state at protected path: $AGENT_DIR"
	elif [[ ! -f "$PROFILE_MARKER" ]]; then
		note_warn "Skipping --purge-state: $AGENT_DIR lacks the .pi-forge-profile-path marker; not clearly a pi-forge agent directory."
	else
		REMOVE_AGENT_DIR=true
		note_plan "Purge agent state (credentials, sessions, settings): $AGENT_DIR"
	fi
elif [[ -d "$AGENT_DIR" ]]; then
	note_plan "Preserve agent state (credentials, sessions, settings): $AGENT_DIR"
fi

# --- Report and confirm ----------------------------------------------------
if [[ ${#LAUNCHERS_TO_REMOVE[@]} -eq 0 && "$REMOVE_INSTALL_DIR" == false && "$REMOVE_AGENT_DIR" == false ]]; then
	echo "Nothing to uninstall."
	for warning in "${WARNINGS[@]:-}"; do
		[[ -n "$warning" ]] && echo "  - $warning"
	done
	exit 0
fi

echo "pi-forge uninstall plan:"
for item in "${PLANNED[@]}"; do
	echo "  - $item"
done
if [[ ${#WARNINGS[@]} -gt 0 ]]; then
	echo "Notes:"
	for warning in "${WARNINGS[@]}"; do
		[[ -n "$warning" ]] && echo "  - $warning"
	done
fi

if [[ "$DRY_RUN" == true ]]; then
	echo "Dry run: no changes made."
	exit 0
fi

if [[ "$ASSUME_YES" != true ]]; then
	prompt="Proceed?"
	[[ "$REMOVE_AGENT_DIR" == true ]] && prompt="Proceed? This permanently deletes credentials and sessions."
	read -r -p "$prompt [y/N] " reply
	case "$reply" in
		[yY] | [yY][eE][sS]) ;;
		*) echo "Aborted."; exit 1 ;;
	esac
fi

# --- Execute ---------------------------------------------------------------
for launcher in "${LAUNCHERS_TO_REMOVE[@]}"; do
	remove_path "$launcher" && echo "Removed $launcher" || true
done
if [[ "$REMOVE_INSTALL_DIR" == true ]]; then
	remove_path "$INSTALL_DIR" && echo "Removed $INSTALL_DIR" || true
fi
if [[ "$REMOVE_AGENT_DIR" == true ]]; then
	remove_path "$AGENT_DIR" && echo "Removed $AGENT_DIR" || true
fi

echo
echo "pi-forge uninstalled."
if [[ "$REMOVE_AGENT_DIR" != true && -d "$AGENT_DIR" ]]; then
	echo "  Agent state kept at: $AGENT_DIR (re-run with --purge-state to remove it)."
fi
if [[ "$SUDO_NEEDED" == true ]]; then
	echo "  Some paths needed elevated permissions; rerun the printed sudo commands." >&2
	exit 1
fi
