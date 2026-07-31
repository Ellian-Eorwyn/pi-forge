// Which providers answer a given question, and in what order.
//
// The pattern is classifyAcademicQuery -> academicProviderList in
// web-research.mjs, applied to general search: read the query for identifiers
// and topic signals, select providers from what was read, and fall through to
// SearXNG only when nothing authoritative matched.
//
// The ordering rule is the one vault-wiki's canonical-sources.json already
// encodes: lower `authority` is tried first, and Wikipedia sits near the bottom
// on purpose because it is the source most likely to be right and least likely
// to be citable.

import { DECLARED_BUDGETS, loadBudget, providerBudgetState, recordProviderSpend } from "./provider-budget.mjs";
import { SEARCH_PROVIDERS, bookIdentifier, findSuttaReference, searchProviderAvailability } from "./search-providers.mjs";

/**
 * Budget state for every provider that declares one, in the shape
 * `searchProviderAvailability` expects.
 *
 * The ledger is read once and the providers that declare no budget are not
 * consulted at all. vault-wiki calls into here once per note per source, so a
 * file read per provider per lookup would be hundreds of reads for one run.
 */
export function searchProviderBudgets(options = {}) {
	const declared = Object.keys(SEARCH_PROVIDERS).filter((id) => DECLARED_BUDGETS[id]);
	if (declared.length === 0) return {};
	const ledger = options.ledger ?? loadBudget(options);
	const budgets = {};
	for (const id of declared) {
		const state = providerBudgetState(id, { ...options, ledger });
		if (state.exhausted) budgets[id] = state;
	}
	return budgets;
}

// The topic vocabulary is shared with vault-wiki's canonical-sources.json so the
// editorial registry and the transport registry can be described in one
// language. Anything not matched here is "general".
const TOPIC_PATTERNS = [
	[
		"philosophy",
		/\b(philosoph\w*|epistemolog\w*|metaphysic\w*|ontolog\w*|phenomenolog\w*|ethic\w*|aesthetic\w*|hermeneutic\w*|dialectic\w*|kant|hegel|husserl|heidegger|wittgenstein|deleuze|foucault|nietzsche|spinoza|aristotle|plato\b)/i,
	],
	[
		"buddhism",
		/\b(buddhis\w*|buddha|dharma|dhamma|sutta|sutra|sangha|nirvana|nibbana|bodhisattva|madhyamaka|yogacara|abhidharma|abhidhamma|vipassana|anapanasati|zen|chan|theravada|mahayana|vajrayana|tibetan buddhis\w*|pali canon|tripitaka|kangyur|tengyur|taisho)\b/i,
	],
	["religion", /\b(religio\w*|theolog\w*|scriptur\w*|liturg\w*|monastic\w*)\b/i],
	["biomedical", /\b(clinical|biomedical|pubmed|disease|drug|therapy|genetic|neuroscience|epidemiolog\w*|public health|patient)\b/i],
	["psychology", /\b(psycholog\w*|cognitive|behaviou?ral|therapy|perception|memory)\b/i],
	["computing", /\b(code|github|repository|npm|pypi|package|api|sdk|library|algorithm|programming|softwar\w*|typescript|javascript|python|rust\b)/i],
	["mathematics", /\b(mathematic\w*|theorem|proof|topolog\w*|algebra|calculus|geometr\w*)\b/i],
	["history", /\b(histor\w*|century|medieval|ancient|dynasty|war\b|empire)/i],
	["literature", /\b(novel|poem|poetry|literatur\w*|author|fiction)\b/i],
];

const NEWS_PATTERN = /\b(news|today|yesterday|recent|breaking|latest|this week|headline|reported)\b/i;
const BOOK_PATTERN = /\b(book|edition|publisher|isbn|paperback|hardcover|volume|translat(?:ion|ed by))\b/i;
const DEFINITION_PATTERN = /\b(define|definition|what is|what are|meaning of|etymolog\w*|glossar\w*)\b/i;

const DOI_PATTERN = /\b10\.\d{4,9}\/[-._;()/:a-z0-9]+\b/i;
const ARXIV_PATTERN = /\barxiv[:\s]*(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+\/\d{7}(?:v\d+)?)\b/i;
const PMID_PATTERN = /\bpmid[:\s]*(\d{6,9})\b/i;
// A Taisho reference: "T. 262", "T0262", "Taisho 262".
const TAISHO_PATTERN = /\b(?:t\.?\s*|taish[oō]\s*)(\d{1,4})\b/i;

/**
 * Read a query for what it is about. Identifiers are decisive -- an ISBN is a
 * book and a sutta reference is a sutta, whatever else the words look like --
 * so they are collected separately from the softer topic signals.
 */
