# backend-check

A stand-alone sanity check for the local chat backend. Run it after swapping
weights, changing a launch flag, or toggling speculative decoding, to confirm in
one command *what is loaded*, *how fast it is*, and *that it still behaves*.

```bash
python3 forge/evals/backend-check/check.py            # identity + speed + smoke test
python3 forge/evals/backend-check/check.py backend    # identity + speed only
python3 forge/evals/backend-check/check.py test        # smoke test only
python3 forge/evals/backend-check/check.py sweep      # is reasoning_effort honoured? (Qwen 3.8+)
```

Example, against the current deployment:

```
backend
  model id      chat
  gguf          /mnt/LLMs/llamacpp/llm-stack-git/models/Qwen3.8-27B-Q6_K.gguf
  params/quant  27.32B  Q6_K
  context/size  131,072 tok  21.30 GiB
  spec-decode   ON  — method=draft-mtp, draft=built-in MTP head (no external draft)
  stack config  http://llms:8077

speed  (llama.cpp timings)
  prefill       1553.1 tok/s  over 15195 prompt tok
  decode        77.7 tok/s
  MTP accept    424/463 (92%) draft tokens accepted

smoke test  (abstention: answer from source, decline what is not there)
  grounded      correct 9/9   declined 0   confabulated 0   malformed 0   index +1.00
  closed_book   correct 7/7   declined 0   confabulated 0   malformed 0   index +1.00
  overall       16 items   confabulations 0   omniscience index +1.00
```

## Why it is separate from the eval suite

The suite (`../run.py`) is the right tool for *should a skill stage move to this
model* — it drives real skill prompts through real gates. But two things make it
the wrong tool for a quick "is the backend healthy" check, and both bit a run in
August 2026:

- **One broken case stops every run.** Resolving a suite imports every case
  module to read its tier, so a single case whose module raises at import — e.g.
  `meeting-brief`, which builds its prompt from a `vault-transcripts` header that
  an in-progress edit had changed — aborts the whole runner before anything runs.
- **Attribution rode on a stale URL.** The suite verifies the served gguf path
  through a stack-state URL that defaulted to `:8078` while the live API was
  `:8077`. At the wrong port the path check silently no-ops, so a 3.6 → 3.8 swap
  (identical param count and quant) went undetected and a run would have been
  labelled with the old weights.

This checker cannot hit either. It **imports nothing from the repo** (stdlib
only), so no skill or case can break it. It takes every endpoint as an argument,
and it **finds the stack manager itself** — if `--stack-url` does not answer it
tries the known ports and reports which one it used, rather than trusting one
address. When it cannot read the gguf path it prints `UNVERIFIED` loudly instead
of passing an unchecked claim.

## What it checks

**Identity** — the served gguf path, parameter count, quant, and context, read
from the model-metadata port (`:8010`) and the stack manager's `/api/config`
(`:8077`), never inferred from a label. Spec-decode state is read from
`<PREFIX>_SPEC_METHOD` / `<PREFIX>_SPEC_DRAFT_MODEL_PATH`; `draft-mtp` with an
empty draft path is the model's built-in MTP head.

**Speed** — prefill and decode tokens/second from llama.cpp's own `timings`
block, plus the MTP draft-acceptance rate (`draft_n_accepted / draft_n`). This is
where a spec-decode change shows up: with MTP off this build decoded ~33 tok/s,
with `draft-mtp` on it decodes ~77 tok/s at ~92% acceptance.

**Behaviour** — a small abstention smoke test scored AA-Omniscience style
(correct +1, a confident wrong answer −1, declining 0). Half the questions are
answerable from a short synthetic source or are floor-level facts; half are
unanswerable or about things that do not exist, and the model is supposed to
decline them. `confabulations` — answering where the right move was to decline —
is the number that matters; it is the failure every document-reading skill here
is built to avoid. The data lives in [`testset.json`](testset.json), and the two
system prompts there are mirrored verbatim from `../cases/_abstention.py`.

This is a smoke test, not the eval suite: a clean result means the backend is up,
fast, and not obviously broken. It does **not** carry the suite's guarantee that
an output is one production would have accepted — for a routing decision, run
`../run.py`.

**Reasoning-effort sweep** (`sweep`) — confirms the endpoint actually honours a
`reasoning_effort` of `xhigh` / `medium` / `low` / `none` (Qwen 3.8+). This is
worth a command of its own because a template that does not read the field
discards it *silently* — no error, the setting just does nothing — and then a
whole thinking-mode comparison measures one level four times. The signal is the
reasoning trace, not the answer: `none` must come back with zero
`reasoning_content` and a steered level must not. With no `--chat-url` it aims at
the thinking A/B port (`:8008` / `code`); exit status is non-zero unless the
verdict is `PASS`. Run it before every thinking-mode comparison, and again after
any template or port change.

## Options and exit status

| flag | default | meaning |
| --- | --- | --- |
| `--chat-url` | `http://llms:8004/v1` | where completions are served |
| `--meta-url` | `http://llms:8010/v1` | `/v1/models` with params & quant |
| `--stack-url` | `http://llms:8077` | stack manager `/api/config` (path, spec) |
| `--model` | `chat` | model id as the endpoint serves it |
| `--prefix` | `CHAT_PRIMARY` | which backend's config keys to read (e.g. `TASK`) |
| `--expect-path PATH` | — | exit non-zero unless the served gguf matches |
| `--json` | — | machine-readable output instead of text |

Each flag also reads an env var (`FORGE_CHAT_URL`, `FORGE_META_URL`,
`FORGE_STACK_URL`, `FORGE_CHAT_MODEL`). Exit status is `1` when the endpoint is
unreachable or an `--expect-path` assertion fails — so it drops into a pre-flight
check before a longer job:

```bash
python3 forge/evals/backend-check/check.py backend \
  --expect-path /mnt/LLMs/llamacpp/llm-stack-git/models/Qwen3.8-27B-Q6_K.gguf \
  && echo "right weights loaded, proceeding"
```

Checking the `think` (reasoning) profile on `:8008`, or the `task` tier, is a
matter of pointing the flags at them:

```bash
python3 forge/evals/backend-check/check.py backend --chat-url http://llms:8008/v1 --model code
python3 forge/evals/backend-check/check.py --chat-url http://llms:8007/v1 --model task --prefix TASK
```
