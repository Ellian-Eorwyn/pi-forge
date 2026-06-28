import type { AgentMessage } from "@earendil-works/pi-agent-core";
import {
	type Api,
	type AssistantMessage,
	completeSimple,
	type ImageContent,
	type Model,
	type TextContent,
} from "@earendil-works/pi-ai";
import { estimateTokens } from "./compaction/index.ts";
import type { CustomMessage } from "./messages.ts";
import type { ModelRegistry } from "./model-registry.ts";
import type { ContextBudgetSettings, TaskModelSettings } from "./settings-manager.ts";

const CONTEXT_BUDGET_SUMMARY_TYPE = "contextBudgetSummary";
const TASK_SUMMARY_SYSTEM_PROMPT = `You compress old coding-agent context for a stronger model.

Return ONLY JSON. Do not include markdown. Do not include hidden or visible reasoning.

Schema:
{
  "summary": "concise source-linked summary",
  "claims": [{"text": "claim", "sourceRefs": ["message:0"]}],
  "sourceRefs": ["message:0"],
  "omissions": ["important omitted detail"],
  "uncertainty": ["uncertain point"]
}

Rules:
- Every claim must cite sourceRefs from the provided source labels.
- Preserve exact file paths, commands, errors, and tool names.
- Do not decide edits or outcomes.`;

export interface ResolvedContextBudgetSettings {
	enabled: boolean;
	softRatio: number;
	useTaskModel: boolean;
	verbatimRecentTokens: number;
}

export interface ResolvedTaskModelSettings {
	enabled: boolean;
	provider: string;
	model: string;
	baseUrl: string;
	contextWindow: number;
	thinkingEnabled: boolean;
	maxConcurrency: number;
	timeoutMs: number;
	maxTokens: number;
}

export interface TaskContextSummary {
	summary: string;
	claims: Array<{ text: string; sourceRefs: string[] }>;
	sourceRefs: string[];
	omissions: string[];
	uncertainty: string[];
}

interface TaskContextSummaryJson {
	summary?: unknown;
	claims?: unknown;
	sourceRefs?: unknown;
	source_refs?: unknown;
	omissions?: unknown;
	uncertainty?: unknown;
}

interface ProjectContextOptions {
	messages: AgentMessage[];
	model: Model<Api>;
	systemPrompt?: string;
	contextBudget: ResolvedContextBudgetSettings;
	taskModel: ResolvedTaskModelSettings;
	summarizeWithTaskModel?: (request: TaskSummaryRequest) => Promise<TaskContextSummary | undefined>;
	signal?: AbortSignal;
}

export interface TaskSummaryRequest {
	messages: AgentMessage[];
	allowedSourceRefs: Set<string>;
	tokenBudget: number;
	signal?: AbortSignal;
}

export function resolveContextBudgetSettings(settings?: ContextBudgetSettings): ResolvedContextBudgetSettings {
	return {
		enabled: settings?.enabled ?? false,
		softRatio: clampRatio(settings?.softRatio, 0.65),
		useTaskModel: settings?.useTaskModel ?? true,
		verbatimRecentTokens: normalizePositiveInteger(settings?.verbatimRecentTokens, 20000),
	};
}

export function resolveTaskModelSettings(settings?: TaskModelSettings): ResolvedTaskModelSettings {
	return {
		enabled: settings?.enabled ?? false,
		provider: settings?.provider ?? "forge-task-local",
		model: settings?.model ?? "task",
		baseUrl: settings?.baseUrl ?? "http://llms:8007/v1",
		contextWindow: normalizePositiveInteger(settings?.contextWindow, 128000),
		thinkingEnabled: settings?.thinkingEnabled ?? true,
		maxConcurrency: normalizePositiveInteger(settings?.maxConcurrency, 1),
		timeoutMs: normalizePositiveInteger(settings?.timeoutMs, 30000),
		maxTokens: normalizePositiveInteger(settings?.maxTokens, 2048),
	};
}

function clampRatio(value: number | undefined, fallback: number): number {
	if (typeof value !== "number" || !Number.isFinite(value)) {
		return fallback;
	}
	return Math.min(0.95, Math.max(0.1, value));
}

