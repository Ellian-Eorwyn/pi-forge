import { beforeEach, describe, expect, it, vi } from "vitest";
import { streamOpenAICompletions } from "../src/providers/openai-completions.ts";
import type { AssistantMessageEvent, Model } from "../src/types.ts";

const mockState = vi.hoisted(() => ({
	chunks: [] as unknown[],
}));

vi.mock("openai", () => {
	class FakeOpenAI {
		chat = {
			completions: {
				create: () => {
					const chunks = mockState.chunks;
					const stream = {
						async *[Symbol.asyncIterator]() {
							for (const chunk of chunks) yield chunk;
						},
					};
					const promise = Promise.resolve(stream) as Promise<typeof stream> & {
						withResponse: () => Promise<{ data: typeof stream; response: { status: number; headers: Headers } }>;
					};
					promise.withResponse = async () => ({ data: stream, response: { status: 200, headers: new Headers() } });
					return promise;
				},
			},
		};
	}
	return { default: FakeOpenAI };
});

function model(): Model<"openai-completions"> {
	return {
		id: "code",
		name: "Code",
		api: "openai-completions",
		provider: "forge-local",
		baseUrl: "http://llms:8008/v1",
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 262144,
		maxTokens: 32768,
	};
}

describe("openai-completions telemetry", () => {
	beforeEach(() => {
		mockState.chunks = [];
	});

	it("emits normalized live and final telemetry without raw provider payloads", async () => {
		mockState.chunks = [
			{ id: "chatcmpl-telemetry", model: "code", choices: [{ index: 0, delta: { content: "hi" } }] },
			{
				id: "chatcmpl-telemetry",
				model: "code",
				choices: [],
				usage: {
					prompt_tokens: 20,
					completion_tokens: 4,
					total_tokens: 24,
					prompt_tokens_details: { cached_tokens: 5, cache_write_tokens: 1 },
					completion_tokens_details: {
						reasoning_tokens: 2,
						accepted_prediction_tokens: 3,
						rejected_prediction_tokens: 1,
					},
					speculative_decoding: {
						method: "mtp",
						draft_tokens: 6,
						accepted_tokens: 4,
						rejected_tokens: 2,
					},
					dflash_tokens_per_second: 41,
				},
			},
			{ id: "chatcmpl-telemetry", model: "code", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
		];

		const events: AssistantMessageEvent[] = [];
		const stream = streamOpenAICompletions(
			model(),
			{ messages: [{ role: "user", content: "hello", timestamp: Date.now() }] },
			{ apiKey: "secret-api-key", headers: { Authorization: "Bearer secret-header" } },
		);
		for await (const event of stream) events.push(event);

		const telemetryEvents = events.filter((event) => event.type === "telemetry");
		expect(telemetryEvents).toHaveLength(1);
		const telemetry = telemetryEvents[0].telemetry;
		expect(telemetry.schema).toBe("pi.telemetry.v1");
		expect(telemetry.final).toBe(true);
		expect(telemetry.responseId).toBe("chatcmpl-telemetry");
		expect(telemetry.usage).toMatchObject({ input: 14, output: 4, cacheRead: 5, cacheWrite: 1, totalTokens: 24 });
		expect(telemetry.details).toMatchObject({
			reasoningTokens: 2,
			acceptedPredictionTokens: 3,
			rejectedPredictionTokens: 1,
		});
		expect(telemetry.speculative).toMatchObject({
			method: "mtp",
			draftTokens: 6,
			acceptedTokens: 4,
			rejectedTokens: 2,
			acceptanceRate: 4 / 6,
		});
		expect(telemetry.provider?.numericExtras?.["dflash_tokens_per_second"]).toBe(41);
		expect(JSON.stringify(telemetry)).not.toContain("hello");
		expect(JSON.stringify(telemetry)).not.toContain("hi");
		expect(JSON.stringify(telemetry)).not.toContain("secret-api-key");
		expect(JSON.stringify(telemetry)).not.toContain("secret-header");

		const message = await stream.result();
		expect(message.usage.details?.acceptedPredictionTokens).toBe(3);
		expect(message.usage.speculative?.acceptedTokens).toBe(4);
	});

	it("emits telemetry from choice.usage fallbacks", async () => {
		mockState.chunks = [
			{
				id: "chatcmpl-choice-usage",
				choices: [
					{
						index: 0,
						delta: { content: "ok" },
						finish_reason: "stop",
						usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
					},
				],
			},
		];

		const events: AssistantMessageEvent[] = [];
		const stream = streamOpenAICompletions(
			model(),
			{ messages: [{ role: "user", content: "hello", timestamp: Date.now() }] },
			{ apiKey: "local" },
		);
		for await (const event of stream) events.push(event);

		const telemetry = events.find((event) => event.type === "telemetry")?.telemetry;
		expect(telemetry?.usage).toMatchObject({ input: 3, output: 2, totalTokens: 5 });
		expect(telemetry?.final).toBe(true);
	});
});
