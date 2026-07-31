import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { resolveWorkflowRoot } from "./vault-context.ts";

interface WebSearchParams {
	query: string;
	output?: string;
	limit?: number;
	providers?: string[];
	searxng?: string;
	categories?: string;
	engines?: string;
	language?: string;
	safesearch?: number;
	timeRange?: string;
	pageNo?: number;
}

/** Tuning options passed through `advanced`; see ADVANCED_FLAGS. */
type AdvancedParams = Record<string, unknown>;

interface WebReadParams {
	urls: string[];
	output?: string;
	mode?: string;
	render?: boolean;
	noBrowser?: boolean;
	advanced?: AdvancedParams;
}

interface DeepWebResearchParams {
	question?: string;
	queries?: string[];
	output: string;
	mode?: string;
	maxIterations?: number;
	limit?: number;
	readCount?: number;
	maxSources?: number;
	render?: boolean;
	advanced?: AdvancedParams;
}

interface WebDiscoverParams {
	url: string;
	output?: string;
	mode?: string;
	render?: boolean;
	advanced?: AdvancedParams;
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

/**
 * Acquisition and model-budget knobs the CLI accepts. They are reachable through
 * the `advanced` object rather than the tool schema: a schema slot costs tokens on
 * every turn of every session, while these matter only inside a research run. The
 * full list with defaults lives in skills/web-research/SKILL.md, which is loaded
 * for exactly those runs. Flag names mirror the CLI's own option table, so the two
 * must be edited together.
 */
export const ADVANCED_FLAGS: Record<string, { flag: string; boolean?: true }> = {
	cacheDir: { flag: "--cache-dir" },
	categories: { flag: "--categories" },
	delayMs: { flag: "--delay-ms" },
	embeddingBatchSize: { flag: "--embedding-batch-size" },
	embeddingModel: { flag: "--embedding-model" },
	embeddingUrl: { flag: "--embedding-url" },
	engines: { flag: "--engines" },
	evidenceBatchChars: { flag: "--evidence-batch-chars" },
	evidenceBatchSources: { flag: "--evidence-batch-sources" },
	forceRefresh: { flag: "--force-refresh", boolean: true },
	forceStrategy: { flag: "--force-strategy" },
	language: { flag: "--language" },
	maxClaimEvidenceItems: { flag: "--max-claim-evidence-items" },
	maxConcurrency: { flag: "--max-concurrency" },
	maxEvidenceChars: { flag: "--max-evidence-chars" },
	maxFollowupQueries: { flag: "--max-followup-queries" },
	maxModelCalls: { flag: "--max-model-calls" },
	maxQueries: { flag: "--max-queries" },
	maxRuntimeMs: { flag: "--max-runtime-ms" },
	noBrowser: { flag: "--no-browser", boolean: true },
	noEmbeddings: { flag: "--no-embeddings", boolean: true },
	perDomainConcurrency: { flag: "--per-domain-concurrency" },
	playwrightConcurrency: { flag: "--playwright-concurrency" },
	playwrightWsEndpoint: { flag: "--playwright-ws" },
	safesearch: { flag: "--safesearch" },
	searxng: { flag: "--searxng" },
	timeRange: { flag: "--time-range" },
	timeoutMs: { flag: "--timeout-ms" },
};

const ADVANCED_DESCRIPTION =
	"Advanced acquisition, embedding, and budget options (for example evidenceBatchChars, playwrightConcurrency, cacheDir, timeoutMs). See the web-research skill for the full list; unknown keys are rejected.";

/**
 * Unknown or wrongly typed keys fail loudly. Silently dropping them would let a
 * run report success while ignoring the budget or endpoint it was given.
 */
export function buildAdvancedArgs(advanced: AdvancedParams | undefined): string[] {
	if (!advanced) return [];
	const args: string[] = [];
	for (const [key, value] of Object.entries(advanced)) {
		if (value === undefined || value === null) continue;
		const option = ADVANCED_FLAGS[key];
		if (!option) {
			throw new Error(`Unknown advanced option "${key}". Known options: ${Object.keys(ADVANCED_FLAGS).sort().join(", ")}`);
		}
		if (option.boolean) {
			if (typeof value !== "boolean") throw new Error(`Advanced option "${key}" takes a boolean, received ${typeof value}.`);
			if (value) args.push(option.flag);
			continue;
		}
		if (typeof value !== "string" && typeof value !== "number") {
			throw new Error(`Advanced option "${key}" takes a string or number, received ${typeof value}.`);
		}
		args.push(option.flag, String(value));
	}
	return args;
}

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
		description:
			"Run a quick web search and return ranked result metadata. The query is routed to the sources that can answer it -- encyclopedias and philosophy references, the Buddhist canon, book catalogues, news, technical Q&A -- falling back to SearXNG for open-ended questions. Categories, engines, and time range are auto-selected when omitted.",
		promptSnippet: "Quick routed web search",
		promptGuidelines: ["Reach for the forge web tools directly for quick lookups; load the web-research skill only for full research runs."],
		parameters: Type.Object({
			query: Type.String({ description: "Search query." }),
			output: Type.Optional(Type.String({ description: "Optional new output directory. Defaults under forge-output/web-research." })),
			limit: Type.Optional(Type.Integer({ minimum: 1, description: "Maximum ranked results to return." })),
			providers: Type.Optional(
				Type.Array(Type.String(), {
					description:
						"Pin specific providers instead of letting the query be routed, e.g. suttacentral, cbeta, sep, wikipedia, gdelt, openlibrary, searxng.",
				}),
			),
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
		description:
			"Extract readable text from URLs you already have, with rendered Playwright extraction when enabled and HTTP extraction as fallback. For archived source files or provenance manifests, use the web-collection skill instead.",
		promptSnippet: "Read specific URLs as text",
		parameters: Type.Object({
			urls: Type.Array(Type.String(), { minItems: 1, description: "URLs to read." }),
			output: Type.Optional(Type.String({ description: "Optional new output directory. Defaults under forge-output/web-research." })),
			mode: Type.Optional(Type.String({ description: "Acquisition preset: fast, standard, or deep. Defaults to standard." })),
			render: Type.Optional(Type.Boolean({ description: "Use rendered Playwright extraction. Defaults to true." })),
			noBrowser: Type.Optional(Type.Boolean({ description: "Disable browser fallback for this run." })),
			advanced: Type.Optional(Type.Object({}, { additionalProperties: true, description: ADVANCED_DESCRIPTION })),
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
		description:
			"Multi-query research pass producing source provenance, evidence, claims, gaps, and validation artifacts. Use for a full research pass or source-backed synthesis. Never present uncited claims from the report: cite the source, evidence, and claim ids from the run artifacts.",
		promptSnippet: "Multi-query research with provenance artifacts",
		parameters: Type.Object({
			question: Type.Optional(Type.String({ description: "Research question or synthesis objective." })),
			queries: Type.Optional(Type.Array(Type.String(), { description: "Seed queries. If omitted, question is used as the seed query." })),
			output: Type.String({ description: "New output directory. The CLI refuses to overwrite existing directories." }),
			mode: Type.Optional(Type.String({ description: "Acquisition preset: fast, standard, or deep. Defaults to deep." })),
			maxIterations: Type.Optional(Type.Integer({ minimum: 1, description: "Maximum search/read/refine iterations." })),
			limit: Type.Optional(Type.Integer({ minimum: 1, description: "Search results per query." })),
			readCount: Type.Optional(Type.Integer({ minimum: 1, description: "Results to read per query." })),
			maxSources: Type.Optional(Type.Integer({ minimum: 1, description: "Whole-run cap on unique sources read." })),
			render: Type.Optional(Type.Boolean({ description: "Use rendered extraction when Playwright is available." })),
			advanced: Type.Optional(Type.Object({}, { additionalProperties: true, description: ADVANCED_DESCRIPTION })),
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
		name: "forge_web_discover",
		label: "Forge web discover",
		description:
			"Inspect a JavaScript-heavy or unknown URL for embedded structured data and reusable JSON/API endpoints, before scraping the DOM. Discovery reports are evidence to inspect, not endpoints to adopt permanently without verifying stability.",
		promptSnippet: "Find structured data and API endpoints on a page",
		parameters: Type.Object({
			url: Type.String({ description: "URL to inspect." }),
			output: Type.Optional(Type.String({ description: "Optional new output directory. Defaults under forge-output/web-research." })),
			mode: Type.Optional(Type.String({ description: "Acquisition preset: fast, standard, or deep. Defaults to standard." })),
			render: Type.Optional(Type.Boolean({ description: "Use Playwright network observation when available." })),
			advanced: Type.Optional(Type.Object({}, { additionalProperties: true, description: ADVANCED_DESCRIPTION })),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const input = params as WebDiscoverParams;
			const output = input.output ?? defaultOutputDirectory(ctx.cwd, "discover", input.url);
			const result = await runNode(buildWebDiscoverArgs({ ...input, output }), signal);
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
		description:
			"Scholarly literature search across Crossref, Semantic Scholar, PubMed, and arXiv. Use for academic articles, DOI discovery, or citation-manager-ready exports. Produces works.jsonl as the canonical deduped work list, plus works.ris and ris/*.ris exports.",
		promptSnippet: "Academic literature search with RIS export",
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
	if (input.providers && input.providers.length > 0) args.push("--providers", input.providers.join(","));
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
	if (input.mode) args.push("--mode", input.mode);
	if (input.noBrowser) args.push("--no-browser");
	args.push(...buildAdvancedArgs(input.advanced));
	return args;
}

function buildDeepResearchArgs(input: DeepWebResearchParams): string[] {
	const args = [webResearchScript, "deep", "--output", input.output];
	const queries = input.queries ?? [];
	if (input.question && queries.length === 0) args.push(input.question);
	else if (input.question) args.push("--question", input.question);
	for (const query of queries) args.push("--query", query);
	if (input.mode) args.push("--mode", input.mode);
	if (input.maxIterations !== undefined) args.push("--max-iterations", String(input.maxIterations));
	if (input.limit !== undefined) args.push("--limit", String(input.limit));
	if (input.readCount !== undefined) args.push("--read-count", String(input.readCount));
	if (input.maxSources !== undefined) args.push("--max-sources", String(input.maxSources));
	if (input.render === false) args.push("--no-render");
	else if (input.render === true) args.push("--render");
	args.push(...buildAdvancedArgs(input.advanced));
	return args;
}

function buildWebDiscoverArgs(input: WebDiscoverParams & { output: string }): string[] {
	const args = [webResearchScript, "discover", input.url, "--output", input.output];
	if (input.mode) args.push("--mode", input.mode);
	if (input.render === false) args.push("--no-render");
	else if (input.render === true) args.push("--render");
	args.push(...buildAdvancedArgs(input.advanced));
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

export function defaultOutputDirectory(cwd: string, command: string, seed: string): string {
	// Inside an Obsidian vault this is the schema-derived workflows folder; the
	// resolver creates it and falls back to forge-output/web-research elsewhere.
	const root = resolveWorkflowRoot(cwd, "web-research");
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
