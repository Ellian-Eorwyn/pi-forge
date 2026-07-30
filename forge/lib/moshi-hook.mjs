import { spawnSync } from "node:child_process";
import {
	accessSync,
	constants,
	copyFileSync,
	existsSync,
	lstatSync,
	mkdirSync,
	readFileSync,
	readlinkSync,
	rmSync,
	statSync,
	symlinkSync,
} from "node:fs";
import { homedir } from "node:os";
import { delimiter, join } from "node:path";

// Moshi is an optional host integration: a machine either has the daemon or it
// does not, and pi-forge installs the same way either way. Nothing in here may
// throw or fail an install.
const BINARY_NAME = "moshi-hook";
const HOOK_FILE_NAME = "moshi-hooks.ts";
const SERVICE_RESTART_COMMAND = "systemctl --user restart moshi-hook.service";

/**
 * Install the generated Moshi hook into a pi-forge agent directory.
 *
 * `moshi-hook` resolves its `pi` target from PI_CODING_AGENT_DIR, defaulting to
 * ~/.pi/agent, so pointing that variable at the pi-forge agent dir makes moshi
 * generate the hook there itself. That keeps the hook the daemon's own current
 * version instead of a copy in this repo that goes stale on every moshi release.
 *
 * pi discovers `agentDir/extensions/*.ts` on its own (and follows symlinks), so
 * the file landing there is all the registration needed — no settings key, no
 * package manifest entry.
 */
export function ensureMoshiHook({ agentDir, env = process.env } = {}) {
	if (!agentDir) return { status: "skipped", reason: "no agent directory" };
	if (isFlagSet(env.PI_FORGE_SKIP_MOSHI_HOOK)) {
		return { status: "skipped", reason: "PI_FORGE_SKIP_MOSHI_HOOK is set" };
	}
	const binary = resolveMoshiHookBinary(env);
	if (!binary) return { status: "skipped", reason: `${BINARY_NAME} is not installed` };

	const extensionsDir = join(agentDir, "extensions");
	const hookPath = join(extensionsDir, HOOK_FILE_NAME);
	// A link left over from a previous fallback whose source is gone would make pi
	// fail to load an extension at session start. Clear it before anything reads
	// the current state.
	pruneDanglingHookLink(hookPath);
	const before = readHook(hookPath);

	mkdirSync(extensionsDir, { recursive: true });
	const install = spawnSync(binary, ["install", "--target", "pi"], {
		encoding: "utf8",
		env: { ...env, PI_CODING_AGENT_DIR: agentDir },
		stdio: ["ignore", "pipe", "pipe"],
	});

	// Trust the file as well as the exit code: moshi has to have succeeded *and*
	// put the hook module where pi-forge looks for it. A file that was already
	// there must not make a failed install look like a healthy no-op.
	const installed = install.status === 0 && install.error === undefined;
	const after = readHook(hookPath);
	if (installed && after !== undefined) {
		return settle({ before, after, hookPath, env });
	}

	// moshi could not write it here — fall back to the hook it already generated
	// for the standard Pi install, which is the same daemon's current version.
	const link = linkStandardHook(hookPath, env);
	if (link?.ok) {
		return { status: "linked", hookPath, source: link.source, restart: restartDaemon(link.changed, env) };
	}
	const detail = link?.detail || lastLine(install.stderr) || lastLine(install.stdout) || install.error?.message;
	if (after !== undefined) {
		// Something usable is in place — a hook from an earlier install, or a file
		// the user put there — so pi-forge still loads an extension. Say why it was
		// not refreshed rather than reporting a clean run.
		return settle({ before, after, hookPath, env, warning: `could not refresh ${hookPath}: ${detail}` });
	}
	return { status: "failed", hookPath, reason: `${BINARY_NAME} did not write ${hookPath}`, detail };
}

/** Classify by what the hook file actually is now, not by how it got there. */
function settle({ before, after, hookPath, env, warning }) {
	const status = after === before ? "unchanged" : "installed";
	return { status, hookPath, warning, restart: restartDaemon(status === "installed", env) };
}

/** Terse install/update output for a result from `ensureMoshiHook`. */
export function formatMoshiHookNotice(result) {
	const out = [];
	const err = [];
	// A host without moshi, or an update that changed nothing, is not news.
	if (result.status === "installed") out.push(`Moshi hook installed: ${result.hookPath}`);
	if (result.status === "linked") out.push(`Moshi hook linked: ${result.hookPath} -> ${result.source}`);
	if (result.status === "failed") {
		err.push(`Moshi hook: ${result.reason}`);
		if (result.detail) err.push(`  ${result.detail}`);
	}
	if (result.warning) err.push(`Moshi hook: ${result.warning}`);
	const restart = result.restart;
	if (restart?.ok) out.push(`Restarted the moshi-hook daemon (${restart.command}).`);
	if (restart && !restart.ok) {
		err.push(restart.hint ?? `Moshi hook: restart the daemon manually: ${restart.command}`);
		if (restart.detail) err.push(`  ${restart.detail}`);
	}
	return { out, err };
}

