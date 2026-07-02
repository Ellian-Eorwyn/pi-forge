# pi-forge

`@ellian-eorwyn/pi-forge` packages the pi-forge launchers, managed Pi profile,
skills, prompts, themes, extensions, and MCP bridge.

Installers place the package under `~/.pi-forge/app`, stable launchers under
`~/.pi-forge/bin`, and agent state under `~/.pi-forge/agent`.
The default macOS/Linux installer and `pi-forge-update` pack pi-forge from the
GitHub source archive; no published `@ellian-eorwyn/pi-forge` npm package is
required. `pi-forge-update` also refreshes `@earendil-works/pi-coding-agent@latest`
unless `PI_FORGE_PI_PACKAGE_SPEC` overrides it.

```bash
curl -fsSL https://raw.githubusercontent.com/Ellian-Eorwyn/pi-forge/main/install.sh | bash
pi-forge-update
```
