#!/usr/bin/env node

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readdirSync, realpathSync, statSync } from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as z from "zod/v4";

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = resolve(SCRIPT_DIRECTORY, "..");
export const DEFAULT_SKILLS_ROOT = join(PACKAGE_ROOT, "skills");
const MAX_STDOUT_BYTES = 10 * 1024 * 1024;
const RECORDING_TYPES = ["lecture", "interview", "meeting", "call", "voice-note", "other"];
const CONVERSION_TARGETS = ["md", "docx", "html", "txt", "epub", "csv", "xlsx"];

const artifactSchema = z.object({ path: z.string(), role: z.string() });
const errorSchema = z.object({
	code: z.string(),
	message: z.string(),
	retryable: z.boolean(),
	remediation: z.array(z.string()),
});
const resultSchema = {
	schemaVersion: z.literal(1),
	taskId: z.string(),
	operation: z.enum(["transcribe", "convert_files"]),
	status: z.enum(["success", "needs_review", "failed", "canceled"]),
	startedAt: z.string(),
	completedAt: z.string(),
	runDirectory: z.string().nullable(),
	artifacts: z.array(artifactSchema),
	counts: z.record(z.string(), z.number()),
	warnings: z.array(z.string()),
	error: errorSchema.nullable(),
};

class BridgeError extends Error {
	constructor(code, message) {
		super(`${code}: ${message}`);
		this.name = "BridgeError";
		this.code = code;
	}
}

class SerialExecutor {
	#tail = Promise.resolve();

	run(signal, operation) {
		const current = this.#tail.then(async () => {
			if (signal.aborted) throw new BridgeError("request_canceled", "request was canceled before execution");
			return operation();
		});
		this.#tail = current.catch(() => undefined);
		return current;
	}
}

function isWithin(root, candidate) {
	const pathFromRoot = relative(root, candidate);
	return pathFromRoot === "" || (!pathFromRoot.startsWith("..") && !isAbsolute(pathFromRoot));
}

function resolveConfiguredRoot(path, option) {
	if (!isAbsolute(path)) throw new BridgeError("invalid_configuration", `${option} must be absolute: ${path}`);
	let resolvedPath;
	try {
		resolvedPath = realpathSync.native(path);
	} catch (error) {
		throw new BridgeError("invalid_configuration", `${option} does not exist: ${path} (${error.message})`);
	}
	if (!statSync(resolvedPath).isDirectory()) {
		throw new BridgeError("invalid_configuration", `${option} must be a directory: ${path}`);
	}
	return resolvedPath;
}

function resolveAllowedPath(path, roots, kind) {
	if (!isAbsolute(path)) throw new BridgeError("invalid_path", `${kind} must be absolute: ${path}`);
	let resolvedPath;
	try {
		resolvedPath = realpathSync.native(path);
	} catch (error) {
		throw new BridgeError("path_not_found", `${kind} does not exist: ${path} (${error.message})`);
	}
	if (!roots.some((root) => isWithin(root, resolvedPath))) {
		throw new BridgeError("path_not_allowed", `${kind} is outside the configured roots: ${path}`);
	}
	return resolvedPath;
}

function safeStem(path) {
	const name = basename(path).replace(/\.[^.]+$/, "");
	return name.normalize("NFKC").replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "") || "output";
}

function nextRunDirectory(outputRoot, category, stem) {
	const categoryDirectory = join(outputRoot, category);
	mkdirSync(categoryDirectory, { recursive: true });
	let candidate = join(categoryDirectory, stem);
	let suffix = 2;
	while (existsSync(candidate)) {
		candidate = join(categoryDirectory, `${stem}-${suffix}`);
		suffix += 1;
	}
	return candidate;
}

function parseJsonOutput(stdout, label) {
	try {
		return JSON.parse(stdout);
	} catch (error) {
		throw new BridgeError("invalid_worker_output", `${label} returned invalid JSON: ${error.message}`);
	}
}

