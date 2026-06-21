#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
	copyFileSync,
	existsSync,
	lstatSync,
	mkdirSync,
	readFileSync,
	readdirSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { basename, dirname, extname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SKILL_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ASSETS_DIR = join(SKILL_ROOT, "assets");
const TEMPLATES_DIR = join(ASSETS_DIR, "templates");
const THEMES_DIR = join(ASSETS_DIR, "themes");
const PLACEHOLDER = "<!-- TODO: author this section -->";
const THEMES = new Set([
	"editorial",
	"technical",
	"archival",
	"gallery",
	"magazine",
	"academic",
	"brand",
	"terminal",
]);
const HERO_STYLES = new Set(["gradient", "centered", "split", "image"]);
const TEXT_EXTENSIONS = new Set([".md", ".markdown", ".txt", ".rst", ".html", ".htm"]);
const IMAGE_EXTENSIONS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"]);
const SKIP_DIRECTORIES = new Set(["node_modules", ".git", "site"]);
const RECOGNIZED_RUN_ARTIFACTS = new Map([
	["source_map.json", "document-ingest"],
	["web_manifest.json", "web-collection"],
	["evidence_table.csv", "literature-extraction"],
]);

// --- small utilities ------------------------------------------------------

function fail(message, exitCode = 1) {
	process.stderr.write(`Error: ${message}\n`);
	process.exit(exitCode);
}

function sha256(buffer) {
	return createHash("sha256").update(buffer).digest("hex");
}

function nowIso() {
	return new Date().toISOString();
}

function writeJson(filePath, value) {
	writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function readJson(filePath) {
	return JSON.parse(readFileSync(filePath, "utf8"));
}

function safeStem(value) {
	const raw = String(value).normalize("NFKC").trim();
	const safe = raw.replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "");
	return safe.slice(0, 80) || "site";
}

function slugify(value) {
	return (
		String(value)
			.normalize("NFKD")
			.toLowerCase()
			.replace(/[^\p{L}\p{N}]+/gu, "-")
			.replace(/^-+|-+$/g, "")
			.slice(0, 60) || "section"
	);
}

function escapeHtml(text) {
	return String(text)
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

function escapeAttribute(text) {
	return escapeHtml(text).replace(/'/g, "&#39;");
}

function fillTemplate(template, values) {
	let result = template;
	for (const [key, value] of Object.entries(values)) {
		result = result.split(`{{${key}}}`).join(value);
	}
	return result;
}

// --- Markdown rendering (dependency-free subset) ---------------------------

function sanitizeUrl(url) {
	const trimmed = url.trim();
	if (/^\s*javascript:/i.test(trimmed)) return null;
	return trimmed;
}

function isExternal(url) {
	return /^https?:\/\//i.test(url);
}

function renderInline(text) {
	const codeSpans = [];
	let working = text.replace(/`([^`]+)`/g, (_match, code) => {
		codeSpans.push(`<code>${escapeHtml(code)}</code>`);
		return `${codeSpans.length - 1}`;
	});

	working = escapeHtml(working);

	working = working.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)/g, (match, alt, source) => {
		const url = sanitizeUrl(source);
		if (!url) return escapeHtml(match);
		return `<img src="${escapeAttribute(url)}" alt="${escapeAttribute(alt)}" loading="lazy">`;
	});

	working = working.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)/g, (match, label, target) => {
		const url = sanitizeUrl(target);
		if (!url) return escapeHtml(label);
		const rel = isExternal(url) ? ' rel="noopener noreferrer"' : "";
		return `<a href="${escapeAttribute(url)}"${rel}>${label}</a>`;
	});

	working = working.replace(/&lt;(https?:\/\/[^\s&]+)&gt;/g, (_match, url) => {
		return `<a href="${escapeAttribute(url)}" rel="noopener noreferrer">${escapeHtml(url)}</a>`;
	});

	working = working.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
	working = working.replace(/__([^_]+)__/g, "<strong>$1</strong>");
	working = working.replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>");
	working = working.replace(/(^|[^_\w])_([^_\s][^_]*?)_/g, "$1<em>$2</em>");

	working = working.replace(/(\d+)/g, (_match, index) => codeSpans[Number(index)]);
	return working;
}

function indentOf(line) {
	const match = line.match(/^(\s*)/);
	return match[1].replace(/\t/g, "    ").length;
}

function isUnordered(line) {
	return /^\s*[-*+]\s+/.test(line);
}

function isOrdered(line) {
	return /^\s*\d+[.)]\s+/.test(line);
}

function isListItem(line) {
	return isUnordered(line) || isOrdered(line);
}

function parseList(lines, start, minIndent) {
	const ordered = isOrdered(lines[start]);
	const tag = ordered ? "ol" : "ul";
	const items = [];
	let index = start;
	while (index < lines.length) {
		const line = lines[index];
		if (line.trim() === "") {
			let lookahead = index + 1;
			while (lookahead < lines.length && lines[lookahead].trim() === "") lookahead += 1;
			if (lookahead < lines.length && isListItem(lines[lookahead]) && indentOf(lines[lookahead]) >= minIndent) {
				index = lookahead;
				continue;
			}
			break;
		}
		const itemIndent = indentOf(line);
		if (itemIndent < minIndent || !isListItem(line)) break;
		const content = line.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "");
		const continuation = [];
		let children = "";
		index += 1;
		while (index < lines.length) {
			const next = lines[index];
			if (next.trim() === "") {
				let lookahead = index + 1;
				while (lookahead < lines.length && lines[lookahead].trim() === "") lookahead += 1;
				if (lookahead < lines.length && indentOf(lines[lookahead]) > itemIndent && isListItem(lines[lookahead])) {
					const sub = parseList(lines, lookahead, itemIndent + 1);
					children += sub.html;
					index = sub.next;
					continue;
				}
				break;
			}
			const nextIndent = indentOf(next);
			if (nextIndent > itemIndent && isListItem(next)) {
				const sub = parseList(lines, index, itemIndent + 1);
				children += sub.html;
				index = sub.next;
				continue;
			}
			if (nextIndent > itemIndent) {
				continuation.push(next.trim());
				index += 1;
				continue;
			}
			break;
		}
		let itemHtml = renderInline(content);
		if (continuation.length > 0) itemHtml += ` ${renderInline(continuation.join(" "))}`;
		items.push(`<li>${itemHtml}${children}</li>`);
	}
	return { html: `<${tag}>\n${items.join("\n")}\n</${tag}>`, next: index };
}

function isTableDelimiter(line) {
	return /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(line);
}

function splitTableRow(line) {
	return line
		.trim()
		.replace(/^\||\|$/g, "")
		.split("|")
		.map((cell) => cell.trim());
}

function renderTable(lines, start) {
	const header = splitTableRow(lines[start]);
	let index = start + 2;
	const bodyRows = [];
	while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
		bodyRows.push(splitTableRow(lines[index]));
		index += 1;
	}
	const head = `<thead><tr>${header.map((cell) => `<th>${renderInline(cell)}</th>`).join("")}</tr></thead>`;
	const body = bodyRows
		.map((row) => `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join("")}</tr>`)
		.join("\n");
	return { html: `<table>\n${head}\n<tbody>\n${body}\n</tbody>\n</table>`, next: index };
}

function renderMarkdown(markdown) {
	const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
	const blocks = [];
	const headings = [];
	const usedIds = new Set();
	let index = 0;

	while (index < lines.length) {
		const line = lines[index];

		if (line.trim() === "") {
			index += 1;
			continue;
		}

		const fence = line.match(/^```(.*)$/);
		if (fence) {
			const language = fence[1].trim();
			const code = [];
			index += 1;
			while (index < lines.length && !/^```\s*$/.test(lines[index])) {
				code.push(lines[index]);
				index += 1;
			}
			index += 1;
			const classAttribute = language ? ` class="language-${escapeAttribute(language)}"` : "";
			blocks.push(`<pre><code${classAttribute}>${escapeHtml(code.join("\n"))}</code></pre>`);
			continue;
		}

		const heading = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
		if (heading) {
			const level = heading[1].length;
			const rawText = heading[2];
			let id = slugify(rawText);
			let suffix = 2;
			while (usedIds.has(id)) {
				id = `${slugify(rawText)}-${suffix}`;
				suffix += 1;
			}
			usedIds.add(id);
			const anchorable = level === 2 || level === 3;
			if (anchorable) headings.push({ level, text: rawText, id });
			const anchor = anchorable
				? ` <a class="heading-anchor" href="#${id}" aria-hidden="true" tabindex="-1">#</a>`
				: "";
			blocks.push(`<h${level} id="${id}">${renderInline(rawText)}${anchor}</h${level}>`);
			index += 1;
			continue;
		}

		if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
			blocks.push("<hr>");
			index += 1;
			continue;
		}

		if (/^>\s?/.test(line)) {
			const quoteLines = [];
			while (index < lines.length && /^>\s?/.test(lines[index])) {
				quoteLines.push(lines[index].replace(/^>\s?/, ""));
				index += 1;
			}
			const callout = quoteLines[0] ? quoteLines[0].match(/^\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]\s*(.*)$/i) : null;
			if (callout) {
				const type = callout[1].toLowerCase();
				const label = callout[2].trim() || callout[1][0].toUpperCase() + callout[1].slice(1).toLowerCase();
				const body = renderMarkdown(quoteLines.slice(1).join("\n"));
				blocks.push(
					`<div class="callout callout-${type}" role="note">\n<p class="callout-label">${escapeHtml(label)}</p>\n${body.html}\n</div>`,
				);
				continue;
			}
			const inner = renderMarkdown(quoteLines.join("\n"));
			blocks.push(`<blockquote>\n${inner.html}\n</blockquote>`);
			continue;
		}

		const container = line.match(/^:::\s*([a-z][\w-]*)\s*$/i);
		if (container) {
			const name = container[1].toLowerCase();
			const innerLines = [];
			index += 1;
			while (index < lines.length && !/^:::\s*$/.test(lines[index])) {
				innerLines.push(lines[index]);
				index += 1;
			}
			index += 1;
			if (name === "cards") {
				blocks.push(renderCards(innerLines));
			} else {
				const inner = renderMarkdown(innerLines.join("\n"));
				const className = name === "grid" ? "cards" : escapeAttribute(name);
				blocks.push(`<div class="${className}">\n${inner.html}\n</div>`);
			}
			continue;
		}

		if (isListItem(line)) {
			const list = parseList(lines, index, indentOf(line));
			blocks.push(list.html);
			index = list.next;
			continue;
		}

		if (line.includes("|") && index + 1 < lines.length && isTableDelimiter(lines[index + 1])) {
			const table = renderTable(lines, index);
			blocks.push(table.html);
			index = table.next;
			continue;
		}

		const paragraph = [];
		while (
			index < lines.length &&
			lines[index].trim() !== "" &&
			!/^(#{1,6})\s+/.test(lines[index]) &&
			!/^```/.test(lines[index]) &&
			!/^>\s?/.test(lines[index]) &&
			!isListItem(lines[index]) &&
			!/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[index])
		) {
			paragraph.push(lines[index].trim());
			index += 1;
		}
		if (paragraph.length > 0) {
			const imageOnly =
				paragraph.length === 1 ? paragraph[0].match(/^!\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)$/) : null;
			const imageSrc = imageOnly ? sanitizeUrl(imageOnly[2]) : null;
			if (imageOnly && imageSrc) {
				const alt = imageOnly[1];
				const caption = alt ? `\n<figcaption>${escapeHtml(alt)}</figcaption>` : "";
				blocks.push(
					`<figure><img src="${escapeAttribute(imageSrc)}" alt="${escapeAttribute(alt)}" loading="lazy">${caption}</figure>`,
				);
			} else {
				blocks.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
			}
		}
	}

	return { html: blocks.join("\n"), headings };
}

function renderCards(innerLines) {
	const cards = [];
	for (const raw of innerLines) {
		const item = raw.match(/^\s*[-*+]\s+(.*)$/);
		if (!item) continue;
		const text = item[1].trim();
		const link = text.match(/^\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)\s*(?:[:—-]\s*(.*))?$/);
		if (link) {
			const href = sanitizeUrl(link[2]);
			const title = renderInline(link[1]);
			const description = link[3] ? renderInline(link[3].trim()) : "";
			const descriptionHtml = description ? `<span class="card-text">${description}</span>` : "";
			if (href) {
				const rel = isExternal(href) ? ' rel="noopener noreferrer"' : "";
				cards.push(
					`<a class="card" href="${escapeAttribute(href)}"${rel}><span class="card-title">${title}</span>${descriptionHtml}</a>`,
				);
				continue;
			}
		}
		const split = text.match(/^(.*?)\s*(?:[:—]|\s-\s)\s*(.*)$/);
		const title = renderInline(split ? split[1].trim() : text);
		const descriptionHtml = split ? `<span class="card-text">${renderInline(split[2].trim())}</span>` : "";
		cards.push(`<div class="card"><span class="card-title">${title}</span>${descriptionHtml}</div>`);
	}
	return `<div class="cards">\n${cards.join("\n")}\n</div>`;
}

function stripHtml(html) {
	return html
		.replace(/<\/(?:p|h[1-6]|li|blockquote|tr|pre)>/gi, " ")
		.replace(/<[^>]+>/g, "")
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/\s+/g, " ")
		.trim();
}

// --- Discovery and link harvesting ----------------------------------------

function walkInputs(inputs) {
	const files = [];
	const recognizedRuns = [];
	const visit = (target) => {
		const stats = lstatSync(target);
		if (stats.isSymbolicLink()) return;
		if (stats.isDirectory()) {
			const name = basename(target);
			if (SKIP_DIRECTORIES.has(name) || name.startsWith(".")) return;
			const entries = readdirSync(target);
			for (const entry of entries) {
				if (RECOGNIZED_RUN_ARTIFACTS.has(entry)) {
					recognizedRuns.push({ directory: target, artifact: entry, skill: RECOGNIZED_RUN_ARTIFACTS.get(entry) });
				}
			}
			for (const entry of entries) {
				if (entry.startsWith(".")) continue;
				visit(join(target, entry));
			}
			return;
		}
		if (stats.isFile()) files.push(target);
	};
	for (const input of inputs) {
		const resolved = resolve(input);
		if (!existsSync(resolved)) fail(`input does not exist: ${input}`);
		visit(resolved);
	}
	return { files, recognizedRuns };
}

function classify(extension) {
	if (extension === ".md" || extension === ".markdown") return "markdown";
	if (extension === ".html" || extension === ".htm") return "html";
	if (extension === ".txt" || extension === ".rst") return "text";
	if (IMAGE_EXTENSIONS.has(extension)) return "image";
	if (extension === ".pdf") return "pdf";
	if (extension === ".docx" || extension === ".doc") return "document";
	if (extension === ".csv" || extension === ".tsv" || extension === ".json") return "data";
	return "other";
}

function harvestLinks(filePath, content, store) {
	const seenInFile = new Set();
	const record = (url, label) => {
		const cleaned = url.replace(/[).,;'"]+$/, "");
		if (!/^https?:\/\//i.test(cleaned)) return;
		let normalized;
		try {
			const parsed = new URL(cleaned);
			parsed.hash = "";
			normalized = parsed.toString();
		} catch {
			return;
		}
		const existing = store.get(normalized) || { url: normalized, label: label || "", sources: [] };
		if (!existing.label && label) existing.label = label;
		const sourceName = basename(filePath);
		if (!existing.sources.includes(sourceName)) existing.sources.push(sourceName);
		store.set(normalized, existing);
		seenInFile.add(normalized);
	};

	const markdownLink = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
	for (const match of content.matchAll(markdownLink)) record(match[2], match[1].trim());
	const htmlLink = /(?:href|src)\s*=\s*["'](https?:\/\/[^"']+)["']/gi;
	for (const match of content.matchAll(htmlLink)) record(match[1], "");
	const bare = /(?<!["'(])\bhttps?:\/\/[^\s<>"')\]]+/gi;
	for (const match of content.matchAll(bare)) record(match[0], "");
}

// --- init -----------------------------------------------------------------

function commandInit(positionals, flags) {
	if (positionals.length === 0) fail("init requires at least one input file or directory");
	if (!flags.output) fail("init requires --output <new-directory>");
	const theme = flags.theme ?? "editorial";
	if (!THEMES.has(theme)) fail(`unknown theme: ${theme} (choose ${[...THEMES].join(", ")})`);
	const runDirectory = resolve(flags.output);
	if (existsSync(runDirectory)) fail(`output directory already exists: ${runDirectory}`);

	const { files, recognizedRuns } = walkInputs(positionals);
	if (files.length === 0) fail("no usable files found in the provided inputs");

	mkdirSync(join(runDirectory, "content"), { recursive: true });
	mkdirSync(join(runDirectory, "assets"), { recursive: true });

	const linkStore = new Map();
	const sources = [];
	const stagedImages = [];
	const usedAssetNames = new Set();

	files.forEach((filePath, position) => {
		const extension = extname(filePath).toLowerCase();
		const sourceType = classify(extension);
		const buffer = readFileSync(filePath);
		const hash = sha256(buffer);
		sources.push({
			sourceId: `src-${String(position + 1).padStart(3, "0")}`,
			path: filePath,
			name: basename(filePath),
			sha256: hash,
			sizeBytes: buffer.length,
			sourceType,
		});
		if (TEXT_EXTENSIONS.has(extension)) harvestLinks(filePath, buffer.toString("utf8"), linkStore);
		if (IMAGE_EXTENSIONS.has(extension)) {
			let assetName = basename(filePath);
			let counter = 2;
			while (usedAssetNames.has(assetName)) {
				assetName = `${basename(filePath, extension)}-${counter}${extension}`;
				counter += 1;
			}
			usedAssetNames.add(assetName);
			copyFileSync(filePath, join(runDirectory, "assets", assetName));
			stagedImages.push({ asset: `assets/${assetName}`, source: basename(filePath) });
		}
	});

	const links = [...linkStore.values()].sort((left, right) => left.url.localeCompare(right.url));
	writeJson(join(runDirectory, "source_manifest.json"), {
		schemaVersion: 1,
		generatedAt: nowIso(),
		recognizedRuns: recognizedRuns.map((run) => ({
			directory: relative(runDirectory, run.directory) || ".",
			artifact: run.artifact,
			skill: run.skill,
		})),
		sources,
	});
	writeJson(join(runDirectory, "links.json"), {
		schemaVersion: 1,
		generatedAt: nowIso(),
		note: "Links harvested from source materials. Recorded, not fetched. Curate into a resources page.",
		links,
	});

	const title = flags.title ?? safeStem(basename(runDirectory));
	const pages = [
		{ slug: "index", title: "Home", description: `${title} — overview.`, tags: [], file: "content/index.md" },
		{ slug: "overview", title: "Overview", description: "Overview of the material.", tags: [], file: "content/overview.md" },
		{ slug: "resources", title: "Resources", description: "References and further reading.", tags: [], file: "content/resources.md" },
	];
	writeJson(join(runDirectory, "site.json"), {
		schemaVersion: 1,
		title,
		description: "A one-sentence description of this site.",
		lang: "en",
		theme,
		tokens: {},
		hero: { style: "gradient" },
		footer: `Built from source materials with the forge site-builder skill. ${new Date().getFullYear()}.`,
		nav: [
			{ label: "Home", page: "index" },
			{ label: "Overview", page: "overview" },
			{ label: "Resources", page: "resources" },
		],
		pages,
	});

	writeFileSync(
		join(runDirectory, "content", "index.md"),
		`${PLACEHOLDER}\n\nWrite the home-page introduction here. Start with a short paragraph,\nthen use \`##\` headings for sections (the home page supplies its own \`<h1>\`).\n\n## What you'll find here\n\nDescribe the site's sections and link to them, for example [the overview](overview.html).\n`,
	);
	writeFileSync(
		join(runDirectory, "content", "overview.md"),
		`${PLACEHOLDER}\n\n# Overview\n\nAuthor this page from the registered sources. Keep generated synthesis\nseparate from quoted source material and attribute claims to their source files.\n`,
	);

	const resourceLines = links.length
		? links.map((link) => `- [${link.label || link.url}](${link.url}) — from ${link.sources.join(", ")}`).join("\n")
		: "- No links were found in the sources. Add references here if relevant.";
	writeFileSync(
		join(runDirectory, "content", "resources.md"),
		`${PLACEHOLDER}\n\n# Resources\n\nCurate the links harvested from the sources (see \`links.json\`). Remove\nirrelevant entries, group the rest, and describe why each matters.\n\n## References\n\n${resourceLines}\n`,
	);

	process.stdout.write(
		`${JSON.stringify(
			{
				runDirectory,
				sources: sources.length,
				links: links.length,
				stagedImages: stagedImages.length,
				recognizedRuns: recognizedRuns.length,
				pages: pages.map((page) => page.slug),
				theme,
			},
			null,
			2,
		)}\n`,
	);
}

