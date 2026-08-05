# Test fixtures

## `stack-snapshot.json`

A real `GET /api/v1/snapshot` from the llm-stack state API, captured 2026-08-05.

It is a genuine capture rather than a hand-written payload because the shapes
that matter are the awkward ones an invented example would smooth over: backends
whose `unit` is null because the model router holds them, proxy ports that appear
in no `base_url` and no `probe.target`, and a reranker the router spells `rank`
while the backend list spells it `rerank`.

**It has been scrubbed, and a re-capture must be scrubbed the same way.** This
file is committed to a public repository and ships inside the published npm
package, so anything identifying about the machine it came from is a leak. What
was removed, none of which any test reads:

- the API's bind address, which appears in the text of its own
  `api_unauthenticated` alert — replaced with `10.0.0.1`
- the GPU `uuid` values, which are hardware serial numbers — replaced with
  sequential placeholders
- the `deployment` block's HEAD, remote URL, and the list of files dirty in the
  working tree at capture time
- all of `config` except `Ports` (which is load-bearing: it is the only thing
  connecting a proxy port to the backend behind it) and `Secondary Backend`
  (kept so the "read every config block" path stays exercised). The full block
  is 263 keys of one deployment's tuning.

Model paths and the `LLMs` hostname are left as captured — both already appear
throughout this repository, so the fixture adds no exposure.
