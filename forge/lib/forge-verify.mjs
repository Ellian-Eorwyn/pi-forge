/**
 * Thinking-model review of work produced by the non-thinking model, for skills
 * written in JavaScript. The `.mjs` counterpart of `forge_verify.py`.
 *
 * Bulk per-item work runs on the non-thinking backend because reasoning about
 * each item separately costs hundreds of hidden tokens and usually changes
 * nothing. "Usually" is the problem, so every batch is reviewed afterwards by
 * the thinking backend, and anything it flags is redone with reasoning.
 *
 * The economics only work because review is batched: one call carries ~20 items,
 * so a run of 500 buys full coverage for ~25 thinking calls instead of 500.
 * Escalation is per-item and rare, and that is where the reasoning budget goes.
 *
 * Deterministic checks belong *before* this: they are free and exact, and
 * running them first means the thinking model spends its budget on judgment
 * rather than on catching malformed JSON.
 *
 * Verification is advisory about quality, never about safety. It can flag,
 * escalate, and hand something to a human. It never silently drops a result.
 */

import { existsSync } from "node:fs";
import { callJson, ChatError, PreemptedError } from "./forge-llm.mjs";
import { appendJsonlFsync, readJsonlRecoverTail } from "./run-state.mjs";

export const DEFAULT_PACKET_SIZE = 20;
export const DEFAULT_PACKET_CHARACTERS = 24_000;
export const VERDICT_OK = "ok";
export const VERDICT_FLAG = "flag";

export const VERDICT_CONTRACT = `Return exactly one JSON object and nothing else:
{"verdicts": [{"id": "<item id>", "verdict": "ok" | "flag", "reason": "<why, only when flagged>"}]}

Include one verdict for every id you were given, and no ids you were not given.
Flag an item only when it is actually wrong or unjustifiable on the evidence
shown. Do not flag an item merely because you would have phrased it differently.

The evidence shown is the only evidence. What you happen to know about the
subject is not part of it: an item that matches what you would have expected is
not thereby supported, and one that surprises you is not thereby wrong. Judge
each item against what it was given, and where that is not enough to tell, flag
it and say so rather than deciding from memory.
`;

export class VerificationError extends Error {
	constructor(message) {
		super(message);
		this.name = "VerificationError";
	}
}

/** Group items into review packets bounded by count and serialized size. */
export function buildPackets(items, packetSize = DEFAULT_PACKET_SIZE, budgetCharacters = DEFAULT_PACKET_CHARACTERS) {
	const packets = [];
	let current = [];
	let characters = 0;
	for (const item of items) {
		const size = JSON.stringify(item).length;
		const tooMany = current.length >= packetSize;
		const tooLarge = current.length > 0 && characters + size > budgetCharacters;
		if (tooMany || tooLarge) {
			packets.push(current);
			current = [];
			characters = 0;
		}
		current.push(item);
		characters += size;
	}
	if (current.length) packets.push(current);
	return packets;
}

/** Verdicts already recorded, so a resumed run does not re-review them. */
export function loadVerdicts(journalPath) {
	if (!journalPath || !existsSync(journalPath)) return {};
	const { rows } = readJsonlRecoverTail(journalPath, { repair: true });
	const verdicts = {};
	for (const row of rows) {
		if (row.id !== undefined && row.id !== null && "verdict" in row) verdicts[row.id] = row;
	}
	return verdicts;
}

/** Escalations already attempted, so a resumed run does not redo them. */
export function loadEscalations(journalPath) {
	if (!journalPath || !existsSync(journalPath)) return {};
	const { rows } = readJsonlRecoverTail(journalPath, { repair: true });
	const attempts = {};
	for (const row of rows) {
		if (row.id !== undefined && row.id !== null && "escalated" in row) attempts[row.id] = row;
	}
	return attempts;
}

function parseVerdicts(value, expectedIds) {
	if (!value || typeof value !== "object" || !Array.isArray(value.verdicts)) {
		throw new VerificationError('response must be an object with a "verdicts" array');
	}
	const expected = new Set(expectedIds);
	const seen = {};
	for (const entry of value.verdicts) {
		if (!entry || typeof entry !== "object") throw new VerificationError("every verdict must be an object");
		const identifier = entry.id;
		const verdict = entry.verdict;
		if (!expected.has(identifier)) throw new VerificationError(`verdict for unknown id ${JSON.stringify(identifier)}`);
		if (verdict !== VERDICT_OK && verdict !== VERDICT_FLAG) {
			throw new VerificationError(`verdict for ${JSON.stringify(identifier)} must be "${VERDICT_OK}" or "${VERDICT_FLAG}"`);
		}
		seen[identifier] = { verdict, reason: String(entry.reason ?? "").trim() };
	}
	const missing = expectedIds.filter((identifier) => !(identifier in seen));
	if (missing.length) throw new VerificationError(`missing verdicts for: ${missing.slice(0, 5).join(", ")}`);
	for (const [identifier, entry] of Object.entries(seen)) {
		if (entry.verdict === VERDICT_FLAG && !entry.reason) throw new VerificationError(`flagged ${JSON.stringify(identifier)} without a reason`);
	}
	return seen;
}

