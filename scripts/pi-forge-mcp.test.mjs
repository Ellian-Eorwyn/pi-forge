import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
	appendFileSync,
	chmodSync,
	existsSync,
	mkdtempSync,
	mkdirSync,
	readFileSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { afterEach, test } from "node:test";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createBridgeServer, runChildProcess } from "./pi-forge-mcp-server.mjs";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const workspaces = [];

afterEach(() => {
	delete process.env.PI_FORGE_TEST_NOT_READY;
	delete process.env.PI_FORGE_TEST_LOG;
	while (workspaces.length > 0) rmSync(workspaces.pop(), { recursive: true, force: true });
});

function workspace() {
	const path = mkdtempSync(join(tmpdir(), "pi-forge-mcp-"));
	workspaces.push(path);
	return path;
}

function fakeSkills(root) {
	const transcriptionDirectory = join(root, "transcription", "scripts");
	const conversionDirectory = join(root, "file-conversion", "scripts");
	mkdirSync(transcriptionDirectory, { recursive: true });
	mkdirSync(conversionDirectory, { recursive: true });
	const worker = `
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
const script = basename(process.argv[2]);
const command = process.argv[3];
const valueAfter = (flag) => process.argv[process.argv.indexOf(flag) + 1];
const log = (message) => { if (process.env.PI_FORGE_TEST_LOG) appendFileSync(process.env.PI_FORGE_TEST_LOG, message + "\\n"); };
if (script === "transcription.py" && command === "doctor") {
  const ready = process.env.PI_FORGE_TEST_NOT_READY !== "1";
  console.log(JSON.stringify({ ready, remediation: ready ? [] : ["Run transcription setup explicitly."] }));
  process.exitCode = ready ? 0 : 1;
} else if (script === "transcription.py" && command === "transcribe") {
  const output = valueAfter("--output");
  mkdirSync(output, { recursive: true });
  writeFileSync(join(output, "raw_transcript.txt"), "raw\\n");
  writeFileSync(join(output, "corrected_transcript.md"), "# Corrected\\n");
  writeFileSync(join(output, "transcription_manifest.csv"), "source_path\\n");
  console.log(JSON.stringify({ run_directory: output, chunk_count: 1, segment_count: 2, correction_count: 3, warnings: [] }));
} else if (script === "file-conversion.py" && command === "convert") {
  const output = valueAfter("--output");
  const input = process.argv[4];
  log("convert-start:" + basename(input));
  await new Promise((resolve) => setTimeout(resolve, 80));
  mkdirSync(join(output, "converted"), { recursive: true });
  writeFileSync(join(output, "converted", "result.txt"), "converted\\n");
  writeFileSync(join(output, "conversion_manifest.csv"), "source_path,status\\n" + input + ",success\\n");
  writeFileSync(join(output, "conversion_log.md"), "# Log\\n");
  const mixed = basename(input).startsWith("mixed");
  console.log(JSON.stringify({ runDirectory: output, success: 1, needsReview: mixed ? 1 : 0, skipped: 0, failed: mixed ? 1 : 0 }));
  log("convert-end:" + basename(input));
} else if (script === "file-conversion.py" && command === "validate") {
  const output = process.argv[4];
  const invalid = output.includes("invalid");
  log("validate:" + basename(output));
  console.log(JSON.stringify({ valid: !invalid, errors: invalid ? ["synthetic validation failure"] : [], warnings: [] }));
  process.exitCode = invalid ? 1 : 0;
} else {
  console.error("unexpected fake worker invocation", process.argv.slice(1));
  process.exitCode = 2;
}
`;
	writeFileSync(join(transcriptionDirectory, "transcription.py"), "# fake transcription entrypoint\n");
	writeFileSync(join(conversionDirectory, "file-conversion.py"), "# fake conversion entrypoint\n");
	const workerPath = join(root, "fake-worker.mjs");
	const interpreterPath = join(root, "fake-python");
	writeFileSync(workerPath, worker);
	writeFileSync(interpreterPath, `#!/bin/sh\nexec "${process.execPath}" "${workerPath}" "$@"\n`);
	chmodSync(interpreterPath, 0o755);
	return { skillsRoot: root, python: interpreterPath };
}