// --- build -----------------------------------------------------------------

function buildNav(nav, currentSlug) {
	const items = nav
		.map((item) => {
			const href = `${item.page}.html`;
			const current = item.page === currentSlug ? ' aria-current="page"' : "";
			return `        <li><a href="${escapeAttribute(href)}"${current}>${escapeHtml(item.label)}</a></li>`;
		})
		.join("\n");
	return `      <ul>\n${items}\n      </ul>`;
}

function buildBreadcrumbs(pageTitle) {
	return `      <ol>\n        <li><a href="index.html">Home</a></li>\n        <li aria-current="page">${escapeHtml(pageTitle)}</li>\n      </ol>`;
}

function buildToc(headings) {
	if (headings.length === 0) return "";
	const items = headings
		.map((heading) => {
			const className = heading.level === 3 ? ' class="toc-h3"' : ' class="toc-h2"';
			return `      <li${className}><a href="#${heading.id}">${escapeHtml(heading.text)}</a></li>`;
		})
		.join("\n");
	return `    <nav>\n      <strong>On this page</strong>\n      <ol>\n${items}\n      </ol>\n    </nav>`;
}

// Prefer a run-local override file over the shipped skill asset when present.
function resolveAsset(runDirectory, runRelative, fallbackAbsolute) {
	const local = join(runDirectory, runRelative);
	return existsSync(local) ? local : fallbackAbsolute;
}

