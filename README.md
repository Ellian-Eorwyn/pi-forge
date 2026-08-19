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

pi-forge is a research and document-processing-focused fork of pi. It empowers users to automate complex workflows involving research, documentation, file manipulation, and data analysis using AI agents. By equipping agents with workflow skills and deterministic tools, pi-forge acts as an intelligent assistant capable of parsing, summarizing, organizing, and synthesizing massive amounts of information.

### What Can You Do With pi-forge?
- **Research & Web Collection:** Search the web and archive websites to compile research repositories.
- **Data & Document Processing:** Clean raw transcripts, convert documents (Markdown, EPUB, etc.), and analyze spreadsheets.
- **Content Synthesis:** Extract literature, build action plans, and synthesize polished deliverables from raw documents.
- **Workflow Automation:** Automatically organize messy folders and ship small, reviewable codebase changes.

### Included Skills
The `@ellian-eorwyn/pi-forge` package ships Agent Skills under `forge/skills/<name>/SKILL.md`. Skill directory names match their `SKILL.md` frontmatter names, use lowercase hyphenated names, and keep scripts, assets, and references relative to the skill directory. The installed Pi settings point at the package root so Pi and the MCP bridge load skills from the installed package, not from a cloned repository.

pi-forge uses `forge/CAPABILITIES.md` as a compact startup capability index. Full workflows stay in `forge/skills/<name>/SKILL.md` and are loaded on demand. Each skill has a `manifest.json` describing its package boundary and available real scripts/tools; repeatable mechanical operations should live under the skill directory as scripts/tools, while skills keep workflow judgment, review standards, provenance expectations, and output shape. This is a distribution/profile boundary, not a full extension rewrite.

The `forge` profile provides agents with the following built-in skills:
<!-- forge:readme-skills start -->
- **`coding`**: Inspect repos and ship small reviewable changes
- **`document-ingest`**: Normalize documents with provenance
- **`file-conversion`**: Convert files, including Markdown and EPUB
- **`literature-extraction`**: Extract structured evidence from research documents
- **`literature-library`**: Turn a citation list into named PDFs and Markdown
- **`organize-folder`**: Sort a messy folder via a reviewable manifest
- **`personal-admin`**: Summarize personal documents into action plans
- **`project-extraction`**: Track deliverables, dates, and risks across project records
- **`report-output`**: Assemble polished deliverables from processed outputs
- **`reviewer-2`**: Peer-review a draft article without modifying it
- **`site-builder`**: Build a static website from a content folder
- **`skill-builder`**: Create, revise, audit, and package skills
- **`skill-tuner`**: Mine a session log for skill improvements, with cited evidence
- **`spreadsheet-analysis`**: Analyze and enrich tabular datasets
- **`transcript-cleanup`**: Clean and structure raw transcripts
- **`transcription`**: Transcribe audio or video, then correct and clean it
- **`vault-capture`**: Turn a braindump into schema-valid vault notes
- **`vault-compose`**: Compose a vault note from a typed set of held sources
- **`vault-connections`**: Search and connect vault notes, then publish validated research runs through reviewed inbox/wiki proposals
- **`vault-curator`**: Research how a field catalogues its records, then propose schema rows
- **`vault-media`**: Catalog books, films, television, music, and games
- **`vault-naturalist`**: Index phenology from species cards and record field observations
- **`vault-organizer`**: Classify and organize Obsidian notes from a schema note
- **`vault-projects`**: Resolve a project into its corpus and freeze it for handoff
- **`vault-transcripts`**: Classify, clean, and summarize raw transcripts into vault notes
- **`vault-wiki`**: Expand thin wiki entity notes into cited reference cards
- **`web-collection`**: Archive and organize web sources
- **`web-research`**: Quick or deep web research with provenance and validation
<!-- forge:readme-skills end -->

The vault skills share three optional vault-owned layers, each a note the vault
itself holds and each disableable per run: a **voice policy** (`--voice`), a
**lexicon** of terms and speakers (`--lexicon`), and a **personal context**
register of cards (`--profile`). Not every skill takes every layer — a layer
reaches a skill only where it has something to do there, so the lexicon is a
`vault-transcripts` and `vault-capture` flag while voice and personal context
are wider. The personal-context layer informs how notes are
filed, drafted, and searched — never what they say. Its cards are structurally
barred from becoming note content, cards gated to a route are refused wherever
the destination is still undecided, and a missing or ambiguous register costs the
layer rather than the run.


### Skills vs. Extensions (Architectural Boundary)
To maintain a clear architectural boundary, future work on this repository should adhere to the following distinction:
- **Skills (Teaching by Instruction):** Located in `forge/skills/<name>/SKILL.md`, skills are low-complexity, Markdown-based instructional documents. They are used for **judgment**—defining workflows, routing tasks, setting standards, and providing "how-to" steps. If you are telling the agent *how to act* or what process to follow, it should be a Skill.
- **Extensions & Tools (Adding New Capabilities):** Located in `forge/extensions/` or as skill-local `scripts/`, these are high-complexity, functional TypeScript modules or scripts. They are used for **execution**—fetching, converting, extracting, or safely interacting with the system. They should adhere to a structured JSON contract ([forge/SCRIPT_TOOL_CONTRACT.md](forge/SCRIPT_TOOL_CONTRACT.md)). If you are giving the agent a *new mechanical power* or custom UI, it should be an Extension or Tool.

