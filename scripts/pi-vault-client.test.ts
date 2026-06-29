import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, test } from "node:test";
import bridgeModule from "../forge/extensions/pi-vault-client.ts";

const { default: piVaultClientExtension, loadVaultBridgeConfiguration } = bridgeModule as unknown as {
	default: (pi: unknown) => void;
	loadVaultBridgeConfiguration: (path?: string) => { command: string; vaultRoot: string; readRoots: string[] };
};

const workspaces: string[] = [];

afterEach(() => {
	delete process.env.PI_CODING_AGENT_DIR;
	delete process.env.PI_FORGE_HOME;
	while (workspaces.length > 0) rmSync(workspaces.pop()!, { recursive: true, force: true });
});

function workspace(): string {
	const path = mkdtempSync(join(tmpdir(), "pi-forge-vault-client-"));
	workspaces.push(path);
	return path;
}

function fakeServer(root: string): { launcher: string; pidFile: string } {
	const serverScript = join(root, "server.mjs");
	const launcher = join(root, "pi-vault-mcp");
	const pidFile = join(root, "server.pid");
	const sdk = resolve("node_modules/@modelcontextprotocol/sdk/dist/esm");
	writeFileSync(
		serverScript,
		`import { Server } from ${JSON.stringify(`file://${join(sdk, "server/index.js")}`)};
import { StdioServerTransport } from ${JSON.stringify(`file://${join(sdk, "server/stdio.js")}`)};
import { CallToolRequestSchema, ListToolsRequestSchema } from ${JSON.stringify(`file://${join(sdk, "types.js")}`)};
const server = new Server({name:"fake-vault",version:"1"},{capabilities:{tools:{}}});
server.setRequestHandler(ListToolsRequestSchema, async () => ({tools:[{name:"vault_submit_artifact",inputSchema:{type:"object"}}]}));
server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
  if (request.params.arguments?.sourcePath?.endsWith("slow.md")) await new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, 10000);
    extra.signal.addEventListener("abort", () => { clearTimeout(timer); reject(extra.signal.reason); }, {once:true});
  });
  return {content:[{type:"text",text:"pending"}],structuredContent:{schemaVersion:1,status:"pending_review",destinationPath:"01 Inbox/result.md",proposalPath:"00 System/review/import.json"}};
});
await server.connect(new StdioServerTransport());
`,
	);
	writeFileSync(launcher, `#!/bin/sh\necho $$ > "${pidFile}"\nexec "${process.execPath}" "${serverScript}" "$@"\n`);
	chmodSync(launcher, 0o755);
	return { launcher, pidFile };
}

function configure(root: string, command: string): { agent: string; vault: string; output: string } {
	const agent = join(root, "agent");
	const vault = join(root, "vault");
	const output = join(root, "output");
	mkdirSync(agent);
	mkdirSync(vault);
	mkdirSync(output);
	writeFileSync(join(agent, "vault-bridge.json"), JSON.stringify({ command, vaultRoot: vault, readRoots: [output] }));
	process.env.PI_CODING_AGENT_DIR = agent;
	return { agent, vault, output };
}

test("reverse client forwards structured pending proposals and shuts down its subprocess", async () => {
	const root = workspace();
	const server = fakeServer(root);
	const paths = configure(root, server.launcher);
	const artifact = join(paths.output, "result.md");
	writeFileSync(artifact, "# Result\n");
	let tool: { execute: (...args: unknown[]) => Promise<{ details: Record<string, unknown> }> } | undefined;
	let shutdown: (() => Promise<void>) | undefined;
	piVaultClientExtension({
		registerTool(value: unknown) {
			tool = value as typeof tool;
		},
		on(event: string, handler: unknown) {
			if (event === "session_shutdown") shutdown = handler as () => Promise<void>;
		},
	});
	assert.ok(tool);
	assert.ok(shutdown);
	const result = await tool.execute("call", { sourcePath: artifact }, undefined, undefined, {});
	assert.equal(result.details.status, "pending_review");
	assert.equal(result.details.destinationPath, "01 Inbox/result.md");
	const slowArtifact = join(paths.output, "slow.md");
	writeFileSync(slowArtifact, "# Slow\n");
	const controller = new AbortController();
	const pending = tool.execute("slow", { sourcePath: slowArtifact }, controller.signal, undefined, {});
	setTimeout(() => controller.abort(), 25);
	await assert.rejects(pending, /abort/i);
	await shutdown();
	await shutdown();
	assert.equal(existsSync(server.pidFile), true);
	const pid = Number(readFileSync(server.pidFile, "utf8"));
	await new Promise((resolve) => setTimeout(resolve, 50));
	assert.throws(() => process.kill(pid, 0));
});

test("missing configuration is actionable and does not spawn", () => {
	const root = workspace();
	process.env.PI_CODING_AGENT_DIR = root;
	assert.throws(() => loadVaultBridgeConfiguration(), /Cannot read vault bridge configuration/);
});

test("configuration defaults to the pi-forge home agent directory", () => {
	const root = workspace();
	const piForgeHome = join(root, "pi-vault");
	const server = fakeServer(root);
	const agent = join(piForgeHome, "agent");
	const vault = join(root, "vault");
	const output = join(root, "output");
	mkdirSync(agent, { recursive: true });
	mkdirSync(vault);
	mkdirSync(output);
	writeFileSync(join(agent, "vault-bridge.json"), JSON.stringify({ command: server.launcher, vaultRoot: vault, readRoots: [output] }));
	process.env.PI_FORGE_HOME = piForgeHome;
	assert.deepEqual(loadVaultBridgeConfiguration(), {
		command: realpathSync(server.launcher),
		vaultRoot: realpathSync(vault),
		readRoots: [realpathSync(output)],
	});
});