async function connectedBridge(readRoot, writeRoot, workerConfiguration) {
	const server = createBridgeServer({ readRoots: [readRoot], writeRoots: [writeRoot], ...workerConfiguration });
	const client = new Client({ name: "pi-forge-mcp-test", version: "1.0.0" }, { capabilities: {} });
	const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
	await server.connect(serverTransport);
	await client.connect(clientTransport);
	return { client, server };
}

test("MCP bridge discovers tools and returns transcription artifacts", async () => {
	const root = workspace();
	const readRoot = join(root, "read");
	const writeRoot = join(root, "write");
	mkdirSync(readRoot);
	mkdirSync(writeRoot);
	const media = join(readRoot, "lecture.wav");
	writeFileSync(media, "audio");
	const { client, server } = await connectedBridge(readRoot, writeRoot, fakeSkills(join(root, "skills")));
	try {
		const tools = await client.listTools();
		assert.deepEqual(
			tools.tools.map((tool) => tool.name).sort(),
			["forge_convert_files", "forge_transcribe"],
		);
		const response = await client.callTool({
			name: "forge_transcribe",
			arguments: { inputPath: media, outputRoot: writeRoot, recordingType: "lecture" },
		});
		assert.equal(response.isError, false);
		assert.equal(response.structuredContent.status, "success");
		assert.equal(response.structuredContent.counts.corrections, 3);
		assert.equal(
			response.structuredContent.artifacts.some((artifact) => artifact.role === "corrected_transcript.md"),
			true,
		);
	} finally {
		await client.close();
		await server.close();
	}
});

test("stdio launcher keeps stdout protocol-clean", async () => {
	const root = workspace();
	const readRoot = join(root, "read");
	const writeRoot = join(root, "write");
	mkdirSync(readRoot);
	mkdirSync(writeRoot);
	const media = join(readRoot, "stdio.wav");
	writeFileSync(media, "audio");
	const workerConfiguration = fakeSkills(join(root, "skills"));
	const environment = Object.fromEntries(Object.entries(process.env).filter((entry) => entry[1] !== undefined));
	environment.PI_FORGE_MCP_PYTHON = workerConfiguration.python;
	environment.PI_FORGE_MCP_SKILLS_ROOT = workerConfiguration.skillsRoot;
	const transport = new StdioClientTransport({
		command: join(repositoryRoot, "scripts", "pi-forge-mcp-run.sh"),
		args: ["--read-root", readRoot, "--write-root", writeRoot],
		env: environment,
		stderr: "pipe",
	});
	const client = new Client({ name: "pi-forge-stdio-test", version: "1.0.0" }, { capabilities: {} });
	try {
		await client.connect(transport);
		const response = await client.callTool({
			name: "forge_transcribe",
			arguments: { inputPath: media, outputRoot: writeRoot, recordingType: "other" },
		});
		assert.equal(response.structuredContent.status, "success");
	} finally {
		await client.close();
	}
});

test("missing transcription runtime returns structured remediation", async () => {
	const root = workspace();
	const readRoot = join(root, "read");
	const writeRoot = join(root, "write");
	mkdirSync(readRoot);
	mkdirSync(writeRoot);
	const media = join(readRoot, "call.wav");
	writeFileSync(media, "audio");
	process.env.PI_FORGE_TEST_NOT_READY = "1";
	const { client, server } = await connectedBridge(readRoot, writeRoot, fakeSkills(join(root, "skills")));
	try {
		const response = await client.callTool({
			name: "forge_transcribe",
			arguments: { inputPath: media, outputRoot: writeRoot, recordingType: "call" },
		});
		assert.equal(response.isError, true);
		assert.equal(response.structuredContent.error.code, "dependency_not_ready");
		assert.deepEqual(response.structuredContent.error.remediation, ["Run transcription setup explicitly."]);
	} finally {
		await client.close();
		await server.close();
	}
});

