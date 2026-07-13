import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
	chmodSync,
	copyFileSync,
	existsSync,
	mkdtempSync,
	mkdirSync,
	readFileSync,
	realpathSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import test from "node:test";
import webResearchExtension, { formatRunFailure } from "../forge/extensions/web-research.ts";

const repositoryRoot = resolve(import.meta.dirname, "..");
const skillsRoot = join(repositoryRoot, "forge", "skills");
const python = process.env.PYTHON ?? "python3";
const placeholder = "<!-- TODO: author this section -->";
const environment = {
	...process.env,
	FORGE_BASE_CHAT_URL: "http://127.0.0.1:1/v1/chat/completions",
	FORGE_SEARXNG_URL: "",
	PYTHONDONTWRITEBYTECODE: "1",
};

function script(skill, name) {
	return join(skillsRoot, skill, "scripts", name);
}

function run(command, args, expectedStatus = 0) {
	const result = spawnSync(command, args, {
		cwd: repositoryRoot,
		encoding: "utf8",
		env: environment,
		maxBuffer: 64 * 1024 * 1024,
	});
	assert.equal(
		result.status,
		expectedStatus,
		`${command} ${args.join(" ")} exited ${result.status}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
	);
	return result;
}

function runFailure(command, args, expectedText) {
	const result = spawnSync(command, args, {
		cwd: repositoryRoot,
		encoding: "utf8",
		env: environment,
		maxBuffer: 64 * 1024 * 1024,
	});
	assert.notEqual(result.status, 0, `${command} ${args.join(" ")} unexpectedly succeeded`);
	assert.match(`${result.stdout}\n${result.stderr}`, expectedText);
	return result;
}

function runWithEnvironment(command, args, extraEnvironment, expectedStatus = 0) {
	const result = spawnSync(command, args, {
		cwd: repositoryRoot,
		encoding: "utf8",
		env: { ...environment, ...extraEnvironment },
		maxBuffer: 64 * 1024 * 1024,
	});
	assert.equal(
		result.status,
		expectedStatus,
		`${command} ${args.join(" ")} exited ${result.status}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
	);
	return result;
}

function runAsyncWithEnvironment(command, args, extraEnvironment, expectedStatus = 0) {
	return new Promise((resolveRun, rejectRun) => {
		const child = spawn(command, args, {
			cwd: repositoryRoot,
			env: { ...environment, ...extraEnvironment },
			stdio: ["ignore", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stdout.on("data", (chunk) => {
			stdout += chunk;
		});
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});
		child.once("error", rejectRun);
		child.once("exit", (status) => {
			try {
				assert.equal(
					status,
					expectedStatus,
					`${command} ${args.join(" ")} exited ${status}\nstdout:\n${stdout}\nstderr:\n${stderr}`,
				);
				resolveRun({ status, stdout, stderr });
			} catch (error) {
				rejectRun(error);
			}
		});
	});
}

function runAt(directory, command, args, extraEnvironment = {}, expectedStatus = 0) {
	const result = spawnSync(command, args, {
		cwd: directory,
		encoding: "utf8",
		env: { ...environment, ...extraEnvironment },
		maxBuffer: 64 * 1024 * 1024,
	});
	assert.equal(
		result.status,
		expectedStatus,
		`${command} ${args.join(" ")} exited ${result.status}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
	);
	return result;
}

function jsonOutput(result) {
	return JSON.parse(result.stdout);
}

function parseCsvRows(value) {
	const rows = [];
	let row = [];
	let field = "";
	let quoted = false;
	for (let index = 0; index < value.length; index += 1) {
		const character = value[index];
		if (quoted) {
			if (character === '"' && value[index + 1] === '"') {
				field += '"';
				index += 1;
			} else if (character === '"') quoted = false;
			else field += character;
		} else if (character === '"') quoted = true;
		else if (character === ",") {
			row.push(field);
			field = "";
		} else if (character === "\n") {
			row.push(field.replace(/\r$/, ""));
			rows.push(row);
			row = [];
			field = "";
		} else field += character;
	}
	if (field || row.length > 0) {
		row.push(field);
		rows.push(row);
	}
	return rows;
}

function firstManifestRow(runDirectory) {
	const [columns, values] = parseCsvRows(readFileSync(join(runDirectory, "manifest.csv"), "utf8"));
	return Object.fromEntries(columns.map((column, index) => [column, values[index] ?? ""]));
}

function sha256(path) {
	return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function authorFiles(directory, names) {
	for (const name of names) {
		const path = join(directory, name);
		const current = readFileSync(path, "utf8");
		assert.match(current, /<!-- TODO: author this section -->/);
		writeFileSync(path, current.replace(placeholder, "Authored from the staged source evidence."));
	}
}

function csvEscape(value) {
	const text = value === null || value === undefined ? "" : String(value);
	return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function writeCsvRows(path, rows) {
	writeFileSync(path, rows.map((row) => row.map(csvEscape).join(",")).join("\n") + "\n");
}

function readManifestRows(runDirectory) {
	const rows = parseCsvRows(readFileSync(join(runDirectory, "manifest.csv"), "utf8"));
	const columns = rows.shift();
	return {
		columns,
		rows: rows.filter((row) => row.some((field) => field !== "")).map((row) => Object.fromEntries(columns.map((column, index) => [column, row[index] ?? ""]))),
	};
}

function writeManifestRows(runDirectory, columns, rows) {
	writeCsvRows(
		join(runDirectory, "manifest.csv"),
		[columns, ...rows.map((row) => columns.map((column) => row[column] ?? ""))],
	);
}

function completeIngestRun(runDirectory) {
	const manifest = readManifestRows(runDirectory);
	for (const row of manifest.rows) {
		if (row.status !== "needs_review") continue;
		const documentDirectory = join(runDirectory, row.output_directory);
		const metadataPath = join(documentDirectory, "metadata.json");
		const metadata = JSON.parse(readFileSync(metadataPath, "utf8"));
		metadata.extraction.status = "success";
		metadata.review.completed = true;
		metadata.finalOutput = metadata.finalOutput ?? { filename: null, namingReason: null };
		writeFileSync(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`);
		const reportPath = join(documentDirectory, "extraction_report.md");
		writeFileSync(reportPath, readFileSync(reportPath, "utf8").replace("model normalization pending", "model normalization complete"));
		row.status = "success";
	}
	writeManifestRows(runDirectory, manifest.columns, manifest.rows);
	return manifest.rows;
}

function withWorkspace(callback) {
	const workspace = mkdtempSync(join(tmpdir(), "pi-forge-skills-"));
	try {
		callback(workspace);
	} finally {
		rmSync(workspace, { recursive: true, force: true });
	}
}

async function withAsyncWorkspace(callback) {
	const workspace = mkdtempSync(join(tmpdir(), "pi-forge-skills-"));
	try {
		await callback(workspace);
	} finally {
		rmSync(workspace, { recursive: true, force: true });
	}
}

function startDeepResearchFixture() {
	const requests = [];
	const server = createServer((request, response) => {
		const url = new URL(request.url, "http://127.0.0.1");
		const origin = `http://127.0.0.1:${server.address().port}`;
		if (url.pathname === "/searxng/search") {
			const query = url.searchParams.get("q") ?? "";
			const results = [];
			if (/alpha/i.test(query)) {
				results.push(
					{ title: "Alpha Source", url: `${origin}/page-alpha`, content: "Alpha snippet", engine: "fixture", score: 1 },
					{ title: "Alpha Source Duplicate", url: `${origin}/page-alpha#duplicate`, content: "Duplicate snippet", engine: "fixture", score: 0.9 },
				);
			} else if (/beta/i.test(query)) {
				results.push({ title: "Beta Source", url: `${origin}/page-beta`, content: "Beta snippet", engine: "fixture", score: 1 });
			} else if (/follow-up/i.test(query)) {
				results.push({ title: "Gamma Source", url: `${origin}/page-gamma`, content: "Gamma snippet", engine: "fixture", score: 1 });
			} else if (/many/i.test(query)) {
				for (let index = 1; index <= 8; index += 1) {
					results.push({
						title: `Many Source ${index}`,
						url: `${origin}/page-many?query=${encodeURIComponent(query)}&n=${index}`,
						content: `Many snippet ${index}`,
						engine: "fixture",
						score: 1 - index / 100,
					});
				}
			} else if (/mismatch/i.test(query)) {
				results.push({ title: "Mismatch Source", url: `${origin}/page-mismatch`, content: "Mismatch snippet", engine: "fixture", score: 1 });
			} else if (/checkpoint/i.test(query)) {
				results.push({ title: "Checkpoint Source", url: `${origin}/page-checkpoint`, content: "Checkpoint snippet", engine: "fixture", score: 1 });
			} else if (/structured/i.test(query)) {
				results.push(
					{
						title: "Structured Source",
						url: `${origin}/page-structured?utm_source=tracker&utm_medium=test#fragment`,
						content: "Structured snippet",
						engine: "fixture",
						score: 1,
						publishedDate: "2026-07-01",
					},
					{
						title: "Structured Source Duplicate",
						url: `${origin}/page-structured`,
						content: "Duplicate structured snippet",
						engine: "fixture",
						score: 0.8,
					},
				);
			} else if (/binary/i.test(query)) {
				results.push({ title: "Binary Source", url: `${origin}/paper.pdf`, content: "PDF snippet", engine: "fixture", score: 1 });
			} else if (/unsafe/i.test(query)) {
				results.push({ title: "Unsafe Source", url: "http://127.0.0.1:9/unsafe", content: "Unsafe snippet", engine: "fixture", score: 1 });
			}
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(JSON.stringify({ results }));
			return;
		}
		if (url.pathname === "/chat") {
			let body = "";
			request.setEncoding("utf8");
			request.on("data", (chunk) => {
				body += chunk;
			});
			request.on("end", () => {
				const payload = body ? JSON.parse(body) : {};
				requests.push(payload);
				const prompt = payload.messages?.at(-1)?.content ?? "";
				let content = "{}";
				if (prompt.includes("Plan follow-up web searches")) {
					content = JSON.stringify({ queries: ["follow-up provenance validation"], rationale: "Need validation source." });
				} else if (prompt.includes("Extract source-backed evidence")) {
					const sourceId = prompt.match(/source_id: (src-[a-f0-9]+)/)?.[1] ?? "src-unknown";
					let quote = "Alpha evidence quote supports deep research provenance.";
					if (prompt.includes("Beta Source")) quote = "Beta evidence quote describes validation citations.";
					if (prompt.includes("Gamma Source")) quote = "Gamma follow up quote identifies remaining gap.";
					const manyQuote = prompt.match(/Many evidence quote \d+ for [^.]+\./)?.[0];
					if (manyQuote) quote = manyQuote;
					if (prompt.includes("Mismatch Source")) quote = "Invented quote that is absent from the archived source.";
					content = JSON.stringify({
						evidence: [
							{
								text: `${quote} This is relevant to the research question.`,
								direct_quote: quote,
								locator: "article",
								interpretation: "explicit",
								confidence: "high",
								notes: sourceId,
							},
						],
					});
				} else if (prompt.includes("Build a source-backed claim register")) {
					const matches = [...prompt.matchAll(/evidence_id: (ev-\d+)\n  source_id: (src-[a-f0-9]+)/g)];
					const evidenceIds = matches.slice(0, 2).map((match) => match[1]);
					const sourceIds = [...new Set(matches.slice(0, 2).map((match) => match[2]))];
					content = JSON.stringify({
						claims: [
							{
								text: "Deep research provenance and citation validation are supported by fixture sources.",
								evidence_ids: evidenceIds,
								source_ids: sourceIds,
								confidence: "high",
								notes: "Fixture claim.",
							},
						],
						gaps: [{ text: "Long-term WARC export is not covered.", reason: "No fixture evidence.", source_ids: sourceIds }],
					});
				}
				response.writeHead(200, { "Content-Type": "application/json" });
				response.end(JSON.stringify({ choices: [{ message: { content } }] }));
			});
			return;
		}
		const pages = {
			"/page-alpha": ["Alpha Source", "Alpha evidence quote supports deep research provenance."],
			"/page-beta": ["Beta Source", "Beta evidence quote describes validation citations."],
			"/page-gamma": ["Gamma Source", "Gamma follow up quote identifies remaining gap."],
			"/page-mismatch": ["Mismatch Source", "Mismatch source text supports deterministic quote repair."],
			"/page-checkpoint": ["Vercel Security Checkpoint", "Vercel Security Checkpoint | sfo1::test"],
		};
		const page = pages[url.pathname];
		if (page) {
			response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
			response.end(`<!doctype html><title>${page[0]}</title><main><h1>${page[0]}</h1><p>${page[1]}</p></main>`);
			return;
		}
		if (url.pathname === "/page-many") {
			const query = url.searchParams.get("query") ?? "many";
			const index = url.searchParams.get("n") ?? "0";
			response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
			response.end(
				`<!doctype html><title>Many Source ${index}</title><main><h1>Many Source ${index}</h1><p>Many evidence quote ${index} for ${query}. Budgeted research should preserve this source.</p></main>`,
			);
			return;
		}
		if (url.pathname === "/page-structured") {
			response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
			response.end(`<!doctype html>
<html>
<head>
  <title>Structured Source</title>
  <link rel="canonical" href="${origin}/page-structured" />
  <meta property="og:title" content="Structured Source" />
  <meta name="citation_publication_date" content="2026/07/01" />
  <script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","headline":"Structured Source","datePublished":"2026-07-01"}</script>
  <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"article":{"title":"Structured Source","body":"Structured acquisition evidence from Next data."}}}}</script>
  <script src="/api/structured-data"></script>
</head>
<body><main><article><h1>Structured Source</h1><p>Structured acquisition evidence appears in static HTML and should be extracted without browser rendering.</p></article></main></body>
</html>`);
			return;
		}
		if (url.pathname === "/api/structured-data") {
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(JSON.stringify({ title: "Structured API", body: "Structured endpoint payload." }));
			return;
		}
		if (url.pathname === "/paper.pdf") {
			response.writeHead(200, { "Content-Type": "application/pdf" });
			response.end("%PDF-1.4\nsynthetic pdf fixture\n");
			return;
		}
		response.writeHead(404, { "Content-Type": "text/plain" });
		response.end("not found");
	});
	return new Promise((resolveServer, rejectServer) => {
		server.once("error", rejectServer);
		server.listen(0, "127.0.0.1", () => {
			const address = server.address();
			if (!address || typeof address !== "object") {
				rejectServer(new Error("fixture server did not expose a port"));
				return;
			}
			const origin = `http://127.0.0.1:${address.port}`;
			resolveServer({
				origin,
				searxng: `${origin}/searxng`,
				chat: `${origin}/chat`,
				requests,
				close: () =>
					new Promise((resolveClose) => {
						server.closeAllConnections();
						server.closeIdleConnections();
						server.close(resolveClose);
					}),
			});
		});
	});
}