/**
 * The daemon caches hook state, so it keeps reporting the old status until it is
 * restarted. Restart only when the hook actually changed: `pi-forge-update` is
 * often run from inside a Moshi session, and bouncing the bridge for a no-op
 * update would drop it for no reason.
 */
function restartDaemon(changed, env) {
	if (!changed) return undefined;
	const systemctl = resolveSystemctl(env);
	if (!systemctl) {
		// macOS ships the daemon as an app-managed agent; `moshi-hook service` is
		// systemd-only, so there is no portable restart to run here.
		return { ok: false, hint: "Restart the Moshi daemon so it re-checks hook state." };
	}
	const result = spawnSync(systemctl, ["--user", "restart", "moshi-hook.service"], {
		encoding: "utf8",
		env,
		stdio: ["ignore", "pipe", "pipe"],
	});
	if (result.status === 0) return { ok: true, command: SERVICE_RESTART_COMMAND };
	return {
		ok: false,
		command: SERVICE_RESTART_COMMAND,
		detail: lastLine(result.stderr) || lastLine(result.stdout) || result.error?.message,
	};
}

function linkStandardHook(hookPath, env) {
	const source = join(homeDirectory(env), ".pi", "agent", "extensions", HOOK_FILE_NAME);
	if (!existsSync(source)) return null;
	const existing = lstatOrNull(hookPath);
	if (existing && !existing.isSymbolicLink()) {
		return { ok: false, detail: `${hookPath} already exists and is not a symlink` };
	}
	if (existing) {
		if (readlinkOrNull(hookPath) === source) return { ok: true, changed: false, source };
		rmSync(hookPath, { force: true });
	}
	// Windows symlinks need a privilege the installer does not assume it has.
	if (process.platform === "win32") copyFileSync(source, hookPath);
	else symlinkSync(source, hookPath);
	return { ok: true, changed: true, source };
}

function pruneDanglingHookLink(hookPath) {
	const existing = lstatOrNull(hookPath);
	if (!existing?.isSymbolicLink() || existsSync(hookPath)) return;
	if (readlinkOrNull(hookPath)?.endsWith(HOOK_FILE_NAME)) rmSync(hookPath, { force: true });
}

function resolveMoshiHookBinary(env) {
	const override = env.PI_FORGE_MOSHI_HOOK_BIN;
	if (override) return isExecutable(override) ? override : undefined;
	// PATH covers Homebrew on macOS; ~/.local/bin is where the Linux installer
	// puts it and is not always on the PATH of a non-login install run.
	const searchDirs = [...(env.PATH ?? "").split(delimiter), join(homeDirectory(env), ".local", "bin")];
	for (const dir of searchDirs) {
		if (!dir) continue;
		for (const name of binaryNamesForPlatform()) {
			const candidate = join(dir, name);
			if (isExecutable(candidate)) return candidate;
		}
	}
	return undefined;
}

function resolveSystemctl(env) {
	const override = env.PI_FORGE_SYSTEMCTL_BIN;
	if (override) return isExecutable(override) ? override : undefined;
	if (process.platform !== "linux") return undefined;
	for (const dir of (env.PATH ?? "").split(delimiter)) {
		if (!dir) continue;
		const candidate = join(dir, "systemctl");
		if (isExecutable(candidate)) return candidate;
	}
	return undefined;
}

function binaryNamesForPlatform() {
	return process.platform === "win32" ? [`${BINARY_NAME}.exe`, `${BINARY_NAME}.cmd`, BINARY_NAME] : [BINARY_NAME];
}

function homeDirectory(env) {
	return env.HOME || env.USERPROFILE || homedir();
}

function isExecutable(path) {
	try {
		// A directory passes X_OK, so the file check has to come first.
		if (!statSync(path).isFile()) return false;
		accessSync(path, constants.X_OK);
		return true;
	} catch {
		return false;
	}
}

function isFlagSet(value) {
	if (typeof value !== "string") return false;
	const normalized = value.trim().toLowerCase();
	return normalized !== "" && normalized !== "0" && normalized !== "false" && normalized !== "no";
}

function readHook(path) {
	try {
		return readFileSync(path, "utf8");
	} catch {
		return undefined;
	}
}

function lstatOrNull(path) {
	try {
		return lstatSync(path);
	} catch {
		return undefined;
	}
}

function readlinkOrNull(path) {
	try {
		return readlinkSync(path);
	} catch {
		return undefined;
	}
}

function lastLine(value) {
	if (typeof value !== "string") return "";
	const lines = value
		.split("\n")
		.map((line) => line.trim())
		.filter(Boolean);
	return lines.at(-1) ?? "";
}
