import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
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

	const backgroundLeaseActive = () => {
		try {
			return readdirSync(leaseDirectory).some((name) => {
				try {
					const lease = JSON.parse(readFileSync(join(leaseDirectory, name), "utf8")) as { kind?: unknown; updatedAtMs?: unknown };
					return lease.kind === "background" && typeof lease.updatedAtMs === "number" && Date.now() - lease.updatedAtMs < 15_000;
				} catch {
					return false;
				}
			});
		} catch {
			return false;
		}
	};

	const waitForBackgroundYield = () => {
		const deadline = Date.now() + 5_000;
		const signal = new Int32Array(new SharedArrayBuffer(4));
		while (backgroundLeaseActive() && Date.now() < deadline) Atomics.wait(signal, 0, 0, 25);
		if (backgroundLeaseActive()) throw new Error("Background inference did not yield its cooperative lease within 5 seconds.");
	};

	pi.on("before_provider_request", (event, ctx) => {
		if (ctx.model?.provider !== "forge-local") return undefined;
		providerActive = true;
		refreshLease();
		waitForBackgroundYield();
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