function startAcademicResearchFixture() {
	const requests = [];
	const server = createServer((request, response) => {
		const url = new URL(request.url, "http://127.0.0.1");
		requests.push({ pathname: url.pathname, search: url.search });
		if (url.pathname === "/crossref/works") {
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(
				JSON.stringify({
					message: {
						items: [
							{
								DOI: "10.1234/ALPHA",
								title: ["Alpha & Beta RIS Study"],
								abstract: "<jats:p>Crossref abstract for the alpha study.</jats:p>",
								author: [
									{ given: "Ada", family: "Lovelace" },
									{ given: "Grace", family: "Hopper" },
								],
								"published-print": { "date-parts": [[2024, 5, 2]] },
								"container-title": ["Journal of Test Metadata"],
								publisher: "Fixture Press",
								type: "journal-article",
								ISSN: ["1234-5678"],
								subject: ["metadata"],
								URL: "https://doi.org/10.1234/alpha",
							},
							{
								DOI: "10.1234/NOABSTRACT",
								title: ["No Abstract in Crossref"],
								author: [{ given: "Nora", family: "Null" }],
								"published-online": { "date-parts": [[2023]] },
								"container-title": ["Sparse Metadata Quarterly"],
								publisher: "Fixture Press",
								type: "journal-article",
								URL: "https://doi.org/10.1234/noabstract",
							},
						],
					},
				}),
			);
			return;
		}
		if (url.pathname === "/semantic/graph/v1/paper/search") {
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(
				JSON.stringify({
					data: [
						{
							paperId: "S2-ALPHA",
							externalIds: { DOI: "10.1234/alpha", PubMed: "123456" },
							title: "Alpha & Beta RIS Study",
							authors: [
								{ name: "Ada Lovelace", authorId: "1" },
								{ name: "Grace Hopper", authorId: "2" },
							],
							year: 2024,
							venue: "Journal of Test Metadata",
							abstract: "Semantic Scholar abstract for the duplicated alpha study.",
							citationCount: 7,
							influentialCitationCount: 2,
							publicationTypes: ["JournalArticle"],
							fieldsOfStudy: ["Computer Science"],
							openAccessPdf: { url: "https://example.test/alpha.pdf" },
							url: "https://semanticscholar.org/paper/S2-ALPHA",
						},
						{
							paperId: "S2-NOABSTRACT",
							externalIds: { DOI: "10.1234/noabstract" },
							title: "No Abstract in Crossref",
							authors: [{ name: "Nora Null", authorId: "3" }],
							year: 2023,
							venue: "Sparse Metadata Quarterly",
							abstract: "Semantic Scholar supplies the missing abstract.",
							citationCount: 1,
							influentialCitationCount: 0,
							publicationTypes: ["JournalArticle"],
							fieldsOfStudy: ["Information Science"],
							url: "https://semanticscholar.org/paper/S2-NOABSTRACT",
						},
					],
				}),
			);
			return;
		}
		if (url.pathname === "/pubmed/esearch.fcgi") {
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(JSON.stringify({ esearchresult: { idlist: ["123456"] } }));
			return;
		}
		if (url.pathname === "/pubmed/esummary.fcgi") {
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(
				JSON.stringify({
					result: {
						uids: ["123456"],
						123456: {
							uid: "123456",
							title: "Alpha & Beta RIS Study",
							authors: [{ name: "Ada Lovelace" }, { name: "Grace Hopper" }],
							pubdate: "2024 May",
							fulljournalname: "Journal of Test Metadata",
							pubtype: ["Journal Article"],
							articleids: [{ idtype: "doi", value: "10.1234/alpha" }],
						},
					},
				}),
			);
			return;
		}
		if (url.pathname === "/arxiv/query") {
			response.writeHead(200, { "Content-Type": "application/atom+xml" });
			response.end(`<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <title>Alpha &amp; Beta RIS Study</title>
    <summary>Preprint abstract for the alpha study.</summary>
    <published>2024-01-02T00:00:00Z</published>
    <updated>2024-02-03T00:00:00Z</updated>
    <author><name>Ada Lovelace</name></author>
    <category term="cs.DL" />
    <link href="http://arxiv.org/abs/2401.00001v2" />
  </entry>
</feed>`);
			return;
		}
		if (url.pathname.startsWith("/doaj/search/articles/")) {
			response.writeHead(500, { "Content-Type": "application/json" });
			response.end(JSON.stringify({ error: "fixture provider failure" }));
			return;
		}
		response.writeHead(404, { "Content-Type": "text/plain" });
		response.end("not found");
	});
	return new Promise((resolveServer, rejectServer) => {
		server.once("error", rejectServer);
		server.listen(0, "127.0.0.1", () => {
			const address = server.address();
			if (!address || typeof address !== "object") {
				rejectServer(new Error("fixture server did not expose a port"));
				return;
			}
			const origin = `http://127.0.0.1:${address.port}`;
			resolveServer({
				origin,
				environment: {
					FORGE_ACADEMIC_CROSSREF_URL: `${origin}/crossref`,
					FORGE_ACADEMIC_SEMANTIC_SCHOLAR_URL: `${origin}/semantic`,
					FORGE_ACADEMIC_PUBMED_URL: `${origin}/pubmed`,
					FORGE_ACADEMIC_ARXIV_URL: `${origin}/arxiv`,
					FORGE_ACADEMIC_DOAJ_URL: `${origin}/doaj`,
					FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1",
				},
				requests,
				close: () =>
					new Promise((resolveClose) => {
						server.closeAllConnections();
						server.closeIdleConnections();
						server.close(resolveClose);
					}),
			});
		});
	});
}

function startGlmocrFixture(workspace, responseBody) {
	const requestsPath = join(workspace, "glmocr-requests.jsonl");
	const serverPath = join(workspace, "glmocr-server.mjs");
	writeFileSync(
		serverPath,
		`import { appendFileSync } from "node:fs";
import { createServer } from "node:http";

const responseBody = ${JSON.stringify(responseBody)};
const requestsPath = ${JSON.stringify(requestsPath)};
const server = createServer((request, response) => {
	let body = "";
	request.setEncoding("utf8");
	request.on("data", (chunk) => {
		body += chunk;
	});
	request.on("end", () => {
		appendFileSync(requestsPath, JSON.stringify({ method: request.method, url: request.url, body: body ? JSON.parse(body) : {} }) + "\\n");
		response.writeHead(200, { "Content-Type": "application/json" });
		response.end(JSON.stringify(responseBody));
	});
});

server.listen(0, "127.0.0.1", () => {
	const address = server.address();
	if (!address || typeof address !== "object") process.exit(1);
	process.stdout.write(String(address.port) + "\\n");
});
`,
	);
	const child = spawn(process.execPath, [serverPath], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
	return new Promise((resolveServer, rejectServer) => {
		let stdout = "";
		let stderr = "";
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});
		child.stdout.on("data", (chunk) => {
			stdout += chunk;
			const line = stdout.split(/\r?\n/).find((value) => value.trim());
			if (!line) return;
			resolveServer({
				url: `http://127.0.0.1:${line.trim()}/glmocr/parse`,
				requestsPath,
				close: () =>
					new Promise((resolveClose, rejectClose) => {
						child.once("exit", () => resolveClose());
						child.once("error", rejectClose);
						child.kill();
					}),
			});
		});
		child.once("error", rejectServer);
		child.once("exit", (code) => {
			if (!stdout.trim()) rejectServer(new Error(`GLM-OCR fixture exited before startup with code ${code}: ${stderr}`));
		});
	});
}

function startEmbeddingsFixture(workspace) {
	const serverPath = join(workspace, "embeddings-server.mjs");
	writeFileSync(
		serverPath,
		`import { createServer } from "node:http";

function vector(text) {
	const counts = new Array(26).fill(0);
	for (const character of text.toLowerCase()) {
		const code = character.charCodeAt(0) - 97;
		if (code >= 0 && code < 26) counts[code] += 1;
	}
	return counts;
}

const server = createServer((request, response) => {
	let body = "";
	request.setEncoding("utf8");
	request.on("data", (chunk) => {
		body += chunk;
	});
	request.on("end", () => {
		const payload = body ? JSON.parse(body) : {};
		const inputs = Array.isArray(payload.input) ? payload.input : [];
		const data = inputs.map((text, index) => ({ index, embedding: vector(String(text)) }));
		response.writeHead(200, { "Content-Type": "application/json" });
		response.end(JSON.stringify({ object: "list", data, model: "stub" }));
	});
});

server.listen(0, "127.0.0.1", () => {
	const address = server.address();
	if (!address || typeof address !== "object") process.exit(1);
	process.stdout.write(String(address.port) + "\\n");
});
`,
	);
	const child = spawn(process.execPath, [serverPath], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
	return new Promise((resolveServer, rejectServer) => {
		let stdout = "";
		let stderr = "";
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});
		child.stdout.on("data", (chunk) => {
			stdout += chunk;
			const line = stdout.split(/\r?\n/).find((value) => value.trim());
			if (!line) return;
			resolveServer({
				url: `http://127.0.0.1:${line.trim()}/v1/embeddings`,
				close: () =>
					new Promise((resolveClose, rejectClose) => {
						child.once("exit", () => resolveClose());
						child.once("error", rejectClose);
						child.kill();
					}),
			});
		});
		child.once("error", rejectServer);
		child.once("exit", (code) => {
			if (!stdout.trim()) rejectServer(new Error(`embeddings fixture exited before startup with code ${code}: ${stderr}`));
		});
	});
}

function startChatFixture(workspace, responseText) {
	const requestsPath = join(workspace, "chat-requests.jsonl");
	const serverPath = join(workspace, "chat-server.mjs");
	writeFileSync(
		serverPath,
		`import { appendFileSync } from "node:fs";
import { createServer } from "node:http";

const requestsPath = ${JSON.stringify(requestsPath)};
const responseText = ${JSON.stringify(responseText)};
const server = createServer((request, response) => {
	let body = "";
	request.setEncoding("utf8");
	request.on("data", (chunk) => {
		body += chunk;
	});
	request.on("end", () => {
		appendFileSync(requestsPath, JSON.stringify({ method: request.method, url: request.url, body: body ? JSON.parse(body) : {} }) + "\\n");
		response.writeHead(200, { "Content-Type": "application/json" });
		response.end(JSON.stringify({ choices: [{ message: { content: responseText } }] }));
	});
});

server.listen(0, "127.0.0.1", () => {
	const address = server.address();
	if (!address || typeof address !== "object") process.exit(1);
	process.stdout.write(String(address.port) + "\\n");
});
`,
	);
	const child = spawn(process.execPath, [serverPath], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
	return new Promise((resolveServer, rejectServer) => {
		let stdout = "";
		let stderr = "";
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});
		child.stdout.on("data", (chunk) => {
			stdout += chunk;
			const line = stdout.split(/\r?\n/).find((value) => value.trim());
			if (!line) return;
			resolveServer({
				url: `http://127.0.0.1:${line.trim()}/v1/chat/completions`,
				requestsPath,
				close: () =>
					new Promise((resolveClose, rejectClose) => {
						child.once("exit", () => resolveClose());
						child.once("error", rejectClose);
						child.kill();
					}),
			});
		});
		child.once("error", rejectServer);
		child.once("exit", (code) => {
			if (!stdout.trim()) rejectServer(new Error(`chat fixture exited before startup with code ${code}: ${stderr}`));
		});
	});
}

test("transcript cleanup and file conversion preserve their source", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "transcript.txt");
		const original = "Speaker 1: Um, send the draft Friday.  \r\n\r\n\r\nSpeaker 2: Agreed.\r\n";
		writeFileSync(source, original);
		const sourceHash = sha256(source);

		const extracted = join(workspace, "extracted.md");
		const transcriptResult = jsonOutput(
			run("node", [script("transcript-cleanup", "extract-transcript.mjs"), source, extracted]),
		);
		assert.equal(transcriptResult.sha256, sourceHash);
		assert.equal(sha256(source), sourceHash);
		assert.match(readFileSync(extracted, "utf8"), /send the draft Friday/);

		const conversionRun = join(workspace, "conversion");
		const conversion = jsonOutput(
			run(python, [script("file-conversion", "file-conversion.py"), "convert", source, "--to", "txt", "--output", conversionRun]),
		);
		assert.equal(conversion.success, 1);
		run(python, [script("file-conversion", "file-conversion.py"), "validate", conversionRun]);
		assert.equal(sha256(source), sourceHash);
		runFailure(
			python,
			[script("file-conversion", "file-conversion.py"), "convert", source, "--to", "txt", "--output", conversionRun],
			/output already exists/,
		);

		writeFileSync(source, `${original}changed\n`);
		runFailure(
			python,
			[script("file-conversion", "file-conversion.py"), "validate", conversionRun],
			/source file hash differs/,
		);
	});
});