test("conversion reports mixed results and validation failures", async () => {
	const root = workspace();
	const readRoot = join(root, "read");
	const writeRoot = join(root, "write");
	mkdirSync(readRoot);
	mkdirSync(writeRoot);
	const mixed = join(readRoot, "mixed.md");
	const invalid = join(readRoot, "invalid.md");
	writeFileSync(mixed, "# Mixed\n");
	writeFileSync(invalid, "# Invalid\n");
	const { client, server } = await connectedBridge(readRoot, writeRoot, fakeSkills(join(root, "skills")));
	try {
		const mixedResponse = await client.callTool({
			name: "forge_convert_files",
			arguments: { inputPaths: [mixed], target: "txt", outputRoot: writeRoot },
		});
		assert.equal(mixedResponse.structuredContent.status, "needs_review");
		assert.equal(mixedResponse.structuredContent.counts.failed, 1);
		assert.equal(mixedResponse.structuredContent.error, null);

		const invalidResponse = await client.callTool({
			name: "forge_convert_files",
			arguments: { inputPaths: [invalid], target: "txt", outputRoot: writeRoot },
		});
		assert.equal(invalidResponse.isError, true);
		assert.equal(invalidResponse.structuredContent.error.code, "validation_failed");
		assert.match(invalidResponse.structuredContent.error.message, /synthetic validation failure/);
	} finally {
		await client.close();
		await server.close();
	}
});

test("path policy rejects outside paths and symlink escapes", async () => {
	const root = workspace();
	const readRoot = join(root, "read");
	const writeRoot = join(root, "write");
	mkdirSync(readRoot);
	mkdirSync(writeRoot);
	const outside = join(root, "outside.md");
	const escaped = join(readRoot, "escaped.md");
	writeFileSync(outside, "outside");
	symlinkSync(outside, escaped);
	const { client, server } = await connectedBridge(readRoot, writeRoot, fakeSkills(join(root, "skills")));
	try {
		for (const inputPath of [outside, escaped]) {
			const response = await client.callTool({
				name: "forge_convert_files",
				arguments: { inputPaths: [inputPath], target: "txt", outputRoot: writeRoot },
			});
			assert.equal(response.isError, true);
			assert.match(response.content[0].text, /path_not_allowed/);
		}
	} finally {
		await client.close();
		await server.close();
	}
});

test("concurrent MCP calls execute through one FIFO worker", async () => {
	const root = workspace();
	const readRoot = join(root, "read");
	const writeRoot = join(root, "write");
	mkdirSync(readRoot);
	mkdirSync(writeRoot);
	const first = join(readRoot, "first.md");
	const second = join(readRoot, "second.md");
	writeFileSync(first, "first");
	writeFileSync(second, "second");
	const log = join(root, "worker.log");
	process.env.PI_FORGE_TEST_LOG = log;
	const { client, server } = await connectedBridge(readRoot, writeRoot, fakeSkills(join(root, "skills")));
	try {
		await Promise.all([
			client.callTool({
				name: "forge_convert_files",
				arguments: { inputPaths: [first], target: "txt", outputRoot: writeRoot },
			}),
			client.callTool({
				name: "forge_convert_files",
				arguments: { inputPaths: [second], target: "txt", outputRoot: writeRoot },
			}),
		]);
		const entries = readFileSync(log, "utf8").trim().split("\n");
		assert.deepEqual(entries, [
			"convert-start:first.md",
			"convert-end:first.md",
			"validate:first",
			"convert-start:second.md",
			"convert-end:second.md",
			"validate:second",
		]);
	} finally {
		await client.close();
		await server.close();
	}
});

test("worker cancellation terminates the child process", async () => {
	const root = workspace();
	const worker = join(root, "wait.mjs");
	writeFileSync(worker, "setTimeout(() => {}, 60_000);\n");
	const controller = new AbortController();
	const resultPromise = runChildProcess(process.execPath, [worker], {
		signal: controller.signal,
		cwd: root,
		label: "cancellation test",
	});
	setTimeout(() => controller.abort(), 50);
	const result = await resultPromise;
	assert.equal(result.canceled, true);
	assert.equal(result.signal, "SIGTERM");
});