function normalizePositiveInteger(value: number | undefined, fallback: number): number {
	if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
		return fallback;
	}
	return Math.floor(value);
}

function estimateTextTokens(text: string | undefined): number {
	return text ? Math.ceil(text.length / 4) : 0;
}

export function estimateMessageListTokens(messages: AgentMessage[], systemPrompt?: string): number {
	let tokens = estimateTextTokens(systemPrompt);
	for (const message of messages) {
		tokens += estimateTokens(message);
	}
	return tokens;
}

function findRecentStart(messages: AgentMessage[], verbatimRecentTokens: number): number {
	let tokens = 0;
	let start = messages.length;
	for (let i = messages.length - 1; i >= 0; i--) {
		tokens += estimateTokens(messages[i]);
		start = i;
		if (tokens >= verbatimRecentTokens) {
			break;
		}
	}

	for (let i = start; i < messages.length; i++) {
		const role = messages[i]?.role;
		if (
			role === "user" ||
			role === "custom" ||
			role === "bashExecution" ||
			role === "compactionSummary" ||
			role === "branchSummary"
		) {
			return i;
		}
	}

	for (let i = start; i >= 0; i--) {
		const role = messages[i]?.role;
		if (
			role === "user" ||
			role === "custom" ||
			role === "bashExecution" ||
			role === "compactionSummary" ||
			role === "branchSummary"
		) {
			return i;
		}
	}
	return start;
}

export async function projectContextForBudget(options: ProjectContextOptions): Promise<AgentMessage[]> {
	const { messages, model, contextBudget, signal } = options;
	if (!contextBudget.enabled || model.contextWindow <= 0) {
		return messages;
	}

	const softBudget = Math.floor(model.contextWindow * contextBudget.softRatio);
	return projectMessagesForBudget(options, softBudget, signal);
}

async function projectMessagesForBudget(
	options: ProjectContextOptions,
	softBudget: number,
	signal: AbortSignal | undefined,
): Promise<AgentMessage[]> {
	const { messages } = options;
	const currentTokens = estimateMessageListTokens(messages, options.systemPrompt);
	if (currentTokens <= softBudget) {
		return messages;
	}

	const recentStart = findRecentStart(messages, options.contextBudget.verbatimRecentTokens);
	const prefix = messages.slice(0, recentStart);
	const recent = messages.slice(recentStart);
	if (prefix.length === 0) {
		return messages;
	}

	const allowedSourceRefs = new Set(prefix.map((_, index) => `message:${index}`));
	let summary: TaskContextSummary | undefined;
	if (options.contextBudget.useTaskModel && options.taskModel.enabled && options.summarizeWithTaskModel) {
		try {
			summary = await options.summarizeWithTaskModel({
				messages: prefix,
				allowedSourceRefs,
				tokenBudget: Math.max(512, Math.min(options.taskModel.maxTokens, Math.floor(softBudget * 0.05))),
				signal,
			});
		} catch {
			summary = undefined;
		}
	}

	const summaryMessage = createProjectionMessage(summary ?? createDeterministicSummary(prefix, allowedSourceRefs));
	return [summaryMessage, ...recent];
}

function createProjectionMessage(summary: TaskContextSummary): CustomMessage {
	return {
		role: "custom",
		customType: CONTEXT_BUDGET_SUMMARY_TYPE,
		display: false,
		content: formatSummary(summary),
		timestamp: Date.now(),
	};
}

function formatSummary(summary: TaskContextSummary): string {
	const lines = ["Context budget projection of older conversation history.", "", "## Summary", summary.summary];
	if (summary.claims.length > 0) {
		lines.push("", "## Source-Linked Claims");
		for (const claim of summary.claims) {
			lines.push(`- ${claim.text} [${claim.sourceRefs.join(", ")}]`);
		}
	}
	if (summary.omissions.length > 0) {
		lines.push("", "## Omissions");
		for (const omission of summary.omissions) {
			lines.push(`- ${omission}`);
		}
	}
	if (summary.uncertainty.length > 0) {
		lines.push("", "## Uncertainty");
		for (const item of summary.uncertainty) {
			lines.push(`- ${item}`);
		}
	}
	lines.push("", `Source refs: ${summary.sourceRefs.join(", ")}`);
	return lines.join("\n");
}

