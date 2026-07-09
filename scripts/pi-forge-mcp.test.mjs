import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
	appendFileSync,
	chmodSync,
	existsSync,
	mkdtempSync,
	mkdirSync,
	readFileSync,
	readlinkSync,
	realpathSync,
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

function makeFakeInstallSource(source, version = "0.0.0-test") {
	mkdirSync(join(source, "scripts"), { recursive: true });
	mkdirSync(join(source, "forge", "bin"), { recursive: true });
	mkdirSync(join(source, "forge", "scripts"), { recursive: true });
	mkdirSync(join(source, "forge", "skills", "document-ingest"), { recursive: true });
	for (const packageDir of ["ai", "agent", "tui", "coding-agent"]) {
		mkdirSync(join(source, "packages", packageDir, "dist"), { recursive: true });
	}
	for (const script of ["pi-forge-install.sh", "pi-forge-run.sh", "pi-forge-mcp-run.sh"]) {
		const target = join(source, "scripts", script);
		writeFileSync(target, readFileSync(join(repositoryRoot, "scripts", script), "utf8"));
		chmodSync(target, 0o755);
	}
	writeFileSync(
		join(source, "forge", "package.json"),
		`${JSON.stringify({
			name: "@ellian-eorwyn/pi-forge",
			version,
			type: "module",
			bin: {
				"pi-forge": "bin/pi-forge.mjs",
				"pi-forge-mcp": "bin/pi-forge-mcp.mjs",
				"pi-forge-update": "bin/pi-forge-update.mjs",
			},
			files: ["AGENTS.md", "bin", "scripts", "skills"],
		})}\n`,
	);
	writeFileSync(
		join(source, "forge", "scripts", "configure-pi-forge.mjs"),
		readFileSync(join(repositoryRoot, "forge", "scripts", "configure-pi-forge.mjs"), "utf8"),
	);
	writeFileSync(
		join(source, "forge", "bin", "pi-forge.mjs"),
		"#!/usr/bin/env node\nimport { join } from 'node:path';\nconst home = process.env.PI_FORGE_HOME || join(process.env.HOME, '.pi-forge');\nconst agent = process.env.PI_CODING_AGENT_DIR || process.env.PI_FORGE_AGENT_DIR || join(home, 'agent');\nif (process.argv.includes('--print-agent-dir')) console.log(agent);\n",
	);
	writeFileSync(
		join(source, "forge", "bin", "pi-forge-mcp.mjs"),
		"#!/usr/bin/env node\nif (process.argv.includes('--help')) console.log('Usage: pi-forge-mcp');\n",
	);
	writeFileSync(join(source, "forge", "bin", "pi-forge-update.mjs"), "#!/usr/bin/env node\nconsole.log('updated');\n");
	writeFileSync(
		join(source, "package.json"),
		`${JSON.stringify({
			private: true,
			workspaces: ["packages/*", "forge"],
			scripts: {
				build: "node scripts/build.mjs",
				"build:install": "node scripts/build-install.mjs",
			},
		})}\n`,
	);
	writeFileSync(join(source, "forge", "AGENTS.md"), "# Agent\n");
	writeFileSync(
		join(source, "forge", "skills", "document-ingest", "SKILL.md"),
		"---\nname: document-ingest\ndescription: Test skill.\n---\n\n# Test\n",
	);
	for (const command of ["pi-forge.mjs", "pi-forge-mcp.mjs", "pi-forge-update.mjs"]) {
		chmodSync(join(source, "forge", "bin", command), 0o755);
	}
	const packageDefinitions = [
		{
			directory: "ai",
			json: {
				name: "@earendil-works/pi-ai",
				version,
				type: "module",
				main: "./dist/index.js",
				exports: {
					".": "./dist/index.js",
					"./base": "./dist/base.js",
				},
				files: ["dist"],
			},
			files: {
				"dist/index.js": "export const version = '0.0.0-source-runtime';\n",
				"dist/base.js": "export const base = true;\n",
			},
		},
		{
			directory: "agent",
			json: {
				name: "@earendil-works/pi-agent-core",
				version,
				type: "module",
				main: "./dist/index.js",
				exports: {
					".": "./dist/index.js",
				},
				files: ["dist"],
				dependencies: {
					"@earendil-works/pi-ai": version,
				},
			},
			files: {
				"dist/index.js": "export const version = '0.0.0-source-runtime';\n",
			},
		},
		{
			directory: "tui",
			json: {
				name: "@earendil-works/pi-tui",
				version,
				type: "module",
				main: "./dist/index.js",
				files: ["dist"],
			},
			files: {
				"dist/index.js": "export const version = '0.0.0-source-runtime';\n",
			},
		},
		{
			directory: "coding-agent",
			json: {
				name: "@earendil-works/pi-coding-agent",
				version,
				type: "module",
				main: "./dist/index.js",
				exports: {
					".": "./dist/index.js",
				},
				bin: {
					pi: "dist/cli.js",
				},
				files: ["dist", "npm-shrinkwrap.json"],
				dependencies: {
					"@earendil-works/pi-agent-core": version,
					"@earendil-works/pi-ai": version,
					"@earendil-works/pi-tui": version,
				},
			},
			files: {
				"dist/index.js": "export const version = '0.0.0-source-runtime';\n",
				"dist/cli.js": "#!/usr/bin/env node\nif (process.argv.includes('--version')) console.log('0.0.0-source-runtime');\nif (process.argv.includes('--print-agent-dir')) console.log(process.env.PI_CODING_AGENT_DIR);\n",
				"dist/source-runtime-marker.js": "export const packedFromSource = true;\n",
				"npm-shrinkwrap.json": `${JSON.stringify({
					name: "@earendil-works/pi-coding-agent",
					version,
					lockfileVersion: 3,
					requires: true,
					packages: {
						"": {
							name: "@earendil-works/pi-coding-agent",
							version,
						},
					},
				})}\n`,
			},
		},
	];
	for (const definition of packageDefinitions) {
		const packageRoot = join(source, "packages", definition.directory);
		writeFileSync(join(packageRoot, "package.json"), `${JSON.stringify(definition.json)}\n`);
		for (const [relativePath, contents] of Object.entries(definition.files)) {
			writeFileSync(join(packageRoot, relativePath), contents);
		}
	}
	chmodSync(join(source, "packages", "coding-agent", "dist", "cli.js"), 0o755);
	writeFileSync(join(source, "update.sh"), "#!/usr/bin/env bash\nexit 0\n");
	chmodSync(join(source, "update.sh"), 0o755);
	writeFileSync(
		join(source, "scripts", "pi-forge-mcp-server.mjs"),
		"#!/usr/bin/env node\nif (process.argv.includes('--help')) console.log('Usage: pi-forge-mcp');\n",
	);
	chmodSync(join(source, "scripts", "pi-forge-mcp-server.mjs"), 0o755);
}

