import {
	type Api,
	type AssistantMessage,
	type Context,
	completeSimple,
	type Message,
	type Model,
	type TextContent,
	type ToolCall,
} from "@earendil-works/pi-ai/base";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createReadOnlyTools } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { LOCAL_MODEL_PROVIDERS } from "../lib/connected-services.mjs";

// The delegate is the non-thinking `chat` configuration of the same weights the
// interactive session runs on. It MUST be pinned to the background slot: the two
// slots share one llama-server, and letting non-interactive work land on slot 0
// evicts the interactive session's prefix cache (see connected-services.mjs). A
// delegate on slot 0 would trash the main model's cached context every call and
// cost far more than it saves. `cache_prompt` keeps the sub-agent's own multi-step
// prefix warm on slot 1 across its turns.
export const DELEGATE_SLOT = 1;

// Budgets. A read-only investigation that cannot answer within these returns its
// best partial answer with `hitBudget: true` rather than hanging or ballooning.
const MAX_TOOL_TURNS = 10;
const DEADLINE_MS = 120_000;
const PER_CALL_TIMEOUT_MS = 90_000;
const MAX_OUTPUT_TOKENS = 4096;

interface DelegateParams {
	task: string;
	context?: string;
	want_evidence?: boolean;
}

// Applied as the sub-agent's `onPayload`, which runs INSTEAD of the parent
// session's global before_provider_request hook (completeSimple calls the
// provider directly), so this is the only slot policy on these requests and the
// sub-agent stays fully isolated from the parent's slot-0 lease and cache.
export function pinBackgroundSlot(payload: unknown): unknown {
	if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
	return { ...(payload as Record<string, unknown>), id_slot: DELEGATE_SLOT, cache_prompt: true };
}

function assistantText(message: AssistantMessage): string {
	return message.content
		.filter((c): c is TextContent => c.type === "text")
		.map((c) => c.text)
		.join("")
		.trim();
}

const INVESTIGATE_SYSTEM_PROMPT = [
	"You are a fast, read-only investigation sub-agent. Another agent has handed you one",
	"well-scoped task and is blocked waiting for a concise answer. You cannot see its",
	"conversation — work only from the task, the context it gave you, and what your tools find.",
	"",
	"You have read-only tools over the current project: read, grep, find, ls. Use them to",
	"investigate, then reply with your FINAL answer and no further tool calls.",
	"",
	"Rules:",
	"- Ground every claim in what the tools actually returned. Never invent a path, name, value, or quote.",
	"- Be terse. Return only the answer and the minimal evidence for it. Do NOT narrate your search,",
	"  paste raw tool output, or explain your steps — the caller sees only your final message.",
	"- If you cannot determine the answer, say so plainly and state what you checked.",
].join("\n");

const FORCED_ANSWER_SYSTEM_PROMPT = [
	"You are a fast, read-only investigation sub-agent and you have used your entire investigation",
	"budget. Do not request any tools. Give your best concise answer now from what you have already",
	"found. If you are still uncertain, say so plainly and report what you did find. Ground every",
	"claim in what your tools returned; never invent a path, name, value, or quote.",
].join("\n");

function buildTaskPrompt(params: DelegateParams): string {
	const parts = [`Task:\n${params.task}`];
	if (params.context?.trim()) {
		parts.push(
			`\nContext you were given (you cannot see the main conversation, so rely on this):\n${params.context.trim()}`,
		);
	}
	parts.push(
		params.want_evidence
			? "\nIn your answer, cite the specific file:line locators and short (<15 word) quotes for each key fact."
			: "\nAnswer concisely.",
	);
	return parts.join("\n");
}

export interface DelegateResult {
	answer: string;
	toolTurns: number;
	hitBudget: boolean;
	error?: string;
}

/** Provider credentials, resolved the same way the interactive loop resolves them. */
export interface DelegateAuth {
	apiKey?: string;
	headers?: Record<string, string>;
	env?: Record<string, string>;
}

/**
 * The read-only investigation loop, pinned to the background slot. Exported so it
 * can be exercised against the live `chat` backend without the interactive host.
 * Every request carries `pinBackgroundSlot`, which runs instead of the parent
 * session's slot policy, so the sub-agent never touches the interactive slot-0 cache.
 * `auth` is the resolved provider credential — the local `chat` provider is
 * configured with an api key, and the OpenAI-compatible client refuses to send
 * without one.
 */
export async function runDelegate(
	model: Model<Api>,
	cwd: string,
	params: DelegateParams,
	auth: DelegateAuth,
	signal?: AbortSignal,
): Promise<DelegateResult> {
	const tools = createReadOnlyTools(cwd);
	const messages: Message[] = [{ role: "user", content: buildTaskPrompt(params), timestamp: Date.now() }];
	const deadline = Date.now() + DEADLINE_MS;

	let toolTurns = 0;

	// Up to MAX_TOOL_TURNS investigating turns, then one forced answer turn (no
	// tools) so an over-scoped task still returns a partial rather than hanging.
	for (let turn = 0; turn <= MAX_TOOL_TURNS; turn++) {
		const forcing = turn === MAX_TOOL_TURNS || Date.now() > deadline;
		const context: Context = {
			systemPrompt: forcing ? FORCED_ANSWER_SYSTEM_PROMPT : INVESTIGATE_SYSTEM_PROMPT,
			messages,
			tools: forcing ? [] : tools,
		};

		const final = await completeSimple(model, context, {
			onPayload: pinBackgroundSlot,
			apiKey: auth.apiKey,
			headers: auth.headers,
			env: auth.env,
			temperature: 0,
			maxTokens: MAX_OUTPUT_TOKENS,
			timeoutMs: PER_CALL_TIMEOUT_MS,
			signal,
		});
		messages.push(final);

		if (final.stopReason === "error") {
			return {
				answer: assistantText(final),
				toolTurns,
				hitBudget: false,
				error: final.errorMessage ?? "unknown error",
			};
		}
		if (final.stopReason === "aborted" || signal?.aborted) {
			return { answer: assistantText(final), toolTurns, hitBudget: true };
		}

		const calls = forcing ? [] : final.content.filter((c): c is ToolCall => c.type === "toolCall");
		if (calls.length === 0) {
			return { answer: assistantText(final), toolTurns, hitBudget: forcing };
		}

		for (const call of calls) {
			const result = await runReadOnlyTool(tools, call, signal);
			messages.push({
				role: "toolResult",
				toolCallId: call.id,
				toolName: call.name,
				content: result.content,
				details: result.details,
				isError: result.isError,
				timestamp: Date.now(),
			});
		}
		toolTurns = turn + 1;
	}

	// Unreachable: the forcing turn above always returns. Present for exhaustiveness.
	return { answer: "", toolTurns, hitBudget: true };
}