test("Markdown to EPUB creates portable chapters, navigation, metadata, and covers", () => {
	withWorkspace((workspace) => {
		const pandoc = spawnSync("pandoc", ["--version"], { encoding: "utf8" });
		if (pandoc.status !== 0) return;
		const source = join(workspace, "portable-book.md");
		const image = join(workspace, "diagram.png");
		const cover = join(workspace, "cover.png");
		const png = Buffer.from(
			"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
			"base64",
		);
		writeFileSync(image, png);
		writeFileSync(cover, png);
		writeFileSync(
			source,
			`---
title: Frontmatter Title
author: Frontmatter Author
language: en-GB
---

# First Chapter

## Inside Chapter

- outer
  1. numbered
     - deep

| A | B | C | D | E |
|---|---|---|---|---|
| one | two | three | four | https://example.com/a/very/long/unbreakable/value/for/a/table |

![Diagram](diagram.png)

A [link](https://example.com) and a note.[^1]

\`\`\`text
wrapped code
\`\`\`

[^1]: Footnote text.

# Second Chapter

Final text.
`,
		);
		const runDirectory = join(workspace, "epub-run");
		const result = jsonOutput(
			run(python, [
				script("file-conversion", "file-conversion.py"),
				"convert",
				source,
				"--to",
				"epub",
				"--output",
				runDirectory,
				"--cover",
				cover,
				"--title",
				"Override Title",
				"--author",
				"Override Author",
			]),
		);
		assert.equal(result.needsReview, 1);
		const epub = join(runDirectory, "converted", "portable-book.epub");
		const archive = run("unzip", ["-l", epub]).stdout;
		assert.match(archive, /EPUB\/nav\.xhtml/);
		assert.match(archive, /EPUB\/text\/ch001\.xhtml/);
		assert.match(archive, /EPUB\/text\/ch002\.xhtml/);
		const nav = run("unzip", ["-p", epub, "EPUB/nav.xhtml"]).stdout;
		assert.match(nav, /First Chapter/);
		assert.match(nav, /Second Chapter/);
		const packageDocument = run("unzip", ["-p", epub, "EPUB/content.opf"]).stdout;
		assert.match(packageDocument, /Override Title/);
		assert.match(packageDocument, /Override Author/);
		assert.match(packageDocument, /en-GB/);
		assert.match(packageDocument, /cover-image/);
		const firstChapter = run("unzip", ["-p", epub, "EPUB/text/ch001.xhtml"]).stdout;
		assert.match(firstChapter, /Inside Chapter/);
		assert.match(firstChapter, /<table/);
		assert.match(firstChapter, /<ol/);
		assert.match(firstChapter, /<ul/);
		assert.match(firstChapter, /Footnote text/);
		assert.match(readFileSync(join(runDirectory, "warnings.md"), "utf8"), /5 columns/);
		const validation = jsonOutput(
			run(python, [script("file-conversion", "file-conversion.py"), "validate", runDirectory]),
		);
		assert.equal(validation.valid, true);

		const reverseRun = join(workspace, "reverse-run");
		const reverse = jsonOutput(
			run(python, [
				script("file-conversion", "file-conversion.py"),
				"convert",
				epub,
				"--to",
				"md",
				"--output",
				reverseRun,
			]),
		);
		assert.equal(reverse.needsReview, 1);
		const markdown = readFileSync(join(reverseRun, "converted", "portable-book.md"), "utf8");
		assert.match(markdown, /title: Override Title/);
		assert.match(markdown, /^# First Chapter$/m);
		assert.match(markdown, /^## Inside Chapter$/m);
		assert.match(markdown, /\| A\s+\| B\s+\|/);
		assert.match(markdown, /Footnote text/);
		assert.match(markdown, /media\/portable-book\/media\/file0\.png/);
		assert.doesNotMatch(markdown, /epub:type="landmarks"|class="section|<figure>/);
		assert.equal(existsSync(join(reverseRun, "converted", "media", "portable-book", "media", "file0.png")), true);
		assert.equal(existsSync(join(reverseRun, "converted", "media", "portable-book", "cover.png")), true);
		assert.equal(
			jsonOutput(run(python, [script("file-conversion", "file-conversion.py"), "validate", reverseRun])).valid,
			true,
		);

		writeFileSync(epub, "not an epub");
		runFailure(
			python,
			[script("file-conversion", "file-conversion.py"), "validate", runDirectory],
			/EPUB archive is invalid/,
		);
	});
});

test("EPUB to Markdown synthesizes chapter headings and reports unsupported EPUB behavior", () => {
	withWorkspace((workspace) => {
		const pandoc = spawnSync("pandoc", ["--version"], { encoding: "utf8" });
		if (pandoc.status !== 0) return;
		const source = join(workspace, "chapter.md");
		writeFileSync(source, "# Navigation Chapter\n\nOpening text.\n");
		const originalEpub = join(workspace, "original.epub");
		run("pandoc", [source, "--to=epub3", "--toc", "--split-level=1", "--output", originalEpub]);
		const unpacked = join(workspace, "unpacked");
		mkdirSync(unpacked);
		runAt(workspace, "unzip", ["-q", originalEpub, "-d", unpacked]);
		const chapterPath = join(unpacked, "EPUB", "text", "ch001.xhtml");
		writeFileSync(
			chapterPath,
			readFileSync(chapterPath, "utf8").replace(
				/<h1([^>]*)>Navigation Chapter<\/h1>/,
				'<p$1>Opening without a source heading.</p>',
			),
		);
		const packagePath = join(unpacked, "EPUB", "content.opf");
		writeFileSync(
			packagePath,
			readFileSync(packagePath, "utf8")
				.replace("</metadata>", '<meta property="rendition:layout">pre-paginated</meta></metadata>')
				.replace('id="ch001_xhtml"', 'id="ch001_xhtml" properties="scripted"'),
		);
		const modifiedEpub = join(workspace, "modified.epub");
		runAt(unpacked, "zip", ["-X0", modifiedEpub, "mimetype"]);
		runAt(unpacked, "zip", ["-Xr9", modifiedEpub, "META-INF", "EPUB"]);
		const runDirectory = join(workspace, "reverse");
		const result = jsonOutput(
			run(python, [
				script("file-conversion", "file-conversion.py"),
				"convert",
				modifiedEpub,
				"--to",
				"md",
				"--output",
				runDirectory,
			]),
		);
		assert.equal(result.needsReview, 1);
		assert.match(readFileSync(join(runDirectory, "converted", "modified.md"), "utf8"), /^# Navigation Chapter$/m);
		const warnings = readFileSync(join(runDirectory, "warnings.md"), "utf8");
		assert.match(warnings, /Synthesized level-one chapter heading/);
		assert.match(warnings, /fixed layout/);
		assert.match(warnings, /contains scripts/);

		const drmDirectory = join(workspace, "drm-unpacked");
		mkdirSync(drmDirectory);
		runAt(workspace, "unzip", ["-q", originalEpub, "-d", drmDirectory]);
		writeFileSync(join(drmDirectory, "META-INF", "rights.xml"), "<rights />\n");
		const drmEpub = join(workspace, "drm.epub");
		runAt(drmDirectory, "zip", ["-X0", drmEpub, "mimetype"]);
		runAt(drmDirectory, "zip", ["-Xr9", drmEpub, "META-INF", "EPUB"]);
		const drmRun = join(workspace, "drm-run");
		assert.equal(
			jsonOutput(
				run(python, [
					script("file-conversion", "file-conversion.py"),
					"convert",
					drmEpub,
					"--to",
					"md",
					"--output",
					drmRun,
				]),
			).failed,
			1,
		);
		assert.match(readFileSync(join(drmRun, "conversion_manifest.csv"), "utf8"), /DRM-protected/);

		const malformed = join(workspace, "malformed.epub");
		writeFileSync(malformed, "not a zip");
		const malformedRun = join(workspace, "malformed-run");
		assert.equal(
			jsonOutput(
				run(python, [
					script("file-conversion", "file-conversion.py"),
					"convert",
					malformed,
					"--to",
					"md",
					"--output",
					malformedRun,
				]),
			).failed,
			1,
		);
	});
});

test("managed EPUBCheck installation verifies archives and controls validation", () => {
	withWorkspace((workspace) => {
		const fixtureRoot = join(workspace, "epubcheck-5.3.0");
		mkdirSync(join(fixtureRoot, "lib"), { recursive: true });
		writeFileSync(join(fixtureRoot, "epubcheck.jar"), "synthetic jar\n");
		writeFileSync(join(fixtureRoot, "lib", "dependency.jar"), "synthetic dependency\n");
		const archive = join(workspace, "epubcheck.zip");
		runAt(workspace, "zip", ["-qr", archive, "epubcheck-5.3.0"]);

		const fakeBin = join(workspace, "fake-bin");
		mkdirSync(fakeBin);
		writeFileSync(
			join(fakeBin, "java"),
			`#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "openjdk 17"; exit 0; fi
if [[ "$1" == "-jar" && "$3" == "--version" ]]; then echo "EPUBCheck v5.3.0"; exit 0; fi
report=""
for ((index=1; index<=$#; index++)); do
	if [[ "\${!index}" == "--json" ]]; then next=$((index + 1)); report="\${!next}"; fi
done
severity="\${FAKE_EPUBCHECK_SEVERITY:-WARNING}"
printf '{"messages":[{"severity":"%s","ID":"TEST-001","message":"synthetic finding"}]}\n' "$severity" > "$report"
if [[ "$severity" == "ERROR" ]]; then exit 1; fi
`,
		);
		writeFileSync(join(fakeBin, "epubcheck"), "#!/usr/bin/env bash\necho 'EPUBCheck v9.9.9'\n");
		chmodSync(join(fakeBin, "java"), 0o755);
		chmodSync(join(fakeBin, "epubcheck"), 0o755);

		const agentDirectory = join(workspace, "agent");
		const install = jsonOutput(
			runWithEnvironment(
				python,
				[
					script("file-conversion", "file-conversion.py"),
					"install-epubcheck",
					"--tools-dir",
					join(agentDirectory, "tools"),
					"--archive",
					archive,
					"--expected-sha256",
					sha256(archive),
				],
				{ PATH: `${fakeBin}:${process.env.PATH}`, PI_CODING_AGENT_DIR: agentDirectory },
			),
		);
		assert.equal(install.installed, true);
		assert.equal(existsSync(join(agentDirectory, "tools", "epubcheck", "5.3.0", "epubcheck.jar")), true);
		const doctor = jsonOutput(
			runWithEnvironment(
				python,
				[script("file-conversion", "file-conversion.py"), "doctor", "--json"],
				{ PATH: `${fakeBin}:${process.env.PATH}`, PI_CODING_AGENT_DIR: agentDirectory },
			),
		);
		assert.equal(doctor.java, true);
		assert.equal(doctor.epubcheckSource, "managed");
		assert.match(doctor.epubcheckVersion, /5\.3\.0/);

		const explicitJar = join(workspace, "explicit.jar");
		writeFileSync(explicitJar, "explicit synthetic jar\n");
		const explicitDoctor = jsonOutput(
			runWithEnvironment(
				python,
				[script("file-conversion", "file-conversion.py"), "doctor", "--json"],
				{
					EPUBCHECK_JAR: explicitJar,
					PATH: `${fakeBin}:${process.env.PATH}`,
					PI_CODING_AGENT_DIR: agentDirectory,
				},
			),
		);
		assert.equal(explicitDoctor.epubcheckSource, "explicit-jar");

		const pathDoctor = jsonOutput(
			runWithEnvironment(
				python,
				[script("file-conversion", "file-conversion.py"), "doctor", "--json"],
				{ PATH: `${fakeBin}:${process.env.PATH}`, PI_CODING_AGENT_DIR: join(workspace, "empty-agent") },
			),
		);
		assert.equal(pathDoctor.epubcheckSource, "path");

		const checksumFailure = runWithEnvironment(
			python,
			[
				script("file-conversion", "file-conversion.py"),
				"install-epubcheck",
				"--tools-dir",
				join(workspace, "checksum-tools"),
				"--archive",
				archive,
				"--expected-sha256",
				"0".repeat(64),
			],
			{ PATH: `${fakeBin}:${process.env.PATH}` },
			1,
		);
		assert.match(checksumFailure.stderr, /SHA-256 mismatch/);

		const unsafeArchive = join(workspace, "unsafe.zip");
		const archiveWriter = join(workspace, "write-unsafe-archive.py");
		writeFileSync(
			archiveWriter,
			"import sys, zipfile\nwith zipfile.ZipFile(sys.argv[1], 'w') as archive:\n archive.writestr('epubcheck-5.3.0/epubcheck.jar', 'jar')\n archive.writestr('../escape', 'unsafe')\n",
		);
		run(python, [archiveWriter, unsafeArchive]);
		const unsafeFailure = runWithEnvironment(
			python,
			[
				script("file-conversion", "file-conversion.py"),
				"install-epubcheck",
				"--tools-dir",
				join(workspace, "unsafe-tools"),
				"--archive",
				unsafeArchive,
				"--expected-sha256",
				sha256(unsafeArchive),
			],
			{ PATH: `${fakeBin}:${process.env.PATH}` },
			1,
		);
		assert.match(unsafeFailure.stderr, /unsafe or unexpected path/);

		const wrongVersionBin = join(workspace, "wrong-version-bin");
		mkdirSync(wrongVersionBin);
		writeFileSync(
			join(wrongVersionBin, "java"),
			"#!/usr/bin/env bash\nif [[ \"$1\" == \"--version\" ]]; then echo 'openjdk 17'; else echo 'EPUBCheck v4.2.6'; fi\n",
		);
		chmodSync(join(wrongVersionBin, "java"), 0o755);
		const wrongVersion = runWithEnvironment(
			python,
			[
				script("file-conversion", "file-conversion.py"),
				"install-epubcheck",
				"--tools-dir",
				join(workspace, "wrong-version-tools"),
				"--archive",
				archive,
				"--expected-sha256",
				sha256(archive),
			],
			{ PATH: `${wrongVersionBin}:/usr/bin:/bin` },
			1,
		);
		assert.match(wrongVersion.stderr, /did not report expected version 5\.3\.0/);

		const pythonExecutable = spawnSync("which", [python], { encoding: "utf8" }).stdout.trim();
		const emptyBin = join(workspace, "empty-bin");
		mkdirSync(emptyBin);
		const missingJava = runWithEnvironment(
			pythonExecutable,
			[script("file-conversion", "file-conversion.py"), "install-epubcheck", "--tools-dir", join(workspace, "no-java")],
			{ PATH: emptyBin },
			1,
		);
		assert.match(missingJava.stderr, /working Java runtime is required/);

		const source = join(workspace, "validation.md");
		writeFileSync(source, "# Validation\n\nText.\n");
		const runDirectory = join(workspace, "validation-run");
		run(python, [
			script("file-conversion", "file-conversion.py"),
			"convert",
			source,
			"--to",
			"epub",
			"--output",
			runDirectory,
		]);
		const warningValidation = jsonOutput(
			runWithEnvironment(
				python,
				[script("file-conversion", "file-conversion.py"), "validate", runDirectory],
				{ PATH: `${fakeBin}:${process.env.PATH}`, PI_CODING_AGENT_DIR: agentDirectory },
			),
		);
		assert.match(warningValidation.warnings.join("\n"), /TEST-001: synthetic finding/);
		const errorValidation = runWithEnvironment(
			python,
			[script("file-conversion", "file-conversion.py"), "validate", runDirectory],
			{
				FAKE_EPUBCHECK_SEVERITY: "ERROR",
				PATH: `${fakeBin}:${process.env.PATH}`,
				PI_CODING_AGENT_DIR: agentDirectory,
			},
			1,
		);
		assert.match(errorValidation.stdout, /TEST-001: synthetic finding/);
	});
});

test("Markdown to EPUB handles fallbacks and rejects invalid covers", () => {
	withWorkspace((workspace) => {
		const pandoc = spawnSync("pandoc", ["--version"], { encoding: "utf8" });
		if (pandoc.status !== 0) return;
		const source = join(workspace, "fallback-title.md");
		writeFileSync(source, "## Section only\n\nReadable text.\n");
		const runDirectory = join(workspace, "fallback-run");
		const result = jsonOutput(
			run(python, [
				script("file-conversion", "file-conversion.py"),
				"convert",
				source,
				"--to",
				"epub",
				"--output",
				runDirectory,
			]),
		);
		assert.equal(result.needsReview, 1);
		const epub = join(runDirectory, "converted", "fallback-title.epub");
		const packageDocument = run("unzip", ["-p", epub, "EPUB/content.opf"]).stdout;
		assert.match(packageDocument, /fallback-title/);
		assert.match(packageDocument, /en-US/);
		assert.match(readFileSync(join(runDirectory, "warnings.md"), "utf8"), /no level-one heading/);
		assert.equal(
			jsonOutput(run(python, [script("file-conversion", "file-conversion.py"), "validate", runDirectory])).valid,
			true,
		);

		const invalidCover = join(workspace, "cover.gif");
		writeFileSync(invalidCover, "GIF89a");
		const invalidRun = join(workspace, "invalid-cover-run");
		runFailure(
			python,
			[
				script("file-conversion", "file-conversion.py"),
				"convert",
				source,
				"--to",
				"epub",
				"--output",
				invalidRun,
				"--cover",
				invalidCover,
			],
			/cover must be a baseline JPEG or PNG/,
		);
		assert.equal(existsSync(invalidRun), false);

		const jpegCover = join(workspace, "cover.jpg");
		writeFileSync(
			jpegCover,
			Buffer.from(
				"/9j/4AAQSkZJRgABAgAAZABkAAD/7AARRHVja3kAAQAEAAAAMAAA/+4ADkFkb2JlAGTAAAAAAf/bAIQACQYGBgcGCQcHCQ0IBwgNDwsJCQsPEQ4ODw4OERENDg4ODg0RERQUFhQUERoaHBwaGiYmJiYmKysrKysrKysrKwEJCAgJCgkMCgoMDwwODA8TDg4ODhMVDg4PDg4VGhMRERERExoXGhYWFhoXHR0aGh0dJCQjJCQrKysrKysrKysr/8AAEQgAjACMAwEiAAIRAQMRAf/EAF4AAQEBAAAAAAAAAAAAAAAAAAABBwEBAQAAAAAAAAAAAAAAAAAAAAIQAAEDAwIHAQEAAAAAAAAAAADwAREhYaExkUFRcYGxwdHh8REBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AyGFEjHaBS2fDDs2zkhKmBKktb7km+ZwwCnXPkLVmCTMItj6AXFxRS465/BTnkAJvkLkJe+7AKKoi2AtRS2zuAWsCb5GOlBN8gKfmuGHZ8MFqIth3ALmFoFwbwKWyAlTAp17uKqBvgBD8sM4fTjhvAhkzhaRkBMKBrfs7jGPIpzy7gFrAqnC0C0gB0EWwBDW2cBVQwm+QtPpa3wBO3sVvszCnLAhkzgL5/RLf13cLQd8/AGlu0Cb5HTx9KuAEieGJEdcehS3eRTp2ATdt3CpIm+QtZwAhROXFeb7swp/ahaM3kBE/jSIUBc/AWrgBN8uNFAl+b7sAXFxFn2YLUU5Ns7gFX8C4ib+hN8gFWXwK3bZglxEJm+gKdciLPsFV/TClsgJUwKJ5FVA7tvIFrfZhVfGJDcsCKaYgAqv6YRbE+RWOWBtu7+AL3yRalXLyKqAIIfk+zARbDgFyEsncYwJvlgFRW+GEWntIi2P0BooyFxcNr8Ep3+ANLbMO+QyhvbiqdgC0kVvgUUiLYgBS2QtPbiVI1/sgOmG9uO+Y8DW+7jS2zAOnj6O2BndwuIAUtkdRN8gFoK3wwXMQyZwHVbClsuNLd4E3yAUR6FVDBR+BafQGt93LVMxJTv8ABts4CVLhcfYWsCb5kC9/BHdU8CLYFY5bMAd+eX9MGthhpbA1vu4B7+RKkaW2Yq4AQtVBBFsAJU/AuIXBhN8gGWnstefhiZyWvLAEnbYS1uzSFP6Jvn4Baxx70JKkQojLib5AVTey1jjgkKJGO0AKWyOm7N7cSpgSpAdPH0Tfd/gp1z5C1ZgKqN9J2wFxcUUuAFLZAm+QC0Fb4YUVRFsAOvj4KW2dwtYE3yAWk/wS/PLMKfmuGHZ8MAXF/Ja32Yi5haAKWz4Ydm2cSpgU693Atb7km+Zwwh+WGcPpxw3gAkzCLY+iYUDW/Z3Adc/gpzyFrAqnALkJe+7DoItgAtRS2zuKqGE3yAx0oJvkdvYrfZmALURbDuL5/RLf13cAuDeBS2RpbtAm+QFVA3wR+3fUtFHoBDJnC0jIXH0HWsgMY8inPLuOkd9chp4z20ALQLSA8cI9jYAIa2zjzjBd8gRafS1vgiUho/kAKcsCGTOGWvoOpkAtB3z8Hm8x2Ff5ADp4+lXAlIvcmwH/2Q==",
				"base64",
			),
		);
		const jpegRun = join(workspace, "jpeg-cover-run");
		const jpegResult = jsonOutput(
			run(python, [
				script("file-conversion", "file-conversion.py"),
				"convert",
				source,
				"--to",
				"epub",
				"--output",
				jpegRun,
				"--cover",
				jpegCover,
			]),
		);
		assert.equal(jpegResult.failed, 0);
		assert.equal(jsonOutput(run(python, [script("file-conversion", "file-conversion.py"), "validate", jpegRun])).valid, true);

		const missingImageSource = join(workspace, "missing-image.md");
		writeFileSync(missingImageSource, "# Chapter\n\n![Missing](does-not-exist.png)\n");
		const missingImageRun = join(workspace, "missing-image-run");
		const missingImageResult = jsonOutput(
			run(python, [
				script("file-conversion", "file-conversion.py"),
				"convert",
				missingImageSource,
				"--to",
				"epub",
				"--output",
				missingImageRun,
			]),
		);
		assert.equal(missingImageResult.failed, 1);
		assert.match(readFileSync(join(missingImageRun, "conversion_manifest.csv"), "utf8"), /referenced local image is missing/);
	});
});

test("literature extraction rejects scaffolds and accepts authored deliverables", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "study.md");
		writeFileSync(source, "# Study\n\nThe study reports a 12 percent increase.\n");
		const sourceHash = sha256(source);
		const runDirectory = join(workspace, "literature");
		run(python, [script("literature-extraction", "literature-extraction.py"), "init", source, "--output", runDirectory]);
		const pending = jsonOutput(
			run(python, [script("literature-extraction", "literature-extraction.py"), "next", runDirectory]),
		);
		const extractionPath = join(runDirectory, "working", "items.json");
		writeFileSync(
			extractionPath,
			`${JSON.stringify([
				{
					item_type: "finding",
					text: "The study reports a 12 percent increase.",
					direct_quotes: "The study reports a 12 percent increase.",
					locator: "Study",
					interpretation: "explicit",
					confidence: "high",
					notes: null,
				},
				{
					item_type: "definition",
					text: "Increase means a reported twelve percent change in the measured outcome.",
					direct_quotes: "12 percent increase",
					locator: "Study",
					interpretation: "explicit",
					confidence: "high",
					notes: null,
				},
				{
					item_type: "connection",
					text: "The document links the outcome change to the broader study claim.",
					direct_quotes: "The study reports a 12 percent increase.",
					locator: "Study",
					interpretation: "inferred",
					confidence: "medium",
					notes: null,
				},
				{
					item_type: "author",
					text: "The provided content does not identify an author.",
					direct_quotes: null,
					locator: "Study",
					interpretation: "unclear",
					confidence: "high",
					notes: "Author description is limited to the provided source text.",
				},
			])}\n`,
		);
		run(python, [
			script("literature-extraction", "literature-extraction.py"),
			"record",
			runDirectory,
			"--doc-id",
			pending.documentId,
			"--extraction-file",
			extractionPath,
		]);
		run(python, [script("literature-extraction", "literature-extraction.py"), "build", runDirectory, "--no-claim-clusters"]);
		const evidenceRows = parseCsvRows(readFileSync(join(runDirectory, "evidence_table.csv"), "utf8"));
		assert.deepEqual(evidenceRows[0], [
			"document_id",
			"source_path",
			"source_title",
			"item_type",
			"item_text",
			"direct_quotes",
			"locator",
			"interpretation",
			"confidence",
			"notes",
		]);
		assert.match(readFileSync(join(runDirectory, "evidence_table.csv"), "utf8"), /connection/);
		assert.match(readFileSync(join(runDirectory, "evidence_table.csv"), "utf8"), /author/);
		assert.equal(existsSync(join(runDirectory, "evidence_table.xlsx")), false);
		assert.equal(existsSync(join(runDirectory, "key_terms.md")), true);
		runFailure(
			python,
			[script("literature-extraction", "literature-extraction.py"), "validate", runDirectory],
			/unresolved placeholder/,
		);
		authorFiles(runDirectory, ["literature_summary.md", "claims_matrix.md", "key_terms.md", "research_gaps.md", "citation_notes.md"]);
		const validation = jsonOutput(
			run(python, [script("literature-extraction", "literature-extraction.py"), "validate", runDirectory]),
		);
		assert.equal(validation.valid, true);
		assert.equal(sha256(source), sourceHash);
		runFailure(
			python,
			[script("literature-extraction", "literature-extraction.py"), "init", source, "--output", runDirectory],
			/output already exists/,
		);
		writeFileSync(source, "# Study\n\nChanged after initialization.\n");
		runFailure(
			python,
			[script("literature-extraction", "literature-extraction.py"), "validate", runDirectory],
			/source file hash differs/,
		);
	});
});