/** One packet, with a single corrective retry that shows the model its own contract violation. */
async function verifyOne(service, messages, expected, background, timeoutMs) {
	try {
		const { value } = await callJson(service, messages, { background, timeoutMs, task: "verify", responseFormat: { type: "json_object" } });
		return parseVerdicts(value, expected);
	} catch (error) {
		if (error instanceof PreemptedError) throw error;
		if (!(error instanceof VerificationError) && !(error instanceof ChatError)) throw error;
		const repair = [
			...messages,
			{
				role: "user",
				content: `That response was unusable: ${error.message}. Return corrected JSON only, with exactly one verdict for each of these ids: ${JSON.stringify(expected)}`,
			},
		];
		const { value } = await callJson(service, repair, { background, timeoutMs, task: "verify-repair", responseFormat: { type: "json_object" } });
		return parseVerdicts(value, expected);
	}
}

/**
 * Review every item and return `{[id]: {verdict, reason}}`.
 *
 * Items already present in the journal are returned from it rather than
 * re-reviewed, so an interrupted run resumes where it stopped.
 */
export async function verifyPackets(service, systemPrompt, items, options = {}) {
	const {
		journalPath = null,
		packetSize = DEFAULT_PACKET_SIZE,
		budgetCharacters = DEFAULT_PACKET_CHARACTERS,
		background = true,
		timeoutMs = undefined,
		progress = null,
	} = options;
	const recorded = loadVerdicts(journalPath);
	const verdicts = {};
	for (const [identifier, row] of Object.entries(recorded)) {
		verdicts[identifier] = { verdict: row.verdict, reason: row.reason ?? "" };
	}
	const pending = items.filter((item) => !(item.id in verdicts));
	const packets = buildPackets(pending, packetSize, budgetCharacters);
	for (const [index, packet] of packets.entries()) {
		const expected = packet.map((item) => item.id);
		const messages = [
			{ role: "system", content: `${systemPrompt}\n\n${VERDICT_CONTRACT}` },
			{ role: "user", content: JSON.stringify({ items: packet }) },
		];
		let parsed;
		try {
			parsed = await verifyOne(service, messages, expected, background, timeoutMs);
		} catch (error) {
			throw new VerificationError(error.message);
		}
		for (const identifier of expected) {
			verdicts[identifier] = parsed[identifier];
			if (journalPath) appendJsonlFsync(journalPath, { at: new Date().toISOString(), id: identifier, ...parsed[identifier] });
		}
		if (progress) {
			const flagged = expected.filter((identifier) => parsed[identifier].verdict === VERDICT_FLAG).length;
			progress(`[verify ${index + 1}/${packets.length}] ${expected.length} reviewed, ${flagged} flagged`);
		}
	}
	return verdicts;
}

/**
 * Redo each flagged item on the thinking model.
 *
 * `redo(item, reason)` returns the corrected result, or throws to leave the item
 * for a human. The escalated result always wins over the original: it was
 * produced with reasoning, by the stronger configuration, knowing what the
 * reviewer objected to.
 *
 * A flag verdict stays in the journal forever, so an item already escalated is
 * returned from the journal rather than redone. Without this every resumed run
 * pays a fresh reasoning-model call for every item ever flagged. Resumed
 * outcomes carry `resumed: true`; a caller that commits results must skip them,
 * because the corrected value was committed when it was first produced.
 */
export async function escalate(flagged, redo, { journalPath = null, progress = null } = {}) {
	const attempted = loadEscalations(journalPath);
	const results = {};
	const pending = [];
	for (const [item, reason] of flagged) {
		const row = attempted[item.id];
		if (!row) {
			pending.push([item, reason]);
			continue;
		}
		results[item.id] = { ok: Boolean(row.escalated), detail: row.detail ?? "", resumed: true };
		if (progress) progress(`[escalate] ${item.id}: already escalated, keeping the recorded outcome`);
	}
	for (const [index, [item, reason]] of pending.entries()) {
		const identifier = item.id;
		const record = { at: new Date().toISOString(), id: identifier, reason };
		try {
			results[identifier] = { ok: true, value: await redo(item, reason) };
			record.escalated = true;
		} catch (error) {
			results[identifier] = { ok: false, detail: `${error.name}: ${error.message}` };
			record.escalated = false;
			record.detail = results[identifier].detail;
		}
		if (journalPath) appendJsonlFsync(journalPath, record);
		if (progress) {
			progress(`[escalate ${index + 1}/${pending.length}] ${identifier}: ${results[identifier].ok ? "redone" : "needs review"}`);
		}
	}
	return results;
}

/** Counts for the run report. */
export function summarize(verdicts, escalations = {}) {
	const flagged = Object.entries(verdicts)
		.filter(([, entry]) => entry.verdict === VERDICT_FLAG)
		.map(([identifier]) => identifier);
	const outcomes = Object.values(escalations);
	return {
		verified: Object.keys(verdicts).length,
		ok: Object.keys(verdicts).length - flagged.length,
		flagged: flagged.length,
		escalated: outcomes.filter((entry) => entry.ok).length,
		needsReview: outcomes.filter((entry) => !entry.ok).length,
		flaggedIds: flagged,
	};
}
