import { readFileSync, writeFileSync } from "node:fs";

export class ToolInputError extends Error {
	constructor(code, message) {
		super(message);
		this.name = "ToolInputError";
		this.code = code;
	}
}

export function parseToolArguments(argv = process.argv.slice(2)) {
	const args = { input: null, output: null };
	for (let index = 0; index < argv.length; index += 1) {
		const argument = argv[index];
		if (argument === "--input") args.input = argv[++index] ?? "";
		else if (argument === "--output") args.output = argv[++index] ?? "";
		else throw new ToolInputError("unknown_option", `Unknown option: ${argument}`);
	}
	if (args.input === "") throw new ToolInputError("missing_option_value", "--input requires a path");
	if (args.output === "") throw new ToolInputError("missing_option_value", "--output requires a path");
	return args;
}

export function readToolInput(args) {
	const raw = args.input ? readFileSync(args.input, "utf8") : readFileSync(0, "utf8");
	if (!raw.trim()) return {};
	try {
		return JSON.parse(raw);
	} catch (error) {
		throw new ToolInputError("invalid_json", error instanceof Error ? error.message : String(error));
	}
}

export function okResult({ artifacts = [], warnings = [], data = null } = {}) {
	return { status: "ok", artifacts, warnings, errors: [], data };
}

export function errorResult(error) {
	const code = typeof error?.code === "string" ? error.code : "tool_error";
	const message = error instanceof Error ? error.message : String(error);
	return { status: "error", artifacts: [], warnings: [], errors: [{ code, message }] };
}

export function writeToolResult(result, args) {
	const text = `${JSON.stringify(result, null, 2)}\n`;
	if (args.output) writeFileSync(args.output, text);
	else process.stdout.write(text);
}

export async function runTool(handler) {
	let args;
	try {
		args = parseToolArguments();
		const input = readToolInput(args);
		writeToolResult(await handler(input), args);
	} catch (error) {
		writeToolResult(errorResult(error), args ?? { output: null });
		process.exitCode = 1;
	}
}

export function requiredString(input, key) {
	const value = input[key];
	if (typeof value !== "string" || value.trim() === "") {
		throw new ToolInputError("missing_required_field", `${key} is required`);
	}
	return value;
}

export function optionalInteger(input, key, fallback) {
	const value = input[key];
	if (value === undefined || value === null) return fallback;
	if (!Number.isInteger(value) || value < 0) {
		throw new ToolInputError("invalid_integer_field", `${key} must be a non-negative integer`);
	}
	return value;
}
