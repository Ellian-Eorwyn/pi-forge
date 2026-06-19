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

The default layout is:

- checkout: `~/.local/share/pi-forge/repository` for remote installs
- commands: `~/.local/bin/pi-forge` and `~/.local/bin/pi-forge-update`
- credentials, settings, sessions, and other state: `~/.pi-forge/agent`

Set `PI_FORGE_INSTALL_DIR`, `PI_FORGE_BIN_DIR`, or `PI_FORGE_AGENT_DIR` to
override those locations. `PI_FORGE_NPM_CACHE` overrides the isolated npm cache
under the agent directory. Add `~/.local/bin` to `PATH` when it is not already
present.

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
