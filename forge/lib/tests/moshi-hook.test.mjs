import assert from "node:assert/strict";
import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const { ensureMoshiHook, formatMoshiHookNotice } = await import(join(libraryRoot, "moshi-hook.mjs"));

// The stubs are shell scripts, and the fallback path makes symlinks.
const posixOnly = { skip: process.platform === "win32" ? "POSIX stub binaries" : false };

const INSTALLING_STUB = `#!/bin/sh
mkdir -p "$PI_CODING_AGENT_DIR/extensions"
printf '%s\\n' "$STUB_HOOK_BODY" > "$PI_CODING_AGENT_DIR/extensions/moshi-hooks.ts"
echo "pi -> installed"
`;

const FAILING_STUB = `#!/bin/sh
echo "moshi-hook: host is not paired" >&2
exit 1
`;

const RECORDING_SYSTEMCTL = `#!/bin/sh
echo "$@" >> "$SYSTEMCTL_LOG"
`;

const FAILING_SYSTEMCTL = `#!/bin/sh
echo "Failed to restart moshi-hook.service: unit not found" >&2
exit 1
`;

/**
 * A workspace with an isolated HOME, an agent dir, and stub binaries, so no test
 * touches the host's real moshi-hook, its hooks, or its daemon.
 */
function withWorkspace(callback) {
	const workspace = mkdtempSync(join(tmpdir(), "forge-moshi-hook-"));
	const home = join(workspace, "home");
	const agentDir = join(workspace, "agent");
	const binDir = join(workspace, "bin");
	mkdirSync(home, { recursive: true });
	mkdirSync(agentDir, { recursive: true });
	mkdirSync(binDir, { recursive: true });
	const context = {
		workspace,
		home,
		agentDir,
		hookPath: join(agentDir, "extensions", "moshi-hooks.ts"),
		systemctlLog: join(workspace, "systemctl.log"),
		/** Install a stub script and return its path. */
		stub(name, body) {
			const path = join(binDir, name);
			writeFileSync(path, body);
			chmodSync(path, 0o755);
			return path;
		},
		/** The hook moshi already generated for a standard Pi install. */
		standardHook(body) {
			const path = join(home, ".pi", "agent", "extensions", "moshi-hooks.ts");
			mkdirSync(dirname(path), { recursive: true });
			writeFileSync(path, body);
			return path;
		},
		/** An environment with no moshi-hook and no systemctl on it. */
		env(overrides = {}) {
			return { HOME: home, PATH: "", ...overrides };
		},
		systemctlInvocations() {
			if (!existsSync(context.systemctlLog)) return [];
			return readFileSync(context.systemctlLog, "utf8").split("\n").filter(Boolean);
		},
	};
	try {
		return callback(context);
	} finally {
		rmSync(workspace, { recursive: true, force: true });
	}
}

test("a host without moshi-hook is left untouched", posixOnly, () => {
	withWorkspace((context) => {
		const result = ensureMoshiHook({ agentDir: context.agentDir, env: context.env() });
		assert.equal(result.status, "skipped");
		assert.match(result.reason, /not installed/);
		// Not even an empty extensions/ directory: nothing about the install changes.
		assert.equal(existsSync(join(context.agentDir, "extensions")), false);
		const notice = formatMoshiHookNotice(result);
		assert.deepEqual(notice, { out: [], err: [] });
	});
});

test("the opt-out flag skips the step even with moshi-hook present", posixOnly, () => {
	withWorkspace((context) => {
		const moshi = context.stub("moshi-hook", INSTALLING_STUB);
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: moshi, PI_FORGE_SKIP_MOSHI_HOOK: "1", STUB_HOOK_BODY: "hook" }),
		});
		assert.equal(result.status, "skipped");
		assert.equal(existsSync(context.hookPath), false);
	});
});