test("literature extraction ignores finalized ingest workspace folders by default", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "lesson");
		mkdirSync(join(source, "Ingest"), { recursive: true });
		mkdirSync(join(source, "Originals"), { recursive: true });
		mkdirSync(join(source, "Generated"), { recursive: true });
		writeFileSync(join(source, "Final Reading.md"), "# Final Reading\n\nReviewed source.\n");
		writeFileSync(join(source, "Ingest", "document.md"), "# Internal Ingest Artifact\n\nDo not extract.\n");
		writeFileSync(join(source, "Originals", "original.md"), "# Original\n\nArchived source.\n");
		writeFileSync(join(source, "Generated", "literature_summary.md"), "# Generated\n\nPrior synthesis.\n");

		const defaultRun = join(workspace, "literature-default");
		const initialized = jsonOutput(
			run(python, [script("literature-extraction", "literature-extraction.py"), "init", source, "--output", defaultRun]),
		);
		assert.equal(initialized.documents, 1);
		const defaultDocuments = readFileSync(join(defaultRun, "documents.csv"), "utf8");
		assert.match(defaultDocuments, /Final Reading\.md/);
		assert.doesNotMatch(defaultDocuments, /Internal Ingest Artifact|original\.md|literature_summary\.md/);

		const includeRun = join(workspace, "literature-include-reserved");
		const included = jsonOutput(
			run(python, [
				script("literature-extraction", "literature-extraction.py"),
				"init",
				source,
				"--output",
				includeRun,
				"--include-reserved",
			]),
		);
		assert.equal(included.documents, 4);
	});
});

test("literature extraction clusters claims across documents", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const sources = join(workspace, "sources");
		mkdirSync(sources);
		// The stub embeds letter frequencies, so adding "does not" keeps the claims
		// near-identical (high similarity) while flipping the negation hint.
		const claims = {
			"a.md": "The treatment improves patient outcomes significantly.",
			"b.md": "The treatment does not improve patient outcomes significantly.",
		};
		writeFileSync(join(sources, "a.md"), `# A\n\n${claims["a.md"]}\n`);
		writeFileSync(join(sources, "b.md"), `# B\n\n${claims["b.md"]}\n`);
		const server = await startEmbeddingsFixture(workspace);
		try {
			const runDirectory = join(workspace, "literature");
			run(python, [script("literature-extraction", "literature-extraction.py"), "init", sources, "--output", runDirectory]);
			for (let index = 0; index < 2; index += 1) {
				const pending = jsonOutput(
					run(python, [script("literature-extraction", "literature-extraction.py"), "next", runDirectory]),
				);
				const stem = pending.sourcePath.split("/").pop();
				const items = [
					{
						item_type: "claim",
						text: claims[stem],
						direct_quotes: null,
						locator: "para 1",
						interpretation: "explicit",
						confidence: "high",
						notes: null,
					},
				];
				const extractionPath = join(workspace, `items-${pending.documentId}.json`);
				writeFileSync(extractionPath, JSON.stringify(items));
				run(python, [
					script("literature-extraction", "literature-extraction.py"),
					"record",
					runDirectory,
					"--doc-id",
					pending.documentId,
					"--extraction-file",
					extractionPath,
				]);
			}
			const built = jsonOutput(
				runWithEnvironment(
					python,
					[
						script("literature-extraction", "literature-extraction.py"),
						"build",
						runDirectory,
						"--claim-cluster-threshold",
						"0.8",
					],
					{ FORGE_EMBEDDINGS_URL: server.url, FORGE_EMBEDDINGS_MODEL: "stub" },
				),
			);
			assert.equal(built.claimClusters.enabled, true);
			assert.equal(built.claimClusters.itemCount, 2);
			assert.equal(built.claimClusters.crossDocumentGroups, 1);
			assert.equal(built.claimClusters.possibleContradictions, 1);
			const worksheet = readFileSync(join(runDirectory, "claim_clusters.md"), "utf8");
			assert.match(worksheet, /possible contradiction/);
			assert.match(readFileSync(join(runDirectory, "claim_clusters.csv"), "utf8"), /negation_hint/);
			// The optional worksheet does not block validation.
			authorFiles(runDirectory, ["literature_summary.md", "claims_matrix.md", "key_terms.md", "research_gaps.md", "citation_notes.md"]);
			assert.equal(
				jsonOutput(run(python, [script("literature-extraction", "literature-extraction.py"), "validate", runDirectory])).valid,
				true,
			);
		} finally {
			await server.close();
		}
	});
});

test("personal admin and report output enforce authored deliverables", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "notice.txt");
		writeFileSync(source, "Invoice 123 is due on 2026-07-01. Call 555-0100.\n");
		const adminRun = join(workspace, "admin");
		run(python, [script("personal-admin", "personal-admin.py"), "init", source, "--output", adminRun]);
		const pending = jsonOutput(run(python, [script("personal-admin", "personal-admin.py"), "next", adminRun]));
		const factsPath = join(adminRun, "working", "facts.json");
		writeFileSync(
			factsPath,
			`${JSON.stringify([
				{
					fact_type: "deadline",
					text: "Invoice 123 is due on 2026-07-01.",
					value: "123",
					due_date: "2026-07-01",
					locator: "line 1",
					confidence: "high",
					notes: null,
				},
			])}\n`,
		);
		run(python, [
			script("personal-admin", "personal-admin.py"),
			"record",
			adminRun,
			"--doc-id",
			pending.documentId,
			"--facts-file",
			factsPath,
		]);
		run(python, [script("personal-admin", "personal-admin.py"), "build", adminRun]);
		runFailure(python, [script("personal-admin", "personal-admin.py"), "validate", adminRun], /unresolved placeholder/);
		authorFiles(adminRun, ["admin_summary.md", "next_steps.md"]);
		assert.equal(jsonOutput(run(python, [script("personal-admin", "personal-admin.py"), "validate", adminRun])).valid, true);

		const reportRun = join(workspace, "report");
		run(python, [
			script("report-output", "report-output.py"),
			"init",
			join(adminRun, "extracted_facts.csv"),
			"--output",
			reportRun,
			"--detail",
			"brief",
		]);
		runFailure(python, [script("report-output", "report-output.py"), "validate", reportRun], /unresolved placeholder/);
		authorFiles(reportRun, ["executive_summary.md", "assumptions_and_limits.md"]);
		assert.equal(jsonOutput(run(python, [script("report-output", "report-output.py"), "validate", reportRun])).valid, true);
	});
});

test("spreadsheet inspection and row enrichment are reproducible", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "data.csv");
		writeFileSync(source, "name,amount\nalpha,10\nbeta,20\n");
		const sourceHash = sha256(source);
		const profileRun = join(workspace, "profile");
		run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "inspect", source, "--output", profileRun]);
		assert.match(readFileSync(join(profileRun, "transform_log.md"), "utf8"), /No data transformations were performed/);

		const rowRun = join(workspace, "rows");
		run(python, [
			script("spreadsheet-analysis", "spreadsheet-analysis.py"),
			"row-init",
			source,
			"--output",
			rowRun,
			"--column",
			"Review",
		]);
		for (const value of ["reviewed alpha", "reviewed beta"]) {
			const pending = jsonOutput(run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "row-next", rowRun]));
			const valuePath = join(workspace, `value-${pending.rowId}.txt`);
			writeFileSync(valuePath, `${value}\n`);
			run(python, [
				script("spreadsheet-analysis", "spreadsheet-analysis.py"),
				"row-record",
				rowRun,
				"--row-id",
				String(pending.rowId),
				"--value-file",
				valuePath,
			]);
		}
		run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "row-finalize", rowRun]);
		assert.equal(jsonOutput(run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "validate", rowRun])).valid, true);
		assert.equal(sha256(source), sourceHash);
		runFailure(
			python,
			[script("spreadsheet-analysis", "spreadsheet-analysis.py"), "row-finalize", rowRun],
			/output already exists/,
		);
	});
});

test("spreadsheet cluster groups similar rows for review", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const source = join(workspace, "people.csv");
		// The stub embeds letter frequencies, so reordered names share a vector
		// (a realistic fuzzy-linkage case) while distinct names do not.
		writeFileSync(
			source,
			"name,city\nJohn Smith,Boston\nSmith John,Boston\nJane Doe,Reno\nDoe Jane,Reno\nBob,Austin\n",
		);
		const sourceHash = sha256(source);
		const server = await startEmbeddingsFixture(workspace);
		try {
			const runDirectory = join(workspace, "cluster");
			const result = jsonOutput(
				runWithEnvironment(
					python,
					[
						script("spreadsheet-analysis", "spreadsheet-analysis.py"),
						"cluster",
						source,
						"--output",
						runDirectory,
						"--columns",
						"name",
						"--threshold",
						"0.95",
					],
					{ FORGE_EMBEDDINGS_URL: server.url, FORGE_EMBEDDINGS_MODEL: "stub" },
				),
			);
			assert.equal(result.groupedRows, 5);
			assert.equal(result.multiRowGroupCount, 2);
			const clusters = readFileSync(join(runDirectory, "clusters.csv"), "utf8");
			assert.match(clusters, /cluster_id,group_size,is_representative,source_row,similarity_to_representative,key_text/);
			const groups = readFileSync(join(runDirectory, "cluster_groups.md"), "utf8");
			assert.match(groups, /Multi-row groups: 2/);
			assert.match(groups, /John Smith/);
			const run = JSON.parse(readFileSync(join(runDirectory, "cluster_run.json"), "utf8"));
			assert.equal(run.source.sha256, sourceHash);
			assert.equal(run.columns[0], "name");
			assert.equal(sha256(source), sourceHash);
			runFailure(
				python,
				[
					script("spreadsheet-analysis", "spreadsheet-analysis.py"),
					"cluster",
					source,
					"--output",
					runDirectory,
					"--columns",
					"name",
				],
				/output already exists/,
			);
		} finally {
			await server.close();
		}

		const missingEndpoint = join(workspace, "cluster-missing");
		runFailure(
			python,
			[
				script("spreadsheet-analysis", "spreadsheet-analysis.py"),
				"cluster",
				source,
				"--output",
				missingEndpoint,
				"--columns",
				"name",
				"--embeddings-url",
				"http://127.0.0.1:1/v1/embeddings",
			],
			/embeddings endpoint unavailable/,
		);
		assert.equal(existsSync(missingEndpoint), false);
	});
});

test("extracted organize-folder and spreadsheet tools expose structured execution", () => {
	withWorkspace((workspace) => {
		const requiredTools = {
			"organize-folder": ["scan_folder", "generate_manifest", "apply_manifest", "hash_files"],
			"spreadsheet-analysis": ["load_table", "profile_columns", "clean_columns", "export_table"],
		};
		for (const [skill, names] of Object.entries(requiredTools)) {
			const manifest = JSON.parse(readFileSync(join(skillsRoot, skill, "manifest.json"), "utf8"));
			const available = new Set(manifest.tools.map((tool) => tool.name));
			for (const name of names) assert.equal(available.has(name), true, `${skill} manifest lists ${name}`);
		}

		const messy = join(workspace, "messy");
		mkdirSync(messy);
		const note = join(messy, "note.txt");
		writeFileSync(note, "A note to organize.\n");
		const noteHash = sha256(note);
		const hashInput = join(workspace, "hash-input.json");
		writeFileSync(hashInput, `${JSON.stringify({ paths: [note] })}\n`);
		const hashResult = jsonOutput(run(python, [script("organize-folder", "hash_files.py"), "--input", hashInput]));
		assert.equal(hashResult.status, "ok");
		assert.equal(hashResult.data.files[0].sha256, noteHash);
		assert.match(hashResult.data.files[0].fingerprint, /^fp:/);

		const organizeRun = join(workspace, "organize-run");
		const scanInput = join(workspace, "scan-input.json");
		writeFileSync(scanInput, `${JSON.stringify({ target: messy, output: organizeRun, fullHash: true, noEmbeddings: true })}\n`);
		const scanResult = jsonOutput(run(python, [script("organize-folder", "scan_folder.py"), "--input", scanInput]));
		assert.equal(scanResult.status, "ok");
		assert.equal(existsSync(join(organizeRun, "manifest.csv")), true);

		const manifestInput = join(workspace, "manifest-input.json");
		writeFileSync(manifestInput, `${JSON.stringify({ runDirectory: organizeRun })}\n`);
		const manifestResult = jsonOutput(run(python, [script("organize-folder", "generate_manifest.py"), "--input", manifestInput]));
		assert.equal(manifestResult.status, "ok");
		assert.equal(manifestResult.data.plan.valid, true);
		assert.equal(existsSync(join(organizeRun, "plan_report.md")), true);

		const applyResult = jsonOutput(run(python, [script("organize-folder", "apply_manifest.py"), "--input", manifestInput]));
		assert.equal(applyResult.status, "ok");
		assert.equal(applyResult.data.summary.moved, 1);
		assert.equal(existsSync(join(messy, "Documents", "note.txt")), true);

		const source = join(workspace, "data.csv");
		writeFileSync(source, "name,amount,empty\n alpha ,10,N/A\n,,\n beta,20,missing\n");
		const sourceHash = sha256(source);
		const loadInput = join(workspace, "load-input.json");
		writeFileSync(loadInput, `${JSON.stringify({ input: source, previewRows: 2 })}\n`);
		const loadResult = jsonOutput(run(python, [script("spreadsheet-analysis", "load_table.py"), "--input", loadInput]));
		assert.equal(loadResult.status, "ok");
		assert.deepEqual(loadResult.data.sheets[0].headers, ["name", "amount", "empty"]);

		const profileInput = join(workspace, "profile-input.json");
		writeFileSync(profileInput, `${JSON.stringify({ input: source, maxCategories: 5 })}\n`);
		const profileResult = jsonOutput(run(python, [script("spreadsheet-analysis", "profile_columns.py"), "--input", profileInput]));
		assert.equal(profileResult.status, "ok");
		assert.equal(profileResult.data.profile.sheets[0].columns[1].header, "amount");

		const cleaned = join(workspace, "cleaned.csv");
		const cleanInput = join(workspace, "clean-input.json");
		writeFileSync(
			cleanInput,
			`${JSON.stringify({
				input: source,
				output: cleaned,
				operations: [
					{ op: "trim", columns: ["name"] },
					{ op: "normalize_blanks", columns: ["empty"], tokens: ["N/A", "missing"] },
					{ op: "drop_blank_rows" },
					{ op: "rename", from: "amount", to: "Amount" },
					{ op: "drop_columns", columns: ["empty"] },
				],
			})}\n`,
		);
		const cleanResult = jsonOutput(run(python, [script("spreadsheet-analysis", "clean_columns.py"), "--input", cleanInput]));
		assert.equal(cleanResult.status, "ok");
		assert.equal(sha256(source), sourceHash);
		assert.equal(readFileSync(cleaned, "utf8"), "name,Amount\nalpha,10\nbeta,20\n");

		const exported = join(workspace, "cleaned.json");
		const exportInput = join(workspace, "export-input.json");
		writeFileSync(exportInput, `${JSON.stringify({ input: cleaned, output: exported })}\n`);
		const exportResult = jsonOutput(run(python, [script("spreadsheet-analysis", "export_table.py"), "--input", exportInput]));
		assert.equal(exportResult.status, "ok");
		assert.deepEqual(JSON.parse(readFileSync(exported, "utf8")), [
			{ name: "alpha", Amount: "10" },
			{ name: "beta", Amount: "20" },
		]);
	});
});

