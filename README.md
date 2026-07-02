<p align="center">
  <a href="https://pi.dev">
    <img alt="pi logo" src="https://pi.dev/logo-auto.svg" width="128">
  </a>
</p>
<p align="center">
  <a href="https://discord.com/invite/3cU7Bz4UPx"><img alt="Discord" src="https://img.shields.io/badge/discord-community-5865F2?style=flat-square&logo=discord&logoColor=white" /></a>
  <a href="https://www.npmjs.com/package/@earendil-works/pi-coding-agent"><img alt="npm" src="https://img.shields.io/npm/v/@earendil-works/pi-coding-agent?style=flat-square" /></a>
</p>

> New issues and PRs from new contributors are auto-closed by default. Maintainers review auto-closed issues daily. See [CONTRIBUTING.md](CONTRIBUTING.md).

# Pi Agent Harness

## pi-forge

pi-forge is a research and document-processing-focused fork of pi. It empowers users to automate complex workflows involving research, documentation, file manipulation, and data analysis using AI agents. By equipping agents with specialized tools (skills), pi-forge acts as an intelligent assistant capable of parsing, summarizing, organizing, and synthesizing massive amounts of information.

### What Can You Do With pi-forge?
- **Research & Web Collection:** Search the web and archive websites to compile research repositories.
- **Data & Document Processing:** Clean raw transcripts, convert documents (Markdown, EPUB, etc.), and analyze spreadsheets.
- **Content Synthesis:** Extract literature, build action plans, and synthesize polished deliverables from raw documents.
- **Workflow Automation:** Automatically organize messy folders and ship small, reviewable codebase changes.

### Included Skills
The `@ellian-eorwyn/pi-forge` package ships Agent Skills under `forge/skills/<name>/SKILL.md`. Skill directory names match their `SKILL.md` frontmatter names, use lowercase hyphenated names, and keep scripts, assets, and references relative to the skill directory. The installed Pi settings point at the package root so Pi and the MCP bridge load skills from the installed package, not from a cloned repository.

The `forge` profile provides agents with the following built-in skills:
- **`coding`**: Inspect repos and ship small reviewable changes
- **`document-ingest`**: Normalize documents with provenance
- **`file-conversion`**: Convert files, including Markdown and EPUB
- **`literature-extraction`**: Extract structured evidence from research documents
- **`organize-folder`**: Sort a messy folder via a reviewable manifest
- **`personal-admin`**: Summarize personal documents into action plans
- **`report-output`**: Assemble polished deliverables from processed outputs
- **`site-builder`**: Build a static website from a content folder
- **`spreadsheet-analysis`**: Analyze and enrich tabular datasets
- **`transcript-cleanup`**: Clean and structure raw transcripts
- **`transcription`**: Transcribe audio or video, then correct and clean it
- **`vault-handoff`**: Send completed text artifacts to pi-vault review
- **`web-collection`**: Archive and organize web sources
- **`web-research`**: Quick web search and page reading for information lookup

---

## Installation & Setup

pi-forge currently supports macOS, Linux, and Windows with npm and Node.js 22.19 or newer. New installs do not clone this repository. The installer creates one managed home at `~/.pi-forge`:

| Path | Purpose |
|------|---------|
| `~/.pi-forge/app` | npm app containing `@ellian-eorwyn/pi-forge` and the refreshed `@earendil-works/pi-coding-agent` package |
| `~/.pi-forge/bin` | stable launchers for `pi-forge`, `pi-forge-mcp`, and `pi-forge-update` |
| `~/.pi-forge/agent` | credentials, settings, sessions, caches, copied `AGENTS.md`, and managed profile state |

The installer registers the installed package root in Pi settings, so package-owned skills, prompts, themes, extensions, and MCP resources ship from the installed package while user state stays under `~/.pi-forge/agent`.

