# Forge Embeddings Roadmap

pi-forge has a local embedding model always available through an
OpenAI-compatible endpoint (`FORGE_EMBEDDINGS_URL`, default
`http://llms:8005/v1/embeddings`, model `FORGE_EMBEDDINGS_MODEL`, default
`Qwen3-Embedding-0.6B`). Embeddings are worth adding only where the existing
workflow relies on *exact* equality (SHA-256, key columns, normalized URLs) or
asks the model to do *cross-item similarity* by reading everything. Those are the
gaps a similarity model genuinely closes.

This document tracks that work. The shared client and all three high-value
consumers are implemented; the remaining ideas are deferred with rationale below.

## Shared principles

These hold for every embeddings feature, current and future:

- **Embeddings feed a reviewable artifact, never an action.** Output a manifest
  column, a candidate-pair report, or a cluster assignment that a human reviews.
  Never auto-merge, auto-delete, or auto-dedup on a similarity score.
- **Exact stays authoritative.** Where exact equality already decides something
  (SHA-256 duplicates, declared join keys), keep it. Similarity only adds
  *candidates* that exact matching cannot see.
- **Preserve provenance.** Similarity grouping must not detach an item from its
  source path, locator, or hash.
- **Degrade cleanly.** If the endpoint is unreachable or a record has no
  embeddable text, the skill must behave exactly as it did before embeddings,
  and record why in its run metadata.
- **Standard-library client.** Reuse [`forge/lib/forge_embeddings.py`](../forge/lib/forge_embeddings.py)
  (urllib-based, batching, cosine, clustering) rather than adding dependencies.
- **No vector database for per-run work.** Brute-force cosine over a run's items
  is fine at forge scale (thousands of items). A persistent index is only
  justified for the cross-run search idea below.

## Done

### Shared embeddings client

[`forge/lib/forge_embeddings.py`](../forge/lib/forge_embeddings.py): OpenAI-compatible
`/v1/embeddings` client (batched, std-lib), a `doctor` reachability probe, and
`normalize` / `cosine` / `cluster_components` / `similar_pairs` helpers. Imported
by skills via the `forge/lib` path. All three planned Python skills can reuse it.

### organize-folder: content clusters and near-duplicates

[`forge/skills/organize-folder`](../forge/skills/organize-folder): the scan embeds
text-bearing files and adds `content_cluster`, `near_duplicate_of`, and
`content_similarity` manifest columns plus a `near_duplicates.md` report and
`profile.md` content-cluster section. This catches reformatted/exported/versioned
copies that exact SHA-256 duplicate detection misses, and groups related
documents to inform the layout. Near-duplicates are advisory; only exact
duplicates are auto-routed to `_duplicates/`.

### spreadsheet-analysis: fuzzy record linkage and semantic grouping

[`forge/skills/spreadsheet-analysis`](../forge/skills/spreadsheet-analysis): a new
`cluster` command embeds the combined text of one or more columns and groups rows
by cosine similarity into reviewable candidate groups (`clusters.csv`,
`cluster_groups.md`, `cluster_run.json`). A high threshold does fuzzy
duplicate/entity resolution ("Acme Inc" vs "Acme Incorporated") that exact-key
dedup cannot see; a low threshold categorizes a free-text column in one embedding
pass instead of N per-row model calls. Output is advisory candidate groups only;
exact-key dedup/merges stay authoritative and the source is never modified.

### literature-extraction: cross-document claim clustering

[`forge/skills/literature-extraction`](../forge/skills/literature-extraction): the
`build` step embeds `claim` and `finding` items and groups semantically similar
ones across documents into `claim_clusters.csv` and the advisory
`claim_clusters.md` worksheet, flagging groups with a lexical negation difference
as possible contradictions. This raises recall when authoring the claims matrix
and summary on larger corpora. The worksheet preserves each item's document and
locator, never reconciles or merges claims, and degrades cleanly (skipped when
embeddings are unavailable, `--no-claim-clusters` is passed, or fewer than two
items exist); `validate` does not require it. `report-output` can ingest the
worksheet like any other input. The contradiction flag is a crude prompt for
review, not a determination; the model judges each group against the evidence.

## Deferred (lower value or higher maintenance)

- **web-collection near-duplicate pages**: detect syndicated/mirrored content
  before handing `downloads/` to `document-ingest`. The existing URL / SHA-256 /
  Content-Disposition / title dedup already catches most cases, so this saves
  only marginal downstream work. Revisit if large mirror-heavy harvests become
  common.
- **Cross-run semantic search over `forge-output`**: a genuine new capability
  (ask "where did I see X" across all past runs), but it is an additive feature
  with a persistent index and staleness handling to maintain, not an
  acceleration of an existing flow. It also breaks the self-contained-run model.
  Only worth it once enough runs accumulate to want retrieval.
- **document-ingest boilerplate/chunk dedup**: detecting repeated boilerplate
  across chunks is low value relative to the faithful-extraction goal of that
  skill.
- **Skill routing via embeddings**: explicitly *not* worth doing. The 13 skills
  fit in launch context and the model already selects correctly; this would be
  adding embeddings because they are available, not because they help.
