# Obsidian CLI

Obsidian 1.12.7 ships a command line interface that talks to the running app.
pi-forge uses it when it is there and works exactly as before when it is not.

This is deliberate and it is the whole design constraint. The vault skills are
filesystem-only: they operate on any folder of Markdown files, with or without
Obsidian, on a machine where Obsidian has never been installed. Nothing here
changes that. The CLI is an **accelerator and a verifier**, never a dependency,
and no skill fails, degrades its output, or warns about its absence.

## What it buys

Obsidian keeps an index of the whole vault in memory, so every query answers in
about 30 milliseconds regardless of vault size — on a 2,501-note vault, the same
30 milliseconds. That makes two kinds of thing possible.

**Answers pi-forge cannot compute cheaply.** A reverse link index. The set of
properties actually in use across every note, with the type Obsidian registered
for each. Which notes a Bases view returns once its filters run. Each of these
would otherwise mean walking and parsing the entire vault.

**One capability pi-forge cannot implement well at all.** Renaming a note while
rewriting every inbound link to it — in prose, in Markdown links, and inside
frontmatter. Obsidian is the reference implementation of its own link
resolution; reimplementing it and being right about it is not a reasonable thing
to attempt.

The framing that follows from this: Obsidian is an **oracle**. Where its answer
disagrees with ours, that is usually a bug in ours, worth fixing in the
pure-Python path so that vaults without Obsidian get the fix too. `doctor`
compares the two frontmatter parsers on a sample of notes for exactly this
reason.

## Setup

Three settings, each of which pi-forge reports on rather than changes.

1. **Obsidian 1.12.7 or newer**, running. pi-forge never launches it.
2. **Settings → General → Command line interface**, on. This is what installs
   the `obsidian` binary (`/usr/local/bin/obsidian` on macOS) and what pi-forge
   reads from Obsidian's own `obsidian.json` to know the CLI is enabled.
3. **Settings → Files and links → Automatically update internal links**, on.
   This one is not optional for moves and it is not the shipped default. With it
   off, the first rename opens a modal asking what to do and the CLI call blocks
   until somebody answers it — measured here at over two minutes. pi-forge
   refuses to use the CLI for moves unless this is on, and says so in `doctor`
   and in the vault context block.

pi-forge never writes `.obsidian/app.json`. Flipping someone's app settings to
work around a dialog is not its call to make.

Check the result:

```bash
python3 forge/skills/vault-organizer/scripts/vault-organizer.py doctor --vault /path/to/vault
```

The `checks.obsidianCli` block reports `available`, `canWrite`, and a `reason`
when either is false. It never affects the overall `ok`: a vault with no
Obsidian running is a normal vault, not a broken one.

## What changes when it is available

| Command | With the CLI | Without |
| --- | --- | --- |
| `vault-organizer drift` | Adds property-vocabulary findings: unapproved keys, undeclared Obsidian built-ins, shape disagreements, approved-but-unused. All `medium` or below, so they never block an apply. | Folder routes only; the report says the vocabulary was not checked. |
| `vault-organizer` apply | Moves go through the CLI, so inbound links follow the note. | `os.rename`; wikilinks resolve by basename, a changed filename does not. |
| `vault-organizer doctor` | Reports CLI state; compares our frontmatter parser against Obsidian's on ~25 notes. | Nothing; `ok` unchanged. |
| `vault-organizer` base references | Each `.base` view is evaluated: "returns N notes, M of which this run moves". | Substring match against the base file's text. |
| `vault-transcripts` apply | The rename from timestamp to title takes inbound links with it. | The old name is left behind in whatever pointed at it. |
| `vault-connections propose` | Lists the notes nothing links to. | No such section. |
| `vault-connections wiki` | Cross-checks our unresolved-link set against Obsidian's and reports disagreements both ways. | No cross-check. |

The alias guard in `wiki` — which stops a stub being proposed for a target an
existing note already declares as an alias — is unconditional. It is a fix to
pi-forge's own resolution, not a capability borrowed from the app.

## Controls

- `--link-rewrite auto` (default) uses the CLI for moves when it can and falls
  back to a plain rename when it cannot, silently, because that fallback is what
  the workflow has always done.
- `--link-rewrite off` never calls the CLI for a move, even when it is available.
- `--link-rewrite require` fails the run when link-safe moves are unavailable,
  for anyone who wants the guarantee rather than the improvement.
- `FORGE_OBSIDIAN_CLI=off` disables the adapter entirely — reads included — so
  every skill behaves as though Obsidian were not installed.
- `FORGE_OBSIDIAN_CLI=/path/to/obsidian` overrides the binary.
- `FORGE_OBSIDIAN_CONFIG_DIR` overrides where `obsidian.json` is read from. Tests
  and CI set this; you should not need to.

## The sharp edges

Every one of these was measured, and the adapter is designed against them. The
contract lives in the `forge/lib/obsidian_cli.py` module docstring.

**It always exits 0.** `obsidian read path=Nope.md` prints
`Error: File "Nope.md" not found.` and exits 0. So does an unknown command. The
adapter ignores the return code entirely and reads the `Error: ` prefix instead.
This is also why the read-only phases of `/plan` and `/verify` gate `obsidian`
by an allowlist of query subcommands: a blocked-by-default policy is the only
safe one against a tool that cannot report failure.

**`vault=` takes a registered name, not a path.** An unregistered folder answers
`Vault not found.` The name is the registered path's basename, which pi-forge
reads out of `obsidian.json` rather than asking for — because naming a vault
that is not open **opens its window and switches the active vault**, and the
first command against a booting vault fails with a misleading
`Command "vault" not found`. Nothing in pi-forge spawns `obsidian` to discover
anything.

**Obsidian rewrites Markdown links into a form only Obsidian resolves.** Moving
`Notes/Target.md` rewrites `[T](Notes/Target.md)` in a note elsewhere to
`[T](Target.md)` — the shortest path unique in the vault, which Obsidian resolves
like a wikilink and a plain Markdown renderer does not. This is the one place
where Obsidian's convenience and a general-purpose vault genuinely disagree, so
pi-forge measures it and warns per note rather than accepting it quietly. Whether
that trade is worth it is a decision for whoever owns the vault.

**Its blast radius is the whole vault, so writes are rationed.** The adapter will
run exactly two mutating commands, `move` and `rename`, and that set is not
intended to grow. `eval`, `delete`, `create`, `append`, `property:set` and the
rest are refused unconditionally — pi-forge's own atomic, hash-verified,
journalled writes are better, and `property:set` appends new keys to the end of
frontmatter, which breaks the schema's property order. Around each move:

- every note linking to the source is backed up first;
- the moved file's bytes are checked against the hash the plan was built on;
- every note the CLI rewrote must differ only on lines carrying links, or the
  whole operation is restored from backup and fails;
- one failure disables the CLI for the rest of the run.

**A quarantined duplicate is always a plain rename.** It moves into a
dot-directory Obsidian does not index, so rewriting inbound links to chase it
there would point them at something the app cannot resolve.

**External writes are visible immediately.** A file written to disk by pi-forge
shows up in the CLI's `files` and `backlinks` on the very next call. That is what
makes the split work: pi-forge writes, Obsidian verifies.
