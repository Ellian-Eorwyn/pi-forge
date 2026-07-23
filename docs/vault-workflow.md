# Vault Workflow

`forge/extensions/vault-workflow.ts` turns pi-forge into a **plan → execute →
verify** loop for Obsidian-vault changes, driven entirely by the single local
model. It reproduces the structured way a capable cloud agent works — plan in
detail, execute carefully, verify the result — within the constraint that one
27B model is loaded at a time.

## Why it exists

The pi-forge agent brain is already the local model (`forge-local`,
`http://llms:8008/v1`, model `code`). That server *thinks* — it spends hidden
reasoning tokens — which is valuable for planning and verification but wasteful
for mechanical execution. Because :8008 and its non-thinking sibling :8004 are
the same weights and only one fits in VRAM, this extension gets both behaviours
from the one running server: it suppresses thinking on demand with the same
`<think></think>` assistant prefill that `vault-organizer` uses.

## Phases

| Command | Phase | Thinking | Tools | Role |
| --- | --- | --- | --- | --- |
| `/plan` | plan | on | read-only (`read`, `bash`, `grep`, `find`, `ls`, `questionnaire`) | Interview, investigate, write a numbered plan, ask for approval. |
| `/execute` | execute | off (prefill) | read-only **plus** `edit`, `write` | Apply the approved plan one change at a time, dry-run first, approve each change. |
| `/verify` | verify | on | read-only | Check the result against the plan and report. |
| `/workflow off` | off | default | all tools | Leave the workflow; normal pi behaviour. |

Phase is persisted (`pi.appendEntry("vault-workflow", …)`) and restored on
`session_start`, so a restart resumes the same phase and tool set.

## How each lever works

- **Tool gating** — `pi.setActiveTools(...)` on every transition; the desired set
  is intersected with the tools that actually exist. In plan/verify the write
  tools are simply absent, and a `tool_call` handler additionally blocks mutating
  bash (`rm`, `mv`, `--apply`, redirects, `git commit/push`, `sed -i`, …) so the
  model cannot change the vault while "thinking out loud".
- **Thinking toggle** — a `before_provider_request` handler (mirroring
  `forge/extensions/inference-scheduling.ts`) appends
  `{role:"assistant", content:"<think>\n\n</think>\n\n"}` to the outgoing
  `messages` **only** in the execute phase and **only** for the `forge-local`
  provider. The model continues from the closed think block and skips reasoning
  (~4–5x faster mechanical turns). Tool-calling is unaffected — the server still
  returns a normal `tool_calls` array.
- **Per-phase prompt** — a `before_agent_start` handler injects the phase's role
  and rules (the approve-each-change rule, the schema-edit → `doctor` discipline,
  the `--think-prefill` flag for skill sub-calls) fresh each turn.

The `forge-local` model sets `compat.thinkingFormat: "qwen"` (in
`configure-pi-forge.mjs`) so pi parses the model's `<think>…</think>` as
reasoning instead of leaking raw tags into displayed content — this keeps both
the execute-phase prefill and any real thinking clean in the transcript.

## Guardrails

- Read-only tools during plan/verify — the model cannot mutate while planning.
- Approve-each-change: dry-run → show → explicit "yes" → apply. Nothing is
  applied without the user.
- Execution delegates the risky bulk work to the deterministic, tested skills
  (`vault-organizer`: dry-run, per-file backups, never-delete quarantine,
  resumable runs) rather than free-form edits.
- Every schema-note edit is followed by `vault-organizer.py doctor`; the verify
  phase is the backstop.

## Using it

Run `pi-forge` in the vault directory, then:

1. `/plan` — describe the change; answer its questions; review the plan it
   writes; approve.
2. `/execute` — approve each change as it dry-runs and applies. Execute turns are
   fast (no thinking); the plan/verify turns are slower because the model
   reasons.
3. `/verify` — it runs `doctor`/greps and reports pass/fail against the plan.

## Expectations and limits

- A local 27B model is not a frontier cloud model. The design shrinks its job —
  tight per-phase prompts, tool gating, deterministic skills doing the heavy
  lifting, and approval gates — but expect to guide it more than a cloud agent.
- Thinking is slow (~30–60s/turn for hard turns), so plan/verify chat is
  deliberately slower than execute. The phase→behaviour mapping lives in the
  extension and can be tuned.
- **Two-GPU future:** if a non-thinking server (`:8004`) can run alongside
  `:8008`, register it as a second provider and switch execute to it with
  `pi.setModel` instead of the prefill — the phase structure is unchanged.

## Tests

`scripts/vault-workflow.test.ts` (run with `npm run test:vault-workflow` /
`tsx --test`) drives the extension with a fake `ExtensionAPI` and asserts:
phase→tool-set gating, execute-only prefill injection (forge-local only, without
mutating the original payload), per-phase system prompts, mutating-bash blocking
in read-only phases, and phase persistence/restore.
