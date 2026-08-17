import { randomUUID } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	getForgeAgentDir,
	LOCAL_MODEL_PROVIDERS,
	resolveConnectedServices,
	serviceNameForLocalProvider,
} from "../lib/connected-services.mjs";

interface SchedulingConfiguration {
	enabled: boolean;
	interactiveSlot: number;
}

interface SchedulingServices {
	chat?: { scheduling?: SchedulingConfiguration };
	think?: { scheduling?: SchedulingConfiguration };
}

export function addInteractiveSlot(payload: unknown, configuration: SchedulingConfiguration): unknown {
	if (!configuration.enabled || !payload || typeof payload !== "object" || Array.isArray(payload)) return payload;
	return { ...payload, id_slot: configuration.interactiveSlot, cache_prompt: true };
}

// The interactive session runs on either thinking profile by default and moves
// to the non-thinking one during the vault workflow's execute phase. All three
// front the same backend as background batch work, so every local provider must
// select the slot policy for its connected service.
export function schedulingForProvider(
	provider: string | undefined,
	services: SchedulingServices,
): SchedulingConfiguration | undefined {
	const service = serviceNameForLocalProvider(provider) as "think" | "chat" | null;
	const scheduling = service ? services[service]?.scheduling : undefined;
	return scheduling?.enabled ? scheduling : undefined;
}

export default function inferenceSchedulingExtension(pi: ExtensionAPI) {
	const services = resolveConnectedServices();
	const schedulingFor = (provider: string | undefined) => schedulingForProvider(provider, services);
	if (!Object.values(LOCAL_MODEL_PROVIDERS).some((provider) => schedulingFor(provider))) return;

	const leaseDirectory = join(getForgeAgentDir(), "inference-leases");
	const leasePath = join(leaseDirectory, `${process.pid}-${randomUUID()}.json`);
	let providerActive = false;
	let activeSlot = services.think.scheduling.interactiveSlot;

	const refreshLease = () => {
		if (!providerActive) return;
		mkdirSync(leaseDirectory, { recursive: true });
		const temporary = `${leasePath}.tmp`;
		writeFileSync(
			temporary,
			`${JSON.stringify({ pid: process.pid, kind: "interactive", slot: activeSlot, updatedAtMs: Date.now() })}\n`,
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
					const lease = JSON.parse(readFileSync(join(leaseDirectory, name), "utf8")) as {
						kind?: unknown;
						updatedAtMs?: unknown;
					};
					return (
						lease.kind === "background" &&
						typeof lease.updatedAtMs === "number" &&
						Date.now() - lease.updatedAtMs < 15_000
					);
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
		if (backgroundLeaseActive())
			throw new Error("Background inference did not yield its cooperative lease within 5 seconds.");
	};

	pi.on("before_provider_request", (event, ctx) => {
		const active = schedulingFor(ctx.model?.provider);
		if (!active) return undefined;
		activeSlot = active.interactiveSlot;
		providerActive = true;
		refreshLease();
		waitForBackgroundYield();
		return addInteractiveSlot(event.payload, active);
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