function canceledResult(operation, taskId, startedAt, warnings = []) {
	return {
		schemaVersion: 1,
		taskId,
		operation,
		status: "canceled",
		startedAt,
		completedAt: new Date().toISOString(),
		runDirectory: null,
		artifacts: [],
		counts: {},
		warnings,
		error: {
			code: "request_canceled",
			message: "The caller canceled the operation.",
			retryable: true,
			remediation: ["Submit a new request if the operation is still needed."],
		},
	};
}

function failedResult(operation, taskId, startedAt, code, message, remediation, options = {}) {
	return {
		schemaVersion: 1,
		taskId,
		operation,
		status: "failed",
		startedAt,
		completedAt: new Date().toISOString(),
		runDirectory: options.runDirectory ?? null,
		artifacts: options.artifacts ?? [],
		counts: options.counts ?? {},
		warnings: options.warnings ?? [],
		error: { code, message, retryable: options.retryable ?? false, remediation },
	};
}

export function runChildProcess(command, args, { signal, cwd, label }) {
	return new Promise((resolveProcess, rejectProcess) => {
		const child = spawn(command, args, { cwd, env: process.env, stdio: ["ignore", "pipe", "pipe"] });
		let stdout = "";
		let settled = false;
		let killTimer;
		const finish = (callback) => {
			if (settled) return;
			settled = true;
			if (killTimer) clearTimeout(killTimer);
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = () => {
			if (child.exitCode !== null || child.signalCode !== null) return;
			child.kill("SIGTERM");
			killTimer = setTimeout(() => child.kill("SIGKILL"), 5_000);
			killTimer.unref();
		};

		if (signal.aborted) abort();
		else signal.addEventListener("abort", abort, { once: true });
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (chunk) => {
			stdout += chunk;
			if (Buffer.byteLength(stdout) > MAX_STDOUT_BYTES) {
				child.kill("SIGKILL");
				finish(() => rejectProcess(new BridgeError("worker_output_too_large", `${label} exceeded 10 MiB on stdout`)));
			}
		});
		child.stderr.on("data", (chunk) => process.stderr.write(chunk));
		child.on("error", (error) => finish(() => rejectProcess(new BridgeError("worker_start_failed", `${label}: ${error.message}`))));
		child.on("close", (code, processSignal) =>
			finish(() => resolveProcess({ code: code ?? 1, signal: processSignal, stdout: stdout.trim(), canceled: signal.aborted })),
		);
	});
}

function listArtifacts(runDirectory) {
	if (!runDirectory || !existsSync(runDirectory)) return [];
	const artifacts = [];
	const visit = (directory) => {
		for (const entry of readdirSync(directory, { withFileTypes: true })) {
			const path = join(directory, entry.name);
			if (entry.isSymbolicLink()) continue;
			if (entry.isDirectory()) visit(path);
			else if (entry.isFile()) artifacts.push({ path, role: relative(runDirectory, path) });
		}
	};
	visit(runDirectory);
	return artifacts;
}

function toolResponse(result) {
	return {
		content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
		structuredContent: result,
		isError: result.status === "failed" || result.status === "canceled",
	};
}

function requireFile(path, readRoots, label) {
	const resolvedPath = resolveAllowedPath(path, readRoots, label);
	if (!lstatSync(resolvedPath).isFile()) throw new BridgeError("invalid_path", `${label} must be a file: ${path}`);
	return resolvedPath;
}

function requireInput(path, readRoots) {
	const resolvedPath = resolveAllowedPath(path, readRoots, "input path");
	const stat = lstatSync(resolvedPath);
	if (!stat.isFile() && !stat.isDirectory()) throw new BridgeError("invalid_path", `invalid input path: ${path}`);
	return resolvedPath;
}