test("document ingest, coding, and web collection expose review and safety boundaries", () => {
	withWorkspace((workspace) => {
		const document = join(workspace, "document.md");
		writeFileSync(document, "# Source document\n\nSource-backed content.\n");
		const documentHash = sha256(document);
		const ingestRun = join(workspace, "ingest");
		const ingest = jsonOutput(
			run("node", [script("document-ingest", "document-ingest.mjs"), "prepare", document, "--output", ingestRun]),
		);
		assert.equal(ingest.documents, 1);
		runFailure("node", [script("document-ingest", "document-ingest.mjs"), "validate", ingestRun], /model review is not marked complete/);
		const hintedValidation = jsonOutput(
			run("node", [script("document-ingest", "document-ingest.mjs"), "validate", ingestRun, "--fix-hints", "--json"], 1),
		);
		assert.equal(hintedValidation.issues[0].code, "review_incomplete");
		const packet = jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "next-review", ingestRun]));
		assert.equal(packet.complete, false);
		assert.equal(packet.documentId, firstManifestRow(ingestRun).document_id);
		assert.deepEqual(packet.allowedValues.evidenceOrigins, ["embedded-metadata", "document-text", "filename", "user-provided"]);
		const reviewFile = join(workspace, "review.json");
		writeFileSync(
			reviewFile,
			`${JSON.stringify({
				documentId: packet.documentId,
				fields: {
					title: { value: "Source Document", origin: "document-text", confidence: "high", locator: "document.md:1" },
					author: null,
					date: null,
					source: null,
				},
				finalOutput: {
					filename: "Source Document.md",
					namingReason: "Uses the reviewed document title.",
				},
				reviewNotes: ["Reviewed structure and title."],
			})}\n`,
		);
		const recorded = jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "record-review", ingestRun, "--review-file", reviewFile]));
		assert.equal(recorded.status, "success");
		assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "validate", ingestRun])).valid, true);
		assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "status", ingestRun])).counts.success, 1);
		assert.equal(sha256(document), documentHash);

		const repository = join(workspace, "repository");
		mkdirSync(repository);
		writeFileSync(join(repository, "package.json"), '{"scripts":{"check":"node --check index.js"}}\n');
		writeFileSync(join(repository, "index.js"), "console.log('ok');\n");
		const codingRun = join(workspace, "coding");
		run("node", [script("coding", "coding.mjs"), "inspect", repository, "--output", codingRun]);
		runFailure("node", [script("coding", "coding.mjs"), "validate", codingRun], /missing required artifact/);
		writeFileSync(
			join(codingRun, "change_summary.md"),
			"# Change Summary\n\n## Summary\nNone.\n\n## Motivation\nSmoke test.\n\n## Files changed\nNone.\n\n## Verification\nLocal.\n\n## Follow-ups & uncertainties\nNone.\n",
		);
		writeFileSync(join(codingRun, "run_log.md"), "# Run Log\n\n- Inspected repository.\n");
		assert.equal(jsonOutput(run("node", [script("coding", "coding.mjs"), "validate", codingRun])).valid, true);

		const doctor = jsonOutput(run("node", [script("web-collection", "web-collection.mjs"), "doctor", "--json"]));
		assert.equal(doctor.capabilities.httpCollect, true);
		assert.equal(doctor.capabilities.searxngSearch, false);
		assert.match(doctor.searxng.detail, /no SearXNG URL configured/);
		const rejectedWebRun = join(workspace, "web-rejected");
		const rejected = jsonOutput(
			run("node", [
				script("web-collection", "web-collection.mjs"),
				"collect",
				"http://127.0.0.1/test",
				"--output",
				rejectedWebRun,
			]),
		);
		assert.equal(rejected.counts.failed, 1);
		assert.match(readFileSync(join(rejectedWebRun, "failed_downloads.csv"), "utf8"), /refused loopback or metadata host/);
		assert.equal(
			jsonOutput(run("node", [script("web-collection", "web-collection.mjs"), "validate", rejectedWebRun])).valid,
			true,
		);

		const webRun = join(workspace, "web-validation");
		mkdirSync(webRun);
		writeFileSync(
			join(webRun, "web_manifest.csv"),
			"resource_id,source_url,final_url,access_date,status,http_status,content_type,title,filename,output_path,sha256,byte_size,capture_method,rendered,duplicate_of,error\n",
		);
		writeFileSync(join(webRun, "web_manifest.json"), '{"schemaVersion":1,"resources":[]}\n');
		writeFileSync(join(webRun, "failed_downloads.csv"), "source_url,status,http_status,reason,access_date\n");
		writeFileSync(
			join(webRun, "collection_report.md"),
			"# Collection Report\n\n## Status\n\n## Run Summary\n\n## Sources\n\n## Captures\n\n## Duplicates\n\n## Failures and Blocks\n\n## Search\n\n## Review\n",
		);
		assert.equal(jsonOutput(run("node", [script("web-collection", "web-collection.mjs"), "validate", webRun])).valid, true);
	});
});

test("web-research deep creates validated provenance-backed artifacts", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startDeepResearchFixture();
		try {
			const queries = join(workspace, "queries.txt");
			writeFileSync(queries, "seed alpha\nseed beta\nseed alpha\n");
			const deepRun = join(workspace, "deep-run");
			const result = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"deep",
						"--question",
						"How should deep web research preserve provenance?",
						"--query-file",
						queries,
						"--output",
						deepRun,
						"--searxng",
						fixture.searxng,
						"--max-iterations",
						"2",
						"--limit",
						"3",
						"--read-count",
						"3",
						"--no-render",
					],
					{
						FORGE_BASE_CHAT_URL: fixture.chat,
						FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1",
					},
				),
			);
			assert.equal(result.valid, true);
			assert.equal(result.queries, 3);
			assert.equal(result.sources, 3);
			assert.equal(result.evidence, 3);
			assert.equal(result.claims, 1);
			assert.equal(jsonOutput(run("node", [script("web-research", "web-research.mjs"), "validate", deepRun])).valid, true);
			const sourceIndex = JSON.parse(readFileSync(join(deepRun, "source_index.json"), "utf8"));
			assert.equal(sourceIndex.sources.length, 3);
			assert.equal(sourceIndex.sources[0].searchOrigins.length, 2);
			assert.match(readFileSync(join(deepRun, "deep_research_report.md"), "utf8"), /cl-0001/);
			assert.match(readFileSync(join(deepRun, "sources.md"), "utf8"), /src-[a-f0-9]{12}/);
			assert.match(readFileSync(join(deepRun, "web_manifest.csv"), "utf8"), /resource_id,source_url,final_url/);

			const evidencePath = join(deepRun, "evidence_items.jsonl");
			const originalEvidence = readFileSync(evidencePath, "utf8");
			const evidenceRows = originalEvidence
				.trim()
				.split(/\r?\n/)
				.map((line) => JSON.parse(line));
			evidenceRows[0].directQuote = "This quote is not in the archived source text.";
			writeFileSync(evidencePath, evidenceRows.map((row) => JSON.stringify(row)).join("\n") + "\n");
			runFailure("node", [script("web-research", "web-research.mjs"), "validate", deepRun], /direct quote was not found/);

			writeFileSync(evidencePath, originalEvidence);
			const claimsPath = join(deepRun, "claim_register.jsonl");
			const originalClaims = readFileSync(claimsPath, "utf8");
			const claimRows = originalClaims
				.trim()
				.split(/\r?\n/)
				.map((line) => JSON.parse(line));
			claimRows[0].sourceIds = [];
			writeFileSync(claimsPath, claimRows.map((row) => JSON.stringify(row)).join("\n") + "\n");
			runFailure("node", [script("web-research", "web-research.mjs"), "validate", deepRun], /has no sourceIds/);
		} finally {
			await fixture.close();
		}
	});
});

test("web-research deep applies whole-run budgets and records budget gaps", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startDeepResearchFixture();
		try {
			const queries = join(workspace, "many-queries.txt");
			writeFileSync(
				queries,
				Array.from({ length: 10 }, (_, index) => `many budget query ${index + 1}`).join("\n") + "\n",
			);
			const deepRun = join(workspace, "deep-budgeted");
			const result = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"deep",
						"--question",
						"How should deep web research stay bounded?",
						"--query-file",
						queries,
						"--output",
						deepRun,
						"--searxng",
						fixture.searxng,
						"--no-render",
					],
					{
						FORGE_BASE_CHAT_URL: fixture.chat,
						FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1",
					},
				),
			);
			assert.equal(result.valid, true);
			assert.equal(result.queries, 6);
			assert.equal(result.sources, 12);
			assert.ok(result.modelCalls <= 16);
			const runInfo = JSON.parse(readFileSync(join(deepRun, "research_run.json"), "utf8"));
			assert.equal(runInfo.options.maxQueries, 6);
			assert.equal(runInfo.options.maxSources, 12);
			assert.equal(runInfo.options.maxModelCalls, 16);
			assert.ok(runInfo.budgetEvents.some((event) => event.reason === "maxSources" || event.reason === "maxQueries"));
			const gaps = readFileSync(join(deepRun, "gap_log.jsonl"), "utf8");
			assert.match(gaps, /Deep research stopped early/);
		} finally {
			await fixture.close();
		}
	});
});

test("web-research deep repairs invalid direct quote candidates and skips checkpoints", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startDeepResearchFixture();
		try {
			const deepRun = join(workspace, "deep-quote-repair");
			const result = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"deep",
						"--question",
						"How should quote validation work?",
						"--query",
						"mismatch quote",
						"--query",
						"checkpoint page",
						"--output",
						deepRun,
						"--searxng",
						fixture.searxng,
						"--max-iterations",
						"1",
						"--limit",
						"2",
						"--read-count",
						"2",
						"--no-render",
					],
					{
						FORGE_BASE_CHAT_URL: fixture.chat,
						FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1",
					},
				),
			);
			assert.equal(result.valid, true);
			const evidenceRows = readFileSync(join(deepRun, "evidence_items.jsonl"), "utf8")
				.trim()
				.split(/\r?\n/)
				.filter(Boolean)
				.map((line) => JSON.parse(line));
			assert.equal(evidenceRows.length, 1);
			assert.equal(evidenceRows[0].directQuote, null);
			const sourceIndex = JSON.parse(readFileSync(join(deepRun, "source_index.json"), "utf8"));
			const checkpoint = sourceIndex.sources.find((source) => /checkpoint/i.test(source.title ?? ""));
			assert.ok(checkpoint);
			assert.equal(checkpoint.status, "needs_review");
			assert.match(checkpoint.warnings.join("\n"), /skipped evidence extraction/);
			assert.equal(jsonOutput(run("node", [script("web-research", "web-research.mjs"), "validate", deepRun])).valid, true);
		} finally {
			await fixture.close();
		}
	});
});

test("web-research extension blocks duplicate same-turn deep runs and preserves failure stdout", async () => {
	const handlers = new Map();
	const tools = [];
	const pi = {
		on(event, handler) {
			handlers.set(event, [...(handlers.get(event) ?? []), handler]);
		},
		registerTool(tool) {
			tools.push(tool);
		},
	};
	webResearchExtension(pi);
	const toolCall = handlers.get("tool_call")[0];
	const turnStart = handlers.get("turn_start")[0];
	assert.ok(tools.some((tool) => tool.name === "forge_deep_web_research"));
	assert.equal(await toolCall({ type: "tool_call", toolName: "forge_deep_web_research", toolCallId: "one", input: {} }), undefined);
	const blocked = await toolCall({ type: "tool_call", toolName: "forge_deep_web_research", toolCallId: "two", input: {} });
	assert.equal(blocked.block, true);
	assert.match(blocked.reason, /Only one deep web research run/);
	await turnStart({ type: "turn_start" });
	assert.equal(await toolCall({ type: "tool_call", toolName: "forge_deep_web_research", toolCallId: "three", input: {} }), undefined);

	const message = formatRunFailure(
		["/tmp/web-research.mjs", "deep"],
		1,
		JSON.stringify({ valid: false, validationErrors: ["ev-0001 direct quote was not found in archived source text"] }),
		"Error: Could not parse CSS stylesheet\n".repeat(200),
	);
	assert.match(message, /validationErrors/);
	assert.match(message, /direct quote was not found/);
	assert.match(message, /stderr:/);
});

test("web-research quick search and read produce report artifacts", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startDeepResearchFixture();
		try {
			const searchRun = join(workspace, "quick-search");
			const search = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"search",
						"seed alpha",
						"--output",
						searchRun,
						"--searxng",
						fixture.searxng,
						"--limit",
						"2",
						"--categories",
						"general",
					],
					{},
				),
			);
			assert.equal(search.results, 2);
			const searchReport = JSON.parse(readFileSync(join(searchRun, "research_report.json"), "utf8"));
			assert.equal(searchReport.query, "seed alpha");
			assert.equal(searchReport.results[0].title, "Alpha Source");
			assert.equal(searchReport.params.categories, "general");

			const readRun = join(workspace, "quick-read");
			const read = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"read",
						`${fixture.origin}/page-alpha`,
						"--output",
						readRun,
						"--no-render",
					],
					{ FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1" },
				),
			);
			assert.equal(read.success, 1);
			const readReport = JSON.parse(readFileSync(join(readRun, "research_report.json"), "utf8"));
			assert.match(readReport.readings[0].text, /Alpha evidence quote/);
			assert.equal(readReport.readings[0].extractionMethod, "http");
		} finally {
			await fixture.close();
		}
	});
});

test("web-research acquisition writes stage artifacts, cache records, and discovery reports", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startDeepResearchFixture();
		try {
			const cacheDirectory = join(workspace, "cache");
			const searchRun = join(workspace, "structured-search");
			const search = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"search",
						"structured acquisition",
						"--output",
						searchRun,
						"--searxng",
						fixture.searxng,
						"--limit",
						"2",
					],
					{},
				),
			);
			assert.equal(search.results, 2);
			const normalizedSearchUrls = readFileSync(join(searchRun, "normalized_urls.jsonl"), "utf8")
				.trim()
				.split(/\r?\n/)
				.map((line) => JSON.parse(line));
			assert.equal(new Set(normalizedSearchUrls.map((record) => record.canonicalUrl)).size, 1);
			assert.equal(normalizedSearchUrls[0].canonicalUrl, `${fixture.origin}/page-structured`);

			const firstReadRun = join(workspace, "structured-read-1");
			const firstRead = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"read",
						`${fixture.origin}/page-structured?utm_source=tracker#fragment`,
						"--output",
						firstReadRun,
						"--cache-dir",
						cacheDirectory,
						"--mode",
						"standard",
					],
					{ FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1" },
				),
			);
			assert.equal(firstRead.success, 1);
			const firstReport = JSON.parse(readFileSync(join(firstReadRun, "research_report.json"), "utf8"));
			const firstReading = firstReport.readings[0];
			assert.equal(firstReading.extractionMethod, "http");
			assert.match(firstReading.text, /Structured acquisition evidence/);
			assert.equal(firstReading.canonicalUrl, `${fixture.origin}/page-structured`);
			assert.equal(firstReading.metadata.structured.jsonLd[0].headline, "Structured Source");
			assert.equal(firstReading.metadata.structured.nextData.props.pageProps.article.title, "Structured Source");
			assert.equal(existsSync(join(firstReadRun, firstReading.rawArtifact)), true);
			assert.equal(existsSync(join(firstReadRun, firstReading.extractedArtifact)), true);
			const firstMetrics = JSON.parse(readFileSync(join(firstReadRun, "metrics.json"), "utf8"));
			assert.equal(firstMetrics.directHttpSuccesses, 1);
			assert.equal(firstMetrics.playwrightDomFallbacks, 0);
			const firstCacheLog = readFileSync(join(firstReadRun, "cache_log.jsonl"), "utf8");
			assert.match(firstCacheLog, /"status":"miss"/);

			const secondReadRun = join(workspace, "structured-read-2");
			jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"read",
						`${fixture.origin}/page-structured`,
						"--output",
						secondReadRun,
						"--cache-dir",
						cacheDirectory,
						"--mode",
						"standard",
					],
					{ FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1" },
				),
			);
			assert.match(readFileSync(join(secondReadRun, "cache_log.jsonl"), "utf8"), /"status":"hit"/);

			const discoverRun = join(workspace, "structured-discover");
			const discovered = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"discover",
						`${fixture.origin}/page-structured`,
						"--output",
						discoverRun,
						"--no-browser",
						"--cache-dir",
						cacheDirectory,
					],
					{ FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1" },
				),
			);
			assert.equal(discovered.candidateEndpoints, 1);
			assert.equal(discovered.recommendedStrategy, "structured_static");
			const reports = readFileSync(join(discoverRun, "discovery_reports", `${discovered.domain}-${createHash("sha256").update(`${discovered.domain}:${fixture.origin}/page-structured`).digest("hex").slice(0, 12)}.json`), "utf8");
			assert.match(reports, /api\/structured-data/);

			const binaryRun = join(workspace, "binary-read");
			const binary = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"read",
						`${fixture.origin}/paper.pdf`,
						"--output",
						binaryRun,
						"--no-browser",
					],
					{ FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1" },
				),
			);
			assert.equal(binary.success, 0);
			const acquisitionLog = readFileSync(join(binaryRun, "acquisition_log.jsonl"), "utf8")
				.trim()
				.split(/\r?\n/)
				.map((line) => JSON.parse(line));
			assert.equal(acquisitionLog[0].status, "failed");
			assert.equal(acquisitionLog[0].failureType, "unsupported_format");
		} finally {
			await fixture.close();
		}
	});
});