export function classifySearchQuery(query) {
	const text = String(query ?? "").trim();
	const identifiers = {
		doi: text.match(DOI_PATTERN)?.[0] ?? null,
		arxivId: text.match(ARXIV_PATTERN)?.[1] ?? null,
		pmid: text.match(PMID_PATTERN)?.[1] ?? null,
		suttaUid: findSuttaReference(text),
		bookIdentifier: bookIdentifier(text),
		taisho: text.match(TAISHO_PATTERN)?.[1] ?? null,
	};
	const topics = TOPIC_PATTERNS.filter(([, pattern]) => pattern.test(text)).map(([topic]) => topic);
	// The subject stripped of the question that framed it. Asking a dictionary
	// for "what is dependent origination" matches the words "what" and "is";
	// asking it for "dependent origination" matches the term.
	const term = text
		.replace(/^\s*(?:what\s+(?:is|are|was|were)|who\s+(?:is|was)|define|definition\s+of|meaning\s+of|etymolog(?:y|ies)\s+of|tell\s+me\s+about)\s+/i, "")
		.replace(/\?+\s*$/, "")
		.trim();
	const intents = [];
	if (NEWS_PATTERN.test(text)) intents.push("news");
	if (BOOK_PATTERN.test(text) || identifiers.bookIdentifier) intents.push("books");
	if (DEFINITION_PATTERN.test(text)) intents.push("definition");
	if (identifiers.doi || identifiers.arxivId || identifiers.pmid) intents.push("academic");
	if (identifiers.suttaUid || identifiers.taisho) intents.push("canon");
	return { query: text, term: term || text, topics: topics.length > 0 ? topics : ["general"], intents, identifiers };
}

/**
 * "general" means the classifier found no subject signal, not that every
 * general-interest source applies. Treating it as a claimable topic is what made
 * an early version answer "how do I fix a leaky tap" with the Library of
 * Congress: a provider must match something the query actually said.
 */
function providerMatchesTopic(provider, topics) {
	if (!provider.topics || provider.topics.length === 0 || provider.topics.includes("*")) return false;
	const specific = topics.filter((topic) => topic !== "general");
	return specific.length > 0 && provider.topics.some((topic) => specific.includes(topic));
}

/**
 * Ordered provider ids for a query.
 *
 * An explicit --providers list wins outright, exactly as academicProviderList
 * handles it. Otherwise: providers whose declared topics match, plus providers
 * the intent selects, ordered by authority, with SearXNG appended last as the
 * fallback for anything the specific sources do not cover.
 */
export function routeQuery(query, options = {}) {
	const classification = options.classification ?? classifySearchQuery(query);
	const requested = parseProviderList(options.providers);
	const decisions = [];
	const selected = new Set();

	if (requested) {
		for (const id of requested) {
			if (SEARCH_PROVIDERS[id]) selected.add(id);
			else decisions.push({ provider: id, selected: false, reason: "unknown provider" });
		}
		return finalize([...selected], classification, decisions, options, { explicit: true });
	}

	const { topics, intents, identifiers } = classification;

	// Identifiers are decisive: whatever else the words look like, "MN 118" is a
	// sutta and an ISBN is a book. These are tracked separately so they sort
	// ahead of merely topical matches.
	const byIdentifier = new Set();
	if (identifiers.suttaUid) byIdentifier.add("suttacentral");
	if (identifiers.taisho) byIdentifier.add("cbeta");
	if (identifiers.bookIdentifier) {
		byIdentifier.add("hathitrust");
		byIdentifier.add("openlibrary");
	}
	for (const id of byIdentifier) selected.add(id);
	options = { ...options, byIdentifier };

	for (const [id, provider] of Object.entries(SEARCH_PROVIDERS)) {
		if (provider.kind === "general") continue;
		if (providerMatchesTopic(provider, topics)) selected.add(id);
	}

	// GDELT indexes everyone's headlines and nobody's article text; the two
	// papers carry their own. They are skipped without a key, so on an
	// unconfigured machine this stays exactly what it was.
	if (intents.includes("news")) {
		selected.add("gdelt");
		selected.add("guardian");
		selected.add("nyt");
	}
	if (intents.includes("books")) {
		selected.add("openlibrary");
		selected.add("internetarchive");
		selected.add("gutendex");
	}
	if (intents.includes("definition")) {
		selected.add("wiktionary");
		selected.add("wikipedia");
	}

	// A question with no signal at all still deserves the encyclopedia before it
	// falls through to a scraper.
	if (selected.size === 0) selected.add("wikipedia");

	return finalize([...selected], classification, decisions, options, { explicit: false });
}

