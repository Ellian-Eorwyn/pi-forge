/**
 * `/backend` — switch and inspect backend setups from inside the agent.
 *
 *   /backend                 show the active setup and where everything resolves
 *   /backend list            list the named setups
 *   /backend use <name>      make <name> active and apply it (also `/backend <name>`)
 *   /backend on | off        toggle delegation on the active setup
 *
 * The heavy lifting is the `backends.mjs` library (pure Node, loads under the
 * extension sandbox), which writes settings.json + models.json in place. Skills and
 * `forge_delegate` read settings.json fresh on their next call, so those switch
 * immediately; the interactive agent's own model comes from models.json, loaded at
 * launch, so that part lands on the next session rather than mid-turn.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { activeProfileName, applyProfile, loadBackends, setDelegation } from "../lib/backends.mjs";
import { resolveConnectedServices } from "../lib/connected-services.mjs";

interface ApplyResult {
	profile: string;
	description: string;
	delegation: string;
	missingProviders: string[];
}

function statusLines(): string[] {
	const config = loadBackends();
	const active = activeProfileName(config);
	const services = resolveConnectedServices({});
	const lines = [
		`Active setup: ${active}`,
		`  chat          ${services.chat.baseUrl} (${services.chat.contextTokens} ctx)`,
		`  think         ${services.think.baseUrl}`,
		services.delegate.enabled
			? `  delegation    on → ${services.delegate.baseUrl} (${services.delegate.model})`
			: "  delegation    off (forge_delegate runs on primary chat)",
		`  embeddings    ${services.embeddings.url}`,
		`  transcription ${services.transcription.baseUrl}`,
		`  ocr           ${services.ocr.url}`,
	];
	const others = Object.keys(config.profiles ?? {}).filter((name) => name !== active);
	if (others.length) lines.push(`Other setups: ${others.join(", ")} — /backend use <name>`);
	return lines;
}

function announce(ctx: ExtensionContext, result: ApplyResult): void {
	const detail =
		result.delegation === "on"
			? "Delegation on (parallel secondary)."
			: "Delegation off (forge_delegate runs on the primary).";
	const interactive =
		result.missingProviders.length > 0
			? " Skills and forge_delegate switch now."
			: " Skills and forge_delegate switch now; the interactive model updates on your next session.";
	ctx.ui.notify(`Backend setup: ${result.profile}. ${detail}${interactive}`, "info");
}

export default function backendsExtension(pi: ExtensionAPI): void {
	pi.registerCommand("backend", {
		description: "Switch or inspect backend setups (embedding/OCR/transcription/primary/delegation)",
		getArgumentCompletions: (prefix) => {
			try {
				const config = loadBackends();
				const verbs = ["list", "show", "use", "on", "off"];
				const names = Object.keys(config.profiles ?? {});
				return [...verbs, ...names]
					.filter((item) => item.startsWith(prefix))
					.map((item) => ({ value: item, label: item }));
			} catch {
				return null;
			}
		},
		handler: async (args, ctx) => {
			const parts = (typeof args === "string" ? args : "").trim().split(/\s+/).filter(Boolean);
			const [verb, name] = parts;
			try {
				if (!verb || verb === "show" || verb === "status") {
					ctx.ui.notify(statusLines().join("\n"), "info");
					return;
				}
				if (verb === "list") {
					const config = loadBackends();
					const active = activeProfileName(config);
					const lines = Object.entries(config.profiles ?? {}).map(([key, profile]) => {
						const mark = key === active ? "*" : " ";
						const description =
							typeof (profile as { description?: unknown }).description === "string"
								? (profile as { description: string }).description
								: "";
						return `${mark} ${key}${description ? ` — ${description}` : ""}`;
					});
					ctx.ui.notify(lines.join("\n") || "No setups defined.", "info");
					return;
				}
				if (verb === "on" || verb === "off") {
					announce(ctx, setDelegation({ enabled: verb === "on" }) as ApplyResult);
					return;
				}
				// `use <name>` or a bare `<name>` that matches a setup.
				const target = verb === "use" ? name : verb;
				if (!target) {
					ctx.ui.notify("Usage: /backend [list | show | use <name> | on | off]", "warning");
					return;
				}
				const config = loadBackends();
				if (!config.profiles?.[target]) {
					const known = Object.keys(config.profiles ?? {}).join(", ") || "(none)";
					ctx.ui.notify(`Unknown setup "${target}". Known: ${known}`, "error");
					return;
				}
				announce(ctx, applyProfile({ name: target }) as ApplyResult);
			} catch (error) {
				ctx.ui.notify(`/backend failed: ${error instanceof Error ? error.message : String(error)}`, "error");
			}
		},
	});
}
