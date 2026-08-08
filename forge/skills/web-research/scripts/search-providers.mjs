// The search provider registry.
//
// Until this module existed, every general web query in forge went to one
// backend: the self-hosted SearXNG instance. SearXNG is a metasearch scraper, so
// the rate limits it hits belong to Google and Bing rather than to the instance,
// and a throttled instance answers HTTP 200 with an empty result list. That made
// throttling indistinguishable from an honest miss. lib/searxng.mjs now raises
// on that case; this module is the other half of the answer -- when a question
// has an authoritative source, ask the source directly and never involve a
// scraper at all.
//
// The shape deliberately mirrors ACADEMIC_PROVIDERS in web-research.mjs: a
// uniform per-provider interface, a base URL with a per-provider environment
// override (which is also what makes providers testable against a fixture),
// declared capabilities, and selection driven by a query classifier.
//
// Two entry points, because the two consumers want different things:
//   search(query, context)  -> ranked results, for web-research
//   resolve(topic, context) -> one canonical entry or null, for vault-wiki
//
// Providers deliberately absent, having been checked against the live services
// on 2026-07-31 rather than assumed:
//   - 84000 (Tibetan Kangyur/Tengyur) serves HTML only; /api/* is 404 and
//     /section/all-translated.json returns a 6MB HTML page. No JSON API exists
//     to call, and scraping it is not a substitute.
//   - BDRC has no public search endpoint -- library.bdrc.io/api/search is the
//     single-page app's own route and returns the app shell. Resource lookup by
//     ID does work, so bdrc below is lookup-only. Internet Archive indexes a
//     good deal of BDRC's scanned material and covers part of the gap.
//   - PhilPapers answers HTTP 403 to any non-browser client. Cloudflare blocks
//     /help/api, /philpapers/raw/api and philarchive.org alike, so the "key on
//     request" the plan recorded cannot be exercised even if one were granted.
//     vault-wiki still reaches philpapers.org through site-restricted search,
//     because SearXNG's upstream engines are browsers as far as Cloudflare is
//     concerned.
//
// Six providers here need a key. They are declared `authRequired` and skipped
// with a logged reason when none is configured, which is what keeps the no-key
// tier working on a machine nobody has set up. Marginalia publishes a shared
// `public` key for integration work; it is documented rather than used, because
// its own API page says that key "often hits a rate limit" and a provider that
// intermittently fails is worse than one that is honestly absent.

import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { CONFIGURED_ENDPOINT_RULES, httpJson, httpText } from "../../../lib/http-fetch.mjs";
import { atomicWriteFile } from "../../../lib/run-state.mjs";
import { DEFAULT_SEARCH_TIMEOUT_MS, pingSearxng, searchLimiter, searchSearxng } from "../../../lib/searxng.mjs";
import { defaultCacheDirectory, normalizeUrl } from "./acquisition.mjs";

// Re-exported so web-research has one import for everything search-shaped, even
// though the SearXNG client itself lives in lib/ because web-collection uses it
// too and skills do not import across skill boundaries.
export { pingSearxng, searchLimiter, searchSearxng };

export const SEARCH_PROVIDER_ENV = { searxng: "FORGE_SEARXNG_URL" };

function envVarFor(id) {
	return SEARCH_PROVIDER_ENV[id] ?? `FORGE_SEARCH_${id.toUpperCase().replace(/[^A-Z0-9]+/g, "_")}_URL`;
}

/**
 * Base URL for a provider: explicit flag, then environment, then the registry
 * default. Same ladder as academicProviderBase in web-research.mjs, and the
 * environment layer is what lets the suite point a provider at a fixture.
 */
export function searchProviderBase(id, flags = {}, env = process.env) {
	const explicit = flags[`${id}Base`] ?? (id === "searxng" ? flags.searxng : undefined);
	if (typeof explicit === "string" && explicit.trim()) return explicit.trim().replace(/\/+$/, "");
	const fromEnv = env[envVarFor(id)];
	if (typeof fromEnv === "string" && fromEnv.trim()) return fromEnv.trim().replace(/\/+$/, "");
	return SEARCH_PROVIDERS[id]?.base ?? null;
}

function domainFromUrl(url) {
	try {
		return new URL(url).hostname.toLowerCase();
	} catch {
		return null;
	}
}

/**
 * The normalized result shape shared by every provider. Identical to the record
 * web-research already wrote for SearXNG hits, so the URL queue, the report
 * writers and every downstream reader consume a new provider unchanged.
 */
export function searchResultRecord(result, index, query, retrievedAt, provider = null) {
	const url = result.url ?? null;
	return {
		query,
		rank: index + 1,
		title: result.title ?? null,
		url,
		canonicalUrl: url ? normalizeUrl(url) : null,
		domain: url ? domainFromUrl(url) : null,
		content: result.content ?? null,
		snippet: result.content ?? null,
		engine: result.engine ?? (Array.isArray(result.engines) ? result.engines.join(",") : null),
		score: result.score ?? null,
		publishedAt: result.publishedDate ?? result.published_at ?? result.publishedAt ?? null,
		provider,
		retrievedAt,
	};
}

function records(rows, query, context, provider) {
	const retrievedAt = context.retrievedAt ?? new Date().toISOString();
	const limit = context.limit ?? rows.length;
	return rows.slice(0, limit).map((row, index) => searchResultRecord(row, index, query, retrievedAt, provider));
}

/** Collapse HTML to the plain text a snippet field is supposed to hold. */
function stripHtml(value) {
	return String(value ?? "")
		.replace(/<[^>]*>/g, "")
		.replace(/&quot;/g, '"')
		.replace(/&#0?39;/g, "'")
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&nbsp;/g, " ")
		.replace(/\s+/g, " ")
		.trim();
}

