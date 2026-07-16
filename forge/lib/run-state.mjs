import { createHash, randomUUID } from "node:crypto";
import {
	closeSync,
	existsSync,
	fsyncSync,
	mkdirSync,
	openSync,
	readFileSync,
	renameSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { hostname } from "node:os";
import { basename, dirname, join } from "node:path";

export const RUN_STATE_SCHEMA_VERSION = 1;
export const DEFAULT_MAX_ATTEMPTS = 3;

function canonicalValue(value) {
	if (Array.isArray(value)) return value.map(canonicalValue);
	if (value && typeof value === "object") {
		return Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => [key, canonicalValue(item)]));
	}
	return value;
}

export function canonicalJson(value) {
	return JSON.stringify(canonicalValue(value));
}

export function configurationFingerprint(value) {
	return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

function fsyncDirectory(directory) {
	let descriptor;
	try {
		descriptor = openSync(directory, "r");
		fsyncSync(descriptor);
	} catch {
		// Some filesystems do not permit directory fsync. The file itself was synced.
	} finally {
		if (descriptor !== undefined) closeSync(descriptor);
	}
}

export function atomicWriteFile(filePath, value) {
	mkdirSync(dirname(filePath), { recursive: true });
	const temporaryPath = join(dirname(filePath), `.${basename(filePath)}.${process.pid}.${randomUUID()}.tmp`);
	const descriptor = openSync(temporaryPath, "wx");
	try {
		writeFileSync(descriptor, value);
		fsyncSync(descriptor);
	} finally {
		closeSync(descriptor);
	}
	renameSync(temporaryPath, filePath);
	fsyncDirectory(dirname(filePath));
}

export function atomicWriteJson(filePath, value) {
	atomicWriteFile(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

export function appendJsonlFsync(filePath, value) {
	mkdirSync(dirname(filePath), { recursive: true });
	const descriptor = openSync(filePath, "a");
	try {
		writeFileSync(descriptor, `${JSON.stringify(value)}\n`);
		fsyncSync(descriptor);
	} finally {
		closeSync(descriptor);
	}
}

export function readJsonlRecoverTail(filePath, { repair = false } = {}) {
	if (!existsSync(filePath)) return { rows: [], warnings: [] };
	const text = readFileSync(filePath, "utf8");
	const lines = text.split("\n");
	const rows = [];
	const warnings = [];
	const validLines = [];
	for (let index = 0; index < lines.length; index += 1) {
		const line = lines[index];
		if (!line.trim()) continue;
		try {
			rows.push(JSON.parse(line));
			validLines.push(line);
		} catch (error) {
			const isTail = index === lines.length - 1 && !text.endsWith("\n");
			if (!isTail) throw new Error(`invalid JSONL at ${filePath}:${index + 1}: ${error instanceof Error ? error.message : String(error)}`);
			warnings.push(`Ignored an incomplete final JSONL record at ${filePath}:${index + 1}.`);
			if (repair) atomicWriteFile(filePath, validLines.length > 0 ? `${validLines.join("\n")}\n` : "");
		}
	}
	return { rows, warnings };
}

function now() {
	return new Date().toISOString();
}

export function createRunState({ workflow, command, input, options, items = [], phase = "initialized", nextAction = null, children = {} }) {
	const createdAt = now();
	return {
		schemaVersion: RUN_STATE_SCHEMA_VERSION,
		workflow,
		createdAt,
		updatedAt: createdAt,
		command,
		input,
		options,
		optionsFingerprint: configurationFingerprint({ workflow, command, input, options }),
		status: "running",
		phase,
		nextAction,
		items,
		children,
		warnings: [],
	};
}

export function initializeRunState(runDirectory, state) {
	if (existsSync(join(runDirectory, "run_state.json"))) throw new Error(`run state already exists: ${runDirectory}`);
	atomicWriteJson(join(runDirectory, "run_state.json"), state);
	appendRunEvent(runDirectory, { type: "run_initialized", workflow: state.workflow, phase: state.phase });
	return state;
}

export function loadRunState(runDirectory, workflow = null) {
	const statePath = join(runDirectory, "run_state.json");
	if (!existsSync(statePath)) throw new Error(`legacy or unrelated output directory has no run_state.json: ${runDirectory}`);
	const state = JSON.parse(readFileSync(statePath, "utf8"));
	if (state.schemaVersion !== RUN_STATE_SCHEMA_VERSION) throw new Error(`unsupported run state schema version: ${state.schemaVersion}`);
	if (workflow && state.workflow !== workflow) throw new Error(`run belongs to ${state.workflow}, not ${workflow}`);
	return state;
}

export function assertCompatibleRun(state, configuration) {
	const actual = configurationFingerprint(configuration);
	if (actual !== state.optionsFingerprint) throw new Error("existing run options or input do not match this invocation; use status/refresh or choose a new output directory");
}

export function updateRunState(runDirectory, mutate, event = null) {
	const state = loadRunState(runDirectory);
	const draft = structuredClone(state);
	const updated = mutate(draft) ?? draft;
	updated.updatedAt = now();
	atomicWriteJson(join(runDirectory, "run_state.json"), updated);
	if (event) appendRunEvent(runDirectory, event);
	return updated;
}

export function appendRunEvent(runDirectory, event) {
	const eventPath = join(runDirectory, "run_events.jsonl");
	const prior = readJsonlRecoverTail(eventPath, { repair: true }).rows;
	appendJsonlFsync(eventPath, { sequence: prior.length + 1, at: now(), ...event });
}

export function inputDrift(snapshot, current) {
	const original = new Map(snapshot.map((item) => [item.path, item]));
	const observed = new Map(current.map((item) => [item.path, item]));
	const added = current.filter((item) => !original.has(item.path));
	const removed = snapshot.filter((item) => !observed.has(item.path));
	const changed = current.filter((item) => original.has(item.path) && original.get(item.path).sha256 !== item.sha256);
	return { changed: changed.map((item) => ({ before: original.get(item.path), after: item })), added, removed };
}

export function isTransientFailure(error) {
	const code = String(error?.code ?? "").toLowerCase();
	const message = String(error instanceof Error ? error.message : error).toLowerCase();
	return ["econnreset", "econnrefused", "etimedout", "timeout", "interrupted", "aborted"].some((value) => code.includes(value) || message.includes(value)) || /http\s+5\d\d/.test(message);
}

export function retryableItem(item, maximumAttempts = DEFAULT_MAX_ATTEMPTS) {
	return item.status === "pending" || item.status === "in_progress" || (item.status === "failed" && item.transient === true && (item.attempts ?? 0) < maximumAttempts);
}

function processIsAlive(pid) {
	if (!Number.isInteger(pid) || pid < 1) return false;
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return error?.code === "EPERM";
	}
}

export async function withRunLock(runDirectory, callback) {
	const lockPath = join(runDirectory, ".run.lock");
	const acquire = () => {
		try {
			writeFileSync(lockPath, `${JSON.stringify({ pid: process.pid, host: hostname(), createdAt: now() })}\n`, { flag: "wx" });
			return;
		} catch (error) {
			if (error?.code !== "EEXIST") throw error;
		}
		let lock = null;
		try {
			lock = JSON.parse(readFileSync(lockPath, "utf8"));
		} catch {
			// A malformed lock is stale.
		}
		if (lock?.host && lock.host !== hostname()) throw new Error(`run is locked by PID ${lock.pid ?? "unknown"} on ${lock.host}`);
		if (processIsAlive(lock?.pid)) throw new Error(`run is locked by active PID ${lock.pid}`);
		rmSync(lockPath, { force: true });
		writeFileSync(lockPath, `${JSON.stringify({ pid: process.pid, host: hostname(), createdAt: now() })}\n`, { flag: "wx" });
	};
	mkdirSync(runDirectory, { recursive: true });
	acquire();
	try {
		return await callback();
	} finally {
		rmSync(lockPath, { force: true });
	}
}