export default function delegationExtension(pi: ExtensionAPI) {
	pi.registerTool({
		name: "forge_delegate",
		label: "Delegate to non-thinking model",
		description:
			"Delegate a bounded, read-only investigation to the fast non-thinking model on the other GPU slot. " +
			"It has read/grep/find/ls over the current project, runs its own multi-step search, and returns ONLY a " +
			"concise answer — the searches, file dumps, and its reasoning never enter your context or spend your " +
			"thinking budget. Use it for a well-scoped lookup whose intermediate output would be large or that takes " +
			"several search/read steps (e.g. 'where is X defined and used', 'which of these files does Y', 'distill " +
			"this large output down to the facts I need'). Do NOT use it for a single trivial grep you can run " +
			"yourself, for open-ended reasoning, or for anything needing the conversation's context — pass everything " +
			"it needs in task/context. It is read-only: it cannot edit files or run mutating commands. Runs on the " +
			"same weights as you, so it does not run in parallel; it runs while you wait, keeping your context clean.",
		promptSnippet: "Offload a bounded read-only investigation to the fast non-thinking model",
		promptGuidelines: [
			"Delegate a bounded, read-only investigation to forge_delegate when its intermediate output would be large or multi-step, to keep your own context clean; run a single trivial lookup yourself.",
		],
		parameters: Type.Object({
			task: Type.String({
				description:
					"The self-contained instruction for the sub-agent, e.g. 'Find where the chat client's retry/backoff is implemented and report the file:line and the max-attempts value.' It cannot see this conversation.",
			}),
			context: Type.Optional(
				Type.String({
					description:
						"Facts the sub-agent needs but cannot discover on its own: relevant paths, constraints, definitions. Do not paste large dumps — point it at files instead.",
				}),
			),
			want_evidence: Type.Optional(
				Type.Boolean({
					description:
						"When true, the answer includes file:line locators and short quotes for each key fact. Set it when the answer feeds a decision you cannot otherwise verify.",
				}),
			),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const input = params as DelegateParams;
			const model = ctx.modelRegistry.find(LOCAL_MODEL_PROVIDERS.chat, "chat");
			if (!model) {
				return {
					content: [
						{
							type: "text",
							text: `forge_delegate is unavailable: the "${LOCAL_MODEL_PROVIDERS.chat}" provider (model "chat") is not configured in models.json. It ships with the pi-forge profile.`,
						},
					],
					details: { error: "chat-provider-missing" },
				};
			}

			const auth = await ctx.modelRegistry.getApiKeyAndHeaders(model);
			if (!auth.ok) {
				return {
					content: [
						{
							type: "text",
							text: `forge_delegate could not resolve credentials for "${LOCAL_MODEL_PROVIDERS.chat}": ${auth.error}`,
						},
					],
					details: { error: "auth-unresolved" },
				};
			}

			const result = await runDelegate(
				model,
				ctx.cwd,
				input,
				{ apiKey: auth.apiKey, headers: auth.headers, env: auth.env },
				signal,
			);
			const modelLabel = `${LOCAL_MODEL_PROVIDERS.chat}/chat`;
			if (result.error) {
				return {
					content: [
						{
							type: "text",
							text: `Delegate model error: ${result.error}${result.answer ? `\n\nPartial answer:\n${result.answer}` : ""}`,
						},
					],
					details: { error: result.error, toolTurns: result.toolTurns, model: modelLabel },
				};
			}

			return {
				content: [{ type: "text", text: result.answer || "(the delegate produced no answer)" }],
				details: { toolTurns: result.toolTurns, hitBudget: result.hitBudget, model: modelLabel },
			};
		},
	});
}

async function runReadOnlyTool(
	tools: ReturnType<typeof createReadOnlyTools>,
	call: ToolCall,
	signal: AbortSignal | undefined,
): Promise<{ content: TextContent[]; details: unknown; isError: boolean }> {
	const tool = tools.find((t) => t.name === call.name);
	if (!tool) {
		return {
			content: [{ type: "text", text: `Tool "${call.name}" is not available to this read-only sub-agent.` }],
			details: {},
			isError: true,
		};
	}
	try {
		const args = tool.prepareArguments ? tool.prepareArguments(call.arguments) : call.arguments;
		const result = await tool.execute(call.id, args as never, signal, () => {});
		// Tool content is (TextContent | ImageContent)[]; keep the text the model can read.
		const content = result.content.filter((c): c is TextContent => c.type === "text");
		return { content, details: result.details, isError: false };
	} catch (error) {
		return {
			content: [{ type: "text", text: error instanceof Error ? error.message : String(error) }],
			details: {},
			isError: true,
		};
	}
}
