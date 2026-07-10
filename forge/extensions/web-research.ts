import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

interface WebSearchParams {
	query: string;
	output?: string;
	limit?: number;
	searxng?: string;
	categories?: string;
	engines?: string;
	language?: string;
	safesearch?: number;
	timeRange?: string;
	pageNo?: number;
}

interface WebReadParams {
	urls: string[];
	output?: string;
	render?: boolean;
	playwrightWsEndpoint?: string;
	delayMs?: number;
	timeoutMs?: number;
}

interface DeepWebResearchParams {
	question?: string;
	queries?: string[];
	output: string;
	maxIterations?: number;
	limit?: number;
	readCount?: number;
	maxQueries?: number;
	maxSources?: number;
	maxFollowupQueries?: number;
	maxModelCalls?: number;
	maxRuntimeMs?: number;
	maxEvidenceChars?: number;
	maxClaimEvidenceItems?: number;
	timeoutMs?: number;
	delayMs?: number;
	playwrightWsEndpoint?: string;
	searxng?: string;
	categories?: string;
	engines?: string;
	language?: string;
	safesearch?: number;
	timeRange?: string;
	render?: boolean;
}

interface AcademicWebResearchParams {
	query: string;
	output: string;
	limit?: number;
	providers?: string[];
	contactEmail?: string;
	timeoutMs?: number;
}

const extensionDirectory = dirname(fileURLToPath(import.meta.url));
const webResearchScript = join(extensionDirectory, "..", "skills", "web-research", "scripts", "web-research.mjs");

