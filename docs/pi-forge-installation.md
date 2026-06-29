# pi-forge installation

pi-forge currently supports macOS and Linux with Git, npm, and Node.js 22.19 or
newer.

## Install

From a checkout:

```bash
./install.sh
```

From a new machine:

```bash
curl -fsSL https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/install.sh | bash
```

The default install home is `${XDG_DATA_HOME:-~/.local/share}/pi-forge`.
pi-forge keeps its installed files under that one directory:

- checkout, scripts, and managed skills: `~/.local/share/pi-forge/repository`
- commands: `~/.local/share/pi-forge/bin/pi-forge`,
  `~/.local/share/pi-forge/bin/pi-forge-mcp`, and
  `~/.local/share/pi-forge/bin/pi-forge-update`
- credentials, settings, copied `AGENTS.md`, sessions, caches, and other state:
  `~/.local/share/pi-forge/agent`

Set `PI_FORGE_HOME` to move the whole tree. Set `PI_FORGE_INSTALL_DIR`,
`PI_FORGE_BIN_DIR`, or `PI_FORGE_AGENT_DIR` only when you intentionally want a
split layout. `PI_FORGE_NPM_CACHE` overrides the isolated npm cache under the
agent directory. The installer adds `~/.local/share/pi-forge/bin` to the user
profile when possible; open a new shell before relying on `pi-forge` from
`PATH`.

Installing from an existing checkout uses that checkout directly. This is
useful for development; do not remove or move it while the installation is in
use.

## Update

```bash
pi-forge-update
```

The updater performs a fast-forward-only Git pull. It rebuilds the CLI only
when core source changed and reinstalls dependencies only when package metadata
changed. Credentials, sessions, and unrelated settings are preserved.

For a profile-only update:

```bash
pi-forge-update --resources-only
```

Profile-only mode still pulls the repository. If that pull also contains core
changes, the current built CLI remains active and the next normal update builds
the accumulated core changes. It rejects an update when no CLI build exists.
Local tracked changes also stop updates rather than being overwritten.

## Uninstall

```bash
./uninstall.sh
```

This removes the `pi-forge`, `pi-forge-mcp`, and `pi-forge-update` launchers from
the bin directory and the managed checkout under `~/.local/share/pi-forge` when a
remote install created one. A launcher is removed only when it still points at a
pi-forge script, and a development checkout is never deleted — including one you
installed from directly.

Agent state in `~/.local/share/pi-forge/agent` (credentials, sessions, settings)
is preserved by default so a later reinstall reuses your login. To remove it too:

```bash
./uninstall.sh --purge-state
```

Preview without changing anything with `--dry-run`, and skip the confirmation
prompt with `--yes`. The `--bin-dir`, `--agent-dir`, and `--install-dir` options
(and the matching `PI_FORGE_*` variables) target a non-default layout. If you
installed from a development checkout, uninstalling leaves that checkout in place;
run `git clean -xdf` there to drop the build artifacts and `node_modules`.

## Profile layout

Add pi-forge-owned resources under:

```text
forge/
├── AGENTS.md
├── extensions/
├── prompts/
├── skills/
└── themes/
```

The installer registers `forge/` as a local Pi package in the isolated
pi-forge settings and copies `forge/AGENTS.md` to the isolated agent directory
as managed global instructions. Profile edits become available after
`pi-forge-update` and do not require a CLI rebuild.