function buildStyles(runDirectory, theme, tokens) {
	const tokensPath =
		theme === "custom"
			? join(runDirectory, "theme", "tokens.css")
			: resolveAsset(runDirectory, "theme/tokens.css", join(THEMES_DIR, theme, "tokens.css"));
	const themeCss = readFileSync(tokensPath, "utf8");
	const baseCss = readFileSync(resolveAsset(runDirectory, "theme/base.css", join(ASSETS_DIR, "base.css")), "utf8");
	const printCss = readFileSync(resolveAsset(runDirectory, "theme/print.css", join(ASSETS_DIR, "print.css")), "utf8");
	const entries = Object.entries(tokens || {});
	const overrides = entries.length
		? `\n/* Site token overrides from site.json */\n:root {\n${entries
				.map(([key, value]) => `\t${key}: ${value};`)
				.join("\n")}\n}\n`
		: "";
	return `${themeCss}${overrides}\n${baseCss}\n${printCss}\n`;
}

function buildHero(site) {
	const hero = site.hero || {};
	let style = HERO_STYLES.has(hero.style) ? hero.style : "gradient";
	if ((style === "image" || style === "split") && !hero.image) style = style === "image" ? "gradient" : "centered";
	const title = escapeHtml(site.title || "Site");
	const tagline = escapeHtml(site.description || "");
	const cta =
		hero.cta && hero.cta.label && hero.cta.href
			? `<a class="hero-cta" href="${escapeAttribute(hero.cta.href)}">${escapeHtml(hero.cta.label)}</a>`
			: "";
	const copy = `<h1 class="hero-title">${title}</h1>\n<p class="hero-tagline">${tagline}</p>${cta ? `\n${cta}` : ""}`;
	let inner;
	let styleAttribute = "";
	if (style === "split") {
		inner = `<div class="hero-inner">\n<div>\n${copy}\n</div>\n<figure class="hero-figure"><img src="${escapeAttribute(hero.image)}" alt="${escapeAttribute(hero.imageAlt || "")}"></figure>\n</div>`;
	} else {
		inner = `<div class="hero-inner">\n${copy}\n</div>`;
		if (style === "image") styleAttribute = ` style="background-image: url('${escapeAttribute(hero.image)}')"`;
	}
	return `<section class="hero hero--${style}"${styleAttribute}>\n${inner}\n</section>`;
}