export function createBridgeServer(configuration) {
	const readRoots = configuration.readRoots.map((path) => resolveConfiguredRoot(path, "--read-root"));
	const writeRoots = configuration.writeRoots.map((path) => resolveConfiguredRoot(path, "--write-root"));
	if (readRoots.length === 0) throw new BridgeError("invalid_configuration", "at least one --read-root is required");
	if (writeRoots.length === 0) throw new BridgeError("invalid_configuration", "at least one --write-root is required");
	const python = configuration.python ?? process.env.PI_FORGE_MCP_PYTHON ?? "python3";
	const skillsRoot = configuration.skillsRoot ?? process.env.PI_FORGE_MCP_SKILLS_ROOT ?? DEFAULT_SKILLS_ROOT;
	const transcriptionScript = join(skillsRoot, "transcription", "scripts", "transcription.py");
	const conversionScript = join(skillsRoot, "file-conversion", "scripts", "file-conversion.py");
	const queue = new SerialExecutor();
	const server = new McpServer({ name: "pi-forge", version: "1.0.0" });

	server.registerTool(
		"forge_transcribe",
		{
			description: "Transcribe one local audio or video file and apply pi-forge dictionary corrections.",
			inputSchema: {
				inputPath: z.string().describe("Absolute path to one audio or video file."),
				outputRoot: z.string().describe("Existing absolute directory under an allowed write root."),
				recordingType: z.enum(RECORDING_TYPES),
				projectDictionaryPath: z.string().optional().describe("Optional absolute project dictionary JSON path."),
			},
			outputSchema: resultSchema,
			annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
		},
		async (args, extra) => {
			const result = await queue.run(extra.signal, async () => {
				const taskId = randomUUID();
				const startedAt = new Date().toISOString();
				const inputPath = requireFile(args.inputPath, readRoots, "input path");
				const outputRoot = resolveAllowedPath(args.outputRoot, writeRoots, "output root");
				if (!lstatSync(outputRoot).isDirectory()) throw new BridgeError("invalid_path", "output root must be a directory");
				const projectDictionary = args.projectDictionaryPath
					? requireFile(args.projectDictionaryPath, readRoots, "project dictionary")
					: undefined;
				const doctor = await runChildProcess(python, [transcriptionScript, "doctor"], {
					signal: extra.signal,
					cwd: process.cwd(),
					label: "transcription doctor",
				});
				if (doctor.canceled) return canceledResult("transcribe", taskId, startedAt);
				let doctorReport;
				try {
					doctorReport = parseJsonOutput(doctor.stdout, "transcription doctor");
				} catch (error) {
					return failedResult("transcribe", taskId, startedAt, error.code, error.message, []);
				}
				if (doctor.code !== 0 || doctorReport.ready !== true) {
					return failedResult(
						"transcribe",
						taskId,
						startedAt,
						"dependency_not_ready",
						"The local transcription runtime is not ready.",
						Array.isArray(doctorReport.remediation) ? doctorReport.remediation : [],
					);
				}
				const runDirectory = nextRunDirectory(outputRoot, "transcription", safeStem(inputPath));
				const commandArgs = [
					transcriptionScript,
					"transcribe",
					inputPath,
					"--output",
					runDirectory,
					"--type",
					args.recordingType,
				];
				if (projectDictionary) commandArgs.push("--project-dictionary", projectDictionary);
				const transcription = await runChildProcess(python, commandArgs, {
					signal: extra.signal,
					cwd: process.cwd(),
					label: "transcription",
				});
				if (transcription.canceled) return canceledResult("transcribe", taskId, startedAt);
				if (transcription.code !== 0) {
					return failedResult(
						"transcribe",
						taskId,
						startedAt,
						"transcription_failed",
						"The transcription worker exited unsuccessfully.",
						["Review MCP stderr and run the transcription skill doctor."],
						{ runDirectory: existsSync(runDirectory) ? runDirectory : null, artifacts: listArtifacts(runDirectory) },
					);
				}
				let workerResult;
				try {
					workerResult = parseJsonOutput(transcription.stdout, "transcription");
				} catch (error) {
					return failedResult("transcribe", taskId, startedAt, error.code, error.message, [], {
						runDirectory: existsSync(runDirectory) ? runDirectory : null,
						artifacts: listArtifacts(runDirectory),
					});
				}
				let actualRunDirectory;
				try {
					actualRunDirectory = resolveAllowedPath(workerResult.run_directory ?? runDirectory, writeRoots, "worker run directory");
				} catch (error) {
					return failedResult(
						"transcribe",
						taskId,
						startedAt,
						"invalid_worker_output",
						error.message,
						["Review the transcription worker and configured write roots."],
					);
				}
				const artifacts = listArtifacts(actualRunDirectory);
				const requiredRoles = ["raw_transcript.txt", "corrected_transcript.md", "transcription_manifest.csv"];
				const missing = requiredRoles.filter((role) => !artifacts.some((artifact) => artifact.role === role));
				if (missing.length > 0) {
					return failedResult(
						"transcribe",
						taskId,
						startedAt,
						"incomplete_output",
						`Missing required artifacts: ${missing.join(", ")}`,
						["Review the transcription worker log and output directory."],
						{ runDirectory: actualRunDirectory, artifacts },
					);
				}
				return {
					schemaVersion: 1,
					taskId,
					operation: "transcribe",
					status: Array.isArray(workerResult.warnings) && workerResult.warnings.length > 0 ? "needs_review" : "success",
					startedAt,
					completedAt: new Date().toISOString(),
					runDirectory: actualRunDirectory,
					artifacts,
					counts: {
						chunks: workerResult.chunk_count ?? 0,
						segments: workerResult.segment_count ?? 0,
						corrections: workerResult.correction_count ?? 0,
					},
					warnings: Array.isArray(workerResult.warnings) ? workerResult.warnings : [],
					error: null,
				};
			});
			return toolResponse(result);
		},
	);

	server.registerTool(
		"forge_convert_files",
		{
			description: "Convert local files with pi-forge and validate the generated run.",
			inputSchema: {
				inputPaths: z.array(z.string()).min(1).describe("Absolute input file or directory paths."),
				target: z.enum(CONVERSION_TARGETS),
				outputRoot: z.string().describe("Existing absolute directory under an allowed write root."),
				sourceFormat: z.string().optional().describe("Optional source extension filter."),
				coverPath: z.string().optional().describe("Optional absolute JPEG or PNG EPUB cover path."),
				title: z.string().optional(),
				author: z.string().optional(),
				language: z.string().optional(),
				date: z.string().optional(),
			},
			outputSchema: resultSchema,
			annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
		},
		async (args, extra) => {
			const result = await queue.run(extra.signal, async () => {
				const taskId = randomUUID();
				const startedAt = new Date().toISOString();
				const inputPaths = args.inputPaths.map((path) => requireInput(path, readRoots));
				const outputRoot = resolveAllowedPath(args.outputRoot, writeRoots, "output root");
				if (!lstatSync(outputRoot).isDirectory()) throw new BridgeError("invalid_path", "output root must be a directory");
				const coverPath = args.coverPath ? requireFile(args.coverPath, readRoots, "cover path") : undefined;
				const runStem = inputPaths.length === 1 ? safeStem(inputPaths[0]) : `batch-${taskId.slice(0, 8)}`;
				const runDirectory = nextRunDirectory(outputRoot, "file-conversion", runStem);
				const commandArgs = [conversionScript, "convert", ...inputPaths, "--to", args.target, "--output", runDirectory];
				for (const [flag, value] of [
					["--from", args.sourceFormat],
					["--cover", coverPath],
					["--title", args.title],
					["--author", args.author],
					["--language", args.language],
					["--date", args.date],
				]) {
					if (value !== undefined) commandArgs.push(flag, value);
				}
				const conversion = await runChildProcess(python, commandArgs, {
					signal: extra.signal,
					cwd: process.cwd(),
					label: "file conversion",
				});
				if (conversion.canceled) return canceledResult("convert_files", taskId, startedAt);
				if (conversion.code !== 0) {
					return failedResult(
						"convert_files",
						taskId,
						startedAt,
						"conversion_failed",
						"The conversion worker exited unsuccessfully.",
						["Review MCP stderr and run the file-conversion doctor."],
						{ runDirectory: existsSync(runDirectory) ? runDirectory : null, artifacts: listArtifacts(runDirectory) },
					);
				}
				let conversionResult;
				try {
					conversionResult = parseJsonOutput(conversion.stdout, "file conversion");
				} catch (error) {
					return failedResult("convert_files", taskId, startedAt, error.code, error.message, [], {
						runDirectory: existsSync(runDirectory) ? runDirectory : null,
						artifacts: listArtifacts(runDirectory),
					});
				}
				let actualRunDirectory;
				try {
					actualRunDirectory = resolveAllowedPath(
						conversionResult.runDirectory ?? runDirectory,
						writeRoots,
						"worker run directory",
					);
				} catch (error) {
					return failedResult(
						"convert_files",
						taskId,
						startedAt,
						"invalid_worker_output",
						error.message,
						["Review the conversion worker and configured write roots."],
					);
				}
				const validation = await runChildProcess(python, [conversionScript, "validate", actualRunDirectory], {
					signal: extra.signal,
					cwd: process.cwd(),
					label: "file conversion validation",
				});
				if (validation.canceled) return canceledResult("convert_files", taskId, startedAt);
				let validationResult;
				try {
					validationResult = parseJsonOutput(validation.stdout, "file conversion validation");
				} catch (error) {
					return failedResult("convert_files", taskId, startedAt, error.code, error.message, [], {
						runDirectory: actualRunDirectory,
						artifacts: listArtifacts(actualRunDirectory),
					});
				}
				const counts = {
					success: conversionResult.success ?? 0,
					needs_review: conversionResult.needsReview ?? 0,
					skipped: conversionResult.skipped ?? 0,
					failed: conversionResult.failed ?? 0,
				};
				const warnings = Array.isArray(validationResult.warnings) ? [...validationResult.warnings] : [];
				if (counts.needs_review > 0) warnings.push(`${counts.needs_review} converted files require review.`);
				if (validation.code !== 0 || validationResult.valid !== true) {
					const errors = Array.isArray(validationResult.errors) ? validationResult.errors : [];
					return failedResult(
						"convert_files",
						taskId,
						startedAt,
						"validation_failed",
						errors.join("; ") || "Conversion validation failed.",
						["Review conversion_manifest.csv, warnings.md, and MCP stderr."],
						{ runDirectory: actualRunDirectory, artifacts: listArtifacts(actualRunDirectory), counts, warnings },
					);
				}
				if (counts.failed > 0) warnings.push(`${counts.failed} input files failed; inspect conversion_manifest.csv.`);
				return {
					schemaVersion: 1,
					taskId,
					operation: "convert_files",
					status:
						counts.failed > 0 || counts.needs_review > 0 || counts.skipped > 0 || warnings.length > 0
							? "needs_review"
							: "success",
					startedAt,
					completedAt: new Date().toISOString(),
					runDirectory: actualRunDirectory,
					artifacts: listArtifacts(actualRunDirectory),
					counts,
					warnings,
					error: null,
				};
			});
			return toolResponse(result);
		},
	);
	return server;
}

