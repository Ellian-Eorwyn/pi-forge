# Overnight eval run — results

Ran 21:15 → 05:37. All three models completed at `--suite full --stabilize 3`
against the stored `chat-27b` baseline. Nothing failed.

## The answer, in one line each

| Decision | Verdict |
| --- | --- |
| Does `moe-35a3b` replace `chat-27b` as the bulk tier? | **No.** Better on 0 cases, worse on 7. |
| Does `task-9b` replace `task-4b` as the small tier? | **No.** It loses or ties both stages actually routed there. |
| Does `moe-35a3b-think` replace `think-27b` as the verify tier? | **No.** Better on 0, worse on 2. |

**No change to `forge/lib/forge_routing.py` is needed** — unless you keep the
MoE, in which case see "If you keep the MoE" below, which is the one place this
run changes a shipped decision.

All three lose on deterministic gates, which is why these verdicts stand without
waiting on grading: a grader can add silent failures but cannot turn a 0/8 into
a pass. Grading is running now and will only sharpen the margins.

## Cost, per attempt — the comparable figure

| Model | Attempts | Wall | **s/attempt** | tok/attempt |
| --- | --- | --- | --- | --- |
| `moe-35a3b` | 458 | 2792s | **6.1** | 600 |
| `task-9b` | 404 | 3221s | **8.0** | 473 |
| `task-4b` | 412 | 3902s | **9.5** | 859 |
| `chat-27b` | 230 | 2495s | **10.8** | 540 |
| `moe-35a3b-think` | 466 | 16836s | **36.1** | 3497 |
| `think-27b` | 386 | 19376s | **50.2** | 3173 |

Read s/attempt, not wall: `--stabilize` repeated different cases for each model,
so the wall column covers different amounts of work. The MoE really is the
fastest thing here — 1.8x the baseline — and `task-9b` is faster than `task-4b`
despite being twice the size, because it generates 473 tokens per attempt
against the 4B's 859. Verbosity, not parameter count, was always the 4B's cost.

## 1. `moe-35a3b` — fast, and worse almost everywhere

Better on 0, worse on 7. Finished in 63 minutes.

| Case | `chat-27b` | `moe-35a3b` |
| --- | --- | --- |
| `meeting-brief` | 6/8 | **1/8** |
| `verifier-seeded` | 7/8 | **2/8** |
| `classify-hard` | 3/8 | **0/8** |
| `grounding-draft` | 3/8 | **0/8** |
| `enumeration-breadth` | 3/8 | **0/8** |
| `abstention-closed-book` | 10/12 | **6/12** |
| `braindump-split` | 4/8 | **2/8** |

Two of those are worse than "a bit worse". **`meeting-brief` 1/8** is long-form
reasoning, the thing `vault-transcripts` leans on hardest. **`verifier-seeded`
2/8** means it is a poor reviewer, and verification is the mechanism the whole
non-thinking pipeline rests on — a weak verifier degrades every stage, not one.

It holds `abstention-grounded` at 12/12, matching the dense model with a source
in front of it, while collapsing to 6/12 closed-book. Fewer active parameters is
less knowledge: it guesses where the dense model knows. That is the only shape
it is safe for — cheap, fast, and only when the answer is already in the prompt.

**Action: put the 27B dense back on the primary backend.** Everything the vault
pipeline does today is running on the weaker model.

## 2. `task-9b` — better at knowing, worse at composing, and it takes neither routed stage

Better on 3, worse on 4. Finished in 66 minutes. The table sends exactly two
stages to the small tier and the 9B loses or ties both:

| Routed stage | Case | `task-4b` | `task-9b` | |
| --- | --- | --- | --- | --- |
| `clean-transcript-chunk-multi` | `transcript-cleanup-meeting` | **7/8** | 5/8 | 4B keeps it |
| `connection-judgment` | `connection-judgment` | **16/16** | 16/16 | tie; 4B marginally faster per item |

That is a real result, not a null one: the 4B was already beating *both* 27B
profiles at diarized cleanup, and a model twice its size does not reach it.

Where the 9B is better is a different shape of work — closed-book knowledge
(5/12 → 7/12), long context (`lcr-60k` 8/10 → **10/10**, the only small model to
take a rung perfectly), single-speaker cleanup (5/8 → 7/8). Where it is worse is
structured generation: `grounding-draft` 5/8 → **0/8**, `doc-cleanup-ocr` 3/8 →
**0/8**, `classify-notes` 4/8 → 2/8. More knowledge, worse instruction-following
— consistent with a Q4 9B beside a Q6 4B, and some of that gap is the quant.