test("the hook is generated into the pi-forge agent directory and the daemon restarts", posixOnly, () => {
	withWorkspace((context) => {
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({
				PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", INSTALLING_STUB),
				PI_FORGE_SYSTEMCTL_BIN: context.stub("systemctl", RECORDING_SYSTEMCTL),
				SYSTEMCTL_LOG: context.systemctlLog,
				STUB_HOOK_BODY: "generated hook",
			}),
		});
		assert.equal(result.status, "installed");
		assert.equal(result.hookPath, context.hookPath);
		assert.equal(readFileSync(context.hookPath, "utf8").trim(), "generated hook");
		assert.equal(result.restart.ok, true);
		assert.deepEqual(context.systemctlInvocations(), ["--user restart moshi-hook.service"]);
		assert.deepEqual(formatMoshiHookNotice(result).err, []);
		assert.match(formatMoshiHookNotice(result).out[0], /Moshi hook installed/);
	});
});

test("a rerun that changes nothing leaves the daemon alone", posixOnly, () => {
	withWorkspace((context) => {
		const env = context.env({
			PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", INSTALLING_STUB),
			PI_FORGE_SYSTEMCTL_BIN: context.stub("systemctl", RECORDING_SYSTEMCTL),
			SYSTEMCTL_LOG: context.systemctlLog,
			STUB_HOOK_BODY: "generated hook",
		});
		ensureMoshiHook({ agentDir: context.agentDir, env });
		const result = ensureMoshiHook({ agentDir: context.agentDir, env });
		assert.equal(result.status, "unchanged");
		assert.equal(result.restart, undefined);
		// One restart, from the first run: a no-op update must not bounce the bridge.
		assert.equal(context.systemctlInvocations().length, 1);
		assert.deepEqual(formatMoshiHookNotice(result), { out: [], err: [] });
	});
});

test("a moshi-hook upgrade that rewrites the hook restarts the daemon again", posixOnly, () => {
	withWorkspace((context) => {
		const moshi = context.stub("moshi-hook", INSTALLING_STUB);
		const systemctl = context.stub("systemctl", RECORDING_SYSTEMCTL);
		ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: moshi, PI_FORGE_SYSTEMCTL_BIN: systemctl, SYSTEMCTL_LOG: context.systemctlLog, STUB_HOOK_BODY: "old hook" }),
		});
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: moshi, PI_FORGE_SYSTEMCTL_BIN: systemctl, SYSTEMCTL_LOG: context.systemctlLog, STUB_HOOK_BODY: "new hook" }),
		});
		assert.equal(result.status, "installed");
		assert.equal(readFileSync(context.hookPath, "utf8").trim(), "new hook");
		assert.equal(context.systemctlInvocations().length, 2);
	});
});

test("a failed install falls back to the standard Pi hook", posixOnly, () => {
	withWorkspace((context) => {
		const source = context.standardHook("standard hook");
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({
				PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", FAILING_STUB),
				PI_FORGE_SYSTEMCTL_BIN: context.stub("systemctl", RECORDING_SYSTEMCTL),
				SYSTEMCTL_LOG: context.systemctlLog,
			}),
		});
		assert.equal(result.status, "linked");
		assert.equal(result.source, source);
		assert.equal(lstatSync(context.hookPath).isSymbolicLink(), true);
		assert.equal(readlinkSync(context.hookPath), source);
		assert.equal(readFileSync(context.hookPath, "utf8"), "standard hook");
		assert.equal(result.restart.ok, true);
	});
});

test("a fallback link that already points at the standard hook is left as is", posixOnly, () => {
	withWorkspace((context) => {
		context.standardHook("standard hook");
		const env = context.env({
			PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", FAILING_STUB),
			PI_FORGE_SYSTEMCTL_BIN: context.stub("systemctl", RECORDING_SYSTEMCTL),
			SYSTEMCTL_LOG: context.systemctlLog,
		});
		ensureMoshiHook({ agentDir: context.agentDir, env });
		const result = ensureMoshiHook({ agentDir: context.agentDir, env });
		assert.equal(result.status, "linked");
		assert.equal(result.restart, undefined);
		assert.equal(context.systemctlInvocations().length, 1);
	});
});

test("a failed install with nothing to fall back to reports and does not throw", posixOnly, () => {
	withWorkspace((context) => {
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", FAILING_STUB) }),
		});
		assert.equal(result.status, "failed");
		assert.match(result.reason, /did not write/);
		assert.equal(result.detail, "moshi-hook: host is not paired");
		assert.equal(existsSync(context.hookPath), false);
		const notice = formatMoshiHookNotice(result);
		assert.deepEqual(notice.out, []);
		assert.match(notice.err[0], /^Moshi hook: /);
	});
});

