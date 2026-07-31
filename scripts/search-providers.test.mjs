import assert from "node:assert/strict";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const scriptsDirectory = join(repositoryRoot, "forge", "skills", "web-research", "scripts");
const { SEARCH_PROVIDERS, __setSepIndexForTesting, bookIdentifier, findSuttaReference, searchProviderAvailability, searchProviderBase, searchProviderCapabilities, suttaUid } =
	await import(join(scriptsDirectory, "search-providers.mjs"));
const { classifySearchQuery, routeQuery, runRoutedSearch } = await import(join(scriptsDirectory, "search-router.mjs"));

/**
 * One fixture server standing in for every provider, routed by path. The shapes
 * below were copied from live responses on 2026-07-31 rather than invented, so a
 * normalizer that passes here is reading the field names the service really
 * sends.
 */
const ROUTES = {
	// MediaWiki action API
	"/w/api.php": (url) => {
		if (url.searchParams.get("action") === "wbsearchentities") {
			return {
				search: [{ id: "Q2123673", label: "Anapanasati", description: "Buddhist meditation practice", concepturi: "http://www.wikidata.org/entity/Q2123673" }],
			};
		}
		return {
			query: {
				searchinfo: { totalhits: 2115 },
				search: [
					{ ns: 0, title: "Anapanasati", pageid: 443371, size: 37817, wordcount: 4510, snippet: '<span class="searchmatch">Ānāpānasati</span> means mindfulness of breathing', timestamp: "2026-01-04T10:00:00Z" },
					{ ns: 0, title: "Anapanasati Sutta", pageid: 443372, size: 12000, wordcount: 1500, snippet: "The discourse on <b>mindfulness</b> of breathing", timestamp: "2026-02-01T10:00:00Z" },
				],
			},
		};
	},
	"/api/rest_v1/page/summary/Anapanasati": () => ({
		type: "standard",
		title: "Anapanasati",
		titles: { normalized: "Anapanasati" },
		wikibase_item: "Q2123673",
		extract: "Anapanasati is mindfulness of breathing, a form of Buddhist meditation.",
	}),
	"/api/rest_v1/page/summary/Ambiguous": () => ({ type: "disambiguation", title: "Ambiguous", extract: "May refer to:" }),
	// SuttaCentral
	"/api/search/instant": () => ({
		total: 2,
		hits: [
			{ uid: "mn118", acronym: "MN 118", name: "Mindfulness of Breathing", url: "/mn118/en/sujato", lang: "en", highlight: { content: ['<strong class="highlight">Ānāpānasati</strong> is of great fruit'] } },
			{ uid: "thag10.3", acronym: "Thag 10.3", name: "Mahākappina", url: "/thag10.3/pli/ms", lang: "pli", highlight: { content: ["<strong>Ānāpānasatī</strong> yassa"] } },
		],
	}),
	"/api/suttaplex/mn118": () => [
		{ uid: "mn118", acronym: "MN 118", original_title: "Ānāpānassatisutta", translated_title: "Mindfulness of Breathing", blurb: "The Buddha teaches mindfulness of breathing in detail." },
	],
	// Open Library
	"/search.json": () => ({
		numFound: 676,
		docs: [
			{ key: "/works/OL18935815W", title: "The Dhammapada", author_name: ["Rose Kramer"], first_publish_year: 1995, edition_count: 2, isbn: ["0938077872"] },
		],
	}),
	// Gutendex
	"/books": () => ({
		count: 2,
		results: [
			{ id: 2017, title: "Dhammapada, a Collection of Verses", authors: [{ name: "Max Müller" }], formats: { "text/html": "https://www.gutenberg.org/ebooks/2017.html" } },
			{ id: 2018, title: "Untitled", authors: [], formats: {} },
		],
	}),
	// Internet Archive
	"/advancedsearch.php": () => ({ response: { numFound: 845, docs: [{ identifier: "bdrc-W1KG13992", title: "Dhammapada", year: 2012, creator: ["Unknown"] }] } }),
	// Library of Congress
	"/search/": () => ({ results: [{ id: "https://www.loc.gov/item/unk81062171/", title: "Dhammapada or path of virtue", description: ["A translation"], date: "1901" }] }),
	// HathiTrust
	"/api/volumes/brief/json/isbn:9780861713219": () => ({
		"isbn:9780861713219": { records: { "000123456": { titles: ["The Long Discourses"], recordURL: "https://catalog.hathitrust.org/Record/000123456", publishDates: ["1995"] } }, items: [{ htid: "mdp.111" }] },
	}),
	"/api/volumes/brief/json/isbn:0000000000": () => ({ "isbn:0000000000": { records: {}, items: [] } }),
	// GDELT
	"/doc/doc": () => ({
		articles: [{ url: "https://example.org/a", title: "Tibet news", seendate: "20260726T200000Z", domain: "example.org", language: "English", sourcecountry: "United States" }],
	}),
	// Stack Exchange
	"/search/advanced": () => ({
		items: [{ title: "How do I handle errors in async Rust&#39;s tokio?", link: "https://stackoverflow.com/q/1", score: 42, tags: ["rust"], is_answered: true, creation_date: 1700000000 }],
	}),
	// InPhO
	"/thinker.json": () => ({ responseData: { total: 1, results: [{ wiki: "Plato", url: "/thinker/3724", sep_dir: "plato", label: "Plato", type: "thinker", ID: 3724 }] } }),
	"/idea.json": () => ({ responseData: { total: 0, results: [] } }),
	// IEP
	"/wp-json/wp/v2/search": () => [{ id: 25926, title: "Cognitive Phenomenology", url: "https://iep.utm.edu/cognitive-phenomenology/", type: "post" }],
	// BDRC
	"/resource/P1614.jsonld": () => ({ "@graph": [{ "@id": "bdr:NM1246", "rdfs:label": { "@language": "bo-x-ewts", "@value": "mi la ras pa" }, "@type": "PersonPersonalName" }] }),
	// SEP contents index
	"/contents.html": () =>
		`<html><body>
			<a href="entries/phenomenology/">Phenomenology</a>
			<a href="entries/perception-problem/">Perception, The Problem of</a>
			<a href="entries/japanese-zen/">Japanese Philosophy, Zen Buddhism in</a>
		</body></html>`,
};

