---
type: template
status: active
domain: meta
subdomain: templates
capture_type: manual
---

%% ---------------------------------------------------------------------------
   TEMPLATE BLUEPRINT — the shape every template in this folder starts from.

   Copy this file, rename it, then delete every block the new template does not
   need. Deleting is the main work: a template that keeps all of these produces
   notes that are mostly empty scaffolding, which is worse than no template.

   The rules behind it are in [[0.04 Note Format]]. The registry there is the
   only source of callout types — a block not listed there does not exist.

   Frontmatter above: set `type` to what the template produces, and drop
   `subdomain` if the template is used across several. Everything must validate
   against [[0.00 Vault Schema]]. Never put `cssclasses` in a template unless the
   template is for a dashboard; it is human-owned.

   These %% comments are invisible in reading view. Keep the ones that explain a
   decision to whoever edits the template next; delete the rest.
   --------------------------------------------------------------------------- %%

# {{title}}

%% The lead. One or two sentences that decide whether to keep reading — for a
   reference card, the definition; for a memo, what happened. Keep it to the
   registry's one-lead rule: no second summary further down. %%

> [!summary]
> {{summary}}

{{body}}

%% The body is prose, and for many templates that is the whole note. Add `##`
   headings only once the note genuinely moves between parts. Per-type prose
   style is in [[0.01 Voice and Style]]. %%

## {{section}}

{{section_body}}

%% ---------------------------------------------------------------------------
   OPTIONAL BLOCKS — keep only what this kind of note actually produces every
   time. A block that is usually empty belongs in the author's head, not here.
   --------------------------------------------------------------------------- %%

> [!key] Key points
> {{key_points}}

%% Only when the note has claims worth extracting. Not a bulleted restatement of
   the lead — if the two would say the same thing, keep the lead. %%

> [!define] {{term}}
> {{definition}}

%% Only for a term this note stipulates the meaning of, not one it mentions. %%

> [!evidence] Evidence
> {{evidence}}[^1]

%% Carries an obligation: the source must be one this note actually has, and it
   must appear in ## Sources. A claim with no source is prose, not evidence. %%

> [!caution] {{caution_title}}
> {{caution}}

%% Limits, misuse, contested ground — the "use carefully" material. Ordinary
   qualifications belong in the sentence they qualify. %%

> [!question] Open questions
> {{open_questions}}

%% Genuinely unresolved. A question the note goes on to answer is a heading. %%

%% ---------------------------------------------------------------------------
   APPARATUS — about the note rather than part of it, which is why it sits after
   the content. All three are folded, and all three are machine-written: if this
   template is for notes a person writes by hand, delete this whole section.
   --------------------------------------------------------------------------- %%

> [!reflection]- {{reflection_section}}
> {{reflection}}

> [!connections]- Connections
> {{connections}}

> [!provenance]- Provenance
> {{provenance}}

## Sources

{{sources}}

%% A plain bullet list of links. Renders in both reading and source mode, and it
   is what a reader skims. The full URL appears once, here. %%

## Notes

%% Owner-authored. Arrives blank as a slot to write into, and no tool ever writes
   or reads it. Keep this heading in any template for a note a person will
   return to; drop it from templates for generated notes nobody annotates. %%

[^1]: {{footnote}}

%% Footnote definitions go here, at the very end, with no heading of their own —
   Obsidian hoists them into its own rendered area, so a heading above them would
   render empty. Each is a short locator; the URL lives in ## Sources. %%