test("web-research deep records unsafe result URLs as failed sources", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startDeepResearchFixture();
		try {
			const deepRun = join(workspace, "deep-unsafe");
			const result = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"deep",
						"unsafe lookup",
						"--output",
						deepRun,
						"--searxng",
						fixture.searxng,
						"--max-iterations",
						"1",
						"--limit",
						"1",
						"--read-count",
						"1",
						"--no-render",
					],
					{
						FORGE_BASE_CHAT_URL: fixture.chat,
					},
				),
			);
			assert.equal(result.valid, true);
			const sourceIndex = JSON.parse(readFileSync(join(deepRun, "source_index.json"), "utf8"));
			assert.equal(sourceIndex.sources.length, 1);
			assert.equal(sourceIndex.sources[0].status, "failed");
			assert.match(sourceIndex.sources[0].warnings.join("\n"), /refused loopback or metadata host/);
			assert.equal(jsonOutput(run("node", [script("web-research", "web-research.mjs"), "validate", deepRun])).valid, true);
		} finally {
			await fixture.close();
		}
	});
});

test("web-research academic dedupes works and exports RIS artifacts", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startAcademicResearchFixture();
		try {
			const academicRun = join(workspace, "academic-run");
			const result = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"academic",
						"alpha beta scholarly metadata",
						"--output",
						academicRun,
						"--providers",
						"crossref,semantic-scholar,pubmed,arxiv",
						"--limit",
						"5",
					],
					fixture.environment,
				),
			);
			assert.equal(result.valid, true);
			assert.equal(result.works, 3);
			assert.equal(result.risRecords, 3);
			assert.equal(jsonOutput(run("node", [script("web-research", "web-research.mjs"), "validate", academicRun])).valid, true);
			const works = readFileSync(join(academicRun, "works.jsonl"), "utf8")
				.trim()
				.split(/\r?\n/)
				.map((line) => JSON.parse(line));
			const alpha = works.find((work) => work.identifiers.doi === "10.1234/alpha");
			assert.ok(alpha);
			assert.equal(alpha.source_records.length, 3);
			const noAbstract = works.find((work) => work.identifiers.doi === "10.1234/noabstract");
			assert.equal(noAbstract.abstract_best, "Semantic Scholar supplies the missing abstract.");
			const arxiv = works.find((work) => work.identifiers.arxiv_id === "2401.00001");
			assert.ok(arxiv);
			assert.equal(arxiv.type, "preprint");
			assert.equal(arxiv.related_works.some((relation) => relation.relation === "preprint_published_version"), true);
			const aggregateRis = readFileSync(join(academicRun, "works.ris"), "utf8");
			assert.equal((aggregateRis.match(/^TY  - /gm) ?? []).length, 3);
			assert.equal((aggregateRis.match(/^ER  -$/gm) ?? []).length, 3);
			assert.match(aggregateRis, /^DO  - 10\.1234\/alpha$/m);
			assert.match(aggregateRis, /^AU  - Lovelace, Ada$/m);
			const risManifest = JSON.parse(readFileSync(join(academicRun, "ris_manifest.json"), "utf8"));
			assert.equal(risManifest.records.length, 3);
			for (const record of risManifest.records) assert.equal(existsSync(join(academicRun, record.risPath)), true);
		} finally {
			await fixture.close();
		}
	});
});

test("web-research academic records provider failures without blocking RIS export", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fixture = await startAcademicResearchFixture();
		try {
			const academicRun = join(workspace, "academic-failure-run");
			const result = jsonOutput(
				await runAsyncWithEnvironment(
					"node",
					[
						script("web-research", "web-research.mjs"),
						"academic",
						"alpha beta scholarly metadata",
						"--output",
						academicRun,
						"--providers",
						"crossref,doaj",
						"--limit",
						"5",
					],
					fixture.environment,
				),
			);
			assert.equal(result.valid, true);
			assert.equal(result.works, 2);
			assert.equal(result.providerErrors, 1);
			assert.equal((readFileSync(join(academicRun, "works.ris"), "utf8").match(/^TY  - /gm) ?? []).length, 2);
			const errors = readFileSync(join(academicRun, "provider_errors.jsonl"), "utf8")
				.trim()
				.split(/\r?\n/)
				.map((line) => JSON.parse(line));
			assert.equal(errors[0].provider, "doaj");
			assert.match(errors[0].error, /HTTP 500/);
		} finally {
			await fixture.close();
		}
	});
});

test("extracted web and document tools expose metadata and conversion boundaries", () => {
	withWorkspace((workspace) => {
		const requiredTools = {
			"web-collection": ["fetch_url", "archive_page", "html_to_markdown", "extract_metadata"],
			"document-ingest": ["pdf_to_markdown", "docx_to_markdown", "extract_metadata"],
		};
		for (const [skill, names] of Object.entries(requiredTools)) {
			const manifest = JSON.parse(readFileSync(join(skillsRoot, skill, "manifest.json"), "utf8"));
			const available = new Set(manifest.tools.map((tool) => tool.name));
			for (const name of names) assert.equal(available.has(name), true, `${skill} manifest lists ${name}`);
		}

		const html = join(workspace, "page.html");
		writeFileSync(html, '<!doctype html><title>Example Page</title><h1>Example</h1><a href="/source.pdf">Source</a>\n');
		const metadataInput = join(workspace, "web-metadata-input.json");
		writeFileSync(metadataInput, `${JSON.stringify({ input: html, baseUrl: "https://example.test/articles/page.html" })}\n`);
		const metadata = jsonOutput(run("node", [script("web-collection", "extract_metadata.mjs"), "--input", metadataInput]));
		assert.equal(metadata.status, "ok");
		assert.equal(metadata.data.entries[0].title, "Example Page");
		assert.deepEqual(metadata.data.entries[0].links, ["https://example.test/source.pdf"]);

		const fetchInput = join(workspace, "fetch-input.json");
		writeFileSync(fetchInput, `${JSON.stringify({ url: "http://127.0.0.1/test", output: join(workspace, "rejected-fetch") })}\n`);
		const rejectedFetch = jsonOutput(run("node", [script("web-collection", "fetch_url.mjs"), "--input", fetchInput], 1));
		assert.equal(rejectedFetch.status, "error");
		assert.equal(rejectedFetch.errors[0].code, "fetch_failed");
		assert.match(rejectedFetch.errors[0].message, /refused loopback or metadata host/);

		const pandoc = spawnSync("pandoc", ["--version"], { encoding: "utf8" });
		if (pandoc.status === 0) {
			const markdown = join(workspace, "page.md");
			const htmlInput = join(workspace, "html-input.json");
			writeFileSync(htmlInput, `${JSON.stringify({ input: html, output: markdown })}\n`);
			const htmlResult = jsonOutput(run("node", [script("web-collection", "html_to_markdown.mjs"), "--input", htmlInput]));
			assert.equal(htmlResult.status, "ok");
			assert.match(readFileSync(markdown, "utf8"), /Example/);

			const sourceMarkdown = join(workspace, "source.md");
			const docx = join(workspace, "source.docx");
			writeFileSync(sourceMarkdown, "# DOCX Source\n\nConverted body.\n");
			run("pandoc", [sourceMarkdown, "--output", docx]);
			const docxInput = join(workspace, "docx-input.json");
			const docxRun = join(workspace, "docx-run");
			writeFileSync(docxInput, `${JSON.stringify({ input: docx, output: docxRun })}\n`);
			const docxResult = jsonOutput(run("node", [script("document-ingest", "docx_to_markdown.mjs"), "--input", docxInput]));
			assert.equal(docxResult.status, "ok");
			assert.match(readFileSync(docxResult.data.documents[0].markdownPath, "utf8"), /DOCX Source|Converted body/);
		}

		const fakeBin = join(workspace, "fake-bin");
		mkdirSync(fakeBin);
		writeFileSync(
			join(fakeBin, "pdfinfo"),
			`#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdfinfo fake"; exit 0; fi
printf 'Pages: 1\\nTitle: Synthetic PDF\\nAuthor: Example Author\\n'
`,
		);
		writeFileSync(
			join(fakeBin, "pdftotext"),
			`#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdftotext fake"; exit 0; fi
printf 'Readable PDF text extracted by the fake pdftotext command for the tool wrapper test.\\n'
`,
		);
		chmodSync(join(fakeBin, "pdfinfo"), 0o755);
		chmodSync(join(fakeBin, "pdftotext"), 0o755);
		const pdf = join(workspace, "source.pdf");
		writeFileSync(pdf, "%PDF-1.4\nsynthetic\n");
		const pdfRun = join(workspace, "pdf-run");
		const pdfInput = join(workspace, "pdf-input.json");
		writeFileSync(pdfInput, `${JSON.stringify({ input: pdf, output: pdfRun, ocr: "never", ocrBackend: "local" })}\n`);
		const pdfResult = jsonOutput(
			runWithEnvironment("node", [script("document-ingest", "pdf_to_markdown.mjs"), "--input", pdfInput], {
				PATH: `${fakeBin}:${process.env.PATH}`,
			}),
		);
		assert.equal(pdfResult.status, "ok");
		assert.match(readFileSync(pdfResult.data.documents[0].markdownPath, "utf8"), /Readable PDF text/);
		const ingestMetadataInput = join(workspace, "ingest-metadata-input.json");
		writeFileSync(ingestMetadataInput, `${JSON.stringify({ input: pdfRun })}\n`);
		const ingestMetadata = jsonOutput(run("node", [script("document-ingest", "extract_metadata.mjs"), "--input", ingestMetadataInput]));
		assert.equal(ingestMetadata.status, "ok");
		assert.equal(ingestMetadata.data.documents[0].metadata.fields.title.value, "Synthetic PDF");
	});
});

test("document ingest detects FFmpeg with its supported version flag", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fakeBin = join(workspace, "fake-bin");
		mkdirSync(fakeBin);
		writeFileSync(
			join(fakeBin, "ffmpeg"),
			`#!/usr/bin/env bash
if [[ "$1" == "-version" ]]; then echo "ffmpeg fake 8.1.1"; exit 0; fi
if [[ "$1" == "--version" ]]; then echo "Unrecognized option '$1'." >&2; exit 1; fi
output="\${!#}"
printf 'synthetic audio' > "$output"
`,
		);
		chmodSync(join(fakeBin, "ffmpeg"), 0o755);
		const doctor = jsonOutput(
			runWithEnvironment(
				"node",
				[script("document-ingest", "document-ingest.mjs"), "doctor", "--json"],
				{ PATH: `${fakeBin}:${process.env.PATH}` },
			),
		);
		assert.equal(doctor.tools.ffmpeg.available, true);
		assert.equal(doctor.capabilities.ffmpegMedia, true);

		const source = join(workspace, "clip.mp4");
		writeFileSync(source, "synthetic video");
		const runDirectory = join(workspace, "media-ingest");
		const chatServer = await startChatFixture(workspace, "general");
		try {
			const prepared = jsonOutput(
				runWithEnvironment(
					"node",
					[script("document-ingest", "document-ingest.mjs"), "prepare", source, "--output", runDirectory],
					{ PATH: `${fakeBin}:${process.env.PATH}`, FORGE_BASE_CHAT_URL: chatServer.url },
				),
			);
			assert.equal(prepared.counts.needs_review, 1);
			const documentDirectoryName = firstManifestRow(runDirectory).output_directory;
			assert.equal(existsSync(join(runDirectory, documentDirectoryName, "derived", "audio.mp3")), true);
		} finally {
			await chatServer.close();
		}
	});
});

test("document ingest records cleaned transcripts without manual chunk repair", () => {
	withWorkspace((workspace) => {
		const fakeBin = join(workspace, "fake-bin");
		mkdirSync(fakeBin);
		writeFileSync(
			join(fakeBin, "ffmpeg"),
			`#!/usr/bin/env bash
if [[ "$1" == "-version" ]]; then echo "ffmpeg fake 8.1.1"; exit 0; fi
output="\${!#}"
printf 'synthetic audio' > "$output"
`,
		);
		chmodSync(join(fakeBin, "ffmpeg"), 0o755);
		const source = join(workspace, "lecture.mp3");
		writeFileSync(source, "synthetic media");
		const runDirectory = join(workspace, "media-ingest");
		const prepared = jsonOutput(
			runWithEnvironment(
				"node",
				[script("document-ingest", "document-ingest.mjs"), "prepare", source, "--output", runDirectory],
				{ PATH: `${fakeBin}:${process.env.PATH}` },
			),
		);
		assert.equal(prepared.counts.needs_review, 1);
		const packet = jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "next-review", runDirectory]));
		assert.match(packet.commands.recordTranscript, /record-transcript/);
		assert.equal(existsSync(packet.paths.derivedAudio), true);
		const transcript = join(workspace, "cleaned-transcript.md");
		writeFileSync(transcript, "# Lecture Transcript\n\nThis is the cleaned lecture transcript.\n");
		const recorded = jsonOutput(
			run("node", [
				script("document-ingest", "document-ingest.mjs"),
				"record-transcript",
				runDirectory,
				"--doc-id",
				packet.documentId,
				"--transcript",
				transcript,
				"--title",
				"Lecture Transcript",
				"--filename",
				"Lecture Transcript.md",
			]),
		);
		assert.equal(recorded.status, "success");
		assert.match(readFileSync(packet.paths.extracted, "utf8"), /cleaned lecture transcript/);
		assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "validate", runDirectory])).valid, true);
	});
});

test("document ingest categorizes folders with the base model endpoint", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const chatServer = await startChatFixture(workspace, "literature");
		try {
			const inputDirectory = join(workspace, "sources");
			mkdirSync(inputDirectory);
			writeFileSync(join(inputDirectory, "essay.txt"), "A paper about archival practice and interpretation.\n");
			const runDirectory = join(workspace, "categorized-ingest");
			const prepared = jsonOutput(
				runWithEnvironment(
					"node",
					[script("document-ingest", "document-ingest.mjs"), "prepare", inputDirectory, "--output", runDirectory],
					{ FORGE_BASE_CHAT_URL: chatServer.url },
				),
			);
			assert.equal(prepared.counts.needs_review, 1);
			const requests = readFileSync(chatServer.requestsPath, "utf8").trim().split("\n").map((line) => JSON.parse(line));
			assert.equal(requests.length, 1);
			assert.equal(requests[0].url, "/v1/chat/completions");
			assert.equal(requests[0].body.model, "code");
			assert.match(requests[0].body.messages[1].content, /essay\.txt/);
			assert.equal(firstManifestRow(runDirectory).suggested_pipeline, "literature");
		} finally {
			await chatServer.close();
		}
	});
});