function copyDirectory(from, to) {
	mkdirSync(to, { recursive: true });
	for (const entry of readdirSync(from)) {
		if (entry.startsWith(".")) continue;
		const source = join(from, entry);
		const stats = lstatSync(source);
		if (stats.isSymbolicLink()) continue;
		if (stats.isDirectory()) copyDirectory(source, join(to, entry));
		else if (stats.isFile()) copyFileSync(source, join(to, entry));
	}
}

function commandBuild(positionals) {
	if (positionals.length !== 1) fail("build requires exactly one run directory");
	const runDirectory = resolve(positionals[0]);
	const sitePath = join(runDirectory, "site.json");
	if (!existsSync(sitePath)) fail(`site.json not found in ${runDirectory}; run init first`);
	const site = readJson(sitePath);
	const theme = site.theme || "editorial";
	if (theme !== "custom" && !THEMES.has(theme)) fail(`site.json has an invalid theme: ${site.theme}`);
	if (theme === "custom" && !existsSync(join(runDirectory, "theme", "tokens.css"))) {
		fail('theme is "custom" but theme/tokens.css is missing; run `eject` or author theme/tokens.css first');
	}
	if (!Array.isArray(site.pages) || site.pages.length === 0) fail("site.json has no pages");
	if (!site.pages.some((page) => page.slug === "index")) fail('site.json must include a page with slug "index"');

	const slugs = new Set();
	const placeholderPages = [];
	const missingPages = [];
	for (const page of site.pages) {
		if (slugs.has(page.slug)) fail(`duplicate page slug: ${page.slug}`);
		slugs.add(page.slug);
		const contentPath = join(runDirectory, page.file || `content/${page.slug}.md`);
		if (!existsSync(contentPath)) {
			missingPages.push(page.slug);
			continue;
		}
		if (readFileSync(contentPath, "utf8").includes(PLACEHOLDER)) placeholderPages.push(page.slug);
	}
	if (missingPages.length > 0) fail(`missing content files for pages: ${missingPages.join(", ")}`);
	if (placeholderPages.length > 0) {
		fail(`these pages still contain the placeholder marker; author them first: ${placeholderPages.join(", ")}`);
	}

	const siteDir = join(runDirectory, "site");
	rmSync(siteDir, { recursive: true, force: true });
	mkdirSync(siteDir, { recursive: true });

	const pageTemplate = readFileSync(resolveAsset(runDirectory, "templates/page.html", join(TEMPLATES_DIR, "page.html")), "utf8");
	const indexTemplate = readFileSync(resolveAsset(runDirectory, "templates/index.html", join(TEMPLATES_DIR, "index.html")), "utf8");
	const notFoundTemplate = readFileSync(resolveAsset(runDirectory, "templates/404.html", join(TEMPLATES_DIR, "404.html")), "utf8");
	const heroHtml = buildHero(site);
	const footer = escapeHtml(site.footer || "");
	const siteTitle = escapeHtml(site.title || "Site");
	const lang = escapeAttribute(site.lang || "en");
	const warnings = [];
	const searchIndex = [];
	const tagMap = new Map();

	for (const page of site.pages) {
		const contentPath = join(runDirectory, page.file || `content/${page.slug}.md`);
		const rendered = renderMarkdown(readFileSync(contentPath, "utf8"));
		// On the home page the hero shows the site tagline, so prefer the site description there.
		const descriptionText =
			page.slug === "index"
				? site.description || page.description || site.title || ""
				: page.description || site.description || site.title || "";
		const description = escapeAttribute(descriptionText);
		const nav = buildNav(site.nav || [], page.slug);
		const toc = buildToc(rendered.headings);
		const values = {
			lang,
			title: escapeHtml(page.title || page.slug),
			siteTitle,
			description,
			nav,
			toc,
			content: rendered.html,
			footer,
			breadcrumbs: buildBreadcrumbs(page.title || page.slug),
			hero: heroHtml,
		};
		const template = page.slug === "index" ? indexTemplate : pageTemplate;
		writeFileSync(join(siteDir, `${page.slug}.html`), fillTemplate(template, values));
		searchIndex.push({
			slug: page.slug,
			title: page.title || page.slug,
			url: `${page.slug}.html`,
			text: `${page.title || ""} ${stripHtml(rendered.html)}`.trim().slice(0, 4000),
		});
		for (const tag of page.tags || []) {
			const key = slugify(tag);
			if (!tagMap.has(key)) tagMap.set(key, { label: tag, pages: [] });
			tagMap.get(key).pages.push(page);
		}
	}

	if (tagMap.size > 0) {
		for (const [key, entry] of tagMap) {
			const list = entry.pages
				.map((page) => `<li><a href="${escapeAttribute(`${page.slug}.html`)}">${escapeHtml(page.title || page.slug)}</a></li>`)
				.join("\n");
			const content = `<h1>Tag: ${escapeHtml(entry.label)}</h1>\n<ul>\n${list}\n</ul>\n<p><a href="tags.html">All tags</a></p>`;
			writeFileSync(
				join(siteDir, `tag-${key}.html`),
				fillTemplate(pageTemplate, {
					lang,
					title: escapeHtml(`Tag: ${entry.label}`),
					siteTitle,
					description: escapeAttribute(`Pages tagged ${entry.label}.`),
					nav: buildNav(site.nav || [], null),
					toc: "",
					content,
					footer,
					breadcrumbs: buildBreadcrumbs(`Tag: ${entry.label}`),
				}),
			);
		}
		const tagsList = [...tagMap.entries()]
			.sort((left, right) => left[1].label.localeCompare(right[1].label))
			.map(([key, entry]) => `<li><a href="tag-${key}.html">${escapeHtml(entry.label)}</a> (${entry.pages.length})</li>`)
			.join("\n");
		writeFileSync(
			join(siteDir, "tags.html"),
			fillTemplate(pageTemplate, {
				lang,
				title: "Tags",
				siteTitle,
				description: escapeAttribute("Browse pages by tag."),
				nav: buildNav(site.nav || [], null),
				toc: "",
				content: `<h1>Tags</h1>\n<ul class="tag-list-block">\n${tagsList}\n</ul>`,
				footer,
				breadcrumbs: buildBreadcrumbs("Tags"),
			}),
		);
	}

	writeFileSync(join(siteDir, "styles.css"), buildStyles(runDirectory, theme, site.tokens));
	copyFileSync(join(ASSETS_DIR, "app.js"), join(siteDir, "app.js"));
	copyFileSync(join(ASSETS_DIR, "search.js"), join(siteDir, "search.js"));
	writeJson(join(siteDir, "search-index.json"), searchIndex);
	writeFileSync(
		join(siteDir, "404.html"),
		fillTemplate(notFoundTemplate, { lang, siteTitle, footer }),
	);
	writeFileSync(join(siteDir, "robots.txt"), "User-agent: *\nAllow: /\n");

	const runAssets = join(runDirectory, "assets");
	if (existsSync(runAssets) && readdirSync(runAssets).length > 0) copyDirectory(runAssets, join(siteDir, "assets"));

	writeFileSync(
		join(runDirectory, "warnings.md"),
		`# Site Builder Warnings\n\nGenerated ${nowIso()}.\n\n${warnings.length ? warnings.map((item) => `- ${item}`).join("\n") : "- None."}\n`,
	);
	const logLine = `${nowIso()} — built ${site.pages.length} pages, ${tagMap.size} tags, theme "${site.theme}".\n`;
	writeFileSync(
		join(runDirectory, "build_log.md"),
		existsSync(join(runDirectory, "build_log.md"))
			? `${readFileSync(join(runDirectory, "build_log.md"), "utf8")}${logLine}`
			: `# Site Builder Build Log\n\n${logLine}`,
	);

	process.stdout.write(
		`${JSON.stringify({ runDirectory, siteDirectory: siteDir, pages: site.pages.length, tags: tagMap.size, theme: site.theme }, null, 2)}\n`,
	);
}