// Both CBETA and the Hacker News Algolia index answer at /search, so they cannot
// be two keys in one object. They are told apart by their query parameters,
// which is also how they differ in real life.
const cbetaSearch = (url) => ({
	num_found: 126,
	results: [{ id: 8392, term_hits: 16, canon: "T", work: "T0602", juan: 1, title: "佛說大安般守意經", byline: "後漢 安世高譯", time_dynasty: "東漢" }],
	query_string: url.searchParams.get("q"),
});
const hackerNewsSearch = () => ({
	hits: [{ objectID: "1", title: "Why async Rust is hard", url: "https://ex.org/p", points: 300, num_comments: 120, created_at: "2026-03-01T00:00:00Z" }],
});

function startFixture() {
	const requests = [];
	const server = createServer((request, response) => {
		const url = new URL(request.url, "http://127.0.0.1");
		requests.push(url.pathname + url.search);
		let handler = ROUTES[url.pathname];
		if (url.pathname === "/search") {
			handler = url.searchParams.has("hitsPerPage") ? hackerNewsSearch : cbetaSearch;
		}
		if (!handler) {
			response.writeHead(404, { "content-type": "application/json" });
			response.end(JSON.stringify({ error: "no fixture", path: url.pathname }));
			return;
		}
		const body = handler(url);
		const isHtml = typeof body === "string";
		response.writeHead(200, { "content-type": isHtml ? "text/html" : "application/json" });
		response.end(isHtml ? body : JSON.stringify(body));
	});
	return new Promise((resolve) => {
		server.listen(0, "127.0.0.1", () => {
			resolve({
				origin: `http://127.0.0.1:${server.address().port}`,
				requests,
				close: () => new Promise((done) => server.close(done)),
			});
		});
	});
}

function contextFor(fixture, overrides = {}) {
	return { base: fixture.origin, userAgent: "pi-forge-test/1", timeoutMs: 5000, limit: 10, retrievedAt: "2026-07-31T00:00:00Z", ...overrides };
}

async function withFixture(body) {
	const fixture = await startFixture();
	try {
		await body(fixture);
	} finally {
		await fixture.close();
	}
}