function createDeterministicSummary(messages: AgentMessage[], allowedSourceRefs: Set<string>): TaskContextSummary {
	const claims: Array<{ text: string; sourceRefs: string[] }> = [];
	messages.forEach((message, index) => {
		const sourceRef = `message:${index}`;
		const text = describeMessage(message);
		if (text) {
			claims.push({ text, sourceRefs: [sourceRef] });
		}
	});
	return {
		summary: "Older context was compacted deterministically because the projected request exceeded the soft budget.",
		claims: claims.slice(0, 40),
		sourceRefs: [...allowedSourceRefs],
		omissions: messages.length > 40 ? [`${messages.length - 40} older message summaries omitted.`] : [],
		uncertainty: ["This deterministic projection preserves references and brief snippets, not full output text."],
	};
}

function describeMessage(message: AgentMessage): string | undefined {
	switch (message.role) {
		case "user":
			return `User: ${summarizeContent(message.content, 360)}`;
		case "assistant":
			return describeAssistant(message);
		case "toolResult":
			return `Tool result ${message.toolName}: ${summarizeContent(message.content, 480)}`;
		case "bashExecution":
			return `Bash command \`${message.command}\` exited ${message.exitCode ?? "unknown"}: ${summarizeText(message.output, 420)}`;
		case "custom":
			return `Custom context ${message.customType}: ${summarizeContent(message.content, 360)}`;
		case "branchSummary":
			return `Branch summary from ${message.fromId}: ${summarizeText(message.summary, 360)}`;
		case "compactionSummary":
			return `Compaction summary from ${message.tokensBefore} tokens: ${summarizeText(message.summary, 360)}`;
	}
}

function describeAssistant(message: AssistantMessage): string {
	const textParts: string[] = [];
	const toolCalls: string[] = [];
	for (const block of message.content) {
		if (block.type === "text") {
			textParts.push(block.text);
		} else if (block.type === "toolCall") {
			toolCalls.push(`${block.name}(${summarizeText(JSON.stringify(block.arguments), 160)})`);
		}
	}
	const text = textParts.length > 0 ? `Assistant: ${summarizeText(textParts.join("\n"), 300)}` : "Assistant response";
	return toolCalls.length > 0 ? `${text}; tool calls: ${toolCalls.join("; ")}` : text;
}

function summarizeContent(content: string | (TextContent | ImageContent)[], maxChars: number): string {
	if (typeof content === "string") {
		return summarizeText(content, maxChars);
	}
	return summarizeText(content.map((part) => (part.type === "text" ? part.text : "[image]")).join("\n"), maxChars);
}

function summarizeText(text: string, maxChars: number): string {
	const normalized = text.replace(/\s+/g, " ").trim();
	if (normalized.length <= maxChars) {
		return normalized;
	}
	return `${normalized.slice(0, maxChars)}...`;
}

export function stripTaskModelReasoning(text: string): string {
	return text
		.replace(/<think\b[^>]*>[\s\S]*?<\/think>/gi, "")
		.replace(/<thinking\b[^>]*>[\s\S]*?<\/thinking>/gi, "")
		.trim();
}