function makeFakePackageTarball(root) {
	const packageRoot = join(root, "package-source");
	mkdirSync(join(packageRoot, "bin"), { recursive: true });
	mkdirSync(join(packageRoot, "scripts"), { recursive: true });
	mkdirSync(join(packageRoot, "skills", "document-ingest"), { recursive: true });
	writeFileSync(
		join(packageRoot, "package.json"),
		`${JSON.stringify({
			name: "@ellian-eorwyn/pi-forge",
			version: "0.0.0-test",
			type: "module",
			bin: {
				"pi-forge": "bin/pi-forge.mjs",
				"pi-forge-mcp": "bin/pi-forge-mcp.mjs",
				"pi-forge-update": "bin/pi-forge-update.mjs",
			},
			files: ["AGENTS.md", "bin", "scripts", "skills"],
		})}\n`,
	);
	writeFileSync(join(packageRoot, "AGENTS.md"), "# Fake Package Agent\n");
	writeFileSync(
		join(packageRoot, "scripts", "configure-pi-forge.mjs"),
		readFileSync(join(repositoryRoot, "forge", "scripts", "configure-pi-forge.mjs"), "utf8"),
	);
	writeFileSync(
		join(packageRoot, "scripts", "runtime-env.mjs"),
		readFileSync(join(repositoryRoot, "forge", "scripts", "runtime-env.mjs"), "utf8"),
	);
	writeFileSync(
		join(packageRoot, "bin", "pi-forge.mjs"),
		readFileSync(join(repositoryRoot, "forge", "bin", "pi-forge.mjs"), "utf8"),
	);
	writeFileSync(
		join(packageRoot, "bin", "pi-forge-mcp.mjs"),
		"#!/usr/bin/env node\nif (process.argv.includes('--help')) console.log('Usage: pi-forge-mcp');\nelse console.log(new URL('../skills/', import.meta.url).pathname);\n",
	);
	writeFileSync(
		join(packageRoot, "bin", "pi-forge-update.mjs"),
		readFileSync(join(repositoryRoot, "forge", "bin", "pi-forge-update.mjs"), "utf8"),
	);
	writeFileSync(
		join(packageRoot, "skills", "document-ingest", "SKILL.md"),
		"---\nname: document-ingest\ndescription: Test skill.\n---\n\n# Test\n",
	);
	for (const command of ["pi-forge.mjs", "pi-forge-mcp.mjs", "pi-forge-update.mjs"]) {
		chmodSync(join(packageRoot, "bin", command), 0o755);
	}
	const npmCache = join(root, "npm-cache");
	mkdirSync(npmCache);
	const pack = spawnSync("npm", ["pack", "--json", "--pack-destination", root], {
		cwd: packageRoot,
		encoding: "utf8",
		env: { ...process.env, npm_config_cache: npmCache },
	});
	assert.equal(pack.status, 0, pack.stderr);
	return join(root, JSON.parse(pack.stdout)[0].filename);
}

