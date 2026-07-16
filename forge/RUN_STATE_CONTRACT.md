# pi-forge restart-safe run contract

Batch workflows must be safe to restart with the same input and output paths.
Durable state belongs in scripts and tools; skills only explain how to resume.

## Required files

- `run_state.json` is the current machine-readable projection.
- `run_events.jsonl` is an append-only, fsynced transition journal.
- Domain manifests remain authoritative for domain data and are written
  atomically after each completed unit.

`run_state.json` records schema version, workflow, normalized configuration and
fingerprint, input snapshot, current phase, run and item statuses, attempts,
next action, warnings, and child workflows. A workflow may rebuild stale state
from the event journal and domain artifacts.

## Restart behavior

- Create a run when the output path does not exist.
- Resume when the output contains a compatible incomplete run.
- Return the existing completion summary when a compatible run is complete.
- Refuse a populated output without `run_state.json` as legacy or unrelated.
- Refuse changed options. Report input drift and require `refresh` before
  reconciling added, changed, or removed inputs.
- Retry interrupted and transient failures at most three times by default.
  Permanent failures require an explicit `retry` command.

## Work units

Use the smallest independently commit-able unit: file, URL, row, audio chunk,
document chunk, packet, page, or deliverable. A `next` command returns one
bounded unit and a `record` command atomically commits it. If the caller stops
before recording, `next` must return the same unit again.

Write in-progress output outside its final path. Commit outputs and their hashes
before marking the unit complete. A stale `in_progress` unit becomes an
interrupted retry candidate when the run reopens.

## Filesystem publication

Precompute publish or move operations with source and destination hashes.
Record each completed operation independently. On restart, accept an operation
as complete only when the observed filesystem state matches its expected hash;
otherwise block for review. Never overwrite a mismatched destination.