function makeFakeInstallSource(source) {
	mkdirSync(join(source, "scripts"), { recursive: true });
	mkdirSync(join(source, "forge"), { recursive: true });
	mkdirSync(join(source, "packages", "coding-agent", "dist"), { recursive: true });
	for (const script of ["pi-forge-install.sh", "configure-pi-forge.mjs", "pi-forge-run.sh", "pi-forge-mcp-run.sh"]) {
		const target = join(source, "scripts", script);
		writeFileSync(target, readFileSync(join(repositoryRoot, "scripts", script), "utf8"));
		chmodSync(target, 0o755);
	}
	writeFileSync(join(source, "package.json"), "{}\n");
	writeFileSync(join(source, "forge", "AGENTS.md"), "# Agent\n");
	writeFileSync(
		join(source, "packages", "coding-agent", "dist", "cli.js"),
		"#!/usr/bin/env node\nif (process.argv.includes('--print-agent-dir')) console.log(process.env.PI_CODING_AGENT_DIR);\n",
	);
	writeFileSync(join(source, "update.sh"), "#!/usr/bin/env bash\nexit 0\n");
	chmodSync(join(source, "update.sh"), 0o755);
	writeFileSync(
		join(source, "scripts", "pi-forge-mcp-server.mjs"),
		"#!/usr/bin/env node\nif (process.argv.includes('--help')) console.log('Usage: pi-forge-mcp');\n",
	);
	chmodSync(join(source, "scripts", "pi-forge-mcp-server.mjs"), 0o755);
}

function cleanPiForgeEnvironment() {
	return Object.fromEntries(
		Object.entries(process.env).filter(([key, value]) => value !== undefined && !key.startsWith("PI_FORGE_")),
	);
}