function makeFakePiPackageTarball(root) {
	const packageRoot = join(root, "pi-package-source");
	mkdirSync(join(packageRoot, "dist"), { recursive: true });
	writeFileSync(
		join(packageRoot, "package.json"),
		`${JSON.stringify({
			name: "@earendil-works/pi-coding-agent",
			version: "0.0.0-test",
			type: "module",
			exports: {
				".": "./dist/index.js",
			},
			bin: {
				pi: "dist/cli.js",
			},
			files: ["dist"],
		})}\n`,
	);
	writeFileSync(join(packageRoot, "dist", "index.js"), "export const version = '0.0.0-test';\n");
	writeFileSync(
		join(packageRoot, "dist", "cli.js"),
		"#!/usr/bin/env node\nif (process.argv.includes('--version')) console.log('0.0.0-test');\nif (process.argv.includes('--print-agent-dir')) console.log(process.env.PI_CODING_AGENT_DIR);\n",
	);
	chmodSync(join(packageRoot, "dist", "cli.js"), 0o755);
	const npmCache = join(root, "pi-npm-cache");
	mkdirSync(npmCache);
	const pack = spawnSync("npm", ["pack", "--json", "--pack-destination", root], {
		cwd: packageRoot,
		encoding: "utf8",
		env: { ...process.env, npm_config_cache: npmCache },
	});
	assert.equal(pack.status, 0, pack.stderr);
	return join(root, JSON.parse(pack.stdout)[0].filename);
}

function makeSourceArchive(root, source) {
	const archive = join(root, "pi-forge-source.tar.gz");
	const pack = spawnSync("tar", ["-czf", archive, "-C", dirname(source), basename(source)], { encoding: "utf8" });
	assert.equal(pack.status, 0, pack.stderr);
	return archive;
}

function makeFakeNpmWrapper(root, sourceRootName) {
	const fakeBin = join(root, "fake-bin");
	const npmLog = join(root, "npm.log");
	const realNpm = spawnSync("which", ["npm"], { encoding: "utf8" }).stdout.trim();
	assert.notEqual(realNpm, "");
	mkdirSync(fakeBin);
	writeFileSync(
		join(fakeBin, "npm"),
		`#!/usr/bin/env bash
printf '%s|%s\\n' "$PWD" "$*" >> "$NPM_LOG"
target="$PWD"
command="$1"
subcommand="$2"
if [[ "$1" == "--prefix" ]]; then
	target="$2"
	command="$3"
	subcommand="$4"
fi
if [[ "$(basename "$target")" == "$SOURCE_ROOT_NAME" ]]; then
	if [[ "$command" == "ci" ]]; then
		exit 0
	fi
	if [[ "$command" == "run" && "$subcommand" == "build:install" ]]; then
		exit 0
	fi
fi
case " $* " in
	*" @ellian-eorwyn/pi-forge@latest "*|*" @earendil-works/pi-coding-agent@latest "*) echo "npm error code E404" >&2; exit 1 ;;
esac
exec "$REAL_NPM" "$@"
`,
	);
	chmodSync(join(fakeBin, "npm"), 0o755);
	return { fakeBin, npmLog, realNpm };
}