test("document ingest finalizes flat folders into Ingest Originals Generated layout", () => {
	withWorkspace((workspace) => {
		const sourceDirectory = join(workspace, "flat-source");
		mkdirSync(sourceDirectory);
		mkdirSync(join(sourceDirectory, "Originals"));
		mkdirSync(join(sourceDirectory, "Generated"));
		writeFileSync(join(sourceDirectory, "lecture.txt"), "Speaker 1: Clean this transcript.\n");
		writeFileSync(join(sourceDirectory, "Originals", "previous.txt"), "already archived\n");
		writeFileSync(join(sourceDirectory, "Generated", "previous.md"), "# Prior generated note\n");
		const runDirectory = join(sourceDirectory, "Ingest");

		const prepared = jsonOutput(
			run("node", [script("document-ingest", "document-ingest.mjs"), "prepare", sourceDirectory, "--output", runDirectory]),
		);
		assert.equal(prepared.documents, 1);
		assert.equal(prepared.counts.needs_review, 1);
		const rows = completeIngestRun(runDirectory);
		assert.equal(rows.length, 1);
		const documentDirectory = join(runDirectory, rows[0].output_directory);
		const metadataPath = join(documentDirectory, "metadata.json");
		const metadata = JSON.parse(readFileSync(metadataPath, "utf8"));
		metadata.finalOutput = {
			filename: "Intro to Archival Practice Lecture Transcript.md",
			namingReason: "The source is a lecture-style transcript and the title reflects its cleaned content.",
		};
		writeFileSync(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`);
		writeFileSync(join(runDirectory, "literature_summary.md"), "# Literature Summary\n\nGenerated synthesis.\n");
		assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "validate", runDirectory])).valid, true);

		const finalized = jsonOutput(
			run("node", [script("document-ingest", "document-ingest.mjs"), "finalize", runDirectory, "--destination", sourceDirectory]),
		);
		assert.equal(finalized.layout, "flat");
		assert.equal(finalized.movedOriginals, 1);
		assert.equal(finalized.publishedMarkdown, 1);
		assert.equal(finalized.generatedArtifacts, 1);
		assert.equal(existsSync(join(sourceDirectory, "lecture.txt")), false);
		assert.equal(existsSync(join(sourceDirectory, "Originals", "lecture.txt")), true);
		assert.match(readFileSync(join(sourceDirectory, "Intro to Archival Practice Lecture Transcript.md"), "utf8"), /Clean this transcript/);
		assert.equal(existsSync(join(sourceDirectory, "Generated", "literature_summary.md")), true);
		const artifactManifest = readFileSync(join(runDirectory, "artifact_manifest.csv"), "utf8");
		assert.match(artifactManifest, /role,document_id,source_path,destination_path,sha256,created_at/);
		assert.match(artifactManifest, /original/);
		assert.match(artifactManifest, /final_markdown/);
		assert.match(artifactManifest, /generated_artifact/);
	});
});

test("document ingest finalizes structured folders and refuses unsafe finalize", () => {
	withWorkspace((workspace) => {
		const sourceDirectory = join(workspace, "structured-source");
		mkdirSync(join(sourceDirectory, "Week 1"), { recursive: true });
		mkdirSync(join(sourceDirectory, "Week 2"), { recursive: true });
		writeFileSync(join(sourceDirectory, "Week 1", "reading.txt"), "Reading one source text.\n");
		writeFileSync(join(sourceDirectory, "Week 2", "reading.txt"), "Reading two source text.\n");
		const runDirectory = join(sourceDirectory, "Ingest");
		run("node", [script("document-ingest", "document-ingest.mjs"), "prepare", sourceDirectory, "--output", runDirectory]);
		runFailure(
			"node",
			[script("document-ingest", "document-ingest.mjs"), "finalize", runDirectory, "--destination", sourceDirectory],
			/run must validate before finalize/,
		);
		completeIngestRun(runDirectory);
		assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "validate", runDirectory])).valid, true);
		const finalized = jsonOutput(
			run("node", [script("document-ingest", "document-ingest.mjs"), "finalize", runDirectory, "--destination", sourceDirectory]),
		);
		assert.equal(finalized.layout, "structured");
		assert.equal(existsSync(join(sourceDirectory, "Week 1", "reading.md")), true);
		assert.equal(existsSync(join(sourceDirectory, "Week 2", "reading.md")), true);
		assert.equal(existsSync(join(sourceDirectory, "Originals", "Week 1", "reading.txt")), true);
		assert.equal(existsSync(join(sourceDirectory, "Originals", "Week 2", "reading.txt")), true);
	});
});

test("document ingest finalize refuses generated artifact overwrite conflicts", () => {
	withWorkspace((workspace) => {
		const sourceDirectory = join(workspace, "conflict-source");
		mkdirSync(join(sourceDirectory, "Generated"), { recursive: true });
		writeFileSync(join(sourceDirectory, "note.txt"), "Source text.\n");
		writeFileSync(join(sourceDirectory, "Generated", "literature_summary.md"), "# Existing\n");
		const runDirectory = join(sourceDirectory, "Ingest");
		run("node", [script("document-ingest", "document-ingest.mjs"), "prepare", sourceDirectory, "--output", runDirectory]);
		completeIngestRun(runDirectory);
		writeFileSync(join(runDirectory, "literature_summary.md"), "# New\n");
		runFailure(
			"node",
			[script("document-ingest", "document-ingest.mjs"), "finalize", runDirectory, "--destination", sourceDirectory],
			/finalize destination already exists/,
		);
		assert.equal(existsSync(join(sourceDirectory, "note.txt")), true);
		assert.equal(existsSync(join(sourceDirectory, "Originals", "note.txt")), false);
	});
});

test("document ingest final output filenames support administrative naming and reject unsafe paths", () => {
	withWorkspace((workspace) => {
		const sourceDirectory = join(workspace, "admin-source");
		mkdirSync(sourceDirectory);
		writeFileSync(join(sourceDirectory, "scan.txt"), "Insurance claim for knee MRI at City Hospital.\n");
		const runDirectory = join(sourceDirectory, "Ingest");
		run("node", [script("document-ingest", "document-ingest.mjs"), "prepare", sourceDirectory, "--output", runDirectory]);
		const rows = completeIngestRun(runDirectory);
		const metadataPath = join(runDirectory, rows[0].output_directory, "metadata.json");
		const metadata = JSON.parse(readFileSync(metadataPath, "utf8"));
		metadata.finalOutput = {
			filename: "2026-05-03 Insurance Claim - Knee Pain - MRI - City Hospital.md",
			namingReason: "Administrative insurance claim name starts with date and includes diagnosis, procedure, and facility.",
		};
		writeFileSync(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`);
		assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "validate", runDirectory])).valid, true);
		run("node", [script("document-ingest", "document-ingest.mjs"), "finalize", runDirectory, "--destination", sourceDirectory]);
		assert.equal(existsSync(join(sourceDirectory, "2026-05-03 Insurance Claim - Knee Pain - MRI - City Hospital.md")), true);

		const unsafeDirectory = join(workspace, "unsafe-source");
		mkdirSync(unsafeDirectory);
		writeFileSync(join(unsafeDirectory, "note.txt"), "Administrative note.\n");
		const unsafeRun = join(unsafeDirectory, "Ingest");
		run("node", [script("document-ingest", "document-ingest.mjs"), "prepare", unsafeDirectory, "--output", unsafeRun]);
		const unsafeRows = completeIngestRun(unsafeRun);
		const unsafeMetadataPath = join(unsafeRun, unsafeRows[0].output_directory, "metadata.json");
		const unsafeMetadata = JSON.parse(readFileSync(unsafeMetadataPath, "utf8"));
		unsafeMetadata.finalOutput = { filename: "../escape.md", namingReason: "bad path" };
		writeFileSync(unsafeMetadataPath, `${JSON.stringify(unsafeMetadata, null, 2)}\n`);
		runFailure(
			"node",
			[script("document-ingest", "document-ingest.mjs"), "validate", unsafeRun],
			/finalOutput\.filename must be a safe Markdown filename/,
		);
	});
});

test("transcription pins model downloads to durable managed cache", () => {
	withWorkspace((workspace) => {
		const transcriptionHome = join(workspace, "transcription-home");
		const probe = join(workspace, "transcription-cache-probe.py");
		writeFileSync(
			probe,
			`import importlib.util
import json
import os
import sys

script_path = sys.argv[1]
transcription_home = sys.argv[2]
os.environ["PI_FORGE_TRANSCRIPTION_HOME"] = transcription_home
spec = importlib.util.spec_from_file_location("transcription_skill", script_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
env = module.model_cache_env()
module.ensure_model_cache_env()
keys = ["HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "HF_HUB_DISABLE_TELEMETRY"]
print(json.dumps({
    "transcription_home": str(module.transcription_home()),
    "models_dir": str(module.models_dir()),
    "hub_cache_dir": str(module.hub_cache_dir()),
    "env": {key: env.get(key) for key in keys},
    "process_env": {key: os.environ.get(key) for key in keys},
}))
`,
		);
		const report = jsonOutput(run(python, [probe, script("transcription", "transcription.py"), transcriptionHome]));
		assert.equal(report.transcription_home, transcriptionHome);
		assert.equal(report.models_dir, join(transcriptionHome, "models"));
		assert.equal(report.hub_cache_dir, join(transcriptionHome, "models", "hub"));
		assert.equal(report.env.HF_HOME, join(transcriptionHome, "models"));
		assert.equal(report.env.HF_HUB_CACHE, join(transcriptionHome, "models", "hub"));
		assert.equal(report.env.HUGGINGFACE_HUB_CACHE, join(transcriptionHome, "models", "hub"));
		assert.equal(report.env.TRANSFORMERS_CACHE, join(transcriptionHome, "models", "hub"));
		assert.equal(report.env.HF_HUB_DISABLE_TELEMETRY, "1");
		assert.deepEqual(report.process_env, report.env);
	});
});

test("transcription doctor reports the managed model cache", () => {
	withWorkspace((workspace) => {
		const transcriptionHome = join(workspace, "transcription-home");
		const fakeBin = join(workspace, "fake-bin");
		const cacheMarker = join(
			transcriptionHome,
			"models",
			"hub",
			"models--mlx-community--parakeet-tdt-0.6b-v3",
			"snapshots",
			"cached-revision",
		);
		mkdirSync(fakeBin);
		mkdirSync(cacheMarker, { recursive: true });
		writeFileSync(join(cacheMarker, "config.json"), "{}\n");
		writeFileSync(join(cacheMarker, "model.safetensors"), "synthetic weights\n");
		for (const command of ["ffmpeg", "ffprobe"]) {
			writeFileSync(
				join(fakeBin, command),
				`#!/usr/bin/env bash
if [[ "$1" == "-version" ]]; then echo "${command} fake"; exit 0; fi
exit 1
`,
			);
			chmodSync(join(fakeBin, command), 0o755);
		}
		const doctor = jsonOutput(
			runWithEnvironment(
				python,
				[script("transcription", "transcription.py"), "doctor", "--backend", "mlx"],
				{ PATH: `${fakeBin}:${process.env.PATH}`, PI_FORGE_TRANSCRIPTION_HOME: transcriptionHome },
				1,
			),
		);
		assert.equal(doctor.model_cache, join(transcriptionHome, "models"));
		assert.equal(doctor.model_cached, true);
		assert.equal(doctor.backends.mlx.model_cached, true);
		assert.doesNotMatch(doctor.remediation.join("\n"), /is not cached/);
	});
});

test("document ingest retries garbled PDF text with forced OCR and prepares vision fallback", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "garbled.pdf");
		const runDirectory = join(workspace, "ingest");
		const fakeBin = join(workspace, "fake-bin");
		const ocrLog = join(workspace, "ocr-args.txt");
		mkdirSync(fakeBin);
		writeFileSync(source, "%PDF-1.4\nsynthetic regression fixture\n");
		const commands = {
			pdfinfo: `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdfinfo fake"; exit 0; fi
printf 'Pages: 2\\nTitle: Synthetic PDF\\n'
`,
			pdfimages: `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdfimages fake"; exit 0; fi
printf '   1     0 image\\n   2     1 image\\n'
`,
			pdftotext: `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdftotext fake"; exit 0; fi
input="\${@: -2:1}"
if [[ "$input" == */ocr.pdf ]]; then
	printf 'Health Care Summary claim details and readable words recovered by local optical character recognition.\\f......................................................................................................................................'
else
	printf '......................................................................................................................................\\f......................................................................................................................................'
fi
`,
			ocrmypdf: `#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "ocrmypdf fake"; exit 0; fi
printf '%s\\n' "$@" > "$OCR_LOG"
arguments=("$@")
cp "\${arguments[\${#arguments[@]}-2]}" "\${arguments[\${#arguments[@]}-1]}"
`,
			tesseract: "#!/usr/bin/env bash\necho 'tesseract fake'\n",
			pdftoppm: `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdftoppm fake"; exit 0; fi
printf 'synthetic png bytes' > "\${@: -1}.png"
`,
		};
		for (const [name, body] of Object.entries(commands)) {
			writeFileSync(join(fakeBin, name), body);
			chmodSync(join(fakeBin, name), 0o755);
		}

		const prepared = jsonOutput(
			runWithEnvironment(
				"node",
				[script("document-ingest", "document-ingest.mjs"), "prepare", source, "--output", runDirectory, "--ocr-backend", "local"],
				{ OCR_LOG: ocrLog, PATH: `${fakeBin}:${process.env.PATH}` },
			),
		);
		assert.equal(prepared.counts.needs_review, 1);
		const documentDirectoryName = firstManifestRow(runDirectory).output_directory;
		const documentDirectory = join(runDirectory, documentDirectoryName);
		const metadata = JSON.parse(readFileSync(join(documentDirectory, "metadata.json"), "utf8"));
		assert.equal(metadata.extraction.ocr.commandMode, "force");
		assert.deepEqual(metadata.extraction.ocr.candidatePages, [1, 2]);
		assert.deepEqual(metadata.extraction.ocr.selectedPages, [1]);
		assert.deepEqual(metadata.extraction.ocr.unresolvedPages, [2]);
		assert.equal(metadata.extraction.ocr.beforeQuality[0].suspicious, true);
		assert.equal(metadata.extraction.ocr.afterQuality[0].suspicious, false);
		assert.equal(metadata.extraction.vision.required, true);
		assert.deepEqual(metadata.extraction.vision.candidatePages, [2]);
		assert.match(readFileSync(ocrLog, "utf8"), /--force-ocr/);
		assert.equal(existsSync(join(documentDirectory, "derived", "vision-pages", "page-0002.png")), true);
		assert.match(readFileSync(join(documentDirectory, "document.md"), "utf8"), /Health Care Summary/);

		const transcript = "# Page 2\n\nReadable vision transcription for the unresolved page.\n";
		mkdirSync(join(documentDirectory, "working", "vision-pages"));
		writeFileSync(join(documentDirectory, "working", "vision-pages", "page-0002.md"), transcript);
		writeFileSync(join(documentDirectory, "document.md"), `${readFileSync(join(documentDirectory, "document.md"), "utf8")}\n${transcript}`);
		metadata.extraction.vision.used = true;
		metadata.extraction.vision.completedPages = [2];
		metadata.review.completed = true;
		writeFileSync(join(documentDirectory, "metadata.json"), `${JSON.stringify(metadata, null, 2)}\n`);
		const sourceMapPath = join(documentDirectory, "source_map.json");
		const sourceMap = JSON.parse(readFileSync(sourceMapPath, "utf8"));
		sourceMap.entries.push({
			markdownStartLine: readFileSync(join(documentDirectory, "document.md"), "utf8").split("\n").length - 3,
			markdownEndLine: readFileSync(join(documentDirectory, "document.md"), "utf8").split("\n").length - 1,
			sourceLocator: { type: "pdf-page", page: 2 },
			method: "vision-transcription",
			confidence: "medium",
		});
		writeFileSync(sourceMapPath, `${JSON.stringify(sourceMap, null, 2)}\n`);
		const reportPath = join(documentDirectory, "extraction_report.md");
		writeFileSync(reportPath, readFileSync(reportPath, "utf8").replace("model normalization pending", "model normalization complete"));
		assert.equal(
			jsonOutput(
				runWithEnvironment("node", [script("document-ingest", "document-ingest.mjs"), "validate", runDirectory], {
					PATH: `${fakeBin}:${process.env.PATH}`,
				}),
			).valid,
			true,
		);

		const localOnlyRun = join(workspace, "local-only");
		jsonOutput(
			runWithEnvironment(
				"node",
				[
					script("document-ingest", "document-ingest.mjs"),
					"prepare",
					source,
					"--output",
					localOnlyRun,
					"--ocr",
					"force",
					"--ocr-backend",
					"local",
				],
				{ OCR_LOG: ocrLog, PATH: `${fakeBin}:${process.env.PATH}` },
			),
		);
		const localOnlyDirectoryName = firstManifestRow(localOnlyRun).output_directory;
		const localOnlyDirectory = join(localOnlyRun, localOnlyDirectoryName);
		const localOnlyMetadataPath = join(localOnlyDirectory, "metadata.json");
		const localOnlyMetadata = JSON.parse(readFileSync(localOnlyMetadataPath, "utf8"));
		assert.equal(localOnlyMetadata.extraction.ocr.reason, "forced by user");
		localOnlyMetadata.extraction.vision.unavailableReason = "Current model does not support images.";
		localOnlyMetadata.review.completed = true;
		writeFileSync(localOnlyMetadataPath, `${JSON.stringify(localOnlyMetadata, null, 2)}\n`);
		const localOnlyReportPath = join(localOnlyDirectory, "extraction_report.md");
		writeFileSync(
			localOnlyReportPath,
			readFileSync(localOnlyReportPath, "utf8").replace("model normalization pending", "model normalization complete; vision unavailable"),
		);
		assert.equal(
			jsonOutput(
				runWithEnvironment("node", [script("document-ingest", "document-ingest.mjs"), "validate", localOnlyRun], {
					PATH: `${fakeBin}:${process.env.PATH}`,
				}),
			).valid,
			true,
		);
	});
});

