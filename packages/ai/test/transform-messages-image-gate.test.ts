import { describe, expect, it } from "vitest";
import { transformMessages } from "../src/providers/transform-messages.ts";
import type { ImageContent, Message, Model, TextContent } from "../src/types.ts";

// The interactive-agent fix is a config flip: marking a local model image-capable
// (`input: ["text", "image"]`) is what stops transformMessages from replacing an
// attached image with a text placeholder before the request leaves the client.
// These lock that gate in so a future edit to models.json / configure-pi-forge.mjs
// that drops "image" fails a test rather than silently blinding the model again.

function modelWithInput(input: ("text" | "image")[]): Model<"openai-completions"> {
	return {
		id: "vision",
		name: "Vision (test)",
		api: "openai-completions",
		provider: "faux" as Model<"openai-completions">["provider"],
		baseUrl: "http://localhost:0",
		reasoning: false,
		input,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 4096,
		maxTokens: 1024,
	};
}

function imageMessage(): Message {
	return {
		role: "user",
		content: [
			{ type: "text", text: "Describe this." },
			{ type: "image", data: "aGVsbG8=", mimeType: "image/png" },
		],
		timestamp: 0,
	};
}

function userContent(message: Message): (TextContent | ImageContent)[] {
	if (message.role !== "user" || !Array.isArray(message.content)) {
		throw new Error("expected a user message with array content");
	}
	return message.content;
}

describe("transformMessages image gate", () => {
	it("passes image blocks through when the model accepts images", () => {
		const [message] = transformMessages([imageMessage()], modelWithInput(["text", "image"]));
		const content = userContent(message);
		expect(content.map((block) => block.type)).toEqual(["text", "image"]);
		const image = content.find((block) => block.type === "image") as ImageContent;
		expect(image.data).toBe("aGVsbG8=");
		expect(image.mimeType).toBe("image/png");
	});

	it("downgrades image blocks to a text placeholder for a text-only model", () => {
		const [message] = transformMessages([imageMessage()], modelWithInput(["text"]));
		const content = userContent(message);
		expect(content.every((block) => block.type === "text")).toBe(true);
		const text = content.map((block) => (block.type === "text" ? block.text : "")).join(" ");
		expect(text).toContain("image omitted");
	});
});