function cleanPiForgeEnvironment() {
	return Object.fromEntries(
		Object.entries(process.env).filter(([key, value]) => value !== undefined && !key.startsWith("PI_FORGE_")),
	);
}

test("installer exposes a usable MCP launcher from the npm package install", () => {
	const root = workspace();
	const tarball = makeFakePackageTarball(root);
	const piTarball = makeFakePiPackageTarball(root);
	const piForgeHome = join(root, ".pi-forge");
	const bin = join(piForgeHome, "bin");
	const agent = join(piForgeHome, "agent");
	const environment = cleanPiForgeEnvironment();
	const install = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-install.sh"),
		],
		{
			encoding: "utf8",
			env: {
				...environment,
				HOME: root,
				SHELL: "/bin/zsh",
				PI_FORGE_PACKAGE_SPEC: `file:${tarball}`,
				PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
			},
		},
	);
	assert.equal(install.status, 0, install.stderr);
	const launcher = join(bin, "pi-forge-mcp");
	const packageRoot = join(piForgeHome, "app", "node_modules", "@ellian-eorwyn", "pi-forge");
	assert.equal(existsSync(join(bin, "pi-forge")), true);
	assert.equal(existsSync(launcher), true);
	assert.equal(existsSync(join(piForgeHome, "app", "node_modules", "@earendil-works", "pi-coding-agent", "package.json")), true);
	assert.equal(existsSync(join(agent, "AGENTS.md")), true);
	assert.equal(existsSync(join(agent, "sessions")), true);
	assert.equal(existsSync(join(packageRoot, "skills", "document-ingest", "SKILL.md")), true);
	assert.doesNotMatch(readlinkSync(launcher), /repository/);
	assert.match(readFileSync(join(root, ".zprofile"), "utf8"), new RegExp(piForgeHome.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
	assert.match(install.stdout, new RegExp(`State: ${agent.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
	const settings = JSON.parse(readFileSync(join(agent, "settings.json"), "utf8"));
	assert.equal(settings.packages[0], realpathSync(packageRoot));
	const appPackage = JSON.parse(readFileSync(join(piForgeHome, "app", "package.json"), "utf8"));
	assert.match(appPackage.dependencies["@earendil-works/pi-coding-agent"], /earendil-works-pi-coding-agent/);
	const result = spawnSync(launcher, ["--help"], { encoding: "utf8", env: { ...environment, HOME: root, SHELL: "/bin/zsh" } });
	assert.equal(result.status, 0, result.stderr);
	assert.match(result.stdout, /Usage: pi-forge-mcp/);
	const nestedPiRoot = join(packageRoot, "node_modules", "@earendil-works", "pi-coding-agent");
	mkdirSync(join(nestedPiRoot, "dist"), { recursive: true });
	writeFileSync(
		join(nestedPiRoot, "package.json"),
		`${JSON.stringify({
			name: "@earendil-works/pi-coding-agent",
			version: "0.0.0-nested",
			type: "module",
			exports: {
				".": "./dist/index.js",
			},
		})}\n`,
	);
	writeFileSync(join(nestedPiRoot, "dist", "index.js"), "export const version = '0.0.0-nested';\n");
	writeFileSync(join(nestedPiRoot, "dist", "cli.js"), "#!/usr/bin/env node\nconsole.log('0.0.0-nested');\n");
	chmodSync(join(nestedPiRoot, "dist", "cli.js"), 0o755);
	const piResult = spawnSync(join(bin, "pi-forge"), ["--version"], {
		encoding: "utf8",
		env: { ...environment, HOME: root, SHELL: "/bin/zsh" },
	});
	assert.equal(piResult.status, 0, piResult.stderr);
	assert.equal(piResult.stdout.trim(), "0.0.0-test");
});

test("installer uses the GitHub source archive when no package spec is configured", () => {
	const root = workspace();
	const source = join(root, "source");
	makeFakeInstallSource(source);
	const sourceArchive = makeSourceArchive(root, source);
	const piTarball = makeFakePiPackageTarball(root);
	const { fakeBin, npmLog, realNpm } = makeFakeNpmWrapper(root, basename(source));

	const install = spawnSync("bash", [join(repositoryRoot, "scripts", "pi-forge-install.sh")], {
		encoding: "utf8",
		env: {
			...cleanPiForgeEnvironment(),
			HOME: root,
			NPM_LOG: npmLog,
			PATH: `${fakeBin}:${process.env.PATH}`,
			PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
			PI_FORGE_SOURCE_ARCHIVE_URL: `file://${sourceArchive}`,
			REAL_NPM: realNpm,
			SOURCE_ROOT_NAME: basename(source),
			SHELL: "/bin/zsh",
		},
	});
	assert.equal(install.status, 0, install.stderr);
	assert.match(install.stderr, /Installing pi-forge from file:/);
	assert.doesNotMatch(install.stderr, /npm error code E404/);
	const packageRoot = join(root, ".pi-forge", "app", "node_modules", "@ellian-eorwyn", "pi-forge");
	assert.equal(existsSync(join(packageRoot, "package.json")), true);
	assert.equal(existsSync(join(root, ".pi-forge", "app", "package-cache")), true);
	const npmCalls = readFileSync(npmLog, "utf8");
	assert.doesNotMatch(npmCalls, /@ellian-eorwyn\/pi-forge@latest/);
	assert.match(npmCalls, /install --omit=dev --ignore-scripts file:/);
});