test("document ingest sends image OCR to GLM-OCR SDK and preserves structured artifacts", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const server = await startGlmocrFixture(workspace, {
			markdown_result: "# Invoice\n\n| Item | Total |\n|---|---:|\n| Paper | $12.00 |\n",
			json_result: { blocks: [{ type: "table", page: 1 }] },
			layout_details: { ignored: true },
			data_info: { pages: [{ page_id: 1 }] },
			usage: { prompt_tokens: 1, completion_tokens: 1 },
			model: "glm-ocr",
		});
		try {
			const source = join(workspace, "scan.png");
			writeFileSync(source, Buffer.from("synthetic png fixture"));
			const runDirectory = join(workspace, "ingest");
			const prepared = jsonOutput(
				run("node", [
					script("document-ingest", "document-ingest.mjs"),
					"prepare",
					source,
					"--output",
					runDirectory,
					"--ocr-backend",
					"glmocr",
					"--glmocr-url",
					server.url,
				]),
			);
			assert.equal(prepared.counts.needs_review, 1);
			const requests = readFileSync(server.requestsPath, "utf8")
				.trim()
				.split("\n")
				.map((line) => JSON.parse(line));
			assert.equal(requests.length, 1);
			assert.match(requests[0].body.image_url, /^data:image\/png;base64,/);
			const documentDirectoryName = firstManifestRow(runDirectory).output_directory;
			const documentDirectory = join(runDirectory, documentDirectoryName);
			const metadataPath = join(documentDirectory, "metadata.json");
			const metadata = JSON.parse(readFileSync(metadataPath, "utf8"));
			assert.equal(metadata.extraction.method, "glm-ocr-sdk");
			assert.equal(metadata.extraction.ocr.backend, "glmocr");
			assert.equal(metadata.extraction.ocr.used, true);
			assert.equal(metadata.extraction.ocr.layoutPath, "derived/glmocr-layout.json");
			assert.match(readFileSync(join(documentDirectory, "document.md"), "utf8"), /Paper/);
			assert.deepEqual(JSON.parse(readFileSync(join(documentDirectory, "derived", "glmocr-layout.json"), "utf8")), {
				blocks: [{ type: "table", page: 1 }],
			});

			metadata.review.completed = true;
			writeFileSync(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`);
			const reportPath = join(documentDirectory, "extraction_report.md");
			writeFileSync(reportPath, readFileSync(reportPath, "utf8").replace("model normalization pending", "model normalization complete"));
			assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "validate", runDirectory])).valid, true);
		} finally {
			await server.close();
		}
	});
});

test("document ingest forces GLM-OCR for text PDFs and falls back to vision on low-quality output", async () => {
	await withAsyncWorkspace(async (workspace) => {
		const fakeBin = join(workspace, "fake-bin");
		mkdirSync(fakeBin);
		const commands = {
			pdfinfo: `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdfinfo fake"; exit 0; fi
printf 'Pages: 1\\nTitle: Synthetic PDF\\n'
`,
			pdftotext: `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdftotext fake"; exit 0; fi
printf 'This readable text layer would have satisfied direct extraction without any optical character recognition.'
`,
			pdftoppm: `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then echo "pdftoppm fake"; exit 0; fi
printf 'synthetic png bytes' > "\${@: -1}.png"
`,
		};
		for (const [name, body] of Object.entries(commands)) {
			writeFileSync(join(fakeBin, name), body);
			chmodSync(join(fakeBin, name), 0o755);
		}
		const source = join(workspace, "text.pdf");
		writeFileSync(source, "%PDF-1.4\nsynthetic text-layer fixture\n");

		const cleanServer = await startGlmocrFixture(workspace, {
			markdown_result: "# Report\n\nThis is a clean, high quality GLM-OCR transcription with plenty of readable words across the whole page.\n",
			data_info: { pages: [{ page_id: 1 }] },
		});
		try {
			const cleanRun = join(workspace, "clean");
			const prepared = jsonOutput(
				runWithEnvironment(
					"node",
					[script("document-ingest", "document-ingest.mjs"), "prepare", source, "--output", cleanRun],
					{ PATH: `${fakeBin}:${process.env.PATH}`, FORGE_GLMOCR_URL: cleanServer.url },
				),
			);
			assert.equal(prepared.counts.needs_review, 1);
			const requests = readFileSync(cleanServer.requestsPath, "utf8").trim().split("\n").map((line) => JSON.parse(line));
			assert.equal(requests.length, 1);
			assert.match(requests[0].body.file, /^data:application\/pdf;base64,/);
			const cleanDirectoryName = firstManifestRow(cleanRun).output_directory;
			const cleanDirectory = join(cleanRun, cleanDirectoryName);
			const cleanMetadataPath = join(cleanDirectory, "metadata.json");
			const cleanMetadata = JSON.parse(readFileSync(cleanMetadataPath, "utf8"));
			assert.equal(cleanMetadata.extraction.method, "glm-ocr-sdk");
			assert.equal(cleanMetadata.extraction.ocr.backend, "glmocr");
			assert.equal(cleanMetadata.extraction.vision.required, false);
			assert.match(readFileSync(join(cleanDirectory, "document.md"), "utf8"), /clean, high quality GLM-OCR transcription/);
			cleanMetadata.review.completed = true;
			writeFileSync(cleanMetadataPath, `${JSON.stringify(cleanMetadata, null, 2)}\n`);
			const cleanReportPath = join(cleanDirectory, "extraction_report.md");
			writeFileSync(cleanReportPath, readFileSync(cleanReportPath, "utf8").replace("model normalization pending", "model normalization complete"));
			assert.equal(jsonOutput(run("node", [script("document-ingest", "document-ingest.mjs"), "validate", cleanRun])).valid, true);
		} finally {
			await cleanServer.close();
		}

		const garbledServer = await startGlmocrFixture(workspace, {
			markdown_result: "......................................................................................................................................\n",
			json_result: { blocks: [{ type: "table", page: 1 }] },
			data_info: { pages: [{ page_id: 1 }] },
		});
		try {
			const garbledRun = join(workspace, "garbled");
			jsonOutput(
				runWithEnvironment(
					"node",
					[
						script("document-ingest", "document-ingest.mjs"),
						"prepare",
						source,
						"--output",
						garbledRun,
						"--ocr-backend",
						"auto",
						"--glmocr-url",
						garbledServer.url,
					],
					{ PATH: `${fakeBin}:${process.env.PATH}` },
				),
			);
			const garbledDirectoryName = firstManifestRow(garbledRun).output_directory;
			const garbledDirectory = join(garbledRun, garbledDirectoryName);
			const garbledMetadataPath = join(garbledDirectory, "metadata.json");
			const garbledMetadata = JSON.parse(readFileSync(garbledMetadataPath, "utf8"));
			assert.equal(garbledMetadata.extraction.method, "glm-ocr-sdk");
			assert.equal(garbledMetadata.extraction.vision.required, true);
			assert.deepEqual(garbledMetadata.extraction.vision.candidatePages, [1]);
			assert.equal(garbledMetadata.extraction.vision.renderedPages.length, 1);
			assert.deepEqual(garbledMetadata.extraction.ocr.unresolvedPages, [1]);
			assert.equal(existsSync(join(garbledDirectory, "derived", "vision-pages", "page-0001.png")), true);

			const transcript = "# Page 1\n\nReadable vision transcription recovered by the active model.\n";
			mkdirSync(join(garbledDirectory, "working", "vision-pages"));
			writeFileSync(join(garbledDirectory, "working", "vision-pages", "page-0001.md"), transcript);
			writeFileSync(join(garbledDirectory, "document.md"), `${readFileSync(join(garbledDirectory, "document.md"), "utf8")}\n${transcript}`);
			garbledMetadata.extraction.vision.used = true;
			garbledMetadata.extraction.vision.completedPages = [1];
			garbledMetadata.review.completed = true;
			writeFileSync(garbledMetadataPath, `${JSON.stringify(garbledMetadata, null, 2)}\n`);
			const sourceMapPath = join(garbledDirectory, "source_map.json");
			const sourceMap = JSON.parse(readFileSync(sourceMapPath, "utf8"));
			sourceMap.entries.push({
				markdownStartLine: readFileSync(join(garbledDirectory, "document.md"), "utf8").split("\n").length - 3,
				markdownEndLine: readFileSync(join(garbledDirectory, "document.md"), "utf8").split("\n").length - 1,
				sourceLocator: { type: "pdf-page", page: 1 },
				method: "vision-transcription",
				confidence: "medium",
			});
			writeFileSync(sourceMapPath, `${JSON.stringify(sourceMap, null, 2)}\n`);
			const reportPath = join(garbledDirectory, "extraction_report.md");
			writeFileSync(reportPath, readFileSync(reportPath, "utf8").replace("model normalization pending", "model normalization complete"));
			assert.equal(
				jsonOutput(
					runWithEnvironment("node", [script("document-ingest", "document-ingest.mjs"), "validate", garbledRun], {
						PATH: `${fakeBin}:${process.env.PATH}`,
					}),
				).valid,
				true,
			);
		} finally {
			await garbledServer.close();
		}
	});
});

test("installed launcher symlinks resolve their source checkout", () => {
	withWorkspace((workspace) => {
		const repository = join(workspace, "repository");
		const scriptsDirectory = join(repository, "scripts");
		const binaryDirectory = join(workspace, "bin");
		const fakeBin = join(workspace, "fake-bin");
		mkdirSync(scriptsDirectory, { recursive: true });
		mkdirSync(join(repository, "packages", "coding-agent", "dist"), { recursive: true });
		mkdirSync(binaryDirectory);
		mkdirSync(fakeBin);
		copyFileSync(join(repositoryRoot, "scripts", "pi-forge-run.sh"), join(scriptsDirectory, "pi-forge-run.sh"));
		copyFileSync(join(repositoryRoot, "update.sh"), join(repository, "update.sh"));
		writeFileSync(join(repository, "packages", "coding-agent", "dist", "cli.js"), "");
		writeFileSync(join(fakeBin, "node"), "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n");
		chmodSync(join(fakeBin, "node"), 0o755);
		symlinkSync(join(scriptsDirectory, "pi-forge-run.sh"), join(binaryDirectory, "pi-forge"));
		symlinkSync(join(repository, "update.sh"), join(binaryDirectory, "pi-forge-update"));

		const launcher = spawnSync(join(binaryDirectory, "pi-forge"), ["--version"], {
			encoding: "utf8",
			env: { ...environment, PATH: `${fakeBin}:${process.env.PATH}` },
		});
		assert.equal(launcher.status, 0, launcher.stderr);
		assert.equal(
			launcher.stdout.trim(),
			`${join(realpathSync(repository), "packages", "coding-agent", "dist", "cli.js")}\n--version`,
		);

		const updater = runFailure(join(binaryDirectory, "pi-forge-update"), [], /legacy updater requires a git checkout/);
		assert.match(updater.stderr, new RegExp(realpathSync(repository).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
	});
});

test("profile configuration installs local service defaults without dropping user configuration", () => {
	withWorkspace((workspace) => {
		const agentDirectory = join(workspace, "agent");
		mkdirSync(agentDirectory);
		writeFileSync(
			join(agentDirectory, "settings.json"),
			`${JSON.stringify({
				theme: "light",
				packages: ["/user/package"],
				compaction: { keepRecentTokens: 12345 },
				taskModel: { timeoutMs: 45000, customNote: "keep-task-setting" },
				contextBudget: { verbatimRecentTokens: 23456, customNote: "keep-budget-setting" },
			})}\n`,
		);
		writeFileSync(
			join(agentDirectory, "models.json"),
			`${JSON.stringify({
				providers: {
					existing: { baseUrl: "https://example.invalid/v1" },
					"forge-task-local": { baseUrl: "http://old-task.invalid/v1", models: [] },
				},
			})}\n`,
		);
		run("node", [join(repositoryRoot, "scripts", "configure-pi-forge.mjs"), agentDirectory, join(repositoryRoot, "forge")]);

		const settings = JSON.parse(readFileSync(join(agentDirectory, "settings.json"), "utf8"));
		assert.equal(settings.defaultProvider, "forge-local");
		assert.equal(settings.defaultModel, "code");
		assert.equal(settings.theme, "light");
		assert.deepEqual(settings.compaction, { keepRecentTokens: 12345, enabled: true, reserveTokens: 65536 });
		assert.equal("taskModel" in settings, false);
		assert.deepEqual(settings.contextBudget, {
			verbatimRecentTokens: 20000,
			customNote: "keep-budget-setting",
			enabled: true,
			softRatio: 0.75,
			useTaskModel: false,
		});
		assert.deepEqual(settings.connectedServices, {
			searxng: {
				enabled: true,
				baseUrl: "http://llms/searxng",
			},
			playwright: {
				enabled: true,
				wsEndpoint: "ws://llms/playwright",
			},
		});
		assert.deepEqual(settings.packages, [join(repositoryRoot, "forge"), "/user/package"]);

		const models = JSON.parse(readFileSync(join(agentDirectory, "models.json"), "utf8"));
		assert.equal(models.providers.existing.baseUrl, "https://example.invalid/v1");
		assert.equal(models.providers["forge-local"].baseUrl, "http://llms:8008/v1");
		const localModel = models.providers["forge-local"].models[0];
		assert.equal(localModel.id, "code");
		assert.equal(localModel.contextWindow, 262144);
		assert.equal(localModel.maxTokens, 32768);
		assert.equal(models.providers["forge-local"].compat.supportsDeveloperRole, false);
		assert.equal("forge-task-local" in models.providers, false);
	});
});

test("profile configuration preserves connected service overrides", () => {
	withWorkspace((workspace) => {
		const agentDirectory = join(workspace, "agent");
		mkdirSync(agentDirectory);
		writeFileSync(
			join(agentDirectory, "settings.json"),
			`${JSON.stringify({
				connectedServices: {
					searxng: { enabled: true, baseUrl: "http://search.local/custom/" },
					playwright: { enabled: false, wsEndpoint: "ws://browser.local/custom/" },
				},
			})}\n`,
		);
		run("node", [join(repositoryRoot, "scripts", "configure-pi-forge.mjs"), agentDirectory, join(repositoryRoot, "forge")]);

		const settings = JSON.parse(readFileSync(join(agentDirectory, "settings.json"), "utf8"));
		assert.deepEqual(settings.connectedServices, {
			searxng: {
				enabled: true,
				baseUrl: "http://search.local/custom",
			},
			playwright: {
				enabled: false,
				wsEndpoint: "ws://browser.local/custom",
			},
		});
	});
});

test("piped installer fetches standalone installer without cloning a repository", () => {
	withWorkspace((workspace) => {
		const fakeBin = join(workspace, "bin");
		const curlLog = join(workspace, "curl-args.txt");
		const gitLog = join(workspace, "git-called.txt");
		const installerRanLog = join(workspace, "installer-ran.txt");
		mkdirSync(fakeBin);
		writeFileSync(
			join(fakeBin, "curl"),
			`#!/usr/bin/env bash
printf '%s\\n' "$@" > "$INSTALLER_CURL_LOG"
output=""
while (($#)); do
	if [[ "$1" == "-o" ]]; then
		output="$2"
		break
	fi
	shift
done
cat > "$output" <<'SCRIPT'
#!/usr/bin/env bash
printf 'ran\n' > "$INSTALLER_RAN_LOG"
exit 0
SCRIPT
chmod +x "$output"
`,
		);
		writeFileSync(join(fakeBin, "git"), "#!/usr/bin/env bash\nprintf 'git called\\n' > \"$INSTALLER_GIT_LOG\"\nexit 99\n");
		chmodSync(join(fakeBin, "curl"), 0o755);
		chmodSync(join(fakeBin, "git"), 0o755);

		const result = spawnSync("bash", [], {
			cwd: workspace,
			encoding: "utf8",
			env: {
				...environment,
				HOME: workspace,
				INSTALLER_CURL_LOG: curlLog,
				INSTALLER_GIT_LOG: gitLog,
				INSTALLER_RAN_LOG: installerRanLog,
				PATH: `${fakeBin}:${process.env.PATH}`,
				PI_FORGE_INSTALLER_URL: "https://example.invalid/pi-forge-install.sh",
			},
			input: readFileSync(join(repositoryRoot, "install.sh"), "utf8"),
		});
		assert.equal(result.status, 0, result.stderr);
		assert.doesNotMatch(result.stderr, /BASH_SOURCE/);
		assert.doesNotMatch(result.stderr, /ARGS\[@\]: unbound variable/);
		assert.match(readFileSync(curlLog, "utf8"), /https:\/\/example\.invalid\/pi-forge-install\.sh/);
		assert.equal(readFileSync(installerRanLog, "utf8"), "ran\n");
		assert.equal(existsSync(gitLog), false);
	});
});

test("checkout installer invokes local installer with no arguments under strict Bash", () => {
	withWorkspace((workspace) => {
		const scriptsDirectory = join(workspace, "scripts");
		const installerLog = join(workspace, "installer-args.txt");
		mkdirSync(scriptsDirectory);
		copyFileSync(join(repositoryRoot, "install.sh"), join(workspace, "install.sh"));
		writeFileSync(
			join(scriptsDirectory, "pi-forge-install.sh"),
			`#!/usr/bin/env bash
printf 'argc=%s\\n' "$#" > "$INSTALLER_LOG"
if (($#)); then
	printf '%s\\n' "$@" >> "$INSTALLER_LOG"
fi
`,
		);
		chmodSync(join(scriptsDirectory, "pi-forge-install.sh"), 0o755);

		const result = spawnSync("bash", [join(workspace, "install.sh")], {
			cwd: workspace,
			encoding: "utf8",
			env: { ...environment, HOME: workspace, INSTALLER_LOG: installerLog },
		});
		assert.equal(result.status, 0, result.stderr);
		assert.doesNotMatch(result.stderr, /ARGS\[@\]: unbound variable/);
		assert.equal(readFileSync(installerLog, "utf8"), "argc=0\n");
	});
});

test("checkout installer invokes local installer without cloning by default", () => {
	withWorkspace((workspace) => {
		const fakeBin = join(workspace, "bin");
		const gitLog = join(workspace, "git-args.txt");
		mkdirSync(fakeBin);
		writeFileSync(
			join(fakeBin, "git"),
			"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$INSTALLER_GIT_LOG\"\nexit 99\n",
		);
		chmodSync(join(fakeBin, "git"), 0o755);

		const result = spawnSync("bash", [join(repositoryRoot, "install.sh"), "--help"], {
			cwd: repositoryRoot,
			encoding: "utf8",
			env: {
				...environment,
				HOME: workspace,
				INSTALLER_GIT_LOG: gitLog,
				PATH: `${fakeBin}:${process.env.PATH}`,
			},
		});
		assert.equal(result.status, 0, result.stderr);
		assert.match(result.stdout, /Installs pi-forge into/);
		assert.equal(existsSync(gitLog), false);
	});
});
