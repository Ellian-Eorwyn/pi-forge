import { calculateCost } from "../models.ts";
import type { Api, AssistantMessage, Model, StreamTelemetry } from "../types.ts";

export interface TelemetryTiming {
	sequence: number;
	timestamp?: number;
	startedAtMs?: number;
	nowMs?: number;
	firstTokenAtMs?: number;
	final?: boolean;
	responseId?: string;
	responseModel?: string;
}

type UnknownRecord = Record<string, unknown>;

const EXTRA_KEY_RE =
	/accepted|rejected|draft|speculative|mtp|dflash|tokens[_-]?per[_-]?second|tok[_-]?per[_-]?sec|tps/i;

function asRecord(value: unknown): UnknownRecord | undefined {
	return value && typeof value === "object" && !Array.isArray(value) ? (value as UnknownRecord) : undefined;
}

function readNumber(...values: unknown[]): number | undefined {
	for (const value of values) {
		if (typeof value === "number" && Number.isFinite(value)) return value;
	}
	return undefined;
}

function readString(...values: unknown[]): string | undefined {
	for (const value of values) {
		if (typeof value === "string" && value.trim().length > 0) return value;
	}
	return undefined;
}

function withDefined<T extends object>(value: T): T | undefined {
	return Object.values(value).some((entry) => entry !== undefined) ? value : undefined;
}

function collectNumericExtras(
	value: unknown,
	prefix: string[] = [],
	out: Record<string, number> = {},
): Record<string, number> {
	if (prefix.length > 8) return out;
	if (typeof value === "number" && Number.isFinite(value)) {
		const path = prefix.join(".");
		if (EXTRA_KEY_RE.test(path)) out[path] = value;
		return out;
	}
	const record = asRecord(value);
	if (!record) return out;
	for (const [key, entry] of Object.entries(record)) {
		collectNumericExtras(entry, [...prefix, key], out);
	}
	return out;
}

function extractDetails(rawUsage: UnknownRecord): StreamTelemetry["details"] | undefined {
	const completionDetails = asRecord(rawUsage.completion_tokens_details) ?? asRecord(rawUsage.completionTokensDetails);
	return withDefined({
		reasoningTokens: readNumber(completionDetails?.reasoning_tokens, completionDetails?.reasoningTokens),
		acceptedPredictionTokens: readNumber(
			completionDetails?.accepted_prediction_tokens,
			completionDetails?.acceptedPredictionTokens,
		),
		rejectedPredictionTokens: readNumber(
			completionDetails?.rejected_prediction_tokens,
			completionDetails?.rejectedPredictionTokens,
		),
	});
}

function extractSpeculative(rawUsage: UnknownRecord): StreamTelemetry["speculative"] | undefined {
	const candidates: Array<{ source?: UnknownRecord; fallbackMethod?: string }> = [
		{ source: asRecord(rawUsage.speculative_decoding), fallbackMethod: "speculative" },
		{ source: asRecord(rawUsage.speculativeDecoding), fallbackMethod: "speculative" },
		{ source: asRecord(rawUsage.speculative), fallbackMethod: "speculative" },
		{ source: asRecord(rawUsage.mtp), fallbackMethod: "mtp" },
		{ source: asRecord(rawUsage.dflash), fallbackMethod: "dflash" },
	];

	for (const candidate of candidates) {
		const source = candidate.source;
		if (!source) continue;
		const acceptedTokens = readNumber(source.accepted_tokens, source.acceptedTokens, source.accepted);
		const rejectedTokens = readNumber(source.rejected_tokens, source.rejectedTokens, source.rejected);
		const draftTokens = readNumber(source.draft_tokens, source.draftTokens, source.draft);
		let acceptanceRate = readNumber(source.acceptance_rate, source.acceptanceRate);
		if (acceptanceRate === undefined && acceptedTokens !== undefined && rejectedTokens !== undefined) {
			const total = acceptedTokens + rejectedTokens;
			if (total > 0) acceptanceRate = acceptedTokens / total;
		}
		const speculative = withDefined({
			method: readString(source.method, source.type) ?? candidate.fallbackMethod,
			draftTokens,
			acceptedTokens,
			rejectedTokens,
			acceptanceRate,
		});
		if (speculative) return speculative;
	}

	return undefined;
}