## 3. `moe-35a3b-think` — the thinking profile rescues the MoE, but not past the dense one

Better on 0, worse on 2, against `think-27b`. It is faster per attempt (36.1
against 50.2) but took *longer* in wall time because stabilization repeated more
cases — 466 attempts against 386.

The interesting comparison is not against the dense model but **within the MoE**:

| Case | `moe-35a3b` | `moe-35a3b-think` |
| --- | --- | --- |
| `verifier-seeded` | 2/8 | **7/8** |
| `braindump-split` | 2/8 | **7/8** |
| `meeting-brief` | 1/8 | **4/8** |
| `lcr-60k` / `lcr-80k` | 8/10 | **10/10** |

Thinking is better on 5 and worse on 0 there. So reasoning recovers most of what
sparsity costs — including the verification collapse, which goes 2/8 → 7/8 and
lands exactly level with `think-27b`. A MoE deployment is viable *if* everything
that matters runs on the thinking profile, which is the opposite of the
economics the whole `chat`/`think` split exists for.

## If you keep the MoE — one shipped routing row breaks

`forge_routing` sends two stages to `think`. Those are service names, not
weights, so on a MoE deployment they become the within-MoE comparison above:

- **`split-braindump` → `think` survives.** 2/8 → 7/8 on the MoE, the same jump
  thinking gave on the dense weights. Keep it.
- **`clean-transcript-chunk-single` → `think` does not.** It was justified by
  `think-27b` scoring **8/8** against the bulk model's 2/8. On the MoE thinking
  profile it is **2/8**. The row is wrong for that deployment and single-speaker
  cleanup should go back to `chat` until the dense weights return.

One line in `forge/lib/forge_routing.py`, and only if the MoE stays.

## Graded quality — it corroborates the gates

Twelve subagents graded the blind bundle; 174 of 256 outputs are in at time of
writing, 43–44 per model, which is enough to read.

| Model | Voice | Faithfulness | Coverage | Usability |
| --- | --- | --- | --- | --- |
| `chat-27b` | 3.57 | 4.07 | 3.93 | 3.73 |
| `moe-35a3b-think` | **3.64** | 4.00 | 3.80 | 3.73 |
| `task-9b` | 3.56 | **4.12** | 3.70 | 3.49 |
| `moe-35a3b` | **3.21** | **3.72** | **3.74** | **3.42** |

`moe-35a3b` is last on every axis, which is the same verdict its gates gave. The
MoE thinking profile lands level with the dense baseline on judged quality — and
costs 36.1 s/attempt against 10.8 to get there.

`task-9b` scores the **highest faithfulness of any model** (4.12) while sitting
low on coverage and usability. Careful and thin, consistent with generating 473
tokens per attempt against the 4B's 859: it asserts less, so it invents less, and
it also says less.

**The two grading passes are comparable.** `chat-27b` was graded in both, and the
subagents came within −0.45 (voice), −0.05 (faithfulness) and −0.26 (coverage) of
Ellie's own earlier scores — inside the ±0.5 band the merge script checks for.
That is what makes it legitimate to read these numbers beside the earlier
`task-4b` / `think-27b` grades.

## Grading

Twelve subagents are grading the blind bundle now — one per case, with
`meeting-brief` split four ways and `transcript-cleanup-meeting` two, because
those carry a whole transcript per item as the source and faithfulness is
exactly the axis that needs it. All work from one shared brief so the scores are
comparable.

A trial grader first found four rubric ambiguities that would have made parallel
graders mutually inconsistent — which axis a deterministic flag hits, the
coverage/usability boundary, how to score `voice` when the output is a JSON stub
rather than prose (it collapsed to two distinct values across 24 outputs), and
ties. The brief now rules on all four. That trial also caught a real silent
failure no machine check saw: an output asserting a feature did "automatic
scraping and LLM enrichment" where the source said enrichment should *not* be
automatic.

The bundle covers `chat-27b` plus the three new models. Your earlier grading of
`task-4b` / `think-27b` is preserved at
`forge/evals/results/judge/_prior-grading-2026-08-05/` — `judge` overwrites the
key those verdicts need to stay unblindable, so it was backed up before the
rebuild. When the graders finish:

```bash
python3 forge/evals/merge-verdicts.py
python3 forge/evals/run.py score --verdicts forge/evals/results/judge/verdicts.json
python3 forge/evals/run.py report --models chat-27b,task-4b,think-27b,moe-35a3b,task-9b,moe-35a3b-think --baseline chat-27b
```