// --- Identifier parsing ----------------------------------------------------

test("sutta references parse strictly for lookup and loosely for routing", () => {
	assert.equal(suttaUid("MN 118"), "mn118");
	assert.equal(suttaUid("SN 56.11"), "sn56.11");
	assert.equal(suttaUid("Dhp 1"), "dhp1");
	// Anchored: resolve() uses this to decide the whole input is a citation.
	assert.equal(suttaUid("MN 118 anapanasati"), null);
	assert.equal(suttaUid("chapter 4"), null);
	// Unanchored, for classification.
	assert.equal(findSuttaReference("MN 118 anapanasati"), "mn118");
	assert.equal(findSuttaReference("see an 4.10 on the yokes"), "an4.10");
	assert.equal(findSuttaReference("no reference here"), null);
});

test("book identifiers normalize to the form HathiTrust expects", () => {
	assert.equal(bookIdentifier("9780861713219"), "isbn:9780861713219");
	assert.equal(bookIdentifier("isbn 9780861713219"), "isbn:9780861713219");
	assert.equal(bookIdentifier("978-0-86171-321-9"), "isbn:9780861713219");
	assert.equal(bookIdentifier("0938077872"), "isbn:0938077872");
	assert.equal(bookIdentifier("oclc:12345"), "oclc:12345");
	assert.equal(bookIdentifier("lccn:sh85012345"), "lccn:sh85012345");
	assert.equal(bookIdentifier("not an identifier"), null);
});

// --- Classification and routing --------------------------------------------

test("identifiers are decisive and put their provider first", () => {
	const sutta = routeQuery("MN 118 anapanasati");
	assert.equal(sutta.providers[0], "suttacentral");
	assert.equal(sutta.classification.identifiers.suttaUid, "mn118");
	assert.equal(routeQuery("T. 262").providers[0], "cbeta");
	const isbn = routeQuery("isbn 9780861713219");
	assert.ok(isbn.providers.slice(0, 2).includes("hathitrust"));
	assert.ok(isbn.providers.slice(0, 2).includes("openlibrary"));
});

test("a topic routes to its authoritative sources, not to everything", () => {
	const philosophy = routeQuery("phenomenology of perception");
	assert.deepEqual(philosophy.providers, ["sep", "inpho", "iep", "searxng"]);
	const technical = routeQuery("async rust error handling");
	assert.deepEqual(technical.providers, ["stackexchange", "hackernews", "searxng"]);
});

test("a query with no subject signal does not drag in general-interest sources", () => {
	// "general" means the classifier found nothing, not that every broad source
	// applies. An early version answered this with the Library of Congress.
	const plain = routeQuery("how do I fix a leaky tap");
	assert.deepEqual(plain.providers, ["wikipedia", "searxng"]);
	assert.equal(plain.classification.topics.join(), "general");
});

test("news and definition intents select by question shape", () => {
	assert.equal(routeQuery("latest news on Tibet").providers[0], "gdelt");
	assert.deepEqual(routeQuery("what is dependent origination").providers, ["wiktionary", "wikipedia", "searxng"]);
});

test("SearXNG is always last and never first", () => {
	for (const query of ["MN 118", "phenomenology", "latest news", "how do I fix a leaky tap", "isbn 9780861713219"]) {
		const { providers } = routeQuery(query);
		assert.equal(providers.at(-1), "searxng", query);
		assert.notEqual(providers[0], "searxng", query);
	}
});

test("an explicit provider list wins outright and skips the fallback", () => {
	const routed = routeQuery("anything at all", { providers: "cbeta,wikipedia" });
	assert.deepEqual(routed.providers, ["cbeta", "wikipedia"]);
	assert.equal(routed.providers.includes("searxng"), false);
	const unknown = routeQuery("x", { providers: "nope" });
	assert.deepEqual(unknown.decisions[0], { provider: "nope", selected: false, reason: "unknown provider" });
});

test("a keyed provider with no key is skipped with a reason, never an error", () => {
	// Nothing in the no-key tier requires this, which is the point: the registry
	// has to keep working on a machine nobody has configured.
	const withKey = searchProviderAvailability("stackexchange", { apiKeys: { stackexchange: "k" } });
	assert.deepEqual(withKey, { available: true, reason: null });
	assert.equal(searchProviderAvailability("unknown-provider", {}).available, false);
});