// --- validate --------------------------------------------------------------

function collectHeadingLevels(html) {
	const levels = [];
	for (const match of html.matchAll(/<h([1-4])\b/gi)) levels.push(Number(match[1]));
	return levels;
}

function commandValidate(positionals) {
	if (positionals.length !== 1) fail("validate requires exactly one run directory");
	const runDirectory = resolve(positionals[0]);
	if (!existsSync(runDirectory) || !lstatSync(runDirectory).isDirectory()) {
		fail(`run directory does not exist: ${runDirectory}`);
	}
	const errors = [];
	const warnings = [];

	const manifestPath = join(runDirectory, "source_manifest.json");
	if (!existsSync(manifestPath)) fail(`source_manifest.json not found in ${runDirectory}`);
	const manifest = readJson(manifestPath);
	for (const source of manifest.sources || []) {
		if (!existsSync(source.path)) {
			errors.push(`source missing since init: ${source.name} (${source.path})`);
			continue;
		}
		if (sha256(readFileSync(source.path)) !== source.sha256) errors.push(`source changed since init: ${source.name}`);
	}

	const sitePath = join(runDirectory, "site.json");
	if (!existsSync(sitePath)) fail(`site.json not found in ${runDirectory}`);
	const site = readJson(sitePath);
	const siteDir = join(runDirectory, "site");
	if (!existsSync(siteDir)) {
		errors.push("site/ has not been built; run build");
		process.stdout.write(`${JSON.stringify({ valid: false, errors, warnings }, null, 2)}\n`);
		process.exit(1);
	}

	for (const page of site.pages || []) {
		const builtPath = join(siteDir, `${page.slug}.html`);
		if (!existsSync(builtPath)) {
			errors.push(`page not built: ${page.slug}.html`);
			continue;
		}
		const html = readFileSync(builtPath, "utf8");
		if (html.includes(PLACEHOLDER)) errors.push(`${page.slug}.html still contains the placeholder marker`);
		if (!/<html\s+lang="[^"]+"/i.test(html)) errors.push(`${page.slug}.html is missing <html lang>`);
		if (!/<title>[^<]+<\/title>/i.test(html)) errors.push(`${page.slug}.html has an empty <title>`);
		const h1Count = (html.match(/<h1\b/gi) || []).length;
		if (h1Count !== 1) errors.push(`${page.slug}.html must have exactly one <h1> (found ${h1Count})`);
		const levels = collectHeadingLevels(html);
		for (let i = 1; i < levels.length; i += 1) {
			if (levels[i] - levels[i - 1] > 1) {
				warnings.push(`${page.slug}.html skips a heading level (h${levels[i - 1]} → h${levels[i]})`);
				break;
			}
		}
		const article = html.split('<article')[1] || "";
		for (const imgMatch of article.matchAll(/<img\b[^>]*>/gi)) {
			if (!/\balt\s*=/.test(imgMatch[0])) errors.push(`${page.slug}.html has an image without an alt attribute`);
		}
		for (const refMatch of html.matchAll(/(?:href|src)="([^"]+)"/gi)) {
			const target = refMatch[1];
			if (/^(?:https?:|mailto:|tel:|data:|#)/i.test(target)) {
				if (/^https?:/i.test(target)) warnings.push(`${page.slug}.html links externally to ${target} (not verified)`);
				continue;
			}
			const localPath = decodeURI(target.split("#")[0].split("?")[0]);
			if (!localPath) continue;
			if (!existsSync(join(siteDir, localPath))) errors.push(`${page.slug}.html references missing local file: ${target}`);
		}
	}

	const dedupedWarnings = [...new Set(warnings)];
	process.stdout.write(`${JSON.stringify({ valid: errors.length === 0, errors, warnings: dedupedWarnings }, null, 2)}\n`);
	if (errors.length > 0) process.exit(1);
}