function usageFromOpenAICompatible<TApi extends Api>(
	rawUsage: UnknownRecord,
	model: Model<TApi>,
): AssistantMessage["usage"] {
	const promptTokens =
		readNumber(rawUsage.prompt_tokens, rawUsage.promptTokens, rawUsage.input_tokens, rawUsage.inputTokens) ?? 0;
	const outputTokens =
		readNumber(
			rawUsage.completion_tokens,
			rawUsage.completionTokens,
			rawUsage.output_tokens,
			rawUsage.outputTokens,
		) ?? 0;
	const promptDetails =
		asRecord(rawUsage.prompt_tokens_details) ??
		asRecord(rawUsage.promptTokensDetails) ??
		asRecord(rawUsage.input_tokens_details) ??
		asRecord(rawUsage.inputTokensDetails);
	const cacheReadTokens =
		readNumber(promptDetails?.cached_tokens, promptDetails?.cachedTokens, rawUsage.prompt_cache_hit_tokens) ?? 0;
	const cacheWriteTokens = readNumber(promptDetails?.cache_write_tokens, promptDetails?.cacheWriteTokens) ?? 0;
	const input = Math.max(0, promptTokens - cacheReadTokens - cacheWriteTokens);
	const usage: AssistantMessage["usage"] = {
		input,
		output: outputTokens,
		cacheRead: cacheReadTokens,
		cacheWrite: cacheWriteTokens,
		totalTokens:
			readNumber(rawUsage.total_tokens, rawUsage.totalTokens) ??
			input + outputTokens + cacheReadTokens + cacheWriteTokens,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
	calculateCost(model, usage);
	return usage;
}

function buildThroughput(
	usage: AssistantMessage["usage"],
	speculative: StreamTelemetry["speculative"] | undefined,
	timing: TelemetryTiming,
): StreamTelemetry["throughput"] | undefined {
	const nowMs = timing.nowMs ?? performance.now();
	const elapsedMs = timing.startedAtMs === undefined ? undefined : Math.max(0, nowMs - timing.startedAtMs);
	const seconds = elapsedMs === undefined ? undefined : elapsedMs / 1000;
	const acceptedTokens = speculative?.acceptedTokens ?? usage.details?.acceptedPredictionTokens;
	return withDefined({
		elapsedMs,
		timeToFirstTokenMs:
			timing.startedAtMs !== undefined && timing.firstTokenAtMs !== undefined
				? Math.max(0, timing.firstTokenAtMs - timing.startedAtMs)
				: undefined,
		outputTokensPerSecond: seconds && seconds > 0 && usage.output > 0 ? usage.output / seconds : undefined,
		acceptedTokensPerSecond:
			seconds && seconds > 0 && acceptedTokens !== undefined ? acceptedTokens / seconds : undefined,
	});
}

export function buildTelemetryFromUsage<TApi extends Api>(
	usage: AssistantMessage["usage"],
	model: Model<TApi>,
	timing: TelemetryTiming,
): StreamTelemetry {
	return {
		schema: "pi.telemetry.v1",
		sequence: timing.sequence,
		timestamp: timing.timestamp ?? Date.now(),
		final: timing.final || undefined,
		model: timing.responseModel ?? model.id,
		responseId: timing.responseId,
		usage: {
			input: usage.input,
			output: usage.output,
			cacheRead: usage.cacheRead,
			cacheWrite: usage.cacheWrite,
			totalTokens: usage.totalTokens,
		},
		details: usage.details,
		throughput: usage.throughput ?? buildThroughput(usage, usage.speculative, timing),
		speculative: usage.speculative,
		provider: {
			api: model.api,
			provider: model.provider,
			numericExtras: usage.providerExtras,
		},
	};
}

export function normalizeOpenAICompatibleTelemetry<TApi extends Api>(
	rawUsage: unknown,
	model: Model<TApi>,
	timing: TelemetryTiming,
): { usage: AssistantMessage["usage"]; telemetry: StreamTelemetry } {
	const rawRecord = asRecord(rawUsage) ?? {};
	const usage = usageFromOpenAICompatible(rawRecord, model);
	const details = extractDetails(rawRecord);
	const speculative = extractSpeculative(rawRecord);
	const numericExtras = collectNumericExtras(rawRecord);
	usage.details = details;
	usage.speculative = speculative;
	if (Object.keys(numericExtras).length > 0) usage.providerExtras = numericExtras;
	usage.throughput = buildThroughput(usage, speculative, timing);
	return { usage, telemetry: buildTelemetryFromUsage(usage, model, timing) };
}