test("every provider declares capabilities and a base or an explicit null", () => {
	for (const id of Object.keys(SEARCH_PROVIDERS)) {
		const capabilities = searchProviderCapabilities(id);
		assert.ok(capabilities, `${id} has no capabilities`);
		assert.equal(typeof capabilities.authRequired, "boolean", id);
		assert.ok(Array.isArray(capabilities.strengths) && capabilities.strengths.length > 0, `${id} declares no strengths`);
		assert.ok(Array.isArray(capabilities.limits) && capabilities.limits.length > 0, `${id} declares no limits`);
		assert.ok("base" in SEARCH_PROVIDERS[id], `${id} has no base`);
	}
});

test("a provider base follows flag, then environment, then default", () => {
	assert.equal(searchProviderBase("wikipedia", {}, {}), "https://en.wikipedia.org");
	assert.equal(searchProviderBase("wikipedia", {}, { FORGE_SEARCH_WIKIPEDIA_URL: "http://fixture/" }), "http://fixture");
	assert.equal(searchProviderBase("wikipedia", { wikipediaBase: "http://flag" }, { FORGE_SEARCH_WIKIPEDIA_URL: "http://fixture" }), "http://flag");
	// SearXNG keeps its long-standing variable name rather than the generated one.
	assert.equal(searchProviderBase("searxng", {}, { FORGE_SEARXNG_URL: "http://llms/searxng" }), "http://llms/searxng");
});

// --- Per-provider normalization --------------------------------------------

test("wikipedia normalizes search hits and strips snippet markup", async () => {
	await withFixture(async (fixture) => {
		const results = await SEARCH_PROVIDERS.wikipedia.search("anapanasati", contextFor(fixture));
		assert.equal(results.length, 2);
		assert.equal(results[0].title, "Anapanasati");
		assert.equal(results[0].url, "https://en.wikipedia.org/wiki/Anapanasati");
		assert.equal(results[0].content, "Ānāpānasati means mindfulness of breathing");
		assert.equal(results[0].provider, "wikipedia");
		assert.equal(results[0].rank, 1);
		assert.equal(results[0].domain, "en.wikipedia.org");
	});
});

test("wikipedia resolve prefers the lead extract and refuses a disambiguation page", async () => {
	await withFixture(async (fixture) => {
		const entry = await SEARCH_PROVIDERS.wikipedia.resolve("anapanasati", contextFor(fixture));
		assert.match(entry.content, /mindfulness of breathing, a form of Buddhist meditation/);
		assert.equal(entry.wikidataId, "Q2123673");
	});
});

test("suttacentral searches and resolves a citation to the canonical record", async () => {
	await withFixture(async (fixture) => {
		const results = await SEARCH_PROVIDERS.suttacentral.search("anapanasati", contextFor(fixture));
		assert.equal(results[0].title, "MN 118 — Mindfulness of Breathing");
		assert.match(results[0].url, /\/mn118\/en\/sujato$/);
		assert.equal(results[0].content, "Ānāpānasati is of great fruit");

		// A citation is an identifier, not a search term.
		const resolved = await SEARCH_PROVIDERS.suttacentral.resolve("MN 118", contextFor(fixture));
		assert.equal(resolved.title, "MN 118 — Mindfulness of Breathing");
		assert.match(resolved.content, /teaches mindfulness of breathing in detail/);
		assert.ok(fixture.requests.some((path) => path.startsWith("/api/suttaplex/mn118")));
	});
});

test("cbeta normalizes a Taisho hit into a citable record", async () => {
	await withFixture(async (fixture) => {
		const results = await SEARCH_PROVIDERS.cbeta.search("安般守意", contextFor(fixture));
		assert.equal(results[0].title, "T0602 佛說大安般守意經");
		assert.equal(results[0].url, "https://cbetaonline.dila.edu.tw/T0602");
		assert.match(results[0].content, /後漢 安世高譯/);
		assert.equal(results[0].score, 16);
	});
});

