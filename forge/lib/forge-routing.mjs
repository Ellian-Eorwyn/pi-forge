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

import { resolveService, resolveTaskService, resolveThinkService } from "./forge-llm.mjs";
import { resolveConnectedServices } from "./connected-services.mjs";

/** Stage label -> service name. See `forge_routing.py` for why each is here. */
export const STAGE_SERVICES = Object.freeze({
	"clean-transcript-chunk-multi": "task",
	"clean-transcript-chunk-multi-repair": "task",
	"connection-judgment": "task",
	"clean-transcript-chunk-single": "think",
	"clean-transcript-chunk-single-repair": "think",
	"split-braindump": "think",
});

/** Measured, and deliberately left on `chat`. Nothing reads this at runtime. */
export const STAGES_HELD_ON_CHAT = Object.freeze({
	"classify-note":
		"better on think in isolation (5/8 vs 3/8), but vault-organizer verifies and escalates on think already, so routing classification there leaves one profile reviewing its own work; run the classify-* variants first",
	"summarize-transcript": "thinking ties on gates (8/8) and carries 2 silent failures; the small model 3",
	"summarize-report": "same: gates tie, silent failures do not",
	"meeting-brief": "small model 2/8 with 0.11 fact recall; thinking carries a silent failure",
	"ground-draft": "every candidate either gate-blocked or unstable across repeats",
	"enumerate-items": "small model 1/8 against 3/8, and slower per call on this prompt size",
	"clean-document-chunk": "no candidate cleared; thinking flipped on 4 items between attempts",
	"abstention-grounded": "thinking ties exactly (12/12); the tie rule takes 5.8s over 14.7s",
	"verify-packet": "all three tie, so the case cannot tell them apart; stays on think until it is strengthened",
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
	return { ...resolve(options), stage, routedTo: name };
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
