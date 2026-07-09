import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

interface DeepWebResearchParams {
	question?: string;
	queries?: string[];
	output: string;
	maxIterations?: number;
	limit?: number;
	readCount?: number;
	searxng?: string;
	categories?: string;
	engines?: string;
	language?: string;
	safesearch?: number;
	timeRange?: string;
	render?: boolean;
}

const extensionDirectory = dirname(fileURLToPath(import.meta.url));
const webResearchScript = join(extensionDirectory, "..", "skills", "web-research", "scripts", "web-research.mjs");

export default function webResearchExtension(pi: ExtensionAPI) {
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
			else rejectRun(new Error(`web-research deep exited ${code ?? "without status"}\n${stderr || stdout}`));
		});
	});
}
