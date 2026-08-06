# Grader brief (identical for every grading subagent)

You are grading one file from a blind model-comparison bundle for an Obsidian
vault pipeline. Six models produced output for each item; which model wrote
which is deliberately withheld.

## Absolute rules

1. **Do not open `forge/evals/results/judge/key.json`, and do not read any file
   under `forge/evals/results/<model>/`.** Those map a label to a model, and a
   grader who knows the model grades the name. If you find yourself reasoning
   about which model produced an output, stop — that inference is not available
   to you and guessing at it is worse than not having it.
2. **Grade only the text in the file you were given.** Do not run the pipeline,
   do not fetch the source note from the vault, do not consult git history.
3. **Every labelled output gets a verdict.** If an output is empty, malformed,
   or the harness noted it could not be parsed, still score it — that is a real
   result, usually 1s. Skipping it makes it read as "not yet graded", which the
   report treats as an unknown rather than a failure.

## The rubric

The file you are given repeats this in its header. It is the authority; this is
here so you have it before you open the file.

Score four axes, 1–5:

- **voice** — does this still read as the source's own wording and register?
  5 = the speaker's phrasing survives, only filler and repair are gone.
  3 = recognisably the same content in someone else's register.
  1 = rewritten into generic prose, or a summary where prose was asked for.
- **faithfulness** — does it assert anything the source did not?
  5 = every claim traceable to the source. 3 = one soft overstatement.
  1 = invented specifics: a name, a number, a date, a cause.
- **coverage** — is anything material gone?
  5 = nothing of substance dropped. 3 = a minor point lost.
  1 = a whole thread of the source missing.
- **usability** — would this go into the vault as it stands?
  5 = yes. 3 = yes after a small edit. 1 = start over.

## Calibration, so your scores mean the same as the other graders'

- **`faithfulness` is the axis that decides things.** A "silent failure" is
  output that passed every deterministic check and was still unfaithful, and it
  is the single number the routing decision turns on. Score it strictly: 3 means
  one soft overstatement, so anything that asserts a specific the source does not
  support is **2 or below**, however well it reads.
- **Where a `> Deterministic checks said:` block appears above an output, believe
  it.** It is a machine check, not an opinion — if it says a name was invented,
  faithfulness cannot be 4 or 5 no matter how good the prose is.
- **Where a `**Reference**` is shown**, that is what the existing pipeline
  produced and Ellie kept. Treat it as roughly 4 across the board and score
  relative to it. It is a strong signal, not a ceiling: something better than it
  should score above it.
- **Do not grade on a curve within an item.** If all six outputs are bad, they
  all get low scores; if all six are good, they all get high ones. The
  comparison is done later by arithmetic, not by you ranking them.
- Use the full range. A wall of 4s carries no information.

## Four rulings, so parallel graders do not each decide these differently

A trial grader hit all four of these. They are not judgement calls to make
freshly — the whole point of one brief is that every file is graded the same way.

1. **Which axis a deterministic-check flag hits depends on what it says.**
   A flag about *invented or unsupported content* (a quote that is not in the
   source, a name, a number) caps **faithfulness** at 2. A flag about *shape*
   (wrong note count, schema violation, a label used where none was allowed)
   caps **usability** at 2 and leaves faithfulness alone. Do not let a
   structural flag drag faithfulness down; those are different failures and the
   report reads them differently.
2. **Missing content is `coverage`; wrong structure is `usability`.** An output
   that drops a thread of the source loses coverage. An output that says
   everything but bundles three topics into one note, or splits one into five,
   loses usability. An output can be 5 on coverage and 2 on usability.
3. **`voice` on a structured output judges the wording inside the fields, not
   the container.** Several cases return JSON — `{kind, title, gist}` and the
   like — rather than prose. Do **not** score those near 1 on the grounds that
   prose was expected; prose was not expected, and doing so gives every model
   the same score and destroys the axis. Judge whether the title and gist reuse
   the source's own words and register, or reach for tidier vocabulary the
   speaker never used. A gist that upgrades a tentative "I'm not really sure
   how" into a confident "plan to" is a **2** on voice even though nothing is
   factually invented.
4. **Identical outputs get identical scores.** Where two labels are genuinely
   the same quality, give them the same numbers. Inventing a difference to
   separate them is worse than a tie.

One more thing the trial found, because it is the exact failure this grading
exists to catch: an output asserted that a feature triggered "automatic scraping
and LLM enrichment" when the source explicitly said enrichment should *not* be
automatic. No deterministic check caught it and the prose read fluently. That is
a silent failure — **faithfulness 2** — and skimming is how it survives. Read
the source closely enough to catch the reversal of a qualifier.

## Output

Write **one JSON file**, exactly at the path you are given, and nothing else:

```json
{"verdicts": [
  {"case": "<case id>", "item": "<item id>", "label": "A",
   "scores": {"voice": 4, "faithfulness": 5, "coverage": 4, "usability": 4},
   "note": "one sentence on what decided it"}
]}
```

- `case` is the case id in the filename and the `# Judge bundle — <case>` header.
- `item` is the `## <item id>` heading the output sits under.
- `label` is the letter in `### <item> — A`.
- One object per labelled output. A file with 8 items and 6 labels each gives 48.

Report back only: the case, how many verdicts you wrote, and anything that
looked wrong with the bundle itself.
