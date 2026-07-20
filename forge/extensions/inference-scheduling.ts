import { randomUUID } from "node:crypto";
import { mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { getForgeAgentDir, resolveConnectedServices } from "../lib/connected-services.mjs";

interface SchedulingConfiguration {
	enabled: boolean;
	interactiveSlot: number;
}

export function addInteractiveSlot(payload: unknown, configuration: SchedulingConfiguration): unknown {
	if (!configuration.enabled || !payload || typeof payload !== "object" || Array.isArray(payload)) return payload;
	return { ...payload, id_slot: configuration.interactiveSlot, cache_prompt: true };
}

export default function inferenceSchedulingExtension(pi: ExtensionAPI) {
	const chat = resolveConnectedServices().chat;
	const scheduling = chat.scheduling;
	if (!scheduling.enabled) return;

	const leaseDirectory = join(getForgeAgentDir(), "inference-leases");
	const leasePath = join(leaseDirectory, `${process.pid}-${randomUUID()}.json`);
	let providerActive = false;

	const refreshLease = () => {
		if (!providerActive) return;
		mkdirSync(leaseDirectory, { recursive: true });
		const temporary = `${leasePath}.tmp`;
		writeFileSync(
			temporary,
			`${JSON.stringify({ pid: process.pid, kind: "interactive", slot: scheduling.interactiveSlot, updatedAtMs: Date.now() })}\n`,
			{ encoding: "utf8", mode: 0o600 },
		);
		renameSync(temporary, leasePath);
	};

	const clearLease = () => {
		providerActive = false;
		rmSync(leasePath, { force: true });
	};

	pi.on("before_provider_request", (event, ctx) => {
		if (ctx.model?.provider !== "forge-local") return undefined;
		providerActive = true;
		refreshLease();
		return addInteractiveSlot(event.payload, scheduling);
	});

	pi.on("message_update", (event) => {
		if (event.message.role === "assistant") refreshLease();
	});

	pi.on("message_end", (event) => {
		if (event.message.role === "assistant") clearLease();
	});

	pi.on("agent_end", clearLease);
	pi.on("session_shutdown", clearLease);
}
