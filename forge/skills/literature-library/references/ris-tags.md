# RIS tag vocabulary

The reader in `forge/lib/citation_parse.py` accepts a strict superset of the tags
the Forge RIS **writer** emits. That writer is `buildRisRecord` in
`forge/skills/web-research/scripts/web-research.mjs`. The two are coupled: a
change to either belongs in the same commit as a change to the other, so a file
Forge writes always round-trips through the file Forge reads.

## Written by `buildRisRecord`, therefore required here

| Tag | Canonical field |
| --- | --- |
| `TY` | `type` (via `RIS_TYPE_MAP`), raw value kept as `ris_type` |
| `TI` | `canonical_title` |
| `AU` | `authors[]` |
| `AB` | `abstract_best` |
| `PY` | `publication_year` |
| `Y1` | `publication_date` fallback |
| `JO` | `venue_name` |
| `T2` | `venue_name` fallback |
| `PB` | `publisher` |
| `DO` | `identifiers.doi` |
| `UR` | `urls[]` |
| `KW` | `keywords[]` |
| `SN` | `identifiers.issn` / `isbn` / `serial`, chosen by shape |
| `N1` | `notes[]` |
| `ER` | record terminator |

## Added for other exporters

| Tag | Canonical field | Seen in |
| --- | --- | --- |
| `DA` | `publication_date` | Consensus, Zotero |
| `VL` `IS` `CP` | `volume`, `issue` | most |
| `SP` `EP` | `pages` | most |
| `T1` `CT` `BT` `ST` `TT` | `canonical_title` fallbacks | EndNote, books |
| `JF` `JA` `J1` `J2` | `venue_name` fallbacks | PubMed, EndNote |
| `A1` | primary author | EndNote |
| `A2` `A3` `A4` `ED` | `editors[]` | EndNote, chapters |
| `N2` | `abstract_best` fallback | PubMed |
| `M3` `M1` `M2` | `identifiers.doi` fallback | Wiley, Sage |
| `AN` `ID` | `identifiers.pmid` / `pmcid` / `arxiv_id` | PubMed, arXiv |
| `L1` | `full_text_candidates[]` as `ris-file-link` | **Zotero** |
| `L2` `L3` `L4` | `urls[]` | Zotero, Mendeley |
| `CY` `PP` | `place` | books |
| `ET` | `edition` | books |
| `LA` | `language` | most |
| `C1`–`C8` | provenance only, kept in `source_tags` | custom |

`L1` is the highest-value addition. A Zotero export that already carries local
file paths or publisher PDF links turns the acquisition ladder's first stage into
a no-op for those records.

## Grammar notes

- The canonical separator is two spaces before the hyphen (`TY  - JOUR`). One
  space is tolerated and reported once per file.
- A line matching the tag pattern is only treated as a tag when the tag is in
  `KNOWN_TAGS` or the strict two-space form was used. Without that guard, a
  wrapped abstract line such as `US - based studies` parses as a tag named `US`.
- RIS has no formal continuation syntax, but Zotero, EndNote, and Mendeley all
  wrap long `AB` and `N1` values. An unrecognized non-blank line is appended to
  the previous tag's last value, joined with a single space.
- `ER  - ` may carry trailing whitespace. Consensus emits it that way; the Forge
  writer does not.
- A second `TY` before an `ER` is a hard error with a line number. An
  unterminated final record at end of file is a warning plus recovery, so one
  truncated export does not cost the caller every other record in the file.

## Encoding

Decoding tries `utf-8-sig`, then `cp1252`, then `latin-1` with replacement. The
detected encoding, BOM presence, and U+FFFD count are recorded in
`library_config.json`.

A U+FFFD already present in the source bytes is **destroyed information** — the
exporter lost that character before writing the file, and it cannot be recovered.
`--repair-replacement-chars` guesses a right single quote for the common
`it<U+FFFD>s` case, writes every substitution to `citation_normalizations.jsonl`,
and never touches an identifier. Without the repair the replacement character
survives `safe_title` and lands in the filename, because it is neither a control
character nor path-unsafe.
