/**
 * Read-only client for the llm-stack state API.
 *
 * This is the `.mjs` counterpart of `stack_state.py`, and deliberately mirrors
 * it: `configure-pi-forge` reads capacity through this one while the eval suite
 * and the Python skills read it through the other, and a disagreement between
 * them would mean an install writes one number and a run enforces a different
 * one.
 *
 * The deployment behind `llms` publishes what it is actually running at
 * `http://llms:8078/api/v1/`. Without it, everything forge believes about the
 * backend is a constant someone measured once: the per-slot context window, the
 * number of slots a background call may pin, and which weights a port serves.
 *
 * **Strictly optional.** pi-forge installs on machines with no such API. Every
 * function returns `null` or an empty result when the stack cannot be read, and
 * every caller carries on exactly as it did before.
 *
 * **Never on the request path.** `call()` does not consult this module.
 * Discovery happens where a person is already waiting: install, doctor, run start.
 *
 * Resolving a forge URL to a backend is the one non-obvious part. `backends[]`
 * entries are keyed by the backend's own port — `chat-primary` is
 * `127.0.0.1:8010` — while forge talks to proxy ports (`:8004` bulk chat,
 * `:8008` thinking) that appear in no `base_url` and no `probe.target`. The
 * snapshot's `config["Ports"]` block is what connects them:
 *
 *     CHAT_BACKEND_PORT 8010   NOTHINK_PORT 8004   CODE_PORT 8008   THINK_PORT 8003
 *     EMBED_PORT 8005   EMBED2_PORT 8011   RERANK_PORT 8006   TASK_PORT 8007
 *
 * So a lookup tries the backend's own port first, then falls back to that map.
 */

export const DEFAULT_STACK_STATE_URL = "http://llms:8078";
const API_PREFIX = "/api/v1";
// This client is written against the 1.x contract. A major bump may rename or
// restructure anything read below, and a wrong reading is worse than no
// reading: a bogus nCtxPerSlot would be written into settings as though measured.
export const SUPPORTED_API_MAJOR = "1";
// Short, because an unreachable host must not make `configure-pi-forge` or a
// skill preflight feel hung. DNS failure returns immediately; this budget only
// matters for a host that accepts packets and never answers.
export const DEFAULT_TIMEOUT_MS = 3_000;
// One doctor pass probes chat, think, and embeddings. Without a cache that is
// three identical GETs, or three timeouts when the stack is down.
export const CACHE_TTL_MS = 5_000;

// Which backend serves a given port role. The `*2_*` roles belong to the
// secondary preset, which is a different backend rather than another profile in
// front of the same one. Must match PORT_ROLE_BACKENDS in stack_state.py.
const PORT_ROLE_BACKENDS = {
	CHAT_BACKEND_PORT: "chat-primary",
	NOTHINK_PORT: "chat-primary",
	CODE_PORT: "chat-primary",
	THINK_PORT: "chat-primary",
	CHAT2_BACKEND_PORT: "chat-secondary",
	NOTHINK2_PORT: "chat-secondary",
	CODE2_PORT: "chat-secondary",
	THINK2_PORT: "chat-secondary",
	EMBED_PORT: "embed",
	EMBED2_PORT: "embed2",
	RERANK_PORT: "rerank",
	TASK_PORT: "task",
	OCR_PORT: "ocr",
};

// The router names the reranker `rank` where the backend list calls it `rerank`.
// Everything else agrees, so this is a spelling correction rather than a table.
const ROUTER_IDS = { rerank: "rank" };
// Router states meaning "these weights are not in VRAM right now". The router
// loads on demand, so neither is a fault — but both explain a first call that
// times out where a later one succeeds.
const ROUTER_ABSENT_STATES = new Set(["unloaded", "sleeping"]);
// Observed by probing :8005 while the embedding model was cold: the request
// itself moved the router from `unloaded` to `loading`, and the call timed out
// waiting. Worth its own sentence, because it is the one state where retrying
// the same call in a moment is exactly the right response.
const ROUTER_LOADING_STATES = new Set(["loading", "starting"]);

// Alerts at these levels describe conditions that change how a run behaves — a
// swapping host, a prompt cache too small to hold the working set. `info` is
// excluded: that is where the API's own "no token configured" notice lives, and
// repeating it on every batch report would train the reader to skip warnings.
export const REPORTABLE_ALERT_LEVELS = ["error", "warn"];

const MISS = Symbol("stack-state-miss");
const snapshotCache = new Map();

