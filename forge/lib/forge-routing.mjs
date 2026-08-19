/**
 * Which inference service each stage of work runs on. Twin of `forge_routing.py`.
 *
 * A skill used to bind one service per *command*, while the measurements in
 * `forge/evals` are per *stage* — and several of them point opposite directions
 * inside a single command. So routing is keyed on the stage label skills already
 * pass to the client as `task`, which was being journaled and otherwise ignored.
 *
 * The table itself lives in `forge_routing.py`; this file mirrors it, and
 * `test_forge_routing.py` asserts the two agree. Keeping one copy in each
 * language beats a shared JSON file that neither side owns: the Python table
 * carries the evidence for every entry as prose, and that is the thing a person
 * needs when revisiting a routing decision.
 */

import { resolveConnectedServices } from "./connected-services.mjs";
import { resolveService, resolveTaskService, resolveThinkService } from "./forge-llm.mjs";

/** Stage label -> service name. See `forge_routing.py` for why each is here. */
export const STAGE_SERVICES = Object.freeze({
	"connection-judgment": "task",
	// Transcript cleanup (single- and multi-speaker) and braindump-split all run
	// on the non-thinking bulk tier now; see STAGES_HELD_ON_CHAT in
	// forge_routing.py for the evidence.
});

/**
 * Measured, and deliberately left on `chat`. Nothing reads this at runtime.
 *
 * Every key is the label a call site actually passes as `task`. Seven once named
 * the eval case instead, which made an override written against one of those
 * names parse, validate, and do nothing.
 */
export const STAGES_HELD_ON_CHAT = Object.freeze({
	"clean-transcript-chunk-single":
		"was on think (8/8 vs 2/8) while the gate scored verbatim voice; under the meaning-first gate the non-thinking bulk tier clears it 8/8 (q38-none), and the thinking verify pass escalates a genuinely-unfaithful note at xhigh. See forge_routing.py for the full evidence.",
	"clean-transcript-chunk-single-repair":
		"the corrective retry of single-speaker cleanup, and it goes where that goes",
	"clean-transcript-chunk-multi":
		"diarized cleanup measured better on the small task tier (7/8 vs 1/8), but that baseline was an earlier chat build; the 27B bulk tier is capable enough now (Ellie's call, 2026-08-14), and task ships disabled and is router-bound, so the table pointing here only ever fell back to chat anyway. The thinking verify pass escalates a genuinely-unfaithful note at xhigh. See forge_routing.py for the full evidence.",
	"clean-transcript-chunk-multi-repair": "the corrective retry of multi-speaker cleanup, and it goes where that goes",
	"split-braindump":
		"was on think (7/8 vs 4/8) under a gate stricter than the skill; with the gate aligned to validate_split, non-thinking scores 7/8 and xhigh swings 6-8/8 — within noise, so the tie rule takes the faster bulk tier. See forge_routing.py.",
	"classify-note":
		"better on think in isolation (5/8 vs 3/8), but vault-organizer verifies and escalates on think already, so routing classification there leaves one profile reviewing its own work; run the classify-* variants first",
	"summarize-transcript": "thinking ties on gates (8/8) and carries 2 silent failures; the small model 3",
	"draft-note": "measured as `grounding-draft`: every candidate either gate-blocked or unstable across repeats",
	"clean-chunk": "measured as `doc-cleanup-ocr`: no candidate cleared; thinking flipped on 4 items between attempts",
	verify:
		"measured as `verifier-seeded`: all three tie, so the case cannot tell them apart; stays on think until it is strengthened",
	"verify-repair": "the corrective retry of `verify`, and it goes wherever `verify` goes",
});

/** Capabilities the suite measures that no production stage corresponds to. */
export const CAPABILITIES_MEASURED = Object.freeze({
	"summarize-report": "summarizing a report document: gates tie, silent failures do not",
	"meeting-brief":
		"synthesis over a whole meeting: small model 2/8 with 0.11 fact recall; thinking carries a silent failure",
	"enumerate-items": "breadth: small model 1/8 against 3/8, and slower per call on this prompt size",
	"abstention-grounded": "answering from a source: thinking ties exactly (12/12); the tie rule takes 5.8s over 14.7s",
});

export const DEFAULT_SERVICE = "chat";

const RESOLVERS = {
	chat: (options) => resolveService("chat", options),
	think: (options) => resolveThinkService(options),
	task: (options) => resolveTaskService(options),
};

/** Per-stage overrides from `connectedServices.routing`. */
export function routingOverrides(options = {}) {
	const routing = resolveConnectedServices(options).routing;
	return routing && typeof routing === "object" && !Array.isArray(routing) ? routing : {};
}

/** The service a stage should run on, before any of it is resolved. */
export function serviceNameFor(stage, options = {}) {
	if (options.override) return options.override;
	const configured = routingOverrides(options)[stage];
	if (configured && Object.hasOwn(RESOLVERS, configured)) return configured;
	return STAGE_SERVICES[stage] ?? DEFAULT_SERVICE;
}

/**
 * Resolve the service for `stage`. Precedence is explicit option, then
 * `connectedServices.routing`, then the table, then `chat`.
 *
 * A target that is unconfigured or disabled resolves to `chat` and the returned
 * service says so under `fallback`, so a caller can journal where a call
 * actually went rather than where it was aimed.
 */
export function serviceFor(stage, options = {}) {
	const name = serviceNameFor(stage, options);
	const resolve = RESOLVERS[name] ?? RESOLVERS[DEFAULT_SERVICE];
	const service = { ...resolve(options), stage, routedTo: name };
	// Keep image-bearing work on a vision lane: a stage routed to a text-only lane
	// (`task`, or a GPU-2 lane) would have its image silently dropped by the agent's
	// transform layer, so pin such an item to the primary `chat` vision lane.
	if (options.carriesImage && service.images === false) {
		return { ...RESOLVERS.chat(options), stage, routedTo: name, fallback: "chat", imageGuard: true };
	}
	return service;
}

/** What to journal about where a call went. */
export function routingRecord(service) {
	return {
		stage: service.stage,
		routedTo: service.routedTo,
		ranOn: service.fallback ?? service.name,
		url: service.url,
		model: service.model,
	};
}