test("installer packs Pi runtime packages from the GitHub source archive by default", () => {
	const root = workspace();
	const source = join(root, "source");
	makeFakeInstallSource(source);
	const sourceArchive = makeSourceArchive(root, source);
	const { fakeBin, npmLog, realNpm } = makeFakeNpmWrapper(root, basename(source));

	const install = spawnSync("bash", [join(repositoryRoot, "scripts", "pi-forge-install.sh")], {
		encoding: "utf8",
		env: {
			...cleanPiForgeEnvironment(),
			HOME: root,
			NPM_LOG: npmLog,
			PATH: `${fakeBin}:${process.env.PATH}`,
			PI_FORGE_SOURCE_ARCHIVE_URL: `file://${sourceArchive}`,
			REAL_NPM: realNpm,
			SOURCE_ROOT_NAME: basename(source),
			SHELL: "/bin/zsh",
		},
	});
	assert.equal(install.status, 0, install.stderr);
	assert.match(install.stderr, /Installing pi-forge from file:/);
	assert.match(install.stderr, /Installing Pi runtime packages from file:/);
	const appRoot = join(root, ".pi-forge", "app");
	const appPackage = JSON.parse(readFileSync(join(appRoot, "package.json"), "utf8"));
	for (const packageName of [
		"@earendil-works/pi-ai",
		"@earendil-works/pi-agent-core",
		"@earendil-works/pi-tui",
		"@earendil-works/pi-coding-agent",
	]) {
		assert.match(appPackage.dependencies[packageName], /^file:/);
	}
	assert.equal(
		existsSync(join(appRoot, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "source-runtime-marker.js")),
		true,
	);
	assert.equal(existsSync(join(appRoot, "node_modules", "@earendil-works", "pi-coding-agent", "npm-shrinkwrap.json")), false);
	const npmCalls = readFileSync(npmLog, "utf8");
	assert.match(npmCalls, /ci --ignore-scripts/);
	assert.match(npmCalls, /run build:install/);
	assert.doesNotMatch(npmCalls, /@earendil-works\/pi-coding-agent@latest/);
});

