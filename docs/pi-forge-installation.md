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

The default install home is `~/.pi-forge`.
pi-forge keeps its installed files under that one directory:

- checkout, scripts, and managed skills: `~/.pi-forge/repository`
- commands: `~/.pi-forge/bin/pi-forge`,
  `~/.pi-forge/bin/pi-forge-mcp`, and
  `~/.pi-forge/bin/pi-forge-update`
- credentials, settings, copied `AGENTS.md`, sessions, caches, and other state:
  `~/.pi-forge/agent`

Set `PI_FORGE_HOME` to move the whole tree. Set `PI_FORGE_INSTALL_DIR`,
`PI_FORGE_BIN_DIR`, or `PI_FORGE_AGENT_DIR` only when you intentionally want a
split layout. `PI_FORGE_NPM_CACHE` overrides the isolated npm cache under the
agent directory. The installer adds `~/.pi-forge/bin` to the user
profile when possible; open a new shell before relying on `pi-forge` from
`PATH`.

Installing from an existing checkout clones that checkout into
`~/.pi-forge/repository` by default, so the installed commands and skills do not
depend on the development checkout. For checkout-linked development mode, run
`./install.sh --dev-link`; do not remove or move that checkout while the
development-linked installation is in use.

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
the bin directory and the managed checkout under `~/.pi-forge/repository` when a
remote install created one. A launcher is removed only when it still points at a
pi-forge script, and a development checkout outside the install home is never
deleted.

Agent state in `~/.pi-forge/agent` (credentials, sessions, settings)
is preserved by default so a later reinstall reuses your login. To remove it too:

```bash
./uninstall.sh --purge-state
```

Preview without changing anything with `--dry-run`, and skip the confirmation
prompt with `--yes`. The `--bin-dir`, `--agent-dir`, and `--install-dir` options
(and the matching `PI_FORGE_*` variables) target a non-default layout. If you
installed with `--dev-link`, uninstalling leaves that checkout in place; run
`git clean -xdf` there to drop the build artifacts and `node_modules`.

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