`merge-verdicts.py` validates every verdict against the key, refuses duplicates
and out-of-range scores, and reports what went ungraded — an ungraded output
reads as `unknown`, never as clean.

## Three defects in the harness, found by graders reading closely

**1. A false positive in the invented-number check.** See below.

**2. The coverage check misses accurate paraphrase.** On `meeting-vpp-tech` the
gate reported "3 of 22 reference facts covered" for one output and "0 of 22" for
another; a grader reading both found several reference facts clearly present in
each, paraphrased rather than quoted. `factsCovered` appears to match strings.
That understates coverage across the whole `meeting-brief` case and, because the
brief tells graders to trust the flags, it propagates into the judged scores too.

**3. My bundle splitter cut an item in half.** `split-bundle.py` broke on lines
starting with `## ` — including ones inside the ```` ```text ```` fences, because
a cleaned transcript legitimately contains its own markdown headings. One
`transcript-cleanup-meeting` part opened mid-output with no label heading above
it and without the source block, which had stayed in the previous part. That
grader could not attribute the fragment and graded the rest against a proxy. **So
`transcript-cleanup-meeting` grades are the least trustworthy in this run** —
about two items' worth. Fixed (the splitter now tracks fence state), but the
grading was already done against the broken split.

One more, less severe: on `transcript-cleanup-memo` / `transcript-vaultintegration`
the `**Reference**` block contains a three-point feature list that does not
appear in that item's source at all. The grader correctly graded against the
source rather than the reference, but the reference is wrong for that item.

## A false positive in the deterministic checks

`meeting-brief` / `meeting-kickoff` label C was flagged by the gate for an
invented number, "118". A grader reading the transcript found it there — spelled
out in words rather than digits. The gate's number check compares digit strings,
so a figure the speaker said aloud reads as fabricated.

That is the opposite of the failure the suite is built around and it is worth
fixing: a false flag costs a per-item escalation, which is the most expensive
call in the pipeline, and it also teaches a grader to distrust the flags the
brief tells them to believe. The grader followed the instruction and capped the
score anyway, so this run's numbers are very slightly harsh on whichever model
produced that item.

## An unrelated finding the grading turned up, and it is worth more than the model comparison

On `summary-transcript` / `raw-asr-piforge`, **all four models wrote "PyForge"**
— and the source transcript contains the speaker correcting the transcriber
outright: *"I can build into PyForge. Pi is spelled P I, not P Y."*

So this is not a model failure. Every model faithfully reproduced what the
cleaned text handed it, and the mistranscription was already in the cleaned
text. The transcriber renders "Pi Forge" as "PyForge", nothing corrects it, and
it propagates:

- **54 notes in the vault contain "PyForge"**, including at least one note
  *title* — `2026-07-24 - Memo - PyForge Obsidian Vault Integration Feature`.
- `~/.pi-forge/transcription/dictionary.json` has **no forge-related entry at
  all**, so the lexicon layer that exists precisely for this never fires.

The fix is one dictionary entry, in the shape the file already uses:

```json
{"correct": "Pi Forge", "variants": ["PyForge", "Py Forge", "PieForge"],
 "category": "name", "case_sensitive": false, "whole_word": true}
```

Two cautions before applying it. Some of those 54 notes may be your own writing
rather than transcript output, so a blanket rename is not safe — check before
touching filed notes. And `vault-lexicon` gates new terms on transcript
evidence; this one has it, in the clearest possible form, since the speaker
spells the correction out loud.

This is the kind of thing only a careful reader finds. No deterministic check
looks for a product name contradicting its own spelling correction, and every
model scored well on the summary that contained it.

## Registry changes committed

`d1ddd032e`. The MoE turned out to be **Q6_K, not the Q4_K_M** the entry claimed
— better, because `chat-27b` is also Q6_K, so the comparison isolates
architecture. It is 27.94 GiB, larger than the dense model. `moe-35a3b-think`
was added because loading the MoE takes :8008 with it. The 9B's 65792 context is
recorded as a property of the *pair*, per your note: beside the 27B dense the KV
cache does not fit, so re-run `add-model --write` after any change to what
occupies the primary backend.

## Files

- `pairwise.md` — every comparison, per case, including the two within-family ones
- `report.md` — the full six-model report (ungraded at time of writing)
- `overnight.log` — timeline; `<model>.log` — per-run output