// --- doctor ----------------------------------------------------------------

function commandDoctor(flags) {
	const nodeOk = Number.parseInt(process.versions.node.split(".")[0], 10) >= 18;
	const assetsOk = existsSync(ASSETS_DIR) && existsSync(TEMPLATES_DIR) && existsSync(THEMES_DIR);
	let writable = false;
	try {
		const probe = join(process.cwd(), `.site-builder-probe-${process.pid}`);
		writeFileSync(probe, "ok");
		rmSync(probe);
		writable = true;
	} catch {
		writable = false;
	}
	const report = {
		node: { version: process.version, ok: nodeOk },
		skillAssets: { ok: assetsOk, themes: [...THEMES] },
		cwdWritable: writable,
		capabilities: { build: nodeOk && assetsOk },
		remediation: [
			...(nodeOk ? [] : ["Node 18+ is required."]),
			...(assetsOk ? [] : ["Skill assets are missing; reinstall the site-builder skill."]),
			...(writable ? [] : ["The current working directory is not writable; choose a writable --output path."]),
		],
	};
	if (flags.json) {
		process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
		return;
	}
	process.stdout.write(`node: ${report.node.version} (${nodeOk ? "ok" : "too old"})\n`);
	process.stdout.write(`skill assets: ${assetsOk ? "present" : "missing"}\n`);
	process.stdout.write(`themes: ${[...THEMES].join(", ")}\n`);
	process.stdout.write(`build capability: ${report.capabilities.build ? "available" : "unavailable"}\n`);
	for (const item of report.remediation) process.stdout.write(`Action: ${item}\n`);
}

