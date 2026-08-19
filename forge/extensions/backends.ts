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
import {
	activeProfileName,
	applyProfile,
	DEFAULT_BACKENDS,
	loadBackends,
	saveBackends,
	setDelegation,
} from "../lib/backends.mjs";
import { loadForgeSettings, resolveConnectedServices } from "../lib/connected-services.mjs";

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
	const settings = loadForgeSettings();
	const taskModel =
		settings.taskModel && typeof settings.taskModel === "object" && !Array.isArray(settings.taskModel)
			? (settings.taskModel as { enabled?: boolean; baseUrl?: string })
			: {};
	const contextBudget =
		settings.contextBudget && typeof settings.contextBudget === "object" && !Array.isArray(settings.contextBudget)
			? (settings.contextBudget as { useTaskModel?: boolean })
			: {};
	const verifyName: string | null = services.verify?.service ?? null;
	const verifyLane = verifyName
		? (services as Record<string, { enabled?: boolean; baseUrl?: string }>)[verifyName]
		: undefined;
	const lines = [
		`Active setup: ${active}`,
		`  chat          ${services.chat.baseUrl} (${services.chat.contextTokens} ctx)`,
		`  think         ${services.think.baseUrl}`,
		`  bulk lanes    ${services.bulk.lanes.join(", ")}${services.bulk.lanes.length > 1 ? " (fan-out across GPUs)" : ""}`,
		verifyLane?.enabled
			? `  verify        ${verifyName} → ${verifyLane.baseUrl}`
			: `  verify        primary think (${services.think.baseUrl})`,
	];
	if (services.chat2.enabled) lines.push(`  chat2         ${services.chat2.baseUrl}`);
	if (services.think2.enabled) lines.push(`  think2        ${services.think2.baseUrl}`);
	lines.push(
		services.delegate.enabled
			? `  delegation    on → ${services.delegate.baseUrl} (${services.delegate.model})`
			: "  delegation    off (forge_delegate runs on primary chat)",
		taskModel.enabled && contextBudget.useTaskModel
			? `  compaction    offload → ${taskModel.baseUrl}`
			: "  compaction    on primary (no offload)",
		`  embeddings    ${services.embeddings.url}`,
		`  transcription ${services.transcription.baseUrl}`,
		`  ocr           ${services.ocr.url}`,
	);
	const others = Object.keys(config.profiles ?? {}).filter((name) => name !== active);
	if (others.length) lines.push(`Other setups: ${others.join(", ")} — /backend use <name>`);
	return lines;
}

/**
 * Make sure a shipped setup exists in the runtime backends.json before applying it,
 * so `/parallel` works on an install whose file predates the setup. Seeds the
 * template from DEFAULT_BACKENDS; a setup the user already customized is left as-is.
 */
function ensureShippedProfile(name: string): void {
	const config = loadBackends();
	if (config.profiles?.[name]) return;
	const template = (DEFAULT_BACKENDS.profiles as Record<string, unknown>)[name];
	if (!template) return;
	config.profiles = { ...(config.profiles ?? {}), [name]: template };
	saveBackends(config);
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

	// One-word failsafe: drop back to the single-model setup when the backend
	// hardware changes (GPU 2 off / re-cabled). This reverts EVERY dual-GPU knob —
	// verify lane, bulk fan-out, delegation, and compaction offload — so nothing
	// keeps pointing skills at an absent GPU 2 (the revert lives in applyProfile).
	pi.registerCommand("single", {
		description: "Failsafe: switch to the single-model setup (revert all dual-GPU routing)",
		handler: async (_args, ctx) => {
			try {
				announce(ctx, applyProfile({ name: "single" }) as ApplyResult);
			} catch (error) {
				ctx.ui.notify(`/single failed: ${error instanceof Error ? error.message : String(error)}`, "error");
			}
		},
	});

	// Counterpart: switch to the two-GPU setup. Seeds the shipped profile first so
	// it works even on an install whose backends.json predates it.
	pi.registerCommand("parallel", {
		description: "Switch to the two-GPU distributed-parallel setup (both GPUs at once)",
		handler: async (_args, ctx) => {
			try {
				ensureShippedProfile("distributed-parallel");
				announce(ctx, applyProfile({ name: "distributed-parallel" }) as ApplyResult);
			} catch (error) {
				ctx.ui.notify(`/parallel failed: ${error instanceof Error ? error.message : String(error)}`, "error");
			}
		},
	});
}