---

## Installation & Setup

pi-forge currently supports macOS, Linux, and Windows with npm and Node.js 22.19 or newer. Skills that ship Python scripts run on the `python3` already on your PATH and need 3.9 or newer, which every supported platform satisfies out of the box. New installs do not clone this repository. The installer creates one managed home at `~/.pi-forge`:

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

The macOS/Linux installer downloads the pi-forge GitHub source archive, packs the `forge` package locally, packs the Pi runtime packages from the upstream Pi GitHub source archive, and installs those packages into the same `~/.pi-forge/app` layout. No published `@ellian-eorwyn/pi-forge` or freshly published `@earendil-works/pi-coding-agent` npm package is required.

### 2. Update
To update pi-forge and its bundled Pi runtime packages from GitHub while preserving credentials, sessions, and settings:

**macOS / Linux:**
```bash
pi-forge-update
```

**Windows (PowerShell/CMD):**
```powershell
pi-forge-update
```
*(Or run `pi-forge-update.ps1`)*

`pi-forge-update` downloads the pi-forge GitHub source archive, packs pi-forge locally, packs the Pi runtime packages from the upstream Pi GitHub source archive, installs those tarballs into `~/.pi-forge/app` with `npm install --omit=dev --ignore-scripts`, refreshes managed configuration, and rewrites launchers.

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
| `PI_FORGE_PACKAGE_SPEC` | unset; default install packs the GitHub source archive |
| `PI_FORGE_PI_PACKAGE_SPEC` | unset; default install packs bundled Pi runtime packages from the upstream Pi GitHub source archive |
| `PI_FORGE_SOURCE_ARCHIVE_URL` | `https://github.com/Ellian-Eorwyn/pi-forge/archive/refs/heads/main.tar.gz` |
| `PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL` | `https://github.com/earendil-works/pi/archive/refs/heads/main.tar.gz` |
| `FORGE_SEARXNG_URL` | one-launch override for `connectedServices.searxng.baseUrl` |
| `FORGE_PLAYWRIGHT_WS_ENDPOINT` | one-launch override for `connectedServices.playwright.wsEndpoint` |
| `PI_FORGE_SKIP_MOSHI_HOOK` | unset; set to skip the optional Moshi hook step during install and update |
| `PI_FORGE_MOSHI_HOOK_BIN` | unset; resolved from `PATH` then `~/.local/bin/moshi-hook` |

`PI_FORGE_PACKAGE_SPEC` and `PI_FORGE_PI_PACKAGE_SPEC` can point at `file:<packed-tarball>` for local release and migration smoke tests. Set `PI_FORGE_PACKAGE_SPEC=@ellian-eorwyn/pi-forge@latest` or `PI_FORGE_PI_PACKAGE_SPEC=@earendil-works/pi-coding-agent@latest` only if you intentionally want to install published npm packages. `PI_FORGE_SOURCE_ARCHIVE_URL` overrides the GitHub source archive used for default pi-forge installs and updates. `PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL` overrides the upstream Pi archive used for default runtime package installs and updates. Checkout-linked development installs are still available with `./install.sh --dev-link`; that mode links launchers and package resources to the checkout instead of the npm app.

Persistent local backend settings live in `~/.pi-forge/agent/settings.json` under `connectedServices`. The installed defaults are SearXNG at `http://llms/searxng` and Playwright rendered browsing at `ws://llms/playwright`.

#### Switching backend setups

`~/.pi-forge/agent/backends.json` is a one-file switcher for whole backend setups — where embedding, OCR, transcription, the primary model, and the delegation model each run. It holds named setups and an `active` pointer; switching one file (or running one command) repoints them all. The shipped setups are `single` (one image-capable model on `llms`, no delegation — the default) and `distributed` (embedding + transcription local, plus a vision-free delegation backend on a second GPU that `forge_delegate` offloads to in parallel).

- From inside the agent: `/backend` shows the active setup, `/backend use <name>` switches, and `/backend on|off` toggles delegation. Skills and `forge_delegate` pick up the change on their next call; the interactive model updates on your next session.
- From the terminal: `node <package>/scripts/backends.mjs [list|show|use <name>|apply|delegation on|off]`.

Delegation and OCR also have their own `connectedServices` entries (`delegate`, `ocr`), and every field is still overridable per-launch by the matching `FORGE_*` env var (`FORGE_DELEGATE_URL`, `FORGE_EMBEDDINGS_URL`, `FORGE_TRANSCRIPTION_URL`, `FORGE_GLMOCR_URL`, …).

### Optional Moshi hooks

`moshi-hook` resolves its `pi` target from `PI_CODING_AGENT_DIR`, so a standard `moshi-hook install` covers `~/.pi` and leaves this distribution's `~/.pi-forge/agent` uncovered. Install and `pi-forge-update` close that gap themselves: when `moshi-hook` is present on the host they have it generate `~/.pi-forge/agent/extensions/moshi-hooks.ts`, which pi-forge then discovers like any other agent-directory extension. The generated hook is always the daemon's own current version — nothing is vendored here. The daemon is restarted (`systemctl --user restart moshi-hook.service`) only when the hook actually changed, so a no-op update cannot drop a live session's bridge; elsewhere the step prints a one-line restart reminder. Hosts without `moshi-hook` install exactly as before and print nothing.

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