// --- argument parsing ------------------------------------------------------

const FLAG_SPECS = {
	"--output": { key: "output", value: true },
	"--title": { key: "title", value: true },
	"--theme": { key: "theme", value: true },
	"--templates": { key: "templates", value: false },
	"--all": { key: "all", value: false },
	"--json": { key: "json", value: false },
};

function parseArguments(args) {
	const positionals = [];
	const flags = {};
	for (let index = 0; index < args.length; index += 1) {
		const argument = args[index];
		if (argument.startsWith("--")) {
			const spec = FLAG_SPECS[argument];
			if (!spec) fail(`unknown option: ${argument}`);
			if (!spec.value) {
				flags[spec.key] = true;
				continue;
			}
			const raw = args[++index];
			if (raw === undefined) fail(`${argument} requires a value`);
			flags[spec.key] = raw;
		} else {
			positionals.push(argument);
		}
	}
	return { positionals, flags };
}

function commandEject(positionals, flags) {
	if (positionals.length !== 1) fail("eject requires exactly one run directory");
	const runDirectory = resolve(positionals[0]);
	const sitePath = join(runDirectory, "site.json");
	if (!existsSync(sitePath)) fail(`site.json not found in ${runDirectory}; run init first`);
	const site = readJson(sitePath);
	const theme = flags.theme ?? (site.theme && site.theme !== "custom" ? site.theme : "editorial");
	if (!THEMES.has(theme)) fail(`unknown theme: ${theme} (choose ${[...THEMES].join(", ")})`);
	const copied = [];
	const copyNoClobber = (source, destination, label) => {
		if (existsSync(destination)) fail(`refusing to overwrite existing ${label}; delete it first to re-eject`);
		mkdirSync(dirname(destination), { recursive: true });
		copyFileSync(source, destination);
		copied.push(label);
	};
	copyNoClobber(join(THEMES_DIR, theme, "tokens.css"), join(runDirectory, "theme", "tokens.css"), "theme/tokens.css");
	if (flags.all) {
		copyNoClobber(join(ASSETS_DIR, "base.css"), join(runDirectory, "theme", "base.css"), "theme/base.css");
		copyNoClobber(join(ASSETS_DIR, "print.css"), join(runDirectory, "theme", "print.css"), "theme/print.css");
	}
	if (flags.templates || flags.all) {
		for (const name of ["page.html", "index.html", "404.html"]) {
			copyNoClobber(join(TEMPLATES_DIR, name), join(runDirectory, "templates", name), `templates/${name}`);
		}
	}
	process.stdout.write(
		`${JSON.stringify({ runDirectory, theme, copied, note: "Edit these files freely; build prefers run-local overrides. Set theme to \"custom\" for a fully bespoke look." }, null, 2)}\n`,
	);
}

function usage() {
	process.stdout.write(`Usage:
  site-builder.mjs doctor [--json]
  site-builder.mjs init <inputs...> --output <dir> [--title "<title>"]
      [--theme editorial|technical|archival|gallery|magazine|academic|brand|terminal]
  site-builder.mjs build <run-directory>
  site-builder.mjs eject <run-directory> [--theme <name>] [--templates] [--all]
  site-builder.mjs validate <run-directory>
`);
}

function main() {
	const [command, ...rest] = process.argv.slice(2);
	if (!command || command === "--help" || command === "-h") {
		usage();
		process.exit(command ? 0 : 2);
	}
	const { positionals, flags } = parseArguments(rest);
	if (command === "doctor") commandDoctor(flags);
	else if (command === "init") commandInit(positionals, flags);
	else if (command === "build") commandBuild(positionals);
	else if (command === "eject") commandEject(positionals, flags);
	else if (command === "validate") commandValidate(positionals);
	else fail(`unknown command: ${command}`, 2);
}

main();