test("sep matches its published index rather than guessing a slug", async () => {
	await withFixture(async (fixture) => {
		__setSepIndexForTesting(null);
		const results = await SEARCH_PROVIDERS.sep.search("phenomenology", contextFor(fixture));
		assert.equal(results[0].title, "Phenomenology");
		assert.equal(results[0].url, "https://plato.stanford.edu/entries/phenomenology/");
		// Entry titles are topic-based, so an inverted title still has to match.
		const inverted = await SEARCH_PROVIDERS.sep.search("perception", contextFor(fixture));
		assert.equal(inverted[0].url, "https://plato.stanford.edu/entries/perception-problem/");
		// A subject with no entry resolves to nothing rather than to a wrong one.
		assert.equal(await SEARCH_PROVIDERS.sep.resolve("quantum basket weaving", contextFor(fixture)), null);
		__setSepIndexForTesting(null);
	});
});

test("inpho turns a thinker into their SEP entry slug", async () => {
	await withFixture(async (fixture) => {
		const results = await SEARCH_PROVIDERS.inpho.search("Plato", contextFor(fixture));
		// The one thing SEP's own topic-based index cannot do from a name.
		assert.equal(results[0].url, "https://plato.stanford.edu/entries/plato/");
		assert.equal(results[0].title, "Plato");
	});
});

test("iep, openlibrary, gutendex, internetarchive and loc normalize their own shapes", async () => {
	await withFixture(async (fixture) => {
		const iep = await SEARCH_PROVIDERS.iep.search("phenomenology", contextFor(fixture));
		assert.equal(iep[0].url, "https://iep.utm.edu/cognitive-phenomenology/");

		const books = await SEARCH_PROVIDERS.openlibrary.search("dhammapada", contextFor(fixture));
		assert.equal(books[0].title, "The Dhammapada");
		assert.match(books[0].content, /Rose Kramer · first published 1995 · 2 editions/);
		assert.equal(books[0].publishedAt, "1995");

		const gutenberg = await SEARCH_PROVIDERS.gutendex.search("dhammapada", contextFor(fixture));
		assert.equal(gutenberg[0].url, "https://www.gutenberg.org/ebooks/2017.html");
		// No format links at all still yields the catalogue page, not a null URL.
		assert.equal(gutenberg[1].url, "https://www.gutenberg.org/ebooks/2018");

		const archive = await SEARCH_PROVIDERS.internetarchive.search("dhammapada", contextFor(fixture));
		assert.equal(archive[0].url, `${fixture.origin}/details/bdrc-W1KG13992`);

		const congress = await SEARCH_PROVIDERS.loc.search("dhammapada", contextFor(fixture));
		assert.equal(congress[0].title, "Dhammapada or path of virtue");
		assert.equal(congress[0].publishedAt, "1901");
	});
});

test("gdelt converts its own timestamp format into something Date can parse", async () => {
	await withFixture(async (fixture) => {
		const results = await SEARCH_PROVIDERS.gdelt.search("tibet", contextFor(fixture));
		assert.equal(results[0].publishedAt, "2026-07-26T20:00:00Z");
		assert.equal(Number.isNaN(Date.parse(results[0].publishedAt)), false);
	});
});

test("stackexchange and hackernews decode entities and carry their score", async () => {
	await withFixture(async (fixture) => {
		const answers = await SEARCH_PROVIDERS.stackexchange.search("async rust", contextFor(fixture));
		assert.equal(answers[0].title, "How do I handle errors in async Rust's tokio?");
		assert.equal(answers[0].score, 42);
		assert.match(answers[0].content, /answered · score 42/);

		const news = await SEARCH_PROVIDERS.hackernews.search("async rust", contextFor(fixture));
		assert.equal(news[0].title, "Why async Rust is hard");
		assert.equal(news[0].score, 300);
	});
});

