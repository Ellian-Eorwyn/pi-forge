# Vault Workflow

`forge/extensions/vault-workflow.ts` turns pi-forge into a **plan → execute →
verify** loop for Obsidian-vault changes, driven entirely by the single local
model. It reproduces the structured way a capable cloud agent works — plan in
detail, execute carefully, verify the result — within the constraint that one
27B model is loaded at a time.

## Why it exists

The pi-forge agent brain defaults to the local thinking profile
(`forge-local-think`, `http://llms:8003/v1`, model `think`). That profile spends hidden
reasoning tokens — which is valuable for planning and verification but wasteful
for mechanical execution. Measured on this deployment it spends ~410 hidden
tokens before answering, even to reply with one word.

The same weights are also served without thinking as `forge-local-chat`
(`http://llms:8004/v1`, model `chat`). Both run at once, so each phase simply
uses the model that suits it: the extension calls `pi.setModel` on entering and
leaving execute. Earlier versions suppressed thinking with a `<think></think>`
assistant prefill against the single thinking server; that worked unreliably and
is gone.

## Phases

| Command | Phase | Thinking | Tools | Role |
| --- | --- | --- | --- | --- |
| `/plan` | plan | on | read-only (`read`, `bash`, `grep`, `find`, `ls`, `questionnaire`) | Interview, investigate, write a numbered plan, ask for approval. |
| `/execute` | execute | off (`forge-local-chat`) | read-only **plus** `edit`, `write` | Apply the approved plan one change at a time, dry-run first, approve each change. |
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
- **Thinking toggle** — entering execute looks up `forge-local-chat/chat` in the
  model registry and switches to it with `pi.setModel`; leaving execute restores
  the model that was active before. The model actually in use is restored only
  if it is still the non-thinking one, so a model the user picked by hand during
  execute is never overwritten. Both the phase and the model to return to are
  persisted, so a crash mid-execute cannot strand the session on the
  non-thinking model. An install whose `models.json` predates the provider keeps
  working: the switch is skipped with a warning.
- **Per-phase prompt** — a `before_agent_start` handler injects the phase's role
  and rules (the approve-each-change rule, the schema-edit → `doctor`
  discipline) fresh each turn.

The `forge-local-think` and `forge-local-code` models set
`compat.thinkingFormat: "openai"` (in `configure-pi-forge.mjs`) so pi sends
graded `reasoning_effort` and reads the server's `reasoning_content` separately
from displayed content.

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
- Both servers share one GPU. `forge/extensions/inference-scheduling.ts` writes
  an interactive lease for whichever local provider the session is using, so
  background batch work yields to the user in either phase.

## Tests

`scripts/vault-workflow.test.ts` (run with `npm run test:vault-workflow` /
`tsx --test`) drives the extension with a fake `ExtensionAPI` and asserts:
phase→tool-set gating, the execute-phase model switch and its restore (including
that a hand-picked model survives and that a missing provider degrades quietly),
per-phase system prompts, mutating-bash blocking in read-only phases, and phase
persistence/restore.
