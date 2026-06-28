import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Model, Usage } from "@earendil-works/pi-ai";
import { describe, expect, it } from "vitest";
import {
	estimateMessageListTokens,
	parseTaskContextSummary,
	projectContextForBudget,
	resolveContextBudgetSettings,
	resolveTaskModelSettings,
	stripTaskModelReasoning,
	type TaskContextSummary,
} from "../src/core/context-budget.ts";

const usage: Usage = {
	input: 1,
	output: 1,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 2,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

const model: Model<"openai-completions"> = {
	id: "code",
	name: "Code",
	api: "openai-completions",
	provider: "forge-local",
	baseUrl: "http://llms:8008/v1",
	reasoning: false,
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	contextWindow: 128000,
	maxTokens: 32768,
};

function user(text: string): AgentMessage {
	return { role: "user", content: text, timestamp: 1 };
}

function assistant(text: string): AssistantMessage {
	return {
		role: "assistant",
		content: [{ type: "text", text }],
		api: "openai-completions",
		provider: "forge-local",
		model: "code",
		usage,
		stopReason: "stop",
		timestamp: 1,
	};
}

function toolResult(toolName: string, text: string): AgentMessage {
	return {
		role: "toolResult",
		toolCallId: `${toolName}-call`,
		toolName,
		content: [{ type: "text", text }],
		isError: false,
		timestamp: 1,
	};
}

function largeText(char: string, tokens: number): string {
	return char.repeat(tokens * 4);
}

describe("context budget projection", () => {
	it("leaves contexts below the soft budget unchanged", async () => {
		const messages = [user("hello"), assistant("ok")];
		const projected = await projectContextForBudget({
			messages,
			model,
			contextBudget: resolveContextBudgetSettings({ enabled: true, softRatio: 0.65 }),
			taskModel: resolveTaskModelSettings({ enabled: false }),
		});
		expect(projected).toBe(messages);
	});

	it("projects old bulky context below a 65 percent budget for a 128k model", async () => {
		const messages = [
			user("inspect the repository"),
			assistant("reading"),
			toolResult("read", largeText("a", 90000)),
			user("now continue with the latest task"),
			assistant("latest answer"),
		];

		const projected = await projectContextForBudget({
			messages,
			model,
			contextBudget: resolveContextBudgetSettings({
				enabled: true,
				softRatio: 0.65,
				useTaskModel: false,
				verbatimRecentTokens: 2000,
			}),
			taskModel: resolveTaskModelSettings({ enabled: false }),
		});

		expect(projected.length).toBeLessThan(messages.length);
		expect(projected[0].role).toBe("custom");
		expect(estimateMessageListTokens(projected)).toBeLessThan(128000 * 0.65);
		expect(projected.at(-2)).toEqual(messages.at(-2));
		expect(projected.at(-1)).toEqual(messages.at(-1));
	});

	it("uses a valid task-model summary and strips visible thinking", async () => {
		const messages = [user("inspect the repository"), toolResult("read", largeText("b", 90000)), user("latest task")];
		const taskSummary: TaskContextSummary = {
			summary: "Older read output found config in scripts/configure-pi-forge.mjs.",
			claims: [{ text: "Config file was read.", sourceRefs: ["message:1"] }],
			sourceRefs: ["message:1"],
			omissions: [],
			uncertainty: [],
		};

		const projected = await projectContextForBudget({
			messages,
			model,
			contextBudget: resolveContextBudgetSettings({
				enabled: true,
				softRatio: 0.65,
				useTaskModel: true,
				verbatimRecentTokens: 2000,
			}),
			taskModel: resolveTaskModelSettings({ enabled: true }),
			summarizeWithTaskModel: async () => taskSummary,
		});

		expect(projected[0].role).toBe("custom");
		if (projected[0].role !== "custom") return;
		expect(projected[0].content).toContain("Older read output found config");
		expect(projected[0].content).not.toContain("<think>");
	});

	it("falls back deterministically when task summary validation fails", async () => {
		const messages = [user("start"), toolResult("read", largeText("c", 90000)), user("latest task")];
		const projected = await projectContextForBudget({
			messages,
			model,
			contextBudget: resolveContextBudgetSettings({
				enabled: true,
				softRatio: 0.65,
				useTaskModel: true,
				verbatimRecentTokens: 2000,
			}),
			taskModel: resolveTaskModelSettings({ enabled: true }),
			summarizeWithTaskModel: async () => undefined,
		});

		expect(projected[0].role).toBe("custom");
		if (projected[0].role !== "custom") return;
		expect(projected[0].content).toContain("deterministically");
		expect(projected[0].content).toContain("message:1");
	});
});

describe("task model summary validation", () => {
	it("strips visible thinking blocks", () => {
		expect(stripTaskModelReasoning('<think>private chain</think>{"summary":"ok"}')).toBe('{"summary":"ok"}');
	});

	it("accepts valid source-linked JSON", () => {
		const parsed = parseTaskContextSummary(
			'<think>reasoning</think>{"summary":"ok","claims":[{"text":"claim","sourceRefs":["message:0"]}],"sourceRefs":["message:0"]}',
			new Set(["message:0"]),
		);
		expect(parsed?.summary).toBe("ok");
		expect(parsed?.claims).toEqual([{ text: "claim", sourceRefs: ["message:0"] }]);
	});

	it("rejects hallucinated source refs", () => {
		const parsed = parseTaskContextSummary(
			'{"summary":"ok","claims":[{"text":"claim","sourceRefs":["message:99"]}],"sourceRefs":["message:99"]}',
			new Set(["message:0"]),
		);
		expect(parsed).toBeUndefined();
	});
});