test("a fallback link whose source has gone away is cleared", posixOnly, () => {
	withWorkspace((context) => {
		mkdirSync(join(context.agentDir, "extensions"), { recursive: true });
		symlinkSync(join(context.home, ".pi", "agent", "extensions", "moshi-hooks.ts"), context.hookPath);
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", FAILING_STUB) }),
		});
		assert.equal(result.status, "failed");
		// A dangling extension path would make pi fail to load at session start.
		assert.equal(existsSync(context.hookPath), false);
		assert.throws(() => lstatSync(context.hookPath));
	});
});

test("the fallback never overwrites a real file at the hook path", posixOnly, () => {
	withWorkspace((context) => {
		context.standardHook("standard hook");
		mkdirSync(join(context.agentDir, "extensions"), { recursive: true });
		writeFileSync(context.hookPath, "hand-written hook");
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", FAILING_STUB) }),
		});
		// The file stays, and the run says why it could not be refreshed rather
		// than reporting a clean no-op.
		assert.equal(readFileSync(context.hookPath, "utf8"), "hand-written hook");
		assert.equal(result.status, "unchanged");
		assert.match(result.warning, /not a symlink/);
		assert.equal(result.restart, undefined);
		assert.match(formatMoshiHookNotice(result).err[0], /could not refresh/);
	});
});

test("a broken moshi-hook that leaves an older hook in place is reported", posixOnly, () => {
	withWorkspace((context) => {
		mkdirSync(join(context.agentDir, "extensions"), { recursive: true });
		writeFileSync(context.hookPath, "hook from an earlier install");
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", FAILING_STUB) }),
		});
		assert.equal(result.status, "unchanged");
		assert.match(result.warning, /host is not paired/);
		assert.equal(readFileSync(context.hookPath, "utf8"), "hook from an earlier install");
	});
});

test("a restart failure surfaces the manual command", posixOnly, () => {
	withWorkspace((context) => {
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({
				PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", INSTALLING_STUB),
				PI_FORGE_SYSTEMCTL_BIN: context.stub("systemctl", FAILING_SYSTEMCTL),
				STUB_HOOK_BODY: "generated hook",
			}),
		});
		assert.equal(result.status, "installed");
		assert.equal(result.restart.ok, false);
		assert.equal(result.restart.command, "systemctl --user restart moshi-hook.service");
		const notice = formatMoshiHookNotice(result);
		assert.match(notice.err[0], /systemctl --user restart moshi-hook\.service/);
		assert.match(notice.err[1], /unit not found/);
	});
});

test("without systemd the changed hook asks the user to restart the daemon", posixOnly, () => {
	withWorkspace((context) => {
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PI_FORGE_MOSHI_HOOK_BIN: context.stub("moshi-hook", INSTALLING_STUB), STUB_HOOK_BODY: "generated hook" }),
		});
		assert.equal(result.status, "installed");
		assert.equal(result.restart.ok, false);
		assert.match(formatMoshiHookNotice(result).err[0], /Restart the Moshi daemon/);
	});
});

test("moshi-hook found on PATH rather than by override is used", posixOnly, () => {
	withWorkspace((context) => {
		const moshi = context.stub("moshi-hook", INSTALLING_STUB);
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ PATH: dirname(moshi), STUB_HOOK_BODY: "generated hook" }),
		});
		assert.equal(result.status, "installed");
		assert.equal(readFileSync(context.hookPath, "utf8").trim(), "generated hook");
	});
});

test("moshi-hook installed only in ~/.local/bin is found off PATH", posixOnly, () => {
	withWorkspace((context) => {
		const localBin = join(context.home, ".local", "bin");
		mkdirSync(localBin, { recursive: true });
		const path = join(localBin, "moshi-hook");
		writeFileSync(path, INSTALLING_STUB);
		chmodSync(path, 0o755);
		const result = ensureMoshiHook({
			agentDir: context.agentDir,
			env: context.env({ STUB_HOOK_BODY: "generated hook" }),
		});
		assert.equal(result.status, "installed");
		assert.equal(readFileSync(context.hookPath, "utf8").trim(), "generated hook");
	});
});
