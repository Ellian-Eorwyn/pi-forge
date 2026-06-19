# Faithful Transcript Cleanup

Use this workflow to return the complete cleaned transcript without a summary.
Keep title generation, tag generation, and YAML frontmatter independent from
the cleanup preset.

## Invariants

Always apply these instructions:

```text
- Change as little wording as possible while making the transcript clean and readable.
- Preserve the speaker's intent, uncertainty, and nuance.
- Do not summarize. Output the full cleaned transcript.
```

Never add facts, conclusions, speaker identities, or certainty absent from the
source. Preserve meaningful repetition, hesitation, qualifications, and
ambiguity.

## Presets

| Preset | Paragraphs | Filler | Repetitions | Reorder topics |
|---|---:|---:|---:|---:|
| Raw Transcript | Off | Off | Off | Off |
| Paragraphs Only | On | Off | Off | Off |
| Filler Cleanup | On | On | On | Off |
| Coherent Narrative | On | On | On | On |

Default to **Filler Cleanup** for personal notes, lectures, interviews, and
speaker recordings. Presets do not change title, tags, or frontmatter options.

## Toggle Instructions

Insert exactly one instruction from each pair into the editing request.

### Paragraphs

- On: `Format the result into clean markdown paragraphs. Use headings only when topic regrouping makes them genuinely useful.`
- Off: `Keep the transcript as a continuous block of text without adding headings.`

### Filler

- On: `Remove filler words and obvious speech scaffolding.`
- Off: `Do not remove filler words.`

### Repetitions

- On: `Remove direct repetitions, false starts, and duplicate phrases when they do not change meaning.`
- Off: `Preserve repetitions.`

### Reorder Topics

- On: `You may reorder passages to group related ideas together, but keep the speaker's meaning, claims, and word choices as intact as possible.`
- Off: `Keep the original ordering of ideas.`

### Title

- On: `Create a concise descriptive title suitable for a filename.`
- Off: `Use the original file basename as the title.`

Use the Obsidian-specific wording `Create a concise descriptive title suitable
for an Obsidian note filename.` only when the user requests an Obsidian-ready
output.

### Tags

- On: `Generate 3 to 6 concise lowercase tags. Prefer nouns and themes rather than verbs.`
- Off: `Return an empty tags array.`

Use Obsidian-style tags only when the user requests an Obsidian-ready output.
If the user supplies tag hints, append `User tag hints: <comma-separated hints>`
immediately before `Transcript:`. Treat hints as suggestions, not required tags.

### YAML Frontmatter

Do not change the editing request. Assemble frontmatter from the validated
title and tags after cleanup. Include it only when requested. Quote or escape
YAML values safely and do not add unrelated properties.

## Structured Model Contract

When delegating cleanup to another model or requiring a machine-validated
intermediate response, use this system instruction:

```text
You are a meticulous transcript editor.
Return strict JSON only, with this exact schema:
{
  "title": "Short title",
  "tags": ["tag-one", "tag-two"],
  "markdownBody": "Clean markdown transcript"
}

Never add commentary outside the JSON object.
The markdownBody value must be a valid JSON string literal.
Escape all newline characters inside markdownBody as \n.
Escape any double quotes that appear inside markdownBody.
Never invent facts.
Keep the voice and meaning faithful to the speaker.
```

Assemble the user request in this order:

```text
Source filename: <source file basename>

Editing instructions:
- <topic-order instruction>
- <filler instruction>
- <repetition instruction>
- <paragraph instruction>
- <title instruction>
- <tags instruction>
- Change as little wording as possible while making the transcript clean and readable.
- Preserve the speaker's intent, uncertainty, and nuance.
- Do not summarize. Output the full cleaned transcript.
<optional tag hints line>

Transcript:
<full raw transcript>
```

Parse the response as JSON and validate the exact fields before composing the
Markdown file. Reject commentary outside the object, missing or extra fields,
non-string titles or bodies, and non-string tag values. Do not silently repair
a response whose meaning is ambiguous.

When performing cleanup directly in the current Pi session, apply the same
contract semantically but write the requested Markdown artifact rather than
exposing an unnecessary JSON intermediate to the user.

## Output

Write `cleaned_transcript.md` with the generated or source title followed by
the complete cleaned transcript. Do not add a summary, action list, or analysis.
If requested, place YAML frontmatter before the title. Record source path,
checksum, selected preset, enabled metadata options, and extraction warnings in
the completion report; do not mix this generated provenance into the speaker's
transcript unless the user requests it.