function finalize(ids, classification, decisions, options, { explicit }) {
	const available = [];
	for (const id of ids) {
		const availability = searchProviderAvailability(id, options);
		if (!availability.available) {
			// Skipped, never fatal: this is what keeps the no-key tier working on
			// a machine nobody has configured.
			decisions.push({ provider: id, selected: false, reason: availability.reason });
			continue;
		}
		available.push(id);
		const reason = options.byIdentifier?.has(id)
			? "matched an identifier in the query"
			: explicit
				? "requested explicitly"
				: "matched classification";
		decisions.push({ provider: id, selected: true, reason });
	}

	// An identifier match outranks a topic match, then authority, as
	// canonical-sources.json orders it: lower is tried first.
	available.sort((left, right) => {
		const identifierRank = Number(options.byIdentifier?.has(right) ?? false) - Number(options.byIdentifier?.has(left) ?? false);
		if (identifierRank !== 0) return identifierRank;
		return (SEARCH_PROVIDERS[left].authority ?? 50) - (SEARCH_PROVIDERS[right].authority ?? 50);
	});

	// The general fallback, for the part of a question no authoritative source
	// covers. Every general-kind provider gets a turn in authority order, so an
	// operator with a Marginalia or Exa key stops depending on a scraper -- and
	// an operator with neither gets exactly what they got before, SearXNG alone.
	if (!explicit && options.includeFallback !== false) {
		const fallbacks = Object.values(SEARCH_PROVIDERS)
			.filter((provider) => provider.kind === "general")
			.sort((left, right) => (left.authority ?? 50) - (right.authority ?? 50));
		for (const provider of fallbacks) {
			if (available.includes(provider.id)) continue;
			const availability = searchProviderAvailability(provider.id, options);
			if (availability.available) {
				available.push(provider.id);
				decisions.push({ provider: provider.id, selected: true, reason: "general fallback" });
			} else {
				decisions.push({ provider: provider.id, selected: false, reason: availability.reason });
			}
		}
	}

	return { classification, providers: available, decisions };
}

function parseProviderList(value) {
	if (!value) return null;
	const ids = String(value)
		.split(",")
		.map((entry) => entry.trim())
		.filter(Boolean);
	return ids.length > 0 ? ids : null;
}

/**
 * Run the routed providers and merge their results.
 *
 * A provider that fails is recorded and skipped rather than failing the search:
 * one source being down is not a reason to lose the other five. Deduplication is
 * by canonical URL, keeping the result from the higher-authority provider, which
 * is why `providers` arrives already sorted.
 */
export async function runRoutedSearch(query, providers, contextFor, options = {}) {
	const merged = [];
	const seen = new Map();
	const errors = [];
	const perProvider = [];
	const resolveWith = options.resolveWith ?? {};
	const queryOverrides = options.queryOverrides ?? {};

	for (const id of providers) {
		const provider = SEARCH_PROVIDERS[id];
		const identifier = resolveWith[id];
		const providerQuery = queryOverrides[id] ?? query;
		if (!provider?.search && !(identifier && provider?.resolve)) {
			perProvider.push({ provider: id, results: 0, skipped: "provider has no search interface" });
			continue;
		}
		try {
			// providerId lets the transport pick up the provider's declared spacing.
			const context = { providerId: id, ...contextFor(id, provider) };
			let results = null;
			// An identifier is a lookup, not a query. Full-text searching for
			// "MN 118 anapanasati" finds nothing, because no document contains
			// both the citation and the term; resolving mn118 finds the sutta.
			if (identifier && provider.resolve) {
				const entry = await provider.resolve(identifier, context);
				if (entry) results = [entry];
			}
			if (results === null) results = provider.search ? await provider.search(providerQuery, context) : [];
			// Spend is recorded per call, not per result: a query that matched
			// nothing still cost NYT one of its five hundred.
			recordProviderSpend(id, { options: options.budgetOptions ?? {} });
			perProvider.push({ provider: id, results: results.length, skipped: null });
			// Dedupe across providers only. A provider's own result set is its own
			// judgement -- SearXNG returning the same page twice under different
			// fragments is a signal acquisition already knows how to handle, and
			// collapsing it here silently changed what a single-provider search
			// returned.
			const contributed = [];
			for (const result of results) {
				const key = result.canonicalUrl ?? result.url;
				if (key && seen.has(key)) continue;
				if (key) contributed.push(key);
				merged.push(result);
			}
			for (const key of contributed) seen.set(key, id);
			if (options.stopWhenSatisfied && merged.length >= (options.limit ?? Number.POSITIVE_INFINITY)) break;
		} catch (error) {
			// A refused or failed request is still a request as far as a metered
			// service is concerned, so it is recorded here too.
			recordProviderSpend(id, { options: options.budgetOptions ?? {} });
			const message = error instanceof Error ? error.message : String(error);
			errors.push({ provider: id, error: message, transient: error?.transient === true });
			perProvider.push({ provider: id, results: 0, skipped: message });
		}
	}

	// Re-rank across providers. Within a provider the original order is its own
	// relevance judgement; across providers, authority breaks the tie.
	merged.sort((left, right) => {
		const byAuthority =
			(SEARCH_PROVIDERS[left.provider]?.authority ?? 50) - (SEARCH_PROVIDERS[right.provider]?.authority ?? 50);
		return byAuthority !== 0 ? byAuthority : left.rank - right.rank;
	});
	for (const [index, result] of merged.entries()) result.rank = index + 1;

	return { results: options.limit ? merged.slice(0, options.limit) : merged, errors, perProvider };
}
