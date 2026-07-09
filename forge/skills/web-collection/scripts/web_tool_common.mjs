import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ToolInputError } from "../../../lib/tool_contract.mjs";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const workflowScript = join(scriptDirectory, "web-collection.mjs");

export function sha256(buffer) {
	return createHash("sha256").update(buffer).digest("hex");
}

export function runCollection(url, output, options = {}) {
	const args = ["collect", url, "--output", output];
	if (options.render) args.push("--render");
	if (options.userAgent) args.push("--user-agent", options.userAgent);
	if (options.timeoutMs !== undefined) args.push("--timeout-ms", String(options.timeoutMs));
	if (options.maxBytes !== undefined) args.push("--max-bytes", String(options.maxBytes));
	const result = spawnSync(process.execPath, [workflowScript, ...args], {
		encoding: "utf8",
		maxBuffer: 100 * 1024 * 1024,
	});
	if (result.status !== 0) {
		throw new ToolInputError("collection_command_failed", result.stderr.trim() || result.stdout.trim() || `exit status ${result.status}`);
	}
	return JSON.parse(result.stdout);
}

export function readCollectionManifest(output) {
	const path = join(output, "web_manifest.json");
	if (!existsSync(path)) return null;
	return JSON.parse(readFileSync(path, "utf8"));
}

export function collectionArtifacts(output) {
	const artifacts = [];
	for (const name of ["web_manifest.csv", "web_manifest.json", "failed_downloads.csv", "collection_report.md"]) {
		const path = join(output, name);
		if (existsSync(path)) artifacts.push({ role: name.replace(/\.[^.]+$/, ""), path });
	}
	for (const record of readCollectionManifest(output)?.resources ?? []) {
		if (record.outputPath) artifacts.push({ role: "download", path: join(output, record.outputPath), sourceUrl: record.sourceUrl });
		if (record.capture) {
			for (const name of record.captureArtifacts ?? []) {
				artifacts.push({ role: "capture", path: join(output, record.capture, name), sourceUrl: record.sourceUrl });
			}
			artifacts.push({ role: "capture_metadata", path: join(output, record.capture, "capture.json"), sourceUrl: record.sourceUrl });
		}
	}
	return artifacts;
}

export function collectionWarnings(output) {
	const manifest = readCollectionManifest(output);
	return [
		...new Set(
			(manifest?.resources ?? [])
				.flatMap((record) => record.warnings ?? [])
				.concat(failedReasons(output))
				.filter(Boolean),
		),
	];
}

export function failedReasons(output) {
	const path = join(output, "failed_downloads.csv");
	if (!existsSync(path)) return [];
	return readFileSync(path, "utf8")
		.split(/\r?\n/)
		.slice(1)
		.map((line) => line.trim())
		.filter(Boolean);
}

export function assertDownloaded(output, code) {
	const manifest = readCollectionManifest(output);
	const resources = manifest?.resources ?? [];
	if (resources.some((record) => record.status === "success" || record.status === "needs_review")) return;
	const reasons = failedReasons(output);
	throw new ToolInputError(code, reasons[0] || "No resource was downloaded");
}

export function assertRendered(output) {
	const manifest = readCollectionManifest(output);
	const resources = manifest?.resources ?? [];
	if (resources.some((record) => record.rendered)) return;
	const reasons = collectionWarnings(output);
	throw new ToolInputError("archive_failed", reasons[0] || "No rendered capture was produced");
}

export function htmlTitle(html) {
	const match = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
	if (!match) return null;
	return match[1].replace(/\s+/g, " ").trim() || null;
}

export function extractLinks(html, baseUrl = null) {
	const links = new Set();
	const attributePattern = /(?:href|src)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))/gi;
	let match = attributePattern.exec(html);
	while (match !== null) {
		const raw = (match[2] ?? match[3] ?? match[4] ?? "").trim();
		match = attributePattern.exec(html);
		if (!raw || raw.startsWith("#") || /^(?:javascript|mailto|tel|data):/i.test(raw)) continue;
		try {
			const absolute = baseUrl ? new URL(raw, baseUrl).toString() : raw;
			links.add(absolute);
		} catch {
			links.add(raw);
		}
	}
	return [...links];
}

export function metadataForPath(path, baseUrl = null) {
	const stat = statSync(path);
	const buffer = readFileSync(path);
	const extension = extname(path).toLowerCase();
	const entry = {
		path,
		filename: basename(path),
		extension,
		sizeBytes: stat.size,
		sha256: sha256(buffer),
		modifiedAt: stat.mtime.toISOString(),
	};
	if ([".html", ".htm", ".xhtml"].includes(extension)) {
		const html = buffer.toString("utf8");
		entry.title = htmlTitle(html);
		entry.links = extractLinks(html, baseUrl);
	}
	return entry;
}

export function metadataForInput(inputPath, options = {}) {
	const resolved = resolve(inputPath);
	const stat = statSync(resolved);
	if (stat.isFile()) return [metadataForPath(resolved, options.baseUrl ?? null)];
	const root = resolved;
	const entries = [];
	const pending = [root];
	while (pending.length > 0 && entries.length < (options.maxFiles ?? 500)) {
		const current = pending.pop();
		for (const name of readdirSync(current, { withFileTypes: true })) {
			const path = join(current, name.name);
			if (name.isDirectory()) pending.push(path);
			else if (name.isFile()) {
				const entry = metadataForPath(path, options.baseUrl ?? null);
				entry.relativePath = relative(root, path);
				entries.push(entry);
			}
		}
	}
	return entries.sort((left, right) => (left.relativePath ?? left.path).localeCompare(right.relativePath ?? right.path));
}