export default function webResearchExtension(pi: ExtensionAPI) {
	let deepResearchUsedThisTurn = false;

	pi.on("turn_start", async () => {
		deepResearchUsedThisTurn = false;
	});

	pi.on("tool_call", async (event) => {
		if (event.toolName !== "forge_deep_web_research") return undefined;
		if (deepResearchUsedThisTurn) {
			return {
				block: true,
				reason:
					"Only one deep web research run may execute per assistant turn. Combine related subtopics into a single forge_deep_web_research call with multiple seed queries.",
			};
		}
		deepResearchUsedThisTurn = true;
		return undefined;
	});

	pi.registerTool({
		name: "forge_web_search",
		label: "Forge web search",
		description: "Run a quick SearXNG web search and return ranked result metadata.",
		promptSnippet: "Use forge_web_search for quick current-information lookups through the configured SearXNG service.",
		promptGuidelines: [
			"Use forge_web_search for quick web lookups before loading web-research skills.",
			"For current events use news categories or a time range; for developer topics use it categories or code-focused engines.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "Search query." }),
			output: Type.Optional(Type.String({ description: "Optional new output directory. Defaults under forge-output/web-research." })),
			limit: Type.Optional(Type.Integer({ minimum: 1, description: "Maximum ranked results to return." })),
			searxng: Type.Optional(Type.String({ description: "One-run SearXNG base URL override." })),
			categories: Type.Optional(Type.String({ description: "Comma-separated SearXNG categories." })),
			engines: Type.Optional(Type.String({ description: "Comma-separated SearXNG engines." })),
			language: Type.Optional(Type.String({ description: "SearXNG language code." })),
			safesearch: Type.Optional(Type.Integer({ minimum: 0, maximum: 2, description: "SearXNG safesearch setting." })),
			timeRange: Type.Optional(Type.String({ description: "SearXNG time range: day, week, month, or year." })),
			pageNo: Type.Optional(Type.Integer({ minimum: 1, description: "SearXNG page number." })),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const input = params as WebSearchParams;
			const output = input.output ?? defaultOutputDirectory(ctx.cwd, "search", input.query);
			const result = await runNode(buildWebSearchArgs({ ...input, output }), signal);
			const data = readResearchReport(output);
			const details = {
				runDirectory: output,
				query: data.query,
				params: data.params,
				results: data.results,
				stderr: result.stderr,
			};
			return {
				content: [{ type: "text", text: JSON.stringify(details, null, 2) }],
				details,
			};
		},
	});

	pi.registerTool({
		name: "forge_web_read",
		label: "Forge web read",
		description: "Read known URLs with rendered Playwright extraction when enabled, falling back to HTTP extraction.",
		promptSnippet: "Use forge_web_read to extract readable text from specific URLs through the configured Playwright service.",
		promptGuidelines: [
			"Use forge_web_read when you already have URLs to inspect.",
			"Use the returned full text and warnings; load web-collection only when the user needs archived source files or manifests.",
		],
		parameters: Type.Object({
			urls: Type.Array(Type.String(), { minItems: 1, description: "URLs to read." }),
			output: Type.Optional(Type.String({ description: "Optional new output directory. Defaults under forge-output/web-research." })),
			render: Type.Optional(Type.Boolean({ description: "Use rendered Playwright extraction. Defaults to true." })),
			playwrightWsEndpoint: Type.Optional(Type.String({ description: "One-run Playwright WebSocket endpoint override." })),
			delayMs: Type.Optional(Type.Integer({ minimum: 0, description: "Delay between URL reads in milliseconds." })),
			timeoutMs: Type.Optional(Type.Integer({ minimum: 1, description: "Request/navigation timeout in milliseconds." })),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const input = params as WebReadParams;
			const output = input.output ?? defaultOutputDirectory(ctx.cwd, "read", input.urls.join(" "));
			const result = await runNode(buildWebReadArgs({ ...input, output }), signal);
			const data = readResearchReport(output);
			const details = {
				runDirectory: output,
				readings: data.readings,
				stderr: result.stderr,
			};
			return {
				content: [{ type: "text", text: JSON.stringify(details, null, 2) }],
				details,
			};
		},
	});

	pi.registerTool({
		name: "forge_deep_web_research",
		label: "Deep web research",
		description: "Run pi-forge deep web research with source provenance, evidence, claims, gaps, and validation artifacts.",
		promptSnippet:
			"Use forge_deep_web_research for multi-query web research that must produce source-backed claims and provenance artifacts.",
		promptGuidelines: [
			"Use this tool when the user asks for a full research pass, provenance-first research, or source-backed web synthesis.",
			"Do not present uncited claims from the generated report; cite source, evidence, and claim ids from the run artifacts.",
		],
		parameters: Type.Object({
			question: Type.Optional(Type.String({ description: "Research question or synthesis objective." })),
			queries: Type.Optional(Type.Array(Type.String(), { description: "Seed queries. If omitted, question is used as the seed query." })),
			output: Type.String({ description: "New output directory. The CLI refuses to overwrite existing directories." }),
			maxIterations: Type.Optional(Type.Integer({ minimum: 1, description: "Maximum search/read/refine iterations." })),
			limit: Type.Optional(Type.Integer({ minimum: 1, description: "Search results per query." })),
			readCount: Type.Optional(Type.Integer({ minimum: 1, description: "Results to read per query." })),
			maxQueries: Type.Optional(Type.Integer({ minimum: 1, description: "Whole-run cap on searched queries." })),
			maxSources: Type.Optional(Type.Integer({ minimum: 1, description: "Whole-run cap on unique sources read." })),
			maxFollowupQueries: Type.Optional(Type.Integer({ minimum: 0, description: "Maximum follow-up queries accepted per expansion step." })),
			maxModelCalls: Type.Optional(Type.Integer({ minimum: 1, description: "Whole-run cap on local model calls." })),
			maxRuntimeMs: Type.Optional(Type.Integer({ minimum: 1, description: "Approximate whole-run runtime budget in milliseconds." })),
			maxEvidenceChars: Type.Optional(Type.Integer({ minimum: 1, description: "Maximum source-text characters sent to evidence extraction." })),
			maxClaimEvidenceItems: Type.Optional(Type.Integer({ minimum: 1, description: "Maximum evidence items sent to claim registration." })),
			timeoutMs: Type.Optional(Type.Integer({ minimum: 1, description: "Request/navigation timeout in milliseconds." })),
			delayMs: Type.Optional(Type.Integer({ minimum: 0, description: "Delay between URL reads in milliseconds." })),
			playwrightWsEndpoint: Type.Optional(Type.String({ description: "One-run Playwright WebSocket endpoint override." })),
			searxng: Type.Optional(Type.String({ description: "Override SearXNG base URL." })),
			categories: Type.Optional(Type.String({ description: "Comma-separated SearXNG categories." })),
			engines: Type.Optional(Type.String({ description: "Comma-separated SearXNG engines." })),
			language: Type.Optional(Type.String({ description: "SearXNG language code." })),
			safesearch: Type.Optional(Type.Integer({ minimum: 0, maximum: 2, description: "SearXNG safesearch setting." })),
			timeRange: Type.Optional(Type.String({ description: "SearXNG time range: day, week, month, or year." })),
			render: Type.Optional(Type.Boolean({ description: "Use rendered extraction when Playwright is available." })),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal) {
			const input = params as DeepWebResearchParams;
			const args = buildDeepResearchArgs(input);
			const result = await runNode(args, signal);
			const summary = JSON.parse(result.stdout);
			return {
				content: [{ type: "text", text: JSON.stringify(summary, null, 2) }],
				details: { ...summary, stderr: result.stderr },
			};
		},
	});

	pi.registerTool({
		name: "forge_academic_web_research",
		label: "Academic web research",
		description: "Run pi-forge academic search with deduped canonical works, provider provenance, and RIS exports.",
		promptSnippet:
			"Use forge_academic_web_research for scholarly literature discovery that needs deduped works, metadata provenance, and RIS citation exports.",
		promptGuidelines: [
			"Use this tool when the user asks for academic articles, literature search, DOI/PubMed/arXiv discovery, or citation-manager-ready exports.",
			"Treat works.jsonl as the canonical deduped work list and works.ris plus ris/*.ris as the citation export artifacts.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "Academic search query." }),
			output: Type.String({ description: "New output directory. The CLI refuses to overwrite existing directories." }),
			limit: Type.Optional(Type.Integer({ minimum: 1, description: "Maximum results per provider." })),
			providers: Type.Optional(Type.Array(Type.String(), { description: "Optional provider list, e.g. crossref, semantic-scholar, pubmed, arxiv." })),
			contactEmail: Type.Optional(Type.String({ description: "Contact email for polite API use and Unpaywall when configured." })),
			timeoutMs: Type.Optional(Type.Integer({ minimum: 1, description: "Provider request timeout in milliseconds." })),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal) {
			const input = params as AcademicWebResearchParams;
			const args = buildAcademicResearchArgs(input);
			const result = await runNode(args, signal);
			const summary = JSON.parse(result.stdout);
			return {
				content: [{ type: "text", text: JSON.stringify(summary, null, 2) }],
				details: { ...summary, stderr: result.stderr },
			};
		},
	});
}

function buildWebSearchArgs(input: WebSearchParams & { output: string }): string[] {
	const args = [webResearchScript, "search", input.query, "--output", input.output];
	if (input.limit !== undefined) args.push("--limit", String(input.limit));
	if (input.searxng) args.push("--searxng", input.searxng);
	if (input.categories) args.push("--categories", input.categories);
	if (input.engines) args.push("--engines", input.engines);
	if (input.language) args.push("--language", input.language);
	if (input.safesearch !== undefined) args.push("--safesearch", String(input.safesearch));
	if (input.timeRange) args.push("--time-range", input.timeRange);
	if (input.pageNo !== undefined) args.push("--pageno", String(input.pageNo));
	return args;
}

function buildWebReadArgs(input: WebReadParams & { output: string }): string[] {
	const args = [webResearchScript, "read", ...input.urls, "--output", input.output];
	if (input.render === false) args.push("--no-render");
	else if (input.render === true) args.push("--render");
	if (input.playwrightWsEndpoint) args.push("--playwright-ws", input.playwrightWsEndpoint);
	if (input.delayMs !== undefined) args.push("--delay-ms", String(input.delayMs));
	if (input.timeoutMs !== undefined) args.push("--timeout-ms", String(input.timeoutMs));
	return args;
}

function buildDeepResearchArgs(input: DeepWebResearchParams): string[] {
	const args = [webResearchScript, "deep", "--output", input.output];
	const queries = input.queries ?? [];
	if (input.question && queries.length === 0) args.push(input.question);
	else if (input.question) args.push("--question", input.question);
	for (const query of queries) args.push("--query", query);
	if (input.maxIterations !== undefined) args.push("--max-iterations", String(input.maxIterations));
	if (input.limit !== undefined) args.push("--limit", String(input.limit));
	if (input.readCount !== undefined) args.push("--read-count", String(input.readCount));
	if (input.maxQueries !== undefined) args.push("--max-queries", String(input.maxQueries));
	if (input.maxSources !== undefined) args.push("--max-sources", String(input.maxSources));
	if (input.maxFollowupQueries !== undefined) args.push("--max-followup-queries", String(input.maxFollowupQueries));
	if (input.maxModelCalls !== undefined) args.push("--max-model-calls", String(input.maxModelCalls));
	if (input.maxRuntimeMs !== undefined) args.push("--max-runtime-ms", String(input.maxRuntimeMs));
	if (input.maxEvidenceChars !== undefined) args.push("--max-evidence-chars", String(input.maxEvidenceChars));
	if (input.maxClaimEvidenceItems !== undefined) args.push("--max-claim-evidence-items", String(input.maxClaimEvidenceItems));
	if (input.timeoutMs !== undefined) args.push("--timeout-ms", String(input.timeoutMs));
	if (input.delayMs !== undefined) args.push("--delay-ms", String(input.delayMs));
	if (input.playwrightWsEndpoint) args.push("--playwright-ws", input.playwrightWsEndpoint);
	if (input.searxng) args.push("--searxng", input.searxng);
	if (input.categories) args.push("--categories", input.categories);
	if (input.engines) args.push("--engines", input.engines);
	if (input.language) args.push("--language", input.language);
	if (input.safesearch !== undefined) args.push("--safesearch", String(input.safesearch));
	if (input.timeRange) args.push("--time-range", input.timeRange);
	if (input.render === false) args.push("--no-render");
	else if (input.render === true) args.push("--render");
	return args;
}

function buildAcademicResearchArgs(input: AcademicWebResearchParams): string[] {
	const args = [webResearchScript, "academic", input.query, "--output", input.output];
	if (input.limit !== undefined) args.push("--limit", String(input.limit));
	if (input.providers && input.providers.length > 0) args.push("--providers", input.providers.join(","));
	if (input.contactEmail) args.push("--contact-email", input.contactEmail);
	if (input.timeoutMs !== undefined) args.push("--timeout-ms", String(input.timeoutMs));
	return args;
}

function readResearchReport(output: string): { query: unknown; params: unknown; results: unknown[]; readings: unknown[] } {
	return JSON.parse(readFileSync(join(output, "research_report.json"), "utf8")) as {
		query: unknown;
		params: unknown;
		results: unknown[];
		readings: unknown[];
	};
}

function defaultOutputDirectory(cwd: string, command: string, seed: string): string {
	const root = join(cwd, "forge-output", "web-research");
	mkdirSync(root, { recursive: true });
	const hash = createHash("sha256").update(seed).digest("hex").slice(0, 8);
	const stem = safeStem(`${command}-${seed}`).slice(0, 48) || command;
	for (let index = 1; index <= 1000; index += 1) {
		const suffix = index === 1 ? "" : `-${index}`;
		const candidate = join(root, `${stem}-${hash}${suffix}`);
		if (!existsSync(candidate)) return candidate;
	}
	throw new Error(`Could not allocate output directory under ${root}`);
}

function safeStem(value: string): string {
	return value
		.normalize("NFKC")
		.trim()
		.replace(/[^a-zA-Z0-9._-]+/g, "-")
		.replace(/^-+|-+$/g, "");
}

function runNode(args: string[], signal: AbortSignal): Promise<{ stdout: string; stderr: string }> {
	return new Promise((resolveRun, rejectRun) => {
		const child = spawn(process.execPath, args, { stdio: ["ignore", "pipe", "pipe"] });
		let stdout = "";
		let stderr = "";
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => {
			stdout += chunk;
		});
		child.stderr.on("data", (chunk: string) => {
			stderr += chunk;
		});
		const abort = () => child.kill();
		signal.addEventListener("abort", abort, { once: true });
		child.once("error", (error) => {
			signal.removeEventListener("abort", abort);
			rejectRun(error);
		});
		child.once("exit", (code) => {
			signal.removeEventListener("abort", abort);
			if (code === 0) resolveRun({ stdout, stderr });
			else rejectRun(new Error(formatRunFailure(args, code, stdout, stderr)));
		});
	});
}

function tail(value: string, maxLength = 12_000): string {
	if (value.length <= maxLength) return value;
	return `[truncated ${value.length - maxLength} chars]\n${value.slice(-maxLength)}`;
}

function summarizeStdout(stdout: string): string {
	try {
		const summary = JSON.parse(stdout);
		if (Array.isArray(summary.validationErrors) && summary.validationErrors.length > 0) {
			return JSON.stringify({ ...summary, validationErrors: summary.validationErrors.slice(0, 25) }, null, 2);
		}
		return JSON.stringify(summary, null, 2);
	} catch {
		return tail(stdout);
	}
}

export function formatRunFailure(args: string[], code: number | null, stdout: string, stderr: string): string {
	const command = ["node", ...args.map((arg) => (/\s/.test(arg) ? JSON.stringify(arg) : arg))].join(" ");
	const parts = [`web-research exited ${code ?? "without status"}`, `command: ${command}`];
	if (stdout.trim()) parts.push(`stdout:\n${summarizeStdout(stdout)}`);
	if (stderr.trim()) parts.push(`stderr:\n${tail(stderr)}`);
	return parts.join("\n");
}