function truncate(value, max = 500) {
	const text = String(value ?? "").trim();
	return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/**
 * Minimum milliseconds between requests to a provider, where the service
 * publishes a figure. These are the documented limits, not guesses:
 * GDELT answers HTTP 429 to anyone exceeding one request every five seconds,
 * and says so in the response body.
 */
export const PROVIDER_SPACING_MS = {
	gdelt: 5000,
	inpho: 1000,
	cbeta: 1000,
	suttacentral: 500,
	openlibrary: 1000,
	loc: 1000,
	internetarchive: 1000,
	// The keyed tier publishes exact rates rather than asking for restraint.
	// NYT's is the tightest by an order of magnitude: five requests a minute.
	guardian: 100,
	nyt: 12_000,
	marginalia: 2000,
	wolfram: 1000,
	exa: 500,
	tavily: 500,
};

function spacingFor(id, options) {
	return options.spacingMs ?? PROVIDER_SPACING_MS[id] ?? undefined;
}

async function providerJson(url, context, options = {}) {
	const { json } = await httpJson(url, {
		headers: { "user-agent": context.userAgent, ...(options.headers ?? {}) },
		timeoutMs: context.timeoutMs ?? DEFAULT_SEARCH_TIMEOUT_MS,
		attempts: options.attempts ?? 3,
		hostRules: CONFIGURED_ENDPOINT_RULES,
		limiter: context.limiter ?? searchLimiter,
		spacingMs: spacingFor(context.providerId, options),
	});
	return json;
}

/**
 * The same call with a JSON request body. Exa and Tavily are POST-only -- Exa
 * answers `Cannot GET /search` -- and both take their key in a header, so
 * nothing here needs redacting.
 */
async function providerPostJson(url, body, context, options = {}) {
	const { json } = await httpJson(url, {
		method: "POST",
		body: JSON.stringify(body),
		headers: { "user-agent": context.userAgent, "content-type": "application/json", ...(options.headers ?? {}) },
		timeoutMs: context.timeoutMs ?? DEFAULT_SEARCH_TIMEOUT_MS,
		attempts: options.attempts ?? 3,
		hostRules: CONFIGURED_ENDPOINT_RULES,
		limiter: context.limiter ?? searchLimiter,
		spacingMs: spacingFor(context.providerId, options),
	});
	return json;
}

async function providerText(url, context, options = {}) {
	const { text } = await httpText(url, {
		headers: { "user-agent": context.userAgent, ...(options.headers ?? {}) },
		timeoutMs: context.timeoutMs ?? DEFAULT_SEARCH_TIMEOUT_MS,
		attempts: options.attempts ?? 3,
		hostRules: CONFIGURED_ENDPOINT_RULES,
		limiter: context.limiter ?? searchLimiter,
		spacingMs: spacingFor(context.providerId, options),
	});
	return text;
}

const noAuth = { authRequired: false, optionalAuth: false };

// --- MediaWiki -------------------------------------------------------------

/**
 * Any MediaWiki site behind one implementation. Wikipedia and Wiktionary differ
 * only in base URL and topic, and several other reference wikis in this space
 * (Rangjung Yeshe, the Encyclopedia of Buddhism) run MediaWiki too, so adding
 * one later is a registry entry rather than code.
 */
function mediaWikiProvider({ id, label, base, site, topics, authority, kinds, strengths, limits }) {
	return {
		id,
		label,
		kind: "reference",
		base,
		site,
		topics,
		authority,
		capabilities: () => ({
			...noAuth,
			rateLimit: "anonymous MediaWiki API; be polite rather than parallel",
			fields: ["title", "url", "snippet", "pageid"],
			strengths,
			limits,
			kinds,
		}),
		async search(query, context) {
			const url = `${context.base}/w/api.php?${new URLSearchParams({
				action: "query",
				list: "search",
				srsearch: query,
				srlimit: String(context.limit ?? 10),
				format: "json",
				origin: "*",
			})}`;
			const payload = await providerJson(url, context);
			const hits = payload?.query?.search ?? [];
			return records(
				hits.map((hit) => ({
					title: hit.title,
					url: `https://${site}/wiki/${encodeURIComponent(String(hit.title).replace(/ /g, "_"))}`,
					content: stripHtml(hit.snippet),
					score: hit.wordcount ?? null,
					publishedAt: hit.timestamp ?? null,
					engine: id,
				})),
				query,
				context,
				id,
			);
		},
		/**
		 * Entry candidates for a name, via `opensearch` rather than `list=search`.
		 *
		 * The two are different questions. `list=search` is full text and ranks by
		 * relevance across article bodies; `opensearch` is a title/prefix lookup,
		 * which is what "find the article named X" means and what vault-wiki's own
		 * resolver has always asked for. Callers score these themselves, so no
		 * ranking judgement is made here.
		 */
		async referenceCandidates(subject, context) {
			const url = `${context.base}/w/api.php?${new URLSearchParams({
				action: "opensearch",
				search: subject,
				limit: String(context.limit ?? 6),
				format: "json",
			})}`;
			const payload = await providerJson(url, context);
			if (!Array.isArray(payload) || payload.length < 4) return [];
			const [, titles, descriptions, urls] = payload;
			if (!Array.isArray(titles) || !Array.isArray(urls)) return [];
			return titles.map((title, index) => ({
				title,
				url: urls[index] ?? null,
				snippet: Array.isArray(descriptions) ? descriptions[index] || null : null,
			}));
		},
		/**
		 * The REST summary endpoint, which returns the lead extract rather than
		 * the whole article. A wiki note wants a definition, not a download.
		 */
		async resolve(topic, context) {
			const [best] = await this.search(topic, { ...context, limit: 1 });
			if (!best) return null;
			const title = String(best.title).replace(/ /g, "_");
			try {
				const summary = await providerJson(
					`${context.base}/api/rest_v1/page/summary/${encodeURIComponent(title)}`,
					context,
				);
				if (summary?.type === "disambiguation") return null;
				return {
					...best,
					content: summary?.extract ?? best.content,
					snippet: summary?.extract ?? best.content,
					canonicalTitle: summary?.titles?.normalized ?? best.title,
					wikidataId: summary?.wikibase_item ?? null,
				};
			} catch {
				// A missing summary is not a missing entry; the search hit stands.
				return best;
			}
		},
	};
}

// --- Registry --------------------------------------------------------------

export const SEARCH_PROVIDERS = {
	wikipedia: mediaWikiProvider({
		id: "wikipedia",
		label: "Wikipedia",
		base: "https://en.wikipedia.org",
		site: "en.wikipedia.org",
		topics: ["*"],
		// Ranked last among reference sources on purpose, matching the judgement
		// already recorded in vault-wiki's canonical-sources.json: it is the
		// source most likely to be right and least likely to be citable.
		authority: 9,
		kinds: ["*"],
		strengths: ["universal coverage", "dates for contemporary figures", "stable titles"],
		limits: ["not citable in scholarly work", "quality varies by article"],
	}),

	wiktionary: mediaWikiProvider({
		id: "wiktionary",
		label: "Wiktionary",
		base: "https://en.wiktionary.org",
		site: "en.wiktionary.org",
		// No topic of its own: a dictionary is selected by the shape of the
		// question ("what does X mean") rather than by subject matter.
		topics: [],
		authority: 6,
		kinds: ["term"],
		strengths: ["etymology", "transliteration", "senses across languages"],
		limits: ["lexical only", "no encyclopedic context"],
	}),

	wikidata: {
		id: "wikidata",
		label: "Wikidata",
		kind: "reference",
		base: "https://www.wikidata.org",
		topics: ["*"],
		authority: 8,
		capabilities: () => ({
			...noAuth,
			rateLimit: "anonymous API",
			fields: ["id", "label", "description", "url"],
			strengths: ["entity disambiguation", "identifiers across databases", "dates"],
			limits: ["structured claims only, no prose"],
		}),
		async search(query, context) {
			const url = `${context.base}/w/api.php?${new URLSearchParams({
				action: "wbsearchentities",
				search: query,
				language: "en",
				uselang: "en",
				limit: String(Math.min(context.limit ?? 10, 50)),
				format: "json",
				origin: "*",
			})}`;
			const payload = await providerJson(url, context);
			return records(
				(payload?.search ?? []).map((entity) => ({
					title: entity.label ?? entity.id,
					url: entity.concepturi ?? `https://www.wikidata.org/wiki/${entity.id}`,
					content: entity.description ?? null,
					engine: "wikidata",
				})),
				query,
				context,
				"wikidata",
			);
		},
	},

	sep: {
		id: "sep",
		label: "Stanford Encyclopedia of Philosophy",
		kind: "reference",
		base: "https://plato.stanford.edu",
		site: "plato.stanford.edu",
		topics: [
			"philosophy",
			"ethics",
			"epistemology",
			"metaphysics",
			"phenomenology",
			"logic",
			"buddhism",
			"feminism",
			"gender-studies",
			"science-technology-studies",
			"politics",
		],
		authority: 1,
		capabilities: () => ({
			...noAuth,
			rateLimit: "one index fetch, then local matching",
			fields: ["title", "url"],
			strengths: ["peer-reviewed", "stable", "the strongest source in the philosophy cluster"],
			limits: ["entry titles are topic-based, so a person may have no entry of their own"],
		}),
		/**
		 * SEP has no search API, so this matches against its published table of
		 * contents -- the approach vault-wiki settled on. A URL is never guessed:
		 * entry slugs are topic-based rather than name-based, so there is no
		 * /entries/latour/ to construct, and asking the index is what rejects a
		 * wrong hit before anything is downloaded.
		 */
		async search(query, context) {
			const entries = await sepIndex(context);
			const needle = query.toLowerCase().trim();
			const scored = entries
				.map((entry) => ({ entry, score: titleMatchScore(entry.title.toLowerCase(), needle) }))
				.filter((row) => row.score > 0)
				.sort((left, right) => right.score - left.score);
			return records(
				scored.map(({ entry, score }) => ({
					title: entry.title,
					url: `https://plato.stanford.edu/entries/${entry.slug}/`,
					content: null,
					score,
					engine: "sep",
				})),
				query,
				context,
				"sep",
			);
		},
		/**
		 * The whole published index, unranked and unfiltered.
		 *
		 * Deliberately not `search()`, which scores with this module's own matcher.
		 * vault-wiki's matcher is calibrated against failures this one has never
		 * seen -- it is what keeps "The Theory of Two Truths in India" while
		 * rejecting the entry on *truth* for a note about the two truths doctrine --
		 * so it must see the same 2,511 candidates its `anchor_pairs` gave it, not
		 * a shortlist chosen by a different rule.
		 */
		async referenceCandidates(_subject, context) {
			const entries = await sepIndexPairs(context);
			return entries.map((entry) => ({
				title: entry.title,
				url: `https://plato.stanford.edu/entries/${entry.slug}/`,
				snippet: null,
			}));
		},
		async resolve(topic, context) {
			const [best] = await this.search(topic, { ...context, limit: 1 });
			return best ?? null;
		},
	},

	inpho: {
		id: "inpho",
		label: "InPhO",
		kind: "reference",
		base: "https://www.inphoproject.org",
		topics: ["philosophy", "ethics", "epistemology", "metaphysics", "phenomenology", "logic"],
		authority: 2,
		capabilities: () => ({
			...noAuth,
			rateLimit: "small public service; keep volume low",
			fields: ["label", "url", "sep_dir", "type"],
			strengths: ["philosophy ontology over SEP, IEP and PhilPapers", "resolves a thinker to their SEP entry slug"],
			limits: ["index rather than prose", "philosophy only"],
		}),
		/**
		 * Worth having next to `sep`: InPhO returns `sep_dir`, the actual SEP
		 * entry slug for a thinker, which is the one thing the SEP's own
		 * topic-based index cannot give you from a person's name.
		 */
		async search(query, context) {
			const rows = [];
			for (const kind of ["thinker", "idea"]) {
				const payload = await providerJson(
					`${context.base}/${kind}.json?q=${encodeURIComponent(query)}`,
					context,
				).catch(() => null);
				for (const hit of payload?.responseData?.results ?? []) {
					rows.push({
						title: hit.label ?? hit.wiki ?? null,
						url: hit.sep_dir
							? `https://plato.stanford.edu/entries/${hit.sep_dir}/`
							: `${context.base}${hit.url ?? ""}`,
						content: hit.sep_dir ? `SEP entry: ${hit.sep_dir}` : null,
						engine: `inpho:${kind}`,
					});
				}
			}
			return records(rows, query, context, "inpho");
		},
	},

	iep: {
		id: "iep",
		label: "Internet Encyclopedia of Philosophy",
		kind: "reference",
		base: "https://iep.utm.edu",
		site: "iep.utm.edu",
		topics: [
			"philosophy",
			"ethics",
			"epistemology",
			"metaphysics",
			"phenomenology",
			"logic",
			"feminism",
			"gender-studies",
		],
		authority: 2,
		capabilities: () => ({
			...noAuth,
			rateLimit: "WordPress REST search",
			fields: ["title", "url"],
			strengths: ["peer-reviewed", "often covers a figure the SEP has no standalone entry for"],
			limits: ["shorter and less current than the SEP"],
		}),
		async search(query, context) {
			const url = `${context.base}/wp-json/wp/v2/search?search=${encodeURIComponent(query)}&per_page=${Math.min(context.limit ?? 10, 20)}`;
			const payload = await providerJson(url, context);
			return records(
				(Array.isArray(payload) ? payload : []).map((hit) => ({
					title: hit.title ?? null,
					url: hit.url ?? null,
					content: null,
					engine: "iep",
				})),
				query,
				context,
				"iep",
			);
		},
		/**
		 * WordPress returns its own relevance order and no score. `resolve()` above
		 * takes the first hit on trust, which is exactly what a caller with a real
		 * matcher must not do -- so this hands back the page of hits unjudged.
		 */
		async referenceCandidates(subject, context) {
			const url = `${context.base}/wp-json/wp/v2/search?search=${encodeURIComponent(subject)}&per_page=${Math.min(context.limit ?? 6, 20)}`;
			const payload = await providerJson(url, context);
			return (Array.isArray(payload) ? payload : [])
				.filter((hit) => hit?.url)
				.map((hit) => ({ title: stripHtml(hit.title) || null, url: hit.url, snippet: null }));
		},
		async resolve(topic, context) {
			const [best] = await this.search(topic, { ...context, limit: 1 });
			return best ?? null;
		},
	},

	suttacentral: {
		id: "suttacentral",
		label: "SuttaCentral",
		kind: "canon",
		base: "https://suttacentral.net",
		site: "suttacentral.net",
		topics: ["buddhism", "religion"],
		authority: 1,
		capabilities: () => ({
			...noAuth,
			rateLimit: "public API, no key",
			fields: ["uid", "acronym", "title", "url", "lang", "author", "blurb"],
			strengths: ["Pali canon and parallels", "multiple translations per sutta", "citation-stable uids"],
			limits: ["early Buddhist texts; little later commentary"],
		}),
		async search(query, context) {
			const url = `${context.base}/api/search/instant?${new URLSearchParams({
				query,
				limit: String(context.limit ?? 10),
			})}`;
			const payload = await providerJson(url, context);
			return records(
				(payload?.hits ?? []).map((hit) => ({
					title: [hit.acronym, hit.name].filter(Boolean).join(" — ") || hit.uid,
					url: `${context.base}${hit.url ?? `/${hit.uid}`}`,
					content: truncate(stripHtml((hit.highlight?.content ?? []).join(" "))),
					engine: "suttacentral",
				})),
				query,
				context,
				"suttacentral",
			);
		},
		/**
		 * A sutta reference such as "MN 118" is an identifier, not a search term:
		 * suttaplex resolves it to the canonical record with every available
		 * translation, which is what a citation needs.
		 */
		async resolve(topic, context) {
			const uid = suttaUid(topic);
			if (!uid) {
				const [best] = await this.search(topic, { ...context, limit: 1 });
				return best ?? null;
			}
			const payload = await providerJson(`${context.base}/api/suttaplex/${encodeURIComponent(uid)}`, context).catch(
				() => null,
			);
			const entry = Array.isArray(payload) ? payload[0] : payload;
			if (!entry?.uid) return null;
			const retrievedAt = context.retrievedAt ?? new Date().toISOString();
			return searchResultRecord(
				{
					title: [entry.acronym, entry.translated_title || entry.original_title].filter(Boolean).join(" — "),
					url: `${context.base}/${entry.uid}`,
					content: entry.blurb ?? null,
					engine: "suttacentral",
				},
				0,
				topic,
				retrievedAt,
				"suttacentral",
			);
		},
	},

	cbeta: {
		id: "cbeta",
		label: "CBETA",
		kind: "canon",
		base: "https://cbdata.dila.edu.tw/stable",
		site: "cbetaonline.dila.edu.tw",
		topics: ["buddhism", "religion"],
		authority: 1,
		capabilities: () => ({
			...noAuth,
			rateLimit: "public API run by Dharma Drum Institute; keep volume low",
			fields: ["work", "juan", "title", "byline", "creators", "canon", "dynasty"],
			strengths: ["Chinese Buddhist canon full-text search", "Taisho numbering", "dynasty and translator metadata"],
			limits: ["Chinese script; a query in English will not match"],
		}),
		async search(query, context) {
			const url = `${context.base}/search?${new URLSearchParams({ q: query, rows: String(context.limit ?? 10) })}`;
			const payload = await providerJson(url, context);
			return records(
				(payload?.results ?? []).map((hit) => ({
					title: [hit.work, hit.title].filter(Boolean).join(" "),
					url: `https://cbetaonline.dila.edu.tw/${hit.work}`,
					content: [hit.byline, hit.canon, hit.time_dynasty].filter(Boolean).join(" · ") || null,
					score: hit.term_hits ?? null,
					engine: "cbeta",
				})),
				query,
				context,
				"cbeta",
			);
		},
	},

	bdrc: {
		id: "bdrc",
		label: "Buddhist Digital Resource Center",
		kind: "canon",
		base: "https://purl.bdrc.io",
		site: "purl.bdrc.io",
		topics: ["buddhism", "religion"],
		authority: 2,
		capabilities: () => ({
			...noAuth,
			rateLimit: "public linked-data service",
			fields: ["@id", "rdfs:label", "@type"],
			strengths: ["Tibetan Buddhist texts and persons", "stable BDRC identifiers", "JSON-LD"],
			// Checked live on 2026-07-31: there is no public search endpoint. The
			// SPARQL templates that look like search all require a resource ID,
			// and library.bdrc.io/api/search is the single-page app's own route.
			limits: ["lookup by BDRC ID only, no keyword search"],
		}),
		/** Lookup-only. A BDRC id (P1614, W1KG4884) resolves; a phrase does not. */
		async resolve(topic, context) {
			const id = String(topic)
				.trim()
				.match(/^(?:bdr:)?([PWGCTR]\w{2,})$/i)?.[1];
			if (!id) return null;
			const payload = await providerJson(`${context.base}/resource/${id}.jsonld`, context, {
				headers: { accept: "application/ld+json" },
			}).catch(() => null);
			const graph = payload?.["@graph"] ?? [];
			const labelled = graph.find((node) => node?.["rdfs:label"]);
			const label = labelled?.["rdfs:label"];
			const title = (Array.isArray(label) ? label[0]?.["@value"] : label?.["@value"]) ?? id;
			const retrievedAt = context.retrievedAt ?? new Date().toISOString();
			return searchResultRecord(
				{ title, url: `https://library.bdrc.io/show/bdr:${id}`, content: null, engine: "bdrc" },
				0,
				topic,
				retrievedAt,
				"bdrc",
			);
		},
	},

	openlibrary: {
		id: "openlibrary",
		label: "Open Library",
		kind: "books",
		base: "https://openlibrary.org",
		site: "openlibrary.org",
		topics: ["*"],
		authority: 3,
		capabilities: () => ({
			...noAuth,
			rateLimit: "no key; not for bulk use -- take the data dumps for that",
			fields: ["key", "title", "author_name", "first_publish_year", "isbn", "edition_count"],
			strengths: ["book metadata across all publishers", "ISBN resolution", "edition counts"],
			limits: ["catalogue, not full text", "community-edited records vary"],
		}),
		async search(query, context) {
			const url = `${context.base}/search.json?${new URLSearchParams({
				q: query,
				limit: String(context.limit ?? 10),
				fields: "key,title,author_name,first_publish_year,isbn,edition_count",
			})}`;
			const payload = await providerJson(url, context);
			return records(
				(payload?.docs ?? []).map((doc) => ({
					title: doc.title,
					url: `${context.base}${doc.key}`,
					content:
						[
							(doc.author_name ?? []).join(", "),
							doc.first_publish_year ? `first published ${doc.first_publish_year}` : null,
							doc.edition_count ? `${doc.edition_count} editions` : null,
						]
							.filter(Boolean)
							.join(" · ") || null,
					publishedAt: doc.first_publish_year ? String(doc.first_publish_year) : null,
					engine: "openlibrary",
				})),
				query,
				context,
				"openlibrary",
			);
		},
	},

	gutendex: {
		id: "gutendex",
		label: "Project Gutenberg",
		kind: "books",
		base: "https://gutendex.com",
		site: "gutenberg.org",
		topics: ["*"],
		authority: 3,
		capabilities: () => ({
			...noAuth,
			rateLimit: "no key",
			fields: ["id", "title", "authors", "subjects", "formats"],
			strengths: ["public-domain full texts, not just metadata", "direct plain-text download URLs"],
			limits: ["public domain only"],
		}),
		async search(query, context) {
			const payload = await providerJson(`${context.base}/books?search=${encodeURIComponent(query)}`, context);
			return records(
				(payload?.results ?? []).map((book) => ({
					title: book.title,
					// Prefer the readable HTML edition; fall back to the catalogue page.
					url:
						book.formats?.["text/html"] ??
						book.formats?.["text/plain; charset=utf-8"] ??
						`https://www.gutenberg.org/ebooks/${book.id}`,
					content: truncate((book.authors ?? []).map((author) => author.name).join(", ")) || null,
					engine: "gutendex",
				})),
				query,
				context,
				"gutendex",
			);
		},
	},

	internetarchive: {
		id: "internetarchive",
		label: "Internet Archive",
		kind: "books",
		base: "https://archive.org",
		site: "archive.org",
		topics: ["*"],
		authority: 4,
		capabilities: () => ({
			...noAuth,
			rateLimit: "no key; advancedsearch is not for bulk harvesting",
			fields: ["identifier", "title", "year", "creator"],
			strengths: ["scanned books and media", "indexes a good deal of BDRC's Tibetan material"],
			limits: ["mixed quality", "lending restrictions on many items"],
		}),
		async search(query, context) {
			const params = new URLSearchParams({ q: query, rows: String(context.limit ?? 10), output: "json" });
			for (const field of ["identifier", "title", "year", "creator"]) params.append("fl[]", field);
			const payload = await providerJson(`${context.base}/advancedsearch.php?${params}`, context);
			return records(
				(payload?.response?.docs ?? []).map((doc) => ({
					title: doc.title ?? doc.identifier,
					url: `${context.base}/details/${doc.identifier}`,
					content:
						[Array.isArray(doc.creator) ? doc.creator.join(", ") : doc.creator, doc.year]
							.filter(Boolean)
							.join(" · ") || null,
					publishedAt: doc.year ? String(doc.year) : null,
					engine: "internetarchive",
				})),
				query,
				context,
				"internetarchive",
			);
		},
	},

	loc: {
		id: "loc",
		label: "Library of Congress",
		kind: "books",
		base: "https://www.loc.gov",
		site: "loc.gov",
		topics: ["history", "politics", "literature", "music", "art", "geography"],
		authority: 3,
		capabilities: () => ({
			...noAuth,
			rateLimit: "no key; the service asks for restraint rather than publishing a number",
			fields: ["id", "title", "date", "description"],
			strengths: ["US primary sources", "historical newspapers", "digitized collections"],
			limits: ["US-centric", "JSON shape varies by collection"],
		}),
		async search(query, context) {
			const url = `${context.base}/search/?${new URLSearchParams({ q: query, fo: "json", c: String(context.limit ?? 10) })}`;
			const payload = await providerJson(url, context);
			return records(
				(payload?.results ?? []).map((item) => ({
					title: Array.isArray(item.title) ? item.title[0] : item.title,
					url: item.id ?? item.url ?? null,
					content: truncate(Array.isArray(item.description) ? item.description[0] : item.description),
					publishedAt: Array.isArray(item.date) ? item.date[0] : (item.date ?? null),
					engine: "loc",
				})),
				query,
				context,
				"loc",
			);
		},
	},

	hathitrust: {
		id: "hathitrust",
		label: "HathiTrust",
		kind: "books",
		base: "https://catalog.hathitrust.org",
		site: "catalog.hathitrust.org",
		topics: ["*"],
		authority: 4,
		capabilities: () => ({
			...noAuth,
			rateLimit: "Bibliographic API; the bulk Data API is the one that needs a key",
			fields: ["records", "items"],
			strengths: ["holdings across research libraries", "resolves an ISBN, OCLC or LCCN to scanned volumes"],
			limits: ["lookup by identifier only, no keyword search"],
		}),
		/** Identifier lookup only, matching what the Bibliographic API offers. */
		async resolve(topic, context) {
			const identifier = bookIdentifier(topic);
			if (!identifier) return null;
			const payload = await providerJson(`${context.base}/api/volumes/brief/json/${identifier}`, context).catch(
				() => null,
			);
			const entry = payload?.[identifier];
			const record = entry?.records ? Object.values(entry.records)[0] : null;
			if (!record) return null;
			const retrievedAt = context.retrievedAt ?? new Date().toISOString();
			return searchResultRecord(
				{
					title: record.titles?.[0] ?? identifier,
					url: record.recordURL ?? null,
					content: [record.publishDates?.[0], `${entry.items?.length ?? 0} scanned items`]
						.filter(Boolean)
						.join(" · "),
					engine: "hathitrust",
				},
				0,
				topic,
				retrievedAt,
				"hathitrust",
			);
		},
	},

	gdelt: {
		id: "gdelt",
		label: "GDELT",
		kind: "news",
		base: "https://api.gdeltproject.org/api/v2",
		site: "gdeltproject.org",
		topics: ["*"],
		authority: 2,
		capabilities: () => ({
			...noAuth,
			rateLimit: "no key",
			fields: ["url", "title", "seendate", "domain", "language", "sourcecountry"],
			strengths: ["global news in every language", "no key at any volume", "archive back to 2017"],
			limits: ["rolling three-month window by default", "headline and metadata only, no article text"],
		}),
		async search(query, context) {
			const params = new URLSearchParams({
				query,
				mode: "artlist",
				format: "json",
				maxrecords: String(Math.min(context.limit ?? 10, 250)),
				sort: context.params?.sort ?? "datedesc",
			});
			if (context.params?.timespan) params.set("timespan", context.params.timespan);
			const payload = await providerJson(`${context.base}/doc/doc?${params}`, context);
			return records(
				(payload?.articles ?? []).map((article) => ({
					title: article.title,
					url: article.url,
					content: [article.domain, article.language, article.sourcecountry].filter(Boolean).join(" · ") || null,
					publishedAt: gdeltDate(article.seendate),
					engine: "gdelt",
				})),
				query,
				context,
				"gdelt",
			);
		},
	},

	stackexchange: {
		id: "stackexchange",
		label: "Stack Exchange",
		kind: "technical",
		base: "https://api.stackexchange.com/2.3",
		site: "stackoverflow.com",
		topics: ["computing", "mathematics"],
		authority: 5,
		capabilities: () => ({
			...noAuth,
			optionalAuth: true,
			rateLimit: "300 requests/day per IP without a key",
			fields: ["title", "link", "score", "tags", "is_answered"],
			strengths: ["practical programming answers", "score signals consensus"],
			limits: ["answers age badly", "quota is small without a key"],
		}),
		async search(query, context) {
			const params = new URLSearchParams({
				order: "desc",
				sort: "relevance",
				q: query,
				site: context.params?.site ?? "stackoverflow",
				pagesize: String(Math.min(context.limit ?? 10, 30)),
				filter: "default",
			});
			if (context.apiKey) params.set("key", context.apiKey);
			const payload = await providerJson(`${context.base}/search/advanced?${params}`, context);
			return records(
				(payload?.items ?? []).map((item) => ({
					title: stripHtml(item.title),
					url: item.link,
					content: [
						item.is_answered ? "answered" : "unanswered",
						`score ${item.score}`,
						(item.tags ?? []).join(", "),
					]
						.filter(Boolean)
						.join(" · "),
					score: item.score ?? null,
					publishedAt: item.creation_date ? new Date(item.creation_date * 1000).toISOString() : null,
					engine: "stackexchange",
				})),
				query,
				context,
				"stackexchange",
			);
		},
	},

	hackernews: {
		id: "hackernews",
		label: "Hacker News",
		kind: "technical",
		base: "https://hn.algolia.com/api/v1",
		site: "news.ycombinator.com",
		topics: ["computing"],
		authority: 7,
		capabilities: () => ({
			...noAuth,
			rateLimit: "no key",
			fields: ["title", "url", "points", "num_comments", "created_at"],
			strengths: ["discussion and dissent around a technical claim", "finds the critique of a popular post"],
			limits: ["opinion rather than reference", "one community's view"],
		}),
		async search(query, context) {
			const url = `${context.base}/search?${new URLSearchParams({
				query,
				hitsPerPage: String(context.limit ?? 10),
			})}`;
			const payload = await providerJson(url, context);
			return records(
				(payload?.hits ?? []).map((hit) => ({
					title: hit.title ?? hit.story_title ?? null,
					url: hit.url ?? `https://news.ycombinator.com/item?id=${hit.objectID}`,
					content: [`${hit.points ?? 0} points`, `${hit.num_comments ?? 0} comments`].join(" · "),
					score: hit.points ?? null,
					publishedAt: hit.created_at ?? null,
					engine: "hackernews",
				})),
				query,
				context,
				"hackernews",
			);
		},
	},

	// --- The keyed tier ------------------------------------------------------
	//
	// Everything below needs a credential. `authRequired` is what makes the
	// router skip it with a reason rather than fail, so adding one of these can
	// never break a machine that has no keys at all.

	guardian: {
		id: "guardian",
		label: "The Guardian",
		kind: "news",
		base: "https://content.guardianapis.com",
		site: "theguardian.com",
		topics: ["*"],
		authority: 3,
		capabilities: () => ({
			authRequired: true,
			optionalAuth: false,
			rateLimit: "5,000 requests/day, 12/s; the key is emailed instantly with no approval wait",
			dailyBudget: { calls: 5000 },
			fields: ["webTitle", "webUrl", "sectionName", "webPublicationDate", "trailText"],
			strengths: ["full article text back to 1999", "an editorial archive rather than a headline feed"],
			limits: ["one publication's view", "UK and Australia weighted"],
		}),
		async search(query, context) {
			const params = new URLSearchParams({
				q: query,
				"page-size": String(Math.min(context.limit ?? 10, 50)),
				"show-fields": "trailText",
				"order-by": context.params?.timeRange ? "newest" : "relevance",
			});
			params.set("api-key", context.apiKey);
			const payload = await providerJson(`${context.base}/search?${params}`, context);
			return records(
				(payload?.response?.results ?? []).map((article) => ({
					title: article.webTitle,
					url: article.webUrl,
					content: stripHtml(article.fields?.trailText) || article.sectionName || null,
					publishedAt: article.webPublicationDate ?? null,
					engine: "guardian",
				})),
				query,
				context,
				"guardian",
			);
		},
	},

	nyt: {
		id: "nyt",
		label: "The New York Times",
		kind: "news",
		base: "https://api.nytimes.com/svc",
		site: "nytimes.com",
		topics: ["*"],
		authority: 3,
		capabilities: () => ({
			authRequired: true,
			optionalAuth: false,
			// The tightest budget in the registry, and the reason the ledger exists:
			// spacing alone cannot express "and then stop for the day".
			rateLimit: "500 requests/day and 5/minute for most APIs; the Article API allows 4,000/day and 10/minute",
			dailyBudget: { calls: 500 },
			fields: ["headline", "web_url", "abstract", "pub_date", "section_name"],
			strengths: ["archive back to 1851", "abstracts written by the paper"],
			limits: ["one publication's view", "US weighted", "the smallest daily allowance here"],
		}),
		async search(query, context) {
			const params = new URLSearchParams({ q: query, sort: "relevance" });
			params.set("api-key", context.apiKey);
			const payload = await providerJson(`${context.base}/search/v2/articlesearch.json?${params}`, context);
			return records(
				(payload?.response?.docs ?? []).map((doc) => ({
					title: doc.headline?.main ?? doc.abstract ?? null,
					url: doc.web_url,
					content: truncate(doc.abstract ?? doc.snippet ?? doc.lead_paragraph) || null,
					publishedAt: doc.pub_date ?? null,
					engine: "nyt",
				})),
				query,
				context,
				"nyt",
			);
		},
	},

	marginalia: {
		id: "marginalia",
		label: "Marginalia Search",
		kind: "general",
		base: "https://api2.marginalia-search.com",
		site: "marginalia-search.com",
		topics: [],
		// Ahead of SearXNG, behind everything authoritative. With a key, an
		// open-ended query stops depending on a scraper of other people's engines.
		authority: 90,
		capabilities: () => ({
			authRequired: true,
			optionalAuth: false,
			rateLimit: "free non-commercial key by emailing contact@marginalia-search.com",
			fields: ["url", "title", "description", "quality"],
			strengths: [
				"an independent index, not a reseller of Google's",
				"finds small and old sites nothing else surfaces",
			],
			limits: ["small index", "poor for news and for anything commercial"],
		}),
		async search(query, context) {
			const params = new URLSearchParams({ query, count: String(Math.min(context.limit ?? 10, 100)) });
			const payload = await providerJson(`${context.base}/search?${params}`, context, {
				headers: { "api-key": context.apiKey },
			});
			return records(
				(payload?.results ?? []).map((result) => ({
					title: result.title,
					url: result.url,
					content: truncate(stripHtml(result.description)) || null,
					score: result.quality ?? null,
					engine: "marginalia",
				})),
				query,
				context,
				"marginalia",
			);
		},
	},

	wolfram: {
		id: "wolfram",
		label: "Wolfram|Alpha",
		kind: "reference",
		base: "https://api.wolframalpha.com/v2",
		site: "wolframalpha.com",
		topics: ["mathematics"],
		authority: 4,
		capabilities: () => ({
			authRequired: true,
			optionalAuth: false,
			rateLimit: "2,000 calls/month on the free non-commercial AppID",
			monthlyBudget: { calls: 2000 },
			fields: ["pods", "subpods", "plaintext"],
			strengths: ["computes rather than retrieves", "units, constants, conversions and closed-form results"],
			limits: ["answers are pods of text, not documents with URLs", "no citation to give"],
		}),
		/**
		 * Wolfram answers with computed pods rather than pages, so there is no URL
		 * to cite; the result carries the query's own permalink so a reader can
		 * reproduce it. That is why it is a reference provider and not a search one.
		 */
		async search(query, context) {
			const params = new URLSearchParams({ input: query, output: "json", format: "plaintext" });
			params.set("appid", context.apiKey);
			const payload = await providerJson(`${context.base}/query?${params}`, context);
			const pods = payload?.queryresult?.pods ?? [];
			const permalink = `https://www.wolframalpha.com/input?i=${encodeURIComponent(query)}`;
			return records(
				pods
					.filter((pod) => pod.error !== true)
					.map((pod) => ({
						title: pod.title,
						url: permalink,
						content:
							truncate(
								(pod.subpods ?? [])
									.map((subpod) => subpod.plaintext)
									.filter(Boolean)
									.join(" · "),
							) || null,
						score: pod.primary === true ? 1 : null,
						engine: "wolfram",
					}))
					.filter((row) => row.content),
				query,
				context,
				"wolfram",
			);
		},
	},

	exa: {
		id: "exa",
		label: "Exa",
		kind: "general",
		base: "https://api.exa.ai",
		site: "exa.ai",
		topics: [],
		authority: 92,
		capabilities: () => ({
			authRequired: true,
			optionalAuth: false,
			rateLimit: "20,000 requests/month free",
			fields: ["title", "url", "text", "publishedDate", "score"],
			strengths: ["embedding search, so a description of a page finds it without its keywords"],
			limits: ["its own crawl, not the open web", "ranking is opaque"],
		}),
		async search(query, context) {
			const payload = await providerPostJson(
				`${context.base}/search`,
				{ query, numResults: Math.min(context.limit ?? 10, 100), contents: { text: { maxCharacters: 500 } } },
				context,
				{ headers: { "x-api-key": context.apiKey } },
			);
			return records(
				(payload?.results ?? []).map((result) => ({
					title: result.title,
					url: result.url,
					content: truncate(stripHtml(result.text ?? result.summary)) || null,
					score: result.score ?? null,
					publishedAt: result.publishedDate ?? null,
					engine: "exa",
				})),
				query,
				context,
				"exa",
			);
		},
	},

	tavily: {
		id: "tavily",
		label: "Tavily",
		kind: "general",
		base: "https://api.tavily.com",
		site: "tavily.com",
		topics: [],
		authority: 93,
		capabilities: () => ({
			authRequired: true,
			optionalAuth: false,
			rateLimit: "1,000 credits/month free, no card",
			fields: ["title", "url", "content", "score", "published_date"],
			strengths: ["returns extracted page text alongside the ranking, so one call answers what two would"],
			limits: ["credits are spent per call regardless of result quality"],
		}),
		async search(query, context) {
			const payload = await providerPostJson(
				`${context.base}/search`,
				{ query, max_results: Math.min(context.limit ?? 10, 20), search_depth: "basic" },
				context,
				{ headers: { authorization: `Bearer ${context.apiKey}` } },
			);
			return records(
				(payload?.results ?? []).map((result) => ({
					title: result.title,
					url: result.url,
					content: truncate(stripHtml(result.content)) || null,
					score: result.score ?? null,
					publishedAt: result.published_date ?? null,
					engine: "tavily",
				})),
				query,
				context,
				"tavily",
			);
		},
	},

	searxng: {
		id: "searxng",
		label: "SearXNG",
		kind: "general",
		base: null, // resolved from connectedServices; see searxngBase in web-research.mjs
		topics: ["*"],
		// Last, always. A metasearch scraper is the fallback for questions no
		// authoritative source covers, not the first thing to ask.
		authority: 99,
		capabilities: () => ({
			...noAuth,
			rateLimit: "upstream engines throttle the instance",
			fields: ["title", "url", "content", "engine", "score", "publishedAt"],
			strengths: ["open-ended queries", "recency", "no domain assumptions"],
			limits: ["results come from scrapers that get rate-limited", "no stable identifiers"],
		}),
		async search(query, context) {
			const payload = await searchSearxng(context.base, query, {
				...context.params,
				userAgent: context.userAgent,
				timeoutMs: context.timeoutMs,
				limiter: context.limiter ?? searchLimiter,
			});
			return records(payload?.results ?? [], query, context, "searxng");
		},
	},
};

// --- Provider helpers ------------------------------------------------------

// A published index changes on the order of weeks, and the SEP's is 305 KB.
// Seven days is the figure vault-wiki settled on and it moves here unchanged.
export const INDEX_MAX_AGE_MS = 7 * 24 * 3600 * 1000;

function indexCachePath(key) {
	return join(defaultCacheDirectory(), "index", `${createHash("sha256").update(key).digest("hex").slice(0, 16)}.txt`);
}

/**
 * A source's own published index, cached on disk across processes.
 *
 * On disk rather than in memory because the caller may be a subprocess: vault-wiki
 * resolves one subject per invocation, and a per-process cache would refetch the
 * whole SEP contents page for every note in a 466-note run.
 *
 * A stale copy beats nothing. An index a week old still resolves every entry
 * that existed a week ago, whereas a failed fetch resolves none of them -- and
 * "no source" is the most misleading thing this pipeline can report.
 */
async function cachedIndex(key, context, load) {
	const path = indexCachePath(key);
	if (existsSync(path)) {
		try {
			if (Date.now() - statSync(path).mtimeMs < INDEX_MAX_AGE_MS) return readFileSync(path, "utf8");
		} catch {
			// An unreadable cache entry is refetched, not fatal.
		}
	}
	try {
		const fetched = await load();
		atomicWriteFile(path, fetched);
		return fetched;
	} catch (error) {
		if (existsSync(path)) {
			context.staleIndexes?.push({ key, error: error instanceof Error ? error.message : String(error) });
			return readFileSync(path, "utf8");
		}
		throw error;
	}
}

let sepIndexCache = null;

/**
 * The SEP table of contents. The page is a flat list of
 * `<a href="entries/SLUG/">Title</a>`, which is stable enough to read with a
 * pattern; vault-wiki has parsed it the same way since it was written.
 */
async function sepIndexPairs(context) {
	if (sepIndexCache) return sepIndexCache;
	const html = await cachedIndex(`${context.base}/contents.html`, context, () =>
		providerText(`${context.base}/contents.html`, context),
	);
	const entries = [];
	for (const match of html.matchAll(/<a\s[^>]*href="entries\/([^"#?/]+)\/?"[^>]*>([\s\S]*?)<\/a>/gi)) {
		const title = stripHtml(match[2]);
		if (title) entries.push({ slug: match[1], title });
	}
	sepIndexCache = entries;
	return entries;
}

/**
 * SEP entries, one per slug, for ranked search.
 *
 * The contents page lists an entry more than once when it is cross-filed
 * alphabetically -- `shared-agency` appears as "agency: shared" and again as
 * "shared" -- so 2,511 anchors describe 1,862 entries. Search wants one row per
 * entry; `sepIndexPairs` keeps every listing, because a caller matching titles
 * should see every name an entry is published under.
 */
async function sepIndex(context) {
	const seen = new Set();
	const entries = [];
	for (const entry of await sepIndexPairs(context)) {
		if (seen.has(entry.slug)) continue;
		seen.add(entry.slug);
		entries.push(entry);
	}
	return entries;
}

/** Exposed so a test can install a fixture index without a network fetch. */
export function __setSepIndexForTesting(entries) {
	sepIndexCache = entries;
}

// Words too common to carry a topic. Dropping them is what lets "phenomenology
// of perception" reach the "Phenomenology" entry: SEP titles are short and
// topic-shaped, so a query phrased as prose will rarely match one whole.
const STOPWORDS = new Set([
	"the",
	"a",
	"an",
	"of",
	"in",
	"on",
	"and",
	"or",
	"for",
	"to",
	"is",
	"are",
	"what",
	"who",
	"how",
	"does",
	"do",
]);

function significantWords(text) {
	return text.split(/[^\p{L}\p{N}]+/u).filter((word) => word.length > 2 && !STOPWORDS.has(word));
}

function titleMatchScore(title, needle) {
	if (!needle) return 0;
	if (title === needle) return 100;
	if (title.startsWith(`${needle},`) || title.startsWith(`${needle} `)) return 80;
	if (title.includes(needle)) return 60;
	const words = significantWords(needle);
	if (words.length === 0) return 0;
	const matched = words.filter((word) => title.includes(word));
	if (matched.length === 0) return 0;
	// Partial overlap still scores, ranked by how much of the query the title
	// accounts for. An all-or-nothing rule returned nothing for any query longer
	// than an entry title, which is most of them.
	if (matched.length === words.length) return 40;
	return Math.round((30 * matched.length) / words.length);
}

const SUTTA_COLLECTIONS = new Map([
	["mn", "mn"],
	["dn", "dn"],
	["sn", "sn"],
	["an", "an"],
	["kp", "kp"],
	["dhp", "dhp"],
	["ud", "ud"],
	["iti", "iti"],
	["snp", "snp"],
	["thag", "thag"],
	["thig", "thig"],
]);

/**
 * "MN 118", "SN 56.11", "Dhp 1" -> the SuttaCentral uid. Anchored, because
 * resolve() uses it to decide that the whole input *is* a citation.
 */
export function suttaUid(value) {
	const match = String(value ?? "")
		.trim()
		.match(/^([A-Za-z]{2,4})\s*\.?\s*(\d+(?:\.\d+)*)$/);
	if (!match) return null;
	const collection = SUTTA_COLLECTIONS.get(match[1].toLowerCase());
	return collection ? `${collection}${match[2]}` : null;
}

/**
 * The same reference found anywhere inside a longer query, for classification.
 * "MN 118 anapanasati" is a question about a specific sutta even though the
 * whole string is not a citation, and routing it to SuttaCentral first is the
 * difference between an answer and a web search.
 */
export function findSuttaReference(value) {
	for (const match of String(value ?? "").matchAll(/\b([A-Za-z]{2,4})\s*\.?\s*(\d+(?:\.\d+)*)\b/g)) {
		const collection = SUTTA_COLLECTIONS.get(match[1].toLowerCase());
		if (collection) return `${collection}${match[2]}`;
	}
	return null;
}

/** An ISBN, OCLC or LCCN in the form HathiTrust's Bibliographic API expects. */
export function bookIdentifier(value) {
	const text = String(value ?? "").trim();
	const isbn = text.replace(/[\s-]/g, "").match(/^(?:isbn:?)?(\d{9}[\dXx]|\d{13})$/i);
	if (isbn) return `isbn:${isbn[1]}`;
	const oclc = text.match(/^oclc:?\s*(\d+)$/i);
	if (oclc) return `oclc:${oclc[1]}`;
	const lccn = text.match(/^lccn:?\s*([\w\d-]+)$/i);
	if (lccn) return `lccn:${lccn[1]}`;
	return null;
}

/** GDELT stamps articles as YYYYMMDDTHHMMSSZ, which Date.parse will not take. */
function gdeltDate(value) {
	const match = String(value ?? "").match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
	return match ? `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}Z` : (value ?? null);
}

// --- Registry queries ------------------------------------------------------

export function searchProviderList() {
	return Object.keys(SEARCH_PROVIDERS);
}

/**
 * Entry candidates for a subject name, for a caller that will score them itself.
 *
 * This is the second half of the split `resolve()` does not make. `search()`
 * answers "what pages are relevant to this query" and ranks them; a reference
 * lookup asks "which entry is *about* this thing", and the answer depends on
 * editorial judgement the registry does not hold -- vault-wiki's does. So a
 * provider that has a native name lookup exposes it here unranked, and anything
 * else falls back to its search results with the ranking left intact but no
 * claim made about it.
 */
export async function referenceCandidates(id, subject, context) {
	const provider = SEARCH_PROVIDERS[id];
	if (!provider) throw new Error(`unknown provider: ${id}`);
	if (provider.referenceCandidates) return provider.referenceCandidates(subject, context);
	if (!provider.search) return [];
	const results = await provider.search(subject, context);
	return results.map((result) => ({ title: result.title, url: result.url, snippet: result.content ?? null }));
}

export function searchProviderCapabilities(id) {
	return SEARCH_PROVIDERS[id]?.capabilities?.() ?? null;
}

/**
 * Whether a provider can run right now. A provider that needs a key and has
 * none, or that has spent its allowance for the day, is unavailable -- never an
 * error: the caller skips it and records why, so the no-key tier keeps working
 * on a machine nobody has configured.
 *
 * Deliberately a pure function of its options. The budget state is passed in
 * rather than read from disk here, so a test states the case instead of staging
 * a ledger file for it.
 */
export function searchProviderAvailability(id, options = {}) {
	const provider = SEARCH_PROVIDERS[id];
	if (!provider) return { available: false, reason: `unknown provider: ${id}` };
	const capabilities = provider.capabilities?.() ?? {};
	if (capabilities.authRequired && !options.apiKeys?.[id]) {
		return { available: false, reason: `no API key configured for ${id}` };
	}
	// An exhausted budget is skipped rather than retried. Asking again only
	// spends the retry ladder on an answer the service has already given.
	const budget = options.budget?.[id];
	if (budget?.exhausted) return { available: false, reason: budget.reason ?? `${id} has spent its daily budget` };
	return { available: true, reason: null };
}