test("packaged updater refreshes pi-forge from the GitHub source archive by default", () => {
	const root = workspace();
	const initialTarball = makeFakePackageTarball(root);
	const piTarball = makeFakePiPackageTarball(root);
	const updatedSource = join(root, "updated-source");
	makeFakeInstallSource(updatedSource, "0.0.1-source");
	const sourceArchive = makeSourceArchive(root, updatedSource);
	const { fakeBin, npmLog, realNpm } = makeFakeNpmWrapper(root, basename(updatedSource));
	const environment = cleanPiForgeEnvironment();
	const install = spawnSync("bash", [join(repositoryRoot, "scripts", "pi-forge-install.sh")], {
		encoding: "utf8",
		env: {
			...environment,
			HOME: root,
			SHELL: "/bin/zsh",
			PI_FORGE_PACKAGE_SPEC: `file:${initialTarball}`,
			PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
		},
	});
	assert.equal(install.status, 0, install.stderr);

	const update = spawnSync(join(root, ".pi-forge", "bin", "pi-forge-update"), [], {
		encoding: "utf8",
		env: {
			...environment,
			HOME: root,
			NPM_LOG: npmLog,
			PATH: `${fakeBin}:${process.env.PATH}`,
			REAL_NPM: realNpm,
			SHELL: "/bin/zsh",
			PI_FORGE_SOURCE_ARCHIVE_URL: `file://${sourceArchive}`,
			SOURCE_ROOT_NAME: basename(updatedSource),
		},
	});
	assert.equal(update.status, 0, update.stderr);
	assert.match(update.stderr, /pi-forge-update: installing pi-forge from file:/);
	assert.match(update.stdout, /Pi package: runtime packages from file:/);
	const packageJson = JSON.parse(
		readFileSync(join(root, ".pi-forge", "app", "node_modules", "@ellian-eorwyn", "pi-forge", "package.json"), "utf8"),
	);
	assert.equal(packageJson.version, "0.0.1-source");
	const appRoot = join(root, ".pi-forge", "app");
	const appPackage = JSON.parse(readFileSync(join(appRoot, "package.json"), "utf8"));
	assert.match(appPackage.dependencies["@earendil-works/pi-coding-agent"], /^file:/);
	assert.equal(
		existsSync(join(appRoot, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "source-runtime-marker.js")),
		true,
	);
	const npmCalls = readFileSync(npmLog, "utf8");
	assert.match(npmCalls, /ci --ignore-scripts/);
	assert.doesNotMatch(npmCalls, /@earendil-works\/pi-coding-agent@latest/);
});

test("installer uses package install unless dev-link is explicit", () => {
	const root = workspace();
	const tarball = makeFakePackageTarball(root);
	const piTarball = makeFakePiPackageTarball(root);
	const source = join(root, "source");
	makeFakeInstallSource(source);
	const piForgeHome = join(root, ".pi-forge");
	const agent = join(piForgeHome, "agent");
	const environment = cleanPiForgeEnvironment();
	const packageInstall = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-install.sh"),
			"--source-dir",
			source,
		],
		{
			encoding: "utf8",
			env: {
				...environment,
				HOME: root,
				SHELL: "/bin/zsh",
				PI_FORGE_PACKAGE_SPEC: `file:${tarball}`,
				PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
			},
		},
	);
	assert.equal(packageInstall.status, 0, packageInstall.stderr);
	assert.equal(existsSync(source), true);
	assert.doesNotMatch(readlinkSync(join(piForgeHome, "bin", "pi-forge")), /source/);

	const install = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-install.sh"),
			"--source-dir",
			source,
			"--dev-link",
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