function parseStringArray(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function parseClaims(value: unknown): Array<{ text: string; sourceRefs: string[] }> {
	if (!Array.isArray(value)) {
		return [];
	}
	const claims: Array<{ text: string; sourceRefs: string[] }> = [];
	for (const item of value) {
		if (typeof item !== "object" || item === null) {
			continue;
		}
		const record = item as { text?: unknown; sourceRefs?: unknown; source_refs?: unknown };
		if (typeof record.text !== "string") {
			continue;
		}
		const sourceRefs = parseStringArray(record.sourceRefs ?? record.source_refs);
		claims.push({ text: record.text, sourceRefs });
	}
	return claims;
}

function extractJsonObject(text: string): string | undefined {
	const stripped = stripTaskModelReasoning(text).replace(/^```(?:json)?\s*|\s*```$/g, "");
	const start = stripped.indexOf("{");
	const end = stripped.lastIndexOf("}");
	if (start === -1 || end <= start) {
		return undefined;
	}
	return stripped.slice(start, end + 1);
}

export function parseTaskContextSummary(text: string, allowedSourceRefs: Set<string>): TaskContextSummary | undefined {
	const jsonText = extractJsonObject(text);
	if (!jsonText) {
		return undefined;
	}

	let parsed: TaskContextSummaryJson;
	try {
		parsed = JSON.parse(jsonText) as TaskContextSummaryJson;
	} catch {
		return undefined;
	}
	if (typeof parsed.summary !== "string" || parsed.summary.trim().length === 0) {
		return undefined;
	}

	const sourceRefs = parseStringArray(parsed.sourceRefs ?? parsed.source_refs);
	const claims = parseClaims(parsed.claims);
	const allRefs = new Set([...sourceRefs, ...claims.flatMap((claim) => claim.sourceRefs)]);
	if (allRefs.size === 0) {
		return undefined;
	}
	for (const ref of allRefs) {
		if (!allowedSourceRefs.has(ref)) {
			return undefined;
		}
	}
	for (const claim of claims) {
		if (claim.sourceRefs.length === 0) {
			return undefined;
		}
	}

	return {
		summary: stripTaskModelReasoning(parsed.summary),
		claims,
		sourceRefs: [...allRefs],
		omissions: parseStringArray(parsed.omissions),
		uncertainty: parseStringArray(parsed.uncertainty),
	};
}

class TaskModelQueue {
	private running = 0;
	private queue: Array<() => void> = [];

	enqueue<T>(maxConcurrency: number, task: () => Promise<T>): Promise<T> {
		return new Promise<T>((resolve, reject) => {
			const run = () => {
				this.running++;
				task()
					.then(resolve, reject)
					.finally(() => {
						this.running--;
						this.drain(maxConcurrency);
					});
			};
			this.queue.push(run);
			this.drain(maxConcurrency);
		});
	}

	private drain(maxConcurrency: number): void {
		while (this.running < maxConcurrency && this.queue.length > 0) {
			const run = this.queue.shift();
			run?.();
		}
	}
}

function serializeTaskMessages(messages: AgentMessage[]): string {
	return messages
		.map((message, index) => {
			const sourceRef = `message:${index}`;
			return `<source ref="${sourceRef}">\n${describeMessage(message) ?? "(no text)"}\n</source>`;
		})
		.join("\n\n");
}

export function createTaskContextSummarizer(options: {
	modelRegistry: ModelRegistry;
	getTaskModelSettings: () => ResolvedTaskModelSettings;
}): (request: TaskSummaryRequest) => Promise<TaskContextSummary | undefined> {
	const queue = new TaskModelQueue();
	return async (request) => {
		const settings = options.getTaskModelSettings();
		if (!settings.enabled) {
			return undefined;
		}
		const taskModel = options.modelRegistry.find(settings.provider, settings.model);
		if (!taskModel) {
			return undefined;
		}
		const auth = await options.modelRegistry.getApiKeyAndHeaders(taskModel);
		if (!auth.ok) {
			return undefined;
		}

		return queue.enqueue(settings.maxConcurrency, async () => {
			const prompt = [
				`Compress these sources for a trusted coding model. Allowed source refs are: ${[...request.allowedSourceRefs].join(", ")}`,
				"",
				serializeTaskMessages(request.messages),
			].join("\n");
			const response = await completeSimple(
				taskModel,
				{
					systemPrompt: TASK_SUMMARY_SYSTEM_PROMPT,
					messages: [{ role: "user", content: [{ type: "text", text: prompt }], timestamp: Date.now() }],
				},
				{
					apiKey: auth.apiKey,
					headers: auth.headers,
					env: auth.env,
					maxTokens: request.tokenBudget,
					timeoutMs: settings.timeoutMs,
					maxRetries: 0,
					signal: request.signal,
					reasoning: settings.thinkingEnabled ? "low" : undefined,
				},
			);
			if (response.stopReason === "error" || response.stopReason === "aborted") {
				return undefined;
			}
			const text = response.content
				.filter((part): part is TextContent => part.type === "text")
				.map((part) => part.text)
				.join("\n");
			return parseTaskContextSummary(text, request.allowedSourceRefs);
		});
	};
}