function isTruthy(value) {
	return ["1", "true", "yes", "on"].includes(
		String(value ?? "")
			.trim()
			.toLowerCase(),
	);
}

/**
 * Resolve the state API to `{enabled, baseUrl, token}`.
 *
 * Precedence matches every other connected service: environment, then the
 * agent's `connectedServices` settings, then the built-in default.
 */
export function resolveStackState({ env = process.env, settings } = {}) {
	if (isTruthy(env.PI_FORGE_SKIP_STACK_DISCOVERY)) return { enabled: false, baseUrl: "", token: null };

	const persisted =
		settings?.stackState && typeof settings.stackState === "object" && !Array.isArray(settings.stackState)
			? settings.stackState
			: {};
	let baseUrl;
	if (Object.hasOwn(env, "FORGE_STACK_STATE_URL")) {
		// An env var set to the empty string turns the integration off for this
		// process, the same way FORGE_SEARXNG_URL="" disables search.
		baseUrl = String(env.FORGE_STACK_STATE_URL ?? "")
			.trim()
			.replace(/\/+$/, "");
	} else {
		baseUrl =
			(typeof persisted.baseUrl === "string" ? persisted.baseUrl : "").trim().replace(/\/+$/, "") ||
			DEFAULT_STACK_STATE_URL;
	}

	let token = String(env.FORGE_STACK_STATE_TOKEN ?? "").trim() || null;
	if (!token && typeof settings?.apiKeys?.["stack-state"] === "string") {
		token = settings.apiKeys["stack-state"].trim() || null;
	}
	return { enabled: Boolean(baseUrl) && persisted.enabled !== false, baseUrl, token };
}

/**
 * GET one endpoint, or null for any reason at all.
 *
 * Deliberately total: a caller uses this to decide whether extra detail is
 * available, never to decide whether to proceed.
 */
async function getJson(baseUrl, path, token, timeoutMs) {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const headers = { Accept: "application/json" };
		if (token) headers.Authorization = `Bearer ${token}`;
		const response = await fetch(`${baseUrl}${API_PREFIX}${path}`, { headers, signal: controller.signal });
		if (!response.ok) return null;
		const payload = await response.json();
		if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
		if (String(payload.api_version ?? "").split(".")[0] !== SUPPORTED_API_MAJOR) return null;
		return payload;
	} catch {
		return null;
	} finally {
		clearTimeout(timer);
	}
}

