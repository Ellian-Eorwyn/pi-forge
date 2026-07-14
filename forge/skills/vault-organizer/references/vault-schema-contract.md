# Vault Organizer Schema Contract

`vault-organizer` reads the vault's canonical Markdown schema note, defaulting
to `99 System/0.00 Vault Schema.md`. The schema note is authoritative; cached
JSON under `.vault-organizer/cache/` is generated state.

## Parsed Sections

The compiler parses these exact Markdown sections and fails closed when a
required section, required table column, or row is malformed:

- `Approved properties`
- `Note types`
- `Status values`
- `Domains`
- `Subdomains`, with one `### <domain>` subsection per domain that has
  subdomains
- `Project registry`
- `Source kinds`
- `Capture types`
- `Legacy normalization map`
- `Folder routing`

The parser uses stable headings and table columns. It does not use an LLM to
parse the schema and does not reconstruct routes from prose examples.

## Routing

Only `domain`, `subdomain`, and `project` determine the destination folder.
Folder names are derived by code:

- domain: `<pad2(domain.number)> <domain.label>`
- subdomain: `<domain.number>.<pad2(subdomain.number)> <subdomain.label>`
- domain project: `<domain.number>.<pad2(project.number)> <project-name>`
- subdomain project:
  `<domain.number>.<pad2(subdomain.number)>.<pad2(project.number)> <project-name>`

The script refuses unregistered values, absolute paths, `..` traversal, unsafe
labels, duplicate derived destinations, and destination collisions.

## Frontmatter

Existing YAML frontmatter is untrusted input. The script discards it and emits a
new canonical block using the schema property order. List-valued properties are
always block lists. Wikilinks are quoted. Empty optional values are omitted.

Malformed opening frontmatter delimiters without a closing `---` are not guessed
or repaired. The note is left unchanged and added to the review queue.

## Model Output

The model returns JSON only:

```json
{
  "metadata": {
    "type": "note",
    "status": "active",
    "domain": "technology",
    "subdomain": "obsidian",
    "project": "[[Pi Forge]]",
    "parent": "[[Vault Organization]]",
    "people": [],
    "organization": null,
    "related": [],
    "source_kind": null,
    "capture_type": "manual"
  },
  "needs_review": false,
  "review_reason": null
}
```

The validator treats model output as untrusted. It validates keys, shapes,
controlled values, wikilink syntax, project inheritance, conditional
`source_kind`, and control characters. One repair request is allowed after an
invalid response; if the repair fails, the note remains unchanged and enters the
review queue.

## Apply

Dry run is the default. With `--apply`, the script re-reads every source,
verifies the recorded SHA-256, backs up the original file under the run
directory, writes through a temporary file on the destination filesystem, and
records every operation in `apply-log.jsonl`. It never overwrites existing files
and does not delete backups automatically.