test("installer moves legacy local-share state during default home install", () => {
	const root = workspace();
	const tarball = makeFakePackageTarball(root);
	const piTarball = makeFakePiPackageTarball(root);
	const dataHome = join(root, ".local", "share");
	const oldHome = join(dataHome, "pi-forge");
	const newHome = join(root, ".pi-forge");
	const source = join(newHome, "repository");
	makeFakeInstallSource(source);
	mkdirSync(join(oldHome, "agent"), { recursive: true });
	writeFileSync(join(oldHome, "agent", "auth.json"), "{}\n");
	mkdirSync(join(oldHome, "bin"));
	symlinkSync(join(oldHome, "repository", "scripts", "pi-forge-run.sh"), join(oldHome, "bin", "pi-forge"));

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
			env: {
				...cleanPiForgeEnvironment(),
				HOME: root,
				XDG_DATA_HOME: dataHome,
				SHELL: "/bin/zsh",
				PI_FORGE_PACKAGE_SPEC: `file:${tarball}`,
				PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
			},
		},
	);
	assert.equal(install.status, 0, install.stderr);
	assert.equal(existsSync(join(newHome, "agent", "auth.json")), true);
	assert.equal(existsSync(join(newHome, "agent", "AGENTS.md")), true);
	assert.equal(existsSync(join(newHome, "app", "node_modules", "@ellian-eorwyn", "pi-forge", "package.json")), true);
	assert.equal(existsSync(oldHome), false);
});

test("installer migrates mistaken pi-vault install into pi-forge home", () => {
	const root = workspace();
	const tarball = makeFakePackageTarball(root);
	const piTarball = makeFakePiPackageTarball(root);
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
			env: {
				...cleanPiForgeEnvironment(),
				HOME: root,
				XDG_DATA_HOME: dataHome,
				SHELL: "/bin/zsh",
				PI_FORGE_PACKAGE_SPEC: `file:${tarball}`,
				PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
			},
		},
	);
	assert.equal(install.status, 0, install.stderr);
	assert.equal(existsSync(join(newHome, "repository")), false);
	assert.equal(existsSync(join(newHome, "app", "node_modules", "@ellian-eorwyn", "pi-forge", "package.json")), true);
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

test("installer does not merge unrelated pi-vault state over an existing pi-forge agent", () => {
	const root = workspace();
	const piTarball = makeFakePiPackageTarball(root);
	const dataHome = join(root, ".local", "share");
	const piVaultHome = join(dataHome, "pi-vault");
	const newHome = join(root, ".pi-forge");
	const source = join(newHome, "repository");
	makeFakeInstallSource(source);
	mkdirSync(join(newHome, "agent"), { recursive: true });
	writeFileSync(join(newHome, "agent", "auth.json"), '{"piForge":true}\n');
	mkdirSync(join(piVaultHome, "agent"), { recursive: true });
	writeFileSync(join(piVaultHome, "agent", "auth.json"), '{"piVault":true}\n');

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
			env: {
				...cleanPiForgeEnvironment(),
				HOME: root,
				XDG_DATA_HOME: dataHome,
				SHELL: "/bin/zsh",
				PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
			},
		},
	);
	assert.equal(install.status, 0, install.stderr);
	assert.doesNotMatch(install.stderr, /leaving legacy path in place/);
	assert.equal(readFileSync(join(newHome, "agent", "auth.json"), "utf8"), '{"piForge":true}\n');
	assert.equal(readFileSync(join(piVaultHome, "agent", "auth.json"), "utf8"), '{"piVault":true}\n');
});

test("installer migrates local-share pi-forge install into home pi-forge", () => {
	const root = workspace();
	const tarball = makeFakePackageTarball(root);
	const piTarball = makeFakePiPackageTarball(root);
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
			env: {
				...cleanPiForgeEnvironment(),
				HOME: root,
				XDG_DATA_HOME: dataHome,
				SHELL: "/bin/zsh",
				PI_FORGE_PACKAGE_SPEC: `file:${tarball}`,
				PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
			},
		},
	);
	assert.equal(install.status, 0, install.stderr);
	assert.equal(existsSync(join(newHome, "repository")), false);
	assert.equal(existsSync(join(newHome, "app", "node_modules", "@ellian-eorwyn", "pi-forge", "package.json")), true);
	assert.equal(existsSync(oldHome), false);
	assert.equal(existsSync(join(newHome, "agent", "auth.json")), true);
	assert.equal(existsSync(join(newHome, "bin", "pi-forge")), true);
	assert.match(readFileSync(join(root, ".zprofile"), "utf8"), new RegExp(newHome.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
});

