import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createReadOnlyTools } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
// forge-llm.mjs is pure Node (no @earendil-works imports), so it loads under the
// extension sandbox where the pi-ai provider layer does not. Its `callWithTools`
// pins the background slot (slot 1) via a cooperative lease when `background:true`,
// which is the whole point: the delegate must never touch the interactive slot-0
// prefix cache. See connected-services.mjs.
import { callWithTools, resolveService } from "../lib/forge-llm.mjs";

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

interface DelegateResult {
	answer: string;
	toolTurns: number;
	hitBudget: boolean;
	error?: string;
}

// Minimal OpenAI chat shapes. The chat endpoint speaks OpenAI, and forge-llm's
// `callWithTools` returns the raw assistant message, so the delegate works in
// OpenAI message space end to end.
interface OaToolCall {
	id: string;
	type?: string;
	function: { name: string; arguments: string };
}
interface OaMessage {
	role: "system" | "user" | "assistant" | "tool";
	content?: string | null;
	tool_call_id?: string;
	tool_calls?: OaToolCall[];
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

const FORCED_ANSWER_NUDGE = [
	"You have used your entire investigation budget. Do not request any more tools.",
	"Give your best concise answer now from what you have already found. If you are still",
	"uncertain, say so plainly and report what you did find. Never invent a path, name, value, or quote.",
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

/** Read-only pi tools → OpenAI function-tool definitions. Exported for testing. */
export function toOpenAITools(tools: ReturnType<typeof createReadOnlyTools>) {
	return tools.map((tool) => ({
		type: "function" as const,
		function: { name: tool.name, description: tool.description, parameters: tool.parameters },
	}));
}

async function executeReadOnlyToolCall(
	tools: ReturnType<typeof createReadOnlyTools>,
	call: OaToolCall,
	signal: AbortSignal | undefined,
): Promise<string> {
	const tool = tools.find((candidate) => candidate.name === call.function?.name);
	if (!tool) return `Tool "${call.function?.name}" is not available to this read-only sub-agent.`;
	let args: unknown = {};
	try {
		args = call.function.arguments ? JSON.parse(call.function.arguments) : {};
	} catch {
		args = {};
	}
	try {
		const prepared = tool.prepareArguments ? tool.prepareArguments(args) : args;
		const result = await tool.execute(call.id, prepared as never, signal, () => {});
		const text = result.content
			.filter((part) => part.type === "text")
			.map((part) => (part as { text: string }).text)
			.join("\n");
		return text || "(no output)";
	} catch (error) {
		return `Error: ${error instanceof Error ? error.message : String(error)}`;
	}
}

/**
 * The read-only investigation loop, on the non-thinking `chat` service pinned to
 * the background slot. Exported so it can be exercised against the live backend.
 * Up to MAX_TOOL_TURNS investigating turns, then one forced answer turn (no tools)
 * so an over-scoped task still returns a partial rather than hanging.
 */
export async function runDelegate(cwd: string, params: DelegateParams, signal?: AbortSignal): Promise<DelegateResult> {
	const service = resolveService("chat");
	if (!service?.url) {
		return { answer: "", toolTurns: 0, hitBudget: false, error: "the local chat service is not configured" };
	}

	const tools = createReadOnlyTools(cwd);
	const oaTools = toOpenAITools(tools);
	const messages: OaMessage[] = [
		{ role: "system", content: INVESTIGATE_SYSTEM_PROMPT },
		{ role: "user", content: buildTaskPrompt(params) },
	];
	const deadline = Date.now() + DEADLINE_MS;
	let toolTurns = 0;

	for (let turn = 0; turn <= MAX_TOOL_TURNS; turn++) {
		if (signal?.aborted) return { answer: "", toolTurns, hitBudget: true };
		const forcing = turn === MAX_TOOL_TURNS || Date.now() > deadline;
		if (forcing) messages.push({ role: "user", content: FORCED_ANSWER_NUDGE });

		let message: OaMessage;
		try {
			const result = await callWithTools(service, messages, {
				background: true,
				tools: forcing ? null : oaTools,
				temperature: 0,
				maxTokens: MAX_OUTPUT_TOKENS,
				timeoutMs: PER_CALL_TIMEOUT_MS,
			});
			message = result.message as OaMessage;
		} catch (error) {
			return {
				answer: "",
				toolTurns,
				hitBudget: false,
				error: error instanceof Error ? error.message : String(error),
			};
		}
		messages.push(message);

		const toolCalls = forcing ? [] : (message.tool_calls ?? []);
		if (toolCalls.length === 0) {
			const answer = typeof message.content === "string" ? message.content.trim() : "";
			return { answer, toolTurns, hitBudget: forcing };
		}

		for (const call of toolCalls) {
			const text = await executeReadOnlyToolCall(tools, call, signal);
			messages.push({ role: "tool", tool_call_id: call.id, content: text });
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
			const result = await runDelegate(ctx.cwd, input, signal);
			if (result.error) {
				return {
					content: [
						{
							type: "text",
							text: `forge_delegate error: ${result.error}${result.answer ? `\n\nPartial answer:\n${result.answer}` : ""}`,
						},
					],
					details: { error: result.error, toolTurns: result.toolTurns },
				};
			}
			return {
				content: [{ type: "text", text: result.answer || "(the delegate produced no answer)" }],
				details: { toolTurns: result.toolTurns, hitBudget: result.hitBudget },
			};
		},
	});
}