### 1. Install
From a new machine, run the following command in your terminal:

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/install.ps1'))
```

*Note: The installer adds `~/.pi-forge/bin` to your user PATH. Please open a new shell after installing.*

If `@ellian-eorwyn/pi-forge@latest` is not published to npm yet, the macOS/Linux installer automatically downloads the GitHub source archive, packs the `forge` package locally, and installs that package into the same `~/.pi-forge/app` layout.

### 2. Update
To update pi-forge and `@earendil-works/pi-coding-agent` to the latest versions while preserving credentials, sessions, and settings:

**macOS / Linux:**
```bash
pi-forge-update
```

**Windows (PowerShell/CMD):**
```powershell
pi-forge-update
```
*(Or run `pi-forge-update.ps1`)*

`pi-forge-update` installs `@ellian-eorwyn/pi-forge@latest` and `@earendil-works/pi-coding-agent@latest` into `~/.pi-forge/app` with `npm install --omit=dev --ignore-scripts`, refreshes managed configuration, and rewrites launchers. If the scoped pi-forge package is not available yet, the updater refreshes from the installed package copy and still updates the Pi CLI package.

Existing clone-based installs migrate automatically. The legacy updater runs one final Git pull when a managed repository is present, installs the npm app layout, rewires launchers to `~/.pi-forge/bin`, and removes only the managed `~/.pi-forge/repository` after package installation and configuration succeed. User-owned development checkouts are not removed.

### 3. Uninstall
If you want to remove pi-forge:

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/uninstall.sh | bash
```

**Windows (PowerShell):**
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/uninstall.ps1'))
```
*To completely wipe all agent state and credentials along with the installation, run the uninstall script with `--purge-state` (macOS/Linux) or `-PurgeState` (Windows).*

For advanced installation options, paths, and profile layouts, see the [detailed pi-forge installation guide](docs/pi-forge-installation.md).

### Environment overrides

Use these only when you need a non-default layout, local smoke test, or development install:

| Variable | Default |
|----------|---------|
| `PI_FORGE_HOME` | `~/.pi-forge` |
| `PI_FORGE_BIN_DIR` | `$PI_FORGE_HOME/bin` |
| `PI_FORGE_AGENT_DIR` | `$PI_FORGE_HOME/agent` |
| `PI_FORGE_NPM_CACHE` | `$PI_FORGE_AGENT_DIR/npm-cache` |
| `PI_FORGE_PLAYWRIGHT_BROWSERS` | `$PI_FORGE_AGENT_DIR/playwright-browsers` |
| `PI_FORGE_PACKAGE_SPEC` | `@ellian-eorwyn/pi-forge@latest` |
| `PI_FORGE_PI_PACKAGE_SPEC` | `@earendil-works/pi-coding-agent@latest` |
| `PI_FORGE_SOURCE_ARCHIVE_URL` | `https://github.com/Ellian-Eorwyn/pi-forge/archive/refs/heads/main.tar.gz` |

`PI_FORGE_PACKAGE_SPEC` and `PI_FORGE_PI_PACKAGE_SPEC` can point at `file:<packed-tarball>` for local release and migration smoke tests. `PI_FORGE_SOURCE_ARCHIVE_URL` overrides the source archive fallback used only when the default pi-forge npm package is unavailable. Checkout-linked development installs are still available with `./install.sh --dev-link`; that mode links launchers and package resources to the checkout instead of the npm app.

---

This is the home of the Pi agent harness project including our self extensible coding agent.

* **[@earendil-works/pi-coding-agent](packages/coding-agent)**: Interactive coding agent CLI
* **[@earendil-works/pi-agent-core](packages/agent)**: Agent runtime with tool calling and state management
* **[@earendil-works/pi-ai](packages/ai)**: Unified multi-provider LLM API (OpenAI, Anthropic, Google, …)

To learn more about Pi:

* [Visit pi.dev](https://pi.dev), the project website with demos
* [Read the documentation](https://pi.dev/docs/latest), but you can also ask the agent to explain itself

## All Packages

| Package | Description |
|---------|-------------|
| **[@earendil-works/pi-ai](packages/ai)** | Unified multi-provider LLM API (OpenAI, Anthropic, Google, etc.) |
| **[@earendil-works/pi-agent-core](packages/agent)** | Agent runtime with tool calling and state management |
| **[@earendil-works/pi-coding-agent](packages/coding-agent)** | Interactive coding agent CLI |
| **[@earendil-works/pi-tui](packages/tui)** | Terminal UI library with differential rendering |
| **[@ellian-eorwyn/pi-forge](forge)** | pi-forge launchers, skills, profile resources, and MCP bridge |

For Slack/chat automation and workflows see [earendil-works/pi-chat](https://github.com/earendil-works/pi-chat).

## Permissions & Containerization

Pi does not include a built-in permission system for restricting filesystem, process, network, or credential access. By default, it runs with the permissions of the user and process that launched it.

If you need stronger boundaries, containerize or sandbox Pi. See [packages/coding-agent/docs/containerization.md](packages/coding-agent/docs/containerization.md) for three patterns:

- **Gondolin extension**: keep `pi` and provider auth on the host while routing built-in tools and `!` commands into a local Linux micro-VM.
- **Plain Docker**: run the whole `pi` process in a local container for simple isolation.
- **OpenShell**: run the whole `pi` process in a policy-controlled sandbox.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines and [AGENTS.md](AGENTS.md) for project-specific rules (for both humans and agents).  Longer term plans for Pi can also be found in [RFCs](https://rfc.earendil.com/keyword/pi/).

## Development

```bash
npm install --ignore-scripts  # Install all dependencies without running lifecycle scripts
npm run build        # Build all packages
npm run check        # Lint, format, and type check
./test.sh            # Run tests (skips LLM-dependent tests without API keys)
./pi-test.sh         # Run pi from sources (can be run from any directory)
```

## Supply-chain hardening

We treat npm dependency changes as reviewed code changes.

- Direct external dependencies are pinned to exact versions. Internal workspace packages remain version-ranged.
- `.npmrc` sets `save-exact=true` and `min-release-age=2` to avoid same-day dependency releases during npm resolution.
- `package-lock.json` is the dependency ground truth. Pre-commit blocks accidental lockfile commits unless `PI_ALLOW_LOCKFILE_CHANGE=1` is set.
- `npm run check` verifies pinned direct deps, native TypeScript import compatibility, and the generated coding-agent shrinkwrap.
- The published CLI package includes `packages/coding-agent/npm-shrinkwrap.json`, generated from the root lockfile, to pin transitive deps for npm users.
- Release smoke tests use `npm run release:local` to build, pack, and create isolated npm and Bun installs outside the repo before tagging a release.
- Local release installs, documented npm installs, and `pi update --self` use `--ignore-scripts` where supported.
- CI installs with `npm ci --ignore-scripts`, and a scheduled GitHub workflow runs `npm audit --omit=dev` plus `npm audit signatures --omit=dev`.
- Shrinkwrap generation has an explicit allowlist for dependency lifecycle scripts; new lifecycle-script deps fail checks until reviewed.

## Share your OSS coding agent sessions

If you use Pi or other coding agents for open source work, please share your sessions.

Public OSS session data helps improve coding agents with real-world tasks, tool use, failures, and fixes instead of toy benchmarks.

For the full explanation, see [this post on X](https://x.com/badlogicgames/status/2037811643774652911).

To publish sessions, use [`badlogic/pi-share-hf`](https://github.com/badlogic/pi-share-hf). Read its README.md for setup instructions. All you need is a Hugging Face account, the Hugging Face CLI, and `pi-share-hf`.

You can also watch [this video](https://x.com/badlogicgames/status/2041151967695634619), where I show how I publish my `pi-mono` sessions.

I regularly publish my own `pi-mono` work sessions here:

- [badlogicgames/pi-mono on Hugging Face](https://huggingface.co/datasets/badlogicgames/pi-mono)

## License

MIT

<p align="center">
  <a href="https://pi.dev">pi.dev</a> domain graciously donated by
  <br /><br />
  <a href="https://exe.dev"><img src="packages/coding-agent/docs/images/exy.png" alt="Exy mascot" width="48" /><br />exe.dev</a>
</p>