test("legacy pi-forge-update migrates managed repository to package app", () => {
	const root = workspace();
	const piTarball = makeFakePiPackageTarball(root);
	const piForgeHome = join(root, ".pi-forge");
	const source = join(piForgeHome, "repository");
	const fakeBin = join(root, "fake-bin");
	const gitLog = join(root, "git.log");
	makeFakeInstallSource(source);
	mkdirSync(join(source, ".git"));
	mkdirSync(fakeBin);
	writeFileSync(join(source, "update.sh"), readFileSync(join(repositoryRoot, "update.sh"), "utf8"));
	chmodSync(join(source, "update.sh"), 0o755);
	writeFileSync(
		join(fakeBin, "git"),
		`#!/usr/bin/env bash
if [[ "$1" == "-C" ]]; then
	shift 2
fi
case "$1" in
	status) exit 0 ;;
	rev-parse) printf 'old-head\\n'; exit 0 ;;
	pull) printf 'pull\\n' >> "$GIT_LOG"; exit 0 ;;
	*) printf 'unexpected git command: %s\\n' "$*" >&2; exit 2 ;;
esac
`,
	);
	chmodSync(join(fakeBin, "git"), 0o755);

	const update = spawnSync(join(source, "update.sh"), [], {
		encoding: "utf8",
		env: {
			...cleanPiForgeEnvironment(),
			HOME: root,
			GIT_LOG: gitLog,
			PATH: `${fakeBin}:${process.env.PATH}`,
			SHELL: "/bin/zsh",
			PI_FORGE_PI_PACKAGE_SPEC: `file:${piTarball}`,
		},
	});
	assert.equal(update.status, 0, update.stderr);
	assert.equal(readFileSync(gitLog, "utf8"), "pull\n");
	assert.equal(existsSync(source), false);
	assert.equal(existsSync(join(piForgeHome, "app", "package-cache")), true);
	assert.equal(existsSync(join(piForgeHome, "app", "node_modules", "@ellian-eorwyn", "pi-forge", "package.json")), true);
	assert.doesNotMatch(readlinkSync(join(piForgeHome, "bin", "pi-forge")), /repository/);
});

test("uninstaller removes managed app while preserving agent state", () => {
	const root = workspace();
	const piForgeHome = join(root, ".pi-forge");
	const app = join(piForgeHome, "app");
	const packageRoot = join(app, "node_modules", "@ellian-eorwyn", "pi-forge");
	const npmBin = join(app, "node_modules", ".bin");
	const bin = join(piForgeHome, "bin");
	const agent = join(piForgeHome, "agent");
	mkdirSync(packageRoot, { recursive: true });
	mkdirSync(npmBin, { recursive: true });
	mkdirSync(bin);
	mkdirSync(agent);
	writeFileSync(join(packageRoot, "package.json"), "{}\n");
	writeFileSync(join(npmBin, "pi-forge"), "#!/usr/bin/env bash\n");
	writeFileSync(join(npmBin, "pi-forge-mcp"), "#!/usr/bin/env bash\n");
	writeFileSync(join(npmBin, "pi-forge-update"), "#!/usr/bin/env bash\n");
	writeFileSync(join(agent, ".pi-forge-profile-path"), `${packageRoot}\n`);
	writeFileSync(join(agent, "auth.json"), "{}\n");
	symlinkSync(join(npmBin, "pi-forge"), join(bin, "pi-forge"));
	symlinkSync(join(npmBin, "pi-forge-mcp"), join(bin, "pi-forge-mcp"));
	symlinkSync(join(npmBin, "pi-forge-update"), join(bin, "pi-forge-update"));

	const uninstall = spawnSync(
		"bash",
		[
			join(repositoryRoot, "scripts", "pi-forge-uninstall.sh"),
			"--yes",
		],
		{ encoding: "utf8", env: { ...cleanPiForgeEnvironment(), HOME: root, SHELL: "/bin/zsh" } },
	);
	assert.equal(uninstall.status, 0, uninstall.stderr);
	assert.equal(existsSync(app), false);
	assert.equal(existsSync(join(bin, "pi-forge")), false);
	assert.equal(existsSync(join(agent, "auth.json")), true);
});
