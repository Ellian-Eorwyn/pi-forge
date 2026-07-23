/**
 * Vault Workflow Extension
 *
 * A single-session plan -> execute -> verify loop for local-model vault work.
 *
 * The pi-forge agent brain is one local model (forge-local, http://llms:8008).
 * That server "thinks" (spends hidden reasoning tokens) unless a closed empty
 * <think></think> block is prefilled as the assistant turn. This extension uses
 * that toggle to give each phase the right behaviour from the single running
 * model:
 *
 *   plan    - thinking ON,  read-only tools  -> interview + write a plan
 *   execute - thinking OFF, full vault tools -> apply the plan, one step, on approval
 *   verify  - thinking ON,  read-only tools  -> check the result against the plan
 *
 * Phase is persisted so it survives a restart. Nothing here selects a second
 * provider; a future two-GPU setup can instead point execute at a real
 * non-thinking server by editing the ROLE_ENDPOINTS note below.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type Phase = "off" | "plan" | "execute" | "verify";

// Same prefill vault-organizer.py uses. On llama.cpp-style servers the model
// continues from the closed think block and skips reasoning entirely.
const THINK_PREFILL = "<think>\n\n</think>\n\n";

// Desired tools per phase; intersected with the tools that actually exist so an
// environment without (say) a standalone "grep" tool still works.
const READONLY_DESIRED = ["read", "bash", "grep", "glob", "find", "ls", "questionnaire"];
const EXECUTE_DESIRED = [...READONLY_DESIRED, "edit", "write", "multiedit"];

const PHASE_LABEL: Record<Phase, string> = {
	off: "",
	plan: "📝 plan",
	execute: "⚙ execute",
	verify: "✓ verify",
};

const CONTEXT_CUSTOM_TYPE = "vault-workflow-context";

// Read-only phases still expose bash (for doctor/status/grep). Block the obvious
// mutation vectors so "thinking out loud" cannot change the vault.
const DESTRUCTIVE_BASH = /(^|\s|\|)(rm|rmdir|mv|dd|truncate|tee)\s|--apply\b|\s>>?\s|\bgit\s+(commit|push|mv|rm)\b|\bsed\s+-i\b|\bmkdir\b/;

function looksDestructive(command: string): boolean {
	return DESTRUCTIVE_BASH.test(command);
}

function planPrompt(): string {
	return `[VAULT WORKFLOW — PLAN PHASE]
You are planning a change to the user's Obsidian vault. This is READ-ONLY: you have read, bash (read-only), grep/find/ls, and questionnaire. You cannot edit or write files.

1. Ask clarifying questions with the questionnaire tool until the goal is unambiguous — do not guess.
2. Investigate the vault and the schema note to ground the plan in the real current state.
3. Write a detailed, numbered plan and show it to the user. Cover: exactly what changes, which files or skills are used, how the result will be verified, and any risks.
4. Make NO changes. End by asking the user to approve the plan and run /execute.

The schema note (99 Meta/99.02 Schemas/0.00 Vault Schema.md) is the sole source of truth: folder structure and frontmatter are derived from it.`;
}

function executePrompt(): string {
	return `[VAULT WORKFLOW — EXECUTE PHASE]
Carry out the approved plan, ONE change at a time. You have full tools, scoped to the vault.

For each change:
- Do the safe version first: run the skill without --apply (a dry run), or show the exact edit you will make.
- Show the user the result and WAIT for an explicit "yes" before applying (--apply) or writing files. Never apply without approval.
- Prefer the vetted skills over free-form edits. Run vault-organizer with:
    --base-url http://llms:8008/v1/chat/completions --think-prefill
  so its bulk per-note calls stay fast (non-thinking) on the running server.
- After editing the schema note, run "vault-organizer.py doctor --vault <vault>" and confirm it parses before continuing.
- Never delete notes (the tools quarantine, recoverably). Keep every path inside the vault.

When all approved changes are applied, tell the user to run /verify.`;
}

function verifyPrompt(): string {
	return `[VAULT WORKFLOW — VERIFY PHASE]
READ-ONLY. Confirm the executed change matches the plan.

- Run "vault-organizer.py doctor" and, if a run directory exists, "vault-organizer.py status --run <dir>".
- grep and read the vault to confirm the intended folders and frontmatter exist.
- Compare against the plan's success criteria, point by point.

Report clearly what passed, what did not, and any follow-ups. Make no changes.`;
}

function phasePrompt(phase: Phase): string | undefined {
	if (phase === "plan") return planPrompt();
	if (phase === "execute") return executePrompt();
	if (phase === "verify") return verifyPrompt();
	return undefined;
}

export default function vaultWorkflowExtension(pi: ExtensionAPI): void {
	let phase: Phase = "off";

	function allToolNames(): string[] {
		try {
			return pi.getAllTools().map((tool) => tool.name);
		} catch {
			return [];
		}
	}

	function toolsForPhase(target: Phase): string[] {
		const available = new Set(allToolNames());
		if (available.size === 0) {
			// No introspection available (e.g. in a unit-test stub): fall back to desired.
			return target === "execute" ? EXECUTE_DESIRED : target === "off" ? EXECUTE_DESIRED : READONLY_DESIRED;
		}
		if (target === "off") return [...available];
		const desired = target === "execute" ? EXECUTE_DESIRED : READONLY_DESIRED;
		return desired.filter((name) => available.has(name));
	}

	function updateStatus(ctx: ExtensionContext): void {
		const label = PHASE_LABEL[phase];
		if (!label) {
			ctx.ui.setStatus("vault-workflow", undefined);
			return;
		}
		const color = phase === "execute" ? "accent" : "warning";
		ctx.ui.setStatus("vault-workflow", ctx.ui.theme.fg(color, label));
	}

	function persist(): void {
		pi.appendEntry("vault-workflow", { phase });
	}

	function enter(target: Phase, ctx: ExtensionContext): void {
		phase = target;
		pi.setActiveTools(toolsForPhase(target));
		persist();
		updateStatus(ctx);
		if (target === "off") {
			ctx.ui.notify("Vault workflow off. Full tools restored.", "info");
		} else {
			ctx.ui.notify(
				`Vault workflow: ${target} phase. ${target === "execute" ? "Full vault tools, thinking off." : "Read-only, thinking on."}`,
				"info",
			);
		}
	}

	pi.registerCommand("plan", {
		description: "Vault workflow: plan phase (read-only, thinking on)",
		handler: async (_args, ctx) => enter("plan", ctx),
	});
	pi.registerCommand("execute", {
		description: "Vault workflow: execute phase (full tools, thinking off, approve each change)",
		handler: async (_args, ctx) => enter("execute", ctx),
	});
	pi.registerCommand("verify", {
		description: "Vault workflow: verify phase (read-only, thinking on)",
		handler: async (_args, ctx) => enter("verify", ctx),
	});
	pi.registerCommand("workflow", {
		description: "Show or set the vault workflow phase (off | plan | execute | verify)",
		handler: async (args, ctx) => {
			const requested = (typeof args === "string" ? args : "").trim().toLowerCase();
			if (requested === "off" || requested === "plan" || requested === "execute" || requested === "verify") {
				enter(requested, ctx);
				return;
			}
			ctx.ui.notify(`Vault workflow phase: ${phase}. Use /plan, /execute, /verify, or /workflow off.`, "info");
		},
	});

	// Thinking toggle: in execute phase, prefill a closed think block so the
	// single local server skips reasoning for fast mechanical turns.
	pi.on("before_provider_request", (event, ctx) => {
		if (phase !== "execute") return undefined;
		if (ctx.model?.provider !== "forge-local") return undefined;
		const payload = event.payload;
		if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
		const body = payload as { messages?: unknown };
		if (!Array.isArray(body.messages)) return undefined;
		return { ...body, messages: [...body.messages, { role: "assistant", content: THINK_PREFILL }] };
	});

	// Per-phase role and rules, injected fresh each turn.
	pi.on("before_agent_start", async () => {
		const content = phasePrompt(phase);
		if (!content) return undefined;
		return { message: { customType: CONTEXT_CUSTOM_TYPE, content, display: false } };
	});

	// Read-only guarantee for plan/verify: block mutating bash (edit/write are
	// already absent from the active tool set).
	pi.on("tool_call", async (event) => {
		if (phase !== "plan" && phase !== "verify") return undefined;
		if (event.toolName !== "bash") return undefined;
		const command = String((event.input as { command?: unknown }).command ?? "");
		if (looksDestructive(command)) {
			return {
				block: true,
				reason: `Vault workflow ${phase} phase is read-only. Switch to /execute to apply changes.\nBlocked command: ${command}`,
			};
		}
		return undefined;
	});

	// Drop stale phase-context messages once the workflow is off.
	pi.on("context", async (event) => {
		if (phase !== "off") return undefined;
		const messages = (event as { messages?: unknown }).messages;
		if (!Array.isArray(messages)) return undefined;
		return {
			messages: messages.filter((message) => (message as { customType?: string }).customType !== CONTEXT_CUSTOM_TYPE),
		};
	});

	// Restore phase on session start / resume.
	pi.on("session_start", async (_event, ctx) => {
		try {
			const entries = ctx.sessionManager.getEntries();
			const last = entries
				.filter((entry: { type: string; customType?: string }) => entry.type === "custom" && entry.customType === "vault-workflow")
				.pop() as { data?: { phase?: Phase } } | undefined;
			const restored = last?.data?.phase;
			if (restored === "plan" || restored === "execute" || restored === "verify" || restored === "off") {
				phase = restored;
			}
		} catch {
			// no session manager in a stub; leave phase = off
		}
		if (phase !== "off") pi.setActiveTools(toolsForPhase(phase));
		updateStatus(ctx);
	});
}
