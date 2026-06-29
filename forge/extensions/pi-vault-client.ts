import { accessSync, constants, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

interface VaultBridgeConfiguration {
	command: string;
	vaultRoot: string;
	readRoots: string[];
}

function configurationPath(): string {
	const defaultHome = process.env.PI_FORGE_HOME ?? join(homedir(), ".local", "share", "pi-vault");
	const agentDirectory = process.env.PI_CODING_AGENT_DIR ?? join(defaultHome, "agent");
	return join(agentDirectory, "vault-bridge.json");
}

export function loadVaultBridgeConfiguration(path = configurationPath()): VaultBridgeConfiguration {
	let value: unknown;
	try {
		value = JSON.parse(readFileSync(path, "utf8"));
	} catch (error: unknown) {
		throw new Error(`Cannot read vault bridge configuration ${path}: ${error instanceof Error ? error.message : String(error)}`);
	}
	if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(`${path} must contain a JSON object`);
	const record = value as Record<string, unknown>;
	if (typeof record.command !== "string") throw new Error(`${path}: command must be an absolute executable path`);
	if (typeof record.vaultRoot !== "string") throw new Error(`${path}: vaultRoot must be an absolute directory path`);
	if (!Array.isArray(record.readRoots) || record.readRoots.some((root) => typeof root !== "string")) {
		throw new Error(`${path}: readRoots must be a non-empty array of absolute directory paths`);
	}
	return {
		command: requireExecutable(record.command, "command"),
		vaultRoot: requireDirectory(record.vaultRoot, "vaultRoot"),
		readRoots: requireDirectories(record.readRoots as string[], "readRoots"),
	};
}

export default function piVaultClientExtension(pi: ExtensionAPI) {
	let clientPromise: Promise<Client> | undefined;
	let closed = false;
	const client = () => {
		if (closed) throw new Error("pi-vault MCP client is closed");
		if (!clientPromise) {
			clientPromise = connect(loadVaultBridgeConfiguration()).catch((error: unknown) => {
				clientPromise = undefined;
				throw error;
			});
		}
		return clientPromise;
	};
	pi.registerTool({
		name: "pi_vault_submit_artifact",
		label: "Submit artifact to pi-vault",
		description: "Create a validated pending pi-vault proposal for a completed Markdown or text artifact.",
		parameters: Type.Object({
			sourcePath: Type.String(),
			suggestedName: Type.Optional(Type.String()),
			title: Type.Optional(Type.String()),
			sourceTaskId: Type.Optional(Type.String()),
			sourceOperation: Type.Optional(Type.String()),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal) {
			const result = (await (await client()).callTool(
				{ name: "vault_submit_artifact", arguments: params },
				undefined,
				{ signal },
			)) as Record<string, unknown>;
			const structured = result.structuredContent;
			const details =
				typeof structured === "object" && structured !== null ? (structured as Record<string, unknown>) : result;
			return { content: [{ type: "text", text: JSON.stringify(details) }], details };
		},
	});
	pi.on("session_shutdown", async () => {
		if (closed) return;
		closed = true;
		const pending = clientPromise;
		clientPromise = undefined;
		if (pending) await (await pending).close();
	});
}

async function connect(configuration: VaultBridgeConfiguration): Promise<Client> {
	const args = ["--vault-root", configuration.vaultRoot];
	for (const root of configuration.readRoots) args.push("--read-root", root);
	const transport = new StdioClientTransport({ command: configuration.command, args, stderr: "inherit" });
	const client = new Client({ name: "pi-forge", version: "1.0.0" }, { capabilities: {} });
	await client.connect(transport);
	return client;
}

function requireExecutable(value: string, label: string): string {
	const resolved = requireAbsolute(value, label);
	if (!statSync(resolved).isFile()) throw new Error(`${label} must resolve to a file: ${resolved}`);
	accessSync(resolved, constants.X_OK);
	return resolved;
}

function requireDirectories(values: string[], label: string): string[] {
	if (values.length === 0) throw new Error(`${label} must contain at least one path`);
	return values.map((value) => requireDirectory(value, label));
}

function requireDirectory(value: string, label: string): string {
	const resolved = requireAbsolute(value, label);
	if (!statSync(resolved).isDirectory()) throw new Error(`${label} must resolve to a directory: ${resolved}`);
	return resolved;
}

function requireAbsolute(value: string, label: string): string {
	if (!isAbsolute(value)) throw new Error(`${label} must be an absolute path`);
	return realpathSync(value);
}