test("installer exposes a usable MCP launcher", () => {
	const root = workspace();
	const piForgeHome = join(root, ".pi-forge");
	const source = join(piForgeHome, "repository");
	makeFakeInstallSource(source);
	const bin = join(piForgeHome, "bin");
	const agent = join(piForgeHome, "agent");
	const environment = cleanPiForgeEnvironment();
	const install = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-install.sh"),
			"--source-dir",
			source,
			"--resources-only",
		],
		{ encoding: "utf8", env: { ...environment, HOME: root, SHELL: "/bin/zsh" } },
	);
	assert.equal(install.status, 0, install.stderr);
	const launcher = join(bin, "pi-forge-mcp");
	assert.equal(existsSync(join(bin, "pi-forge")), true);
	assert.equal(existsSync(launcher), true);
	assert.equal(existsSync(join(agent, "AGENTS.md")), true);
	assert.equal(existsSync(join(agent, "sessions")), true);
	assert.match(readFileSync(join(root, ".zprofile"), "utf8"), new RegExp(piForgeHome.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
	assert.match(install.stdout, new RegExp(`State: ${agent.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
	const result = spawnSync(launcher, ["--help"], { encoding: "utf8", env: { ...environment, HOME: root, SHELL: "/bin/zsh" } });
	assert.equal(result.status, 0, result.stderr);
	assert.match(result.stdout, /Usage: pi-forge-mcp/);
	assert.equal(existsSync(join(repositoryRoot, "integrations", "pi-forge-delegation", "SKILL.md")), true);
});

test("development checkout launcher uses pi-forge agent home", () => {
	const root = workspace();
	const source = join(root, "source");
	makeFakeInstallSource(source);
	const piForgeHome = join(root, ".pi-forge");
	const agent = join(piForgeHome, "agent");
	const environment = cleanPiForgeEnvironment();
	const install = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-install.sh"),
			"--source-dir",
			source,
			"--resources-only",
		],
		{ encoding: "utf8", env: { ...environment, HOME: root, SHELL: "/bin/zsh" } },
	);
	assert.equal(install.status, 0, install.stderr);
	const result = spawnSync(join(piForgeHome, "bin", "pi-forge"), ["--print-agent-dir"], {
		encoding: "utf8",
		env: { ...environment, HOME: root, SHELL: "/bin/zsh" },
	});
	assert.equal(result.status, 0, result.stderr);
	assert.equal(result.stdout.trim(), agent);
});

test("installer migrates mistaken pi-vault install into pi-forge home", () => {
	const root = workspace();
	const dataHome = join(root, ".local", "share");
	const mistakenHome = join(dataHome, "pi-vault");
	const newHome = join(root, ".pi-forge");
	const source = join(mistakenHome, "repository");
	makeFakeInstallSource(source);
	mkdirSync(newHome);
	mkdirSync(join(mistakenHome, "bin"));
	symlinkSync(join(source, "scripts", "pi-forge-run.sh"), join(mistakenHome, "bin", "pi-forge"));
	symlinkSync(join(source, "scripts", "pi-forge-mcp-run.sh"), join(mistakenHome, "bin", "pi-forge-mcp"));
	symlinkSync(join(source, "update.sh"), join(mistakenHome, "bin", "pi-forge-update"));
	const oldAgent = join(root, ".pi-forge", "agent");
	mkdirSync(oldAgent, { recursive: true });
	writeFileSync(join(oldAgent, "auth.json"), "{}\n");
	const legacyBin = join(root, ".local", "bin");
	mkdirSync(legacyBin, { recursive: true });
	symlinkSync(join(source, "scripts", "pi-forge-run.sh"), join(legacyBin, "pi-forge"));
	symlinkSync(join(source, "scripts", "pi-forge-mcp-run.sh"), join(legacyBin, "pi-forge-mcp"));
	symlinkSync(join(source, "update.sh"), join(legacyBin, "pi-forge-update"));

	const install = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-install.sh"),
			"--source-dir",
			source,
			"--resources-only",
		],
		{
			encoding: "utf8",
			env: { ...cleanPiForgeEnvironment(), HOME: root, XDG_DATA_HOME: dataHome, SHELL: "/bin/zsh" },
		},
	);
	assert.equal(install.status, 0, install.stderr);
	assert.equal(existsSync(join(newHome, "repository", "scripts", "pi-forge-install.sh")), true);
	assert.equal(existsSync(mistakenHome), false);
	assert.equal(existsSync(join(newHome, "agent", "auth.json")), true);
	assert.equal(existsSync(join(newHome, "agent", "AGENTS.md")), true);
	assert.equal(existsSync(join(newHome, "agent", "sessions")), true);
	assert.equal(existsSync(join(newHome, "bin", "pi-forge")), true);
	assert.equal(existsSync(join(newHome, "bin", "pi-forge-mcp")), true);
	assert.equal(existsSync(join(newHome, "bin", "pi-forge-update")), true);
	assert.match(readFileSync(join(root, ".zprofile"), "utf8"), new RegExp(newHome.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
	assert.equal(existsSync(join(legacyBin, "pi-forge")), false);
	assert.equal(existsSync(join(legacyBin, "pi-forge-mcp")), false);
	assert.equal(existsSync(join(legacyBin, "pi-forge-update")), false);
});

test("installer migrates local-share pi-forge install into home pi-forge", () => {
	const root = workspace();
	const dataHome = join(root, ".local", "share");
	const oldHome = join(dataHome, "pi-forge");
	const newHome = join(root, ".pi-forge");
	const source = join(oldHome, "repository");
	makeFakeInstallSource(source);
	const oldAgent = join(oldHome, "agent");
	mkdirSync(oldAgent, { recursive: true });
	writeFileSync(join(oldAgent, "auth.json"), "{}\n");

	const install = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-install.sh"),
			"--source-dir",
			source,
			"--resources-only",
		],
		{
			encoding: "utf8",
			env: { ...cleanPiForgeEnvironment(), HOME: root, XDG_DATA_HOME: dataHome, SHELL: "/bin/zsh" },
		},
	);
	assert.equal(install.status, 0, install.stderr);
	assert.equal(existsSync(join(newHome, "repository", "scripts", "pi-forge-install.sh")), true);
	assert.equal(existsSync(oldHome), false);
	assert.equal(existsSync(join(newHome, "agent", "auth.json")), true);
	assert.equal(existsSync(join(newHome, "bin", "pi-forge")), true);
	assert.match(readFileSync(join(root, ".zprofile"), "utf8"), new RegExp(newHome.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
});
