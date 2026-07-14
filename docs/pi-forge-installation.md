# pi-forge installation

pi-forge currently supports macOS, Linux, and Windows with npm and Node.js 22.19 or newer. Git is only required for checkout-linked development installs.

## Install

From a checkout, installing the GitHub-packaged app:

```bash
./install.sh
```

From a new machine:

**macOS/Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/install.ps1'))
```

The default install home is `~/.pi-forge`.
pi-forge keeps its installed files under that one directory:

- npm app containing `@ellian-eorwyn/pi-forge`: `~/.pi-forge/app`
- commands: `~/.pi-forge/bin/pi-forge`,
  `~/.pi-forge/bin/pi-forge-mcp`, and
  `~/.pi-forge/bin/pi-forge-update`
  *(On Windows, these are `.cmd` and `.ps1` files)*
- credentials, settings, copied `AGENTS.md`, sessions, caches, and other state:
  `~/.pi-forge/agent`

Set `PI_FORGE_HOME` to move the whole tree. Set `PI_FORGE_INSTALL_DIR`,
`PI_FORGE_BIN_DIR`, or `PI_FORGE_AGENT_DIR` only when you intentionally want a
split layout. `PI_FORGE_NPM_CACHE` overrides the isolated npm cache under the
agent directory. By default, the macOS/Linux installer downloads
`PI_FORGE_SOURCE_ARCHIVE_URL` (default:
`https://github.com/Ellian-Eorwyn/pi-forge/archive/refs/heads/main.tar.gz`),
packs `forge/` locally, packs the Pi runtime packages from the upstream Pi
source archive, and installs those packages into the npm app layout.
`PI_FORGE_PACKAGE_SPEC` overrides that source archive package and can point at
`file:<packed-tarball>` for tests and local release smoke tests, or at
`@ellian-eorwyn/pi-forge@latest` if a registry package is intentionally used.
`PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL` overrides the upstream Pi source archive
used for default runtime package installs (default:
`https://github.com/earendil-works/pi/archive/refs/heads/main.tar.gz`).
`PI_FORGE_PI_PACKAGE_SPEC` overrides the bundled Pi runtime package install;
when unset, pi-forge packs the runtime packages from the upstream Pi archive.
The installer adds `~/.pi-forge/bin` to the user profile/PATH when possible;
open a new shell before relying on `pi-forge` from `PATH`.

For checkout-linked development mode, run `./install.sh --dev-link`; do not
remove or move that checkout while the development-linked installation is in
use. This mode links launchers and package resources to the checkout instead of
the npm app.

## Update

```bash
pi-forge-update
```
*(On Windows: `pi-forge-update.ps1` or just `pi-forge-update`)*

The updater downloads the configured pi-forge GitHub source archive, packs
pi-forge locally, packs the bundled Pi runtime packages from the upstream Pi
source archive, installs those tarballs into
`~/.pi-forge/app` with `npm install --omit=dev --ignore-scripts`, refreshes
managed settings, and rewrites launchers. `PI_FORGE_PACKAGE_SPEC` overrides the
pi-forge package source when set; `PI_FORGE_PI_PACKAGE_SPEC` overrides the
bundled runtime package source when set. `PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL`
overrides the upstream Pi archive when set. Credentials, sessions, and unrelated
settings are preserved. Existing
clone-based installs run one final
fast-forward-only Git pull, migrate to the npm app layout, then remove the
managed `~/.pi-forge/repository` only after package installation and
configuration succeed.

For a profile-only update:

```bash
pi-forge-update --resources-only
```

`--resources-only` is accepted for compatibility. In the package layout, skills,
profile resources, launchers, and the CLI all ship from the same npm package, so
the command still refreshes the installed package.

## Uninstall

```bash
./uninstall.sh
```
*(On Windows: `.\uninstall.ps1`)*

This removes the `pi-forge`, `pi-forge-mcp`, and `pi-forge-update` launchers from
the bin directory and the managed npm app under `~/.pi-forge/app`. If a legacy
managed repository remains under `~/.pi-forge/repository`, it is removed too. A
launcher is removed only when it still points at a pi-forge target, and a
development checkout outside the install home is never deleted.

Agent state in `~/.pi-forge/agent` (credentials, sessions, settings)
is preserved by default so a later reinstall reuses your login. To remove it too:

```bash
./uninstall.sh --purge-state
```
*(On Windows: `.\uninstall.ps1 -PurgeState`)*

Preview without changing anything with `--dry-run`, and skip the confirmation
prompt with `--yes`. The `--bin-dir`, `--agent-dir`, and `--install-dir` options
(and the matching `PI_FORGE_*` variables) target a non-default layout. If you
installed with `--dev-link`, uninstalling leaves that checkout in place.

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

The installer registers the installed package root as a Pi package in the
isolated pi-forge settings and copies its `AGENTS.md` to the isolated agent
directory as managed global instructions. Profile edits become available after
publishing and running `pi-forge-update`.