test("hathitrust and bdrc resolve by identifier and decline anything else", async () => {
	await withFixture(async (fixture) => {
		const volume = await SEARCH_PROVIDERS.hathitrust.resolve("isbn 9780861713219", contextFor(fixture));
		assert.equal(volume.title, "The Long Discourses");
		assert.match(volume.content, /1995 · 1 scanned items/);
		// A phrase is not an identifier, and no request should be made for one.
		const before = fixture.requests.length;
		assert.equal(await SEARCH_PROVIDERS.hathitrust.resolve("the long discourses", contextFor(fixture)), null);
		assert.equal(fixture.requests.length, before);
		// A known-good identifier with no holdings is a miss, not a crash.
		assert.equal(await SEARCH_PROVIDERS.hathitrust.resolve("isbn 0000000000", contextFor(fixture)), null);

		const person = await SEARCH_PROVIDERS.bdrc.resolve("P1614", contextFor(fixture));
		assert.equal(person.title, "mi la ras pa");
		assert.equal(person.url, "https://library.bdrc.io/show/bdr:P1614");
		assert.equal(await SEARCH_PROVIDERS.bdrc.resolve("milarepa", contextFor(fixture)), null);
	});
});

// --- Merging ---------------------------------------------------------------

test("routed search merges providers, dedupes by URL and keeps the higher authority", async () => {
	await withFixture(async (fixture) => {
		const merged = await runRoutedSearch("anapanasati", ["wikipedia", "suttacentral"], () => contextFor(fixture));
		assert.equal(merged.errors.length, 0);
		// suttacentral (authority 1) outranks wikipedia (authority 9).
		assert.equal(merged.results[0].provider, "suttacentral");
		assert.deepEqual(
			merged.results.map((result) => result.rank),
			[1, 2, 3, 4],
		);
		assert.deepEqual(merged.perProvider.map((entry) => entry.provider).sort(), ["suttacentral", "wikipedia"]);
	});
});

test("one provider failing does not lose the others", async () => {
	await withFixture(async (fixture) => {
		// `loc` has no fixture route for this path shape, so it 404s.
		const merged = await runRoutedSearch("anapanasati", ["suttacentral", "hathitrust", "wikipedia"], (id) =>
			contextFor(fixture, id === "hathitrust" ? { base: `${fixture.origin}/missing` } : {}),
		);
		assert.ok(merged.results.length > 0, "a failing provider took the whole search down");
		// hathitrust has no search interface at all; that is recorded, not thrown.
		const skipped = merged.perProvider.find((entry) => entry.provider === "hathitrust");
		assert.match(skipped.skipped, /no search interface/);
	});
});

test("a definition question is stripped to its subject before it reaches a dictionary", () => {
	// Searching Wiktionary for the whole question matched "what" and "is";
	// searching it for the term finds pratityasamutpada.
	assert.equal(classifySearchQuery("what is dependent origination").term, "dependent origination");
	assert.equal(classifySearchQuery("define anatta").term, "anatta");
	assert.equal(classifySearchQuery("who was Nagarjuna?").term, "Nagarjuna");
	// A query that is already just a subject is left alone.
	assert.equal(classifySearchQuery("phenomenology of perception").term, "phenomenology of perception");
});

test("a provider is asked its own query when the router overrides it", async () => {
	await withFixture(async (fixture) => {
		const asked = [];
		const merged = await runRoutedSearch("what is anapanasati", ["wikipedia"], () => contextFor(fixture), {
			queryOverrides: { wikipedia: "anapanasati" },
		});
		assert.ok(merged.results.length > 0);
		asked.push(...fixture.requests.filter((path) => path.startsWith("/w/api.php")));
		assert.ok(
			asked.some((path) => path.includes("srsearch=anapanasati") && !path.includes("what")),
			`the framing question reached the provider: ${asked.join(" ")}`,
		);
	});
});

test("declared provider spacing matches what the services publish", async () => {
	const { PROVIDER_SPACING_MS } = await import(join(scriptsDirectory, "search-providers.mjs"));
	// GDELT states "one every 5 seconds" in its own 429 body.
	assert.equal(PROVIDER_SPACING_MS.gdelt, 5000);
	for (const [id, spacing] of Object.entries(PROVIDER_SPACING_MS)) {
		assert.ok(SEARCH_PROVIDERS[id], `spacing declared for unknown provider ${id}`);
		assert.ok(spacing > 0, id);
	}
});

test("classification records what it read, so a run can explain its routing", () => {
	const classified = classifySearchQuery("latest news about MN 118 and isbn 9780861713219");
	assert.equal(classified.identifiers.suttaUid, "mn118");
	assert.equal(classified.identifiers.bookIdentifier, null); // not the whole string
	assert.ok(classified.intents.includes("news"));
	assert.ok(classified.intents.includes("canon"));
});