export function parseArguments(argumentsList) {
	const readRoots = [];
	const writeRoots = [];
	for (let index = 0; index < argumentsList.length; index += 1) {
		const argument = argumentsList[index];
		if (argument === "--read-root" || argument === "--write-root") {
			const value = argumentsList[index + 1];
			if (!value) throw new BridgeError("invalid_configuration", `${argument} requires a path`);
			(argument === "--read-root" ? readRoots : writeRoots).push(value);
			index += 1;
			continue;
		}
		if (argument === "--help" || argument === "-h") return { help: true, readRoots, writeRoots };
		throw new BridgeError("invalid_configuration", `unknown argument: ${argument}`);
	}
	return { help: false, readRoots, writeRoots };
}

export async function runMcpServerMain(argumentsList = process.argv.slice(2)) {
	const configuration = parseArguments(argumentsList);
	if (configuration.help) {
		process.stdout.write(
			"Usage: pi-forge-mcp --read-root <absolute-path> [--read-root ...] --write-root <absolute-path> [--write-root ...]\n",
		);
		return;
	}
	const server = createBridgeServer(configuration);
	await server.connect(new StdioServerTransport());
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
	runMcpServerMain().catch((error) => {
		process.stderr.write(`pi-forge-mcp: ${error.message}\n`);
		process.exitCode = 1;
	});
}
