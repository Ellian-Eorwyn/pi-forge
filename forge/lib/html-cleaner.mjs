import { spawnSync } from "node:child_process";
import { JSDOM } from "jsdom";
import { Readability } from "@mozilla/readability";

/**
 * Parses raw HTML, extracts the main article via Readability, converts it to Markdown via pandoc,
 * and validates/polishes the result using a local LLM. Features a heavy fallback if extraction fails.
 *
 * @param {Buffer} buffer - The raw HTML buffer
 * @param {string} url - The URL or name of the document being processed (for LLM context)
 * @returns {Promise<string | null>} The cleaned Markdown, or null if empty
 */
export async function htmlToCleanMarkdown(buffer, url) {
	const htmlString = buffer.toString("utf8");
	let rawMarkdown = "";
	let isReadabilityPass = true;
	
	try {
		const doc = new JSDOM(htmlString, { url }).window.document;
		const reader = new Readability(doc);
		const article = reader.parse();
		if (article && article.content) {
			const result = spawnSync("pandoc", ["--from=html", "--to=gfm", "--wrap=none"], { input: article.content, encoding: "utf8" });
			if (result.status === 0 && result.stdout) {
				rawMarkdown = result.stdout;
			}
		}
	} catch (e) {
		// Ignore JSDOM/Readability errors and fall through to failure
	}
	
	if (!rawMarkdown.trim()) {
		isReadabilityPass = false;
		const result = spawnSync("pandoc", ["--from=html", "--to=gfm", "--wrap=none"], { input: buffer, encoding: "utf8" });
		if (result.error || result.status !== 0) {
			throw new Error(result.stderr?.trim() || result.error?.message || `pandoc exited with ${result.status}`);
		}
		rawMarkdown = result.stdout;
	}
	
	if (!rawMarkdown.trim()) return null;

	const baseChatUrl = process.env.FORGE_BASE_CHAT_URL || process.env.FORGE_CHAT_URL || "http://llms:8008/v1/chat/completions";
	const baseModel = process.env.FORGE_BASE_MODEL || "llama-3.3-70b-versatile";
	
	let instruction = "";
	if (isReadabilityPass) {
		instruction = `The following is an article extracted from the document/webpage ${url}. Please review it for formatting issues and clean it up if it's messy. Do not remove core content. If the extraction looks completely wrong (e.g., you only see a copyright footer or navigation links instead of the main article content), output exactly the string <EXTRACTION_FAILED> and nothing else. Do NOT wrap your response in markdown code blocks. Just output the clean markdown.`;
	} else {
		instruction = `The following is raw Markdown extracted from the document/webpage ${url}. Please strip out all boilerplate content, navigation menus, sidebars, headers, and footers. Return only the core informational content of the page, formatted as clean Markdown. Maintain all relevant headings, lists, and links. Do NOT wrap your response in markdown code blocks. Just output the clean markdown.`;
	}
	
	let cleanMarkdown = "";
	try {
		let response = await fetch(baseChatUrl, {
			method: "POST",
			headers: { "Content-Type": "application/json", "Authorization": "Bearer local" },
			body: JSON.stringify({
				model: baseModel,
				messages: [
					{ role: "system", content: "You are an expert web scraper assistant that cleans raw webpage extracts into crisp, readable markdown." },
					{ role: "user", content: `${instruction}\n\n=== EXTRACTED MARKDOWN ===\n${rawMarkdown.substring(0, 100000)}` }
				],
				temperature: 0.1
			})
		});
		if (!response.ok) throw new Error(`LLM returned HTTP ${response.status}`);
		let data = await response.json();
		cleanMarkdown = data.choices[0]?.message?.content?.trim() || "";
	} catch (e) {
		// If the LLM is completely unreachable (e.g. tests or network issues), just return the raw markdown
		return rawMarkdown;
	}
	
	if (isReadabilityPass && cleanMarkdown.includes("<EXTRACTION_FAILED>")) {
		const result = spawnSync("pandoc", ["--from=html", "--to=gfm", "--wrap=none"], { input: buffer, encoding: "utf8" });
		if (result.error || result.status !== 0) throw new Error(result.stderr?.trim() || result.error?.message || `pandoc exited with ${result.status}`);
		
		const fallbackInstruction = `The following is raw Markdown extracted from the document/webpage ${url}. Please strip out all boilerplate content, navigation menus, sidebars, headers, and footers. Return only the core informational content of the page, formatted as clean Markdown. Maintain all relevant headings, lists, and links. Do NOT wrap your response in markdown code blocks. Just output the clean markdown.`;
		
		response = await fetch(baseChatUrl, {
			method: "POST",
			headers: { "Content-Type": "application/json", "Authorization": "Bearer local" },
			body: JSON.stringify({
				model: baseModel,
				messages: [
					{ role: "system", content: "You are an expert web scraper assistant that cleans raw webpage extracts into crisp, readable markdown." },
					{ role: "user", content: `${fallbackInstruction}\n\n=== RAW MARKDOWN ===\n${result.stdout.substring(0, 100000)}` }
				],
				temperature: 0.1
			})
		});
		if (!response.ok) throw new Error(`LLM returned HTTP ${response.status}`);
		data = await response.json();
		cleanMarkdown = data.choices[0]?.message?.content?.trim() || "";
	}
	
	if (cleanMarkdown.startsWith("```markdown")) {
		cleanMarkdown = cleanMarkdown.replace(/^```markdown\n/, "").replace(/\n```$/, "");
	} else if (cleanMarkdown.startsWith("```")) {
		cleanMarkdown = cleanMarkdown.replace(/^```\n/, "").replace(/\n```$/, "");
	}
	return cleanMarkdown || null;
}