/** Whether the state API is up and speaking a version this client reads. */
export async function stackHealth({ env = process.env, settings, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
	const resolved = resolveStackState({ env, settings });
	if (!resolved.enabled) return false;
	const payload = await getJson(resolved.baseUrl, "/health", resolved.token, timeoutMs);
	return Boolean(payload?.ok);
}

/**
 * The whole stack state in one read, or null when it cannot be had.
 *
 * `/api/v1/snapshot` is used rather than the focused endpoints because the port
 * map that resolves a forge URL to a backend lives only in its `config` block,
 * and stitching four responses together would race a restart anyway.
 *
 * Both outcomes are cached. Caching the failure is the point of the negative
 * branch: a doctor pass over three services against a stack that is down should
 * wait one timeout, not three.
 */
export async function readSnapshot({
	env = process.env,
	settings,
	timeoutMs = DEFAULT_TIMEOUT_MS,
	refresh = false,
} = {}) {
	const resolved = resolveStackState({ env, settings });
	if (!resolved.enabled) return null;
	const now = Date.now();
	if (!refresh) {
		const cached = snapshotCache.get(resolved.baseUrl);
		if (cached && cached.expiresAt > now) return cached.value === MISS ? null : cached.value;
	}
	const payload = await getJson(resolved.baseUrl, "/snapshot", resolved.token, timeoutMs);
	snapshotCache.set(resolved.baseUrl, { expiresAt: now + CACHE_TTL_MS, value: payload ?? MISS });
	return payload;
}

/**
 * Drop the memoized snapshot. For tests, and for a long-lived process that wants
 * a fresh reading after restarting something.
 */
export function clearStackStateCache() {
	snapshotCache.clear();
}

/** The TCP port a URL addresses, or null if it cannot be determined. */
function portOf(url) {
	const text = String(url ?? "").trim();
	if (!text) return null;
	let parsed;
	try {
		parsed = new URL(text.includes("://") ? text : `http://${text}`);
	} catch {
		return null;
	}
	if (parsed.port) return Number(parsed.port);
	return { "http:": 80, "https:": 443 }[parsed.protocol] ?? null;
}

/**
 * Every `*_PORT` key in the config, as `{port: role}`.
 *
 * Read from every config block rather than only `Ports`: the secondary preset
 * keeps its own port keys in its own block, and a role that moves between blocks
 * should not silently stop resolving.
 */
function portMap(snapshot) {
	const mapping = new Map();
	if (!snapshot?.config || typeof snapshot.config !== "object") return mapping;
	for (const block of Object.values(snapshot.config)) {
		if (!block || typeof block !== "object" || Array.isArray(block)) continue;
		for (const [key, value] of Object.entries(block)) {
			if (!key.endsWith("_PORT")) continue;
			const port = Number.parseInt(String(value).trim(), 10);
			if (Number.isInteger(port)) mapping.set(port, key);
		}
	}
	return mapping;
}

function servicesByName(snapshot) {
	const services = Array.isArray(snapshot?.services) ? snapshot.services : [];
	return new Map(services.filter((row) => row && typeof row === "object" && row.name).map((row) => [row.name, row]));
}

/** The service whose health probe targets this port — the proxy, usually. */
function serviceForPort(snapshot, port) {
	if (port === null) return null;
	const services = Array.isArray(snapshot?.services) ? snapshot.services : [];
	return (
		services.find((row) => row?.probe && typeof row.probe === "object" && portOf(row.probe.target) === port) ?? null
	);
}

/**
 * Resolve a configured endpoint to what is behind it.
 *
 * Returns `{port, role, backend, unitService, portService}` or null. The two
 * services are different things and both matter: `portService` is what listens
 * on the port forge dials (a proxy), `unitService` is the systemd unit actually
 * holding the weights.
 */
export function backendForUrl(snapshot, url) {
	if (!snapshot || typeof snapshot !== "object") return null;
	const port = portOf(url);
	if (port === null) return null;
	const backends = Array.isArray(snapshot.backends) ? snapshot.backends : null;
	if (!backends) return null;

	const role = portMap(snapshot).get(port) ?? null;
	// The backend's own port is the strongest signal and needs no config block:
	// embeddings on :8005 and the task model on :8007 resolve this way.
	let found = backends.find((entry) => entry && typeof entry === "object" && portOf(entry.base_url) === port);
	if (!found && role) {
		const wanted = PORT_ROLE_BACKENDS[role];
		found = backends.find((entry) => entry?.name === wanted);
	}
	if (!found) return null;

	const services = servicesByName(snapshot);
	// Router-managed backends carry no `unit` at all — they are loaded on demand
	// rather than run as a systemd unit — but they do have a service row under
	// their own name, and it holds the reason worth reporting.
	const unitService = services.get(found.unit) ?? services.get(found.name) ?? null;
	return { port, role, backend: found, unitService, portService: serviceForPort(snapshot, port) };
}

/**
 * What one request may use at this endpoint, read from the deployment.
 *
 * `contextTokens` is the per-slot window rather than the pool: llama.cpp divides
 * `--ctx-size` across `--parallel` slots, so a single request can never reach
 * the total.
 */
export function capacityForUrl(snapshot, url) {
	const located = backendForUrl(snapshot, url);
	const props = located?.backend?.props;
	if (!props || typeof props !== "object") return null;
	const contextTokens = props.n_ctx_per_slot;
	if (!Number.isInteger(contextTokens) || contextTokens <= 0) return null;
	const totalSlots = props.total_slots;
	return {
		contextTokens,
		totalSlots: Number.isInteger(totalSlots) && totalSlots > 0 ? totalSlots : null,
		contextTotal: props.n_ctx_total ?? null,
		active: Boolean(located.backend.active),
		isSleeping: Boolean(props.is_sleeping),
		backendName: located.backend.name ?? null,
	};
}

/**
 * Which weights this endpoint is serving, and which binary is serving them.
 *
 * A model id proves nothing — llama.cpp answers to whatever name it is sent,
 * regardless of what is loaded — so this reads the launched path, its
 * quantization, and the llama.cpp build instead.
 */
export function identityForUrl(snapshot, url) {
	const located = backendForUrl(snapshot, url);
	const props = located?.backend?.props;
	if (!props || typeof props !== "object") return null;
	const identity = {
		modelPath: props.model_path,
		modelAlias: props.model_alias,
		quant: props.model_ftype,
		buildInfo: props.build_info,
		backendName: located.backend.name,
		unit: located.backend.unit,
	};
	const kept = Object.fromEntries(
		Object.entries(identity).filter(([, value]) => value !== null && value !== undefined && value !== ""),
	);
	return Object.keys(kept).length ? kept : null;
}

/** The backend's slots, so a caller can check a slot number it means to pin. */
export function slotsForUrl(snapshot, url) {
	const slots = backendForUrl(snapshot, url)?.backend?.slots;
	return Array.isArray(slots) ? slots : null;
}

/** A router-managed model's load state, or null when it is not router-managed. */
function routerState(snapshot, backend) {
	const models = snapshot?.router?.models;
	if (!Array.isArray(models)) return null;
	const wanted = new Set([backend?.name, ROUTER_IDS[backend?.name], backend?.props?.model_alias].filter(Boolean));
	return models.find((entry) => entry && wanted.has(entry.id))?.state ?? null;
}

/**
 * One sentence for a service that is not active.
 *
 * The stack writes its own `reason` for people ("held by the model router — the
 * model loads on demand and is not run as a unit"), so it is quoted rather than
 * reworded. `expected` separates the two cases that matter: a service that is
 * off on purpose is a configuration answer, one that is off unexpectedly is a
 * fault.
 */
function stoppedSentence(service, role) {
	const label = service.label || service.name;
	const reason = service.reason || service.unit_state;
	const sentence = `${role}, ${label}, is ${service.state}`;
	if (reason) return `${sentence}: ${reason}`;
	return service.expected === "off" ? `${sentence} (it is configured to be off)` : sentence;
}

/**
 * Why an endpoint is not answering, in one sentence, or null.
 *
 * Returns null when the stack looks healthy — the endpoint may still be failing
 * for a reason the stack cannot see, and inventing an explanation would be worse
 * than the transport error the caller already has.
 */
export function explainUnreachable(snapshot, url) {
	const located = backendForUrl(snapshot, url);
	if (!located) return null;
	const { backend, portService, unitService } = located;

	if (portService && portService.state !== "active") {
		return stoppedSentence(portService, `the service on port ${located.port}`);
	}

	// A live proxy with nothing behind it: the connection is accepted and every
	// request fails, which reads as the model misbehaving rather than absent.
	for (const upstream of portService?.upstreams ?? []) {
		if (!upstream || typeof upstream !== "object" || upstream.ok) continue;
		const states = upstream.states;
		const named =
			states && typeof states === "object"
				? Object.entries(states)
						.sort(([a], [b]) => a.localeCompare(b))
						.map(([name, state]) => `${name} is ${state}`)
						.join(", ")
				: "none are running";
		return `${portService.label || portService.name} is running but has no live backend (${named})`;
	}

	// Checked before the unit state, because for these backends the unit is
	// stopped *by design* and the router is the thing that explains the call.
	const router = routerState(snapshot, backend);
	if (ROUTER_LOADING_STATES.has(router)) {
		return `the model router is loading '${backend.name}' right now; the call timed out waiting for weights, and retrying shortly should succeed`;
	}
	if (ROUTER_ABSENT_STATES.has(router)) {
		return `the model router loads '${backend.name}' on demand and it is not resident right now (router state: ${router})`;
	}

	if (unitService && unitService.state !== "active") {
		return stoppedSentence(unitService, `the backend serving ${backend.name}`);
	}
	if (!backend.active) return `the stack reports backend '${backend.name}' as inactive`;
	if (backend.props?.is_sleeping) return `backend '${backend.name}' is sleeping and has to reload before it answers`;

	for (const service of [portService, unitService]) {
		const probe = service?.probe;
		if (!probe || typeof probe !== "object" || probe.ok !== false) continue;
		const suffix = probe.http_status ? ` (HTTP ${probe.http_status})` : "";
		const detail = probe.detail ? `: ${probe.detail}` : "";
		return `the stack's own probe of ${probe.target || service.name} is failing${suffix}${detail}`;
	}
	return null;
}

/** The stack's own warnings, as structured rows. */
export function healthAlerts(snapshot, levels = REPORTABLE_ALERT_LEVELS) {
	const alerts = Array.isArray(snapshot?.alerts) ? snapshot.alerts : [];
	return alerts.filter((row) => row && typeof row === "object" && levels.includes(row.level));
}

/**
 * The stack's own warnings, as sentences fit for a run report.
 *
 * The API already writes these for people ("Host swap is 97% used (7958 MiB)."),
 * so they are passed through rather than reworded.
 */
export function healthWarnings(snapshot, levels = REPORTABLE_ALERT_LEVELS) {
	return healthAlerts(snapshot, levels)
		.map((row) => (typeof row.text === "string" ? row.text.trim() : ""))
		.filter(Boolean);
}
