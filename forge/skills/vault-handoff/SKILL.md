---
name: vault-handoff
description: Send a completed Markdown or text artifact to pi-vault as a validated pending proposal for explicit review.
---

# Vault Handoff

Use this skill when the user asks to send a completed result to their vault, add
it to Obsidian, or equivalent.

1. Select a completed `.md` or `.txt` artifact from the current pi-forge result.
2. Report every outstanding pi-forge warning before submission. Do not submit a
   failed, canceled, partial, or unsupported artifact.
3. Call `pi_vault_submit_artifact` with the absolute artifact path, suggested
   note name when useful, pi-forge task ID, and source operation.
4. Treat the structured status as authoritative. For `pending_review`, report
   the proposal path and proposed inbox destination.
5. State that the note has not been integrated yet and must be reviewed,
   approved, and applied from pi-vault.

Never invoke or suggest an automatic approval or `apply-approved` operation.
Never claim submission completed the vault import while the result remains
`pending_review`.
