#!/usr/bin/env python3
"""Split a judge bundle into per-item files small enough for one grader.

`meeting-brief.md` is 585 KB with three models and grows with every model added,
because each item carries a whole two-hour meeting transcript as its source. A
grader cannot hold that and grade carefully at the same time — and faithfulness
is exactly the axis that needs the source, so dropping it is not an option.

Every chunk keeps the rubric header, so a grader reading one chunk has the same
instructions as one reading the whole file, and the A/B/C labels are untouched:
the split changes who reads what, never what the labels mean.
"""

import sys
from pathlib import Path

JUDGE = Path("forge/evals/results/judge")
# Roughly what one grader can read and still weigh four axes per output.
TARGET_BYTES = 180_000


def split(path, target=TARGET_BYTES):
    text = path.read_text(encoding="utf-8")
    if len(text.encode()) <= target:
        return [path]

    header, _, body = text.partition("\n---\n")
    header = header + "\n---\n"
    # Items start at a level-2 heading; everything under one belongs together.
    #
    # The fence tracking is not decoration. Every output is wrapped in a ```text
    # block, and a cleaned transcript legitimately contains its own `## ` section
    # headings — so a naive line-prefix split cuts an item in half. It did: one
    # grader received a part that opened mid-output with no `###` label heading
    # above it and without the `<details>Source</details>` block, which had
    # stayed with the previous part. It could not grade what it could not
    # attribute, and graded the rest against a proxy for a source it never saw.
    blocks, current, in_fence = [], [], False
    for line in body.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if line.startswith("## ") and current and not in_fence:
            blocks.append("".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("".join(current))

    chunks, batch, size = [], [], 0
    for block in blocks:
        if batch and size + len(block.encode()) > target:
            chunks.append(batch)
            batch, size = [], 0
        batch.append(block)
        size += len(block.encode())
    if batch:
        chunks.append(batch)

    written = []
    for index, batch in enumerate(chunks, start=1):
        out = path.with_name(f"{path.stem}.part{index}of{len(chunks)}.md")
        out.write_text(header + "".join(batch), encoding="utf-8")
        written.append(out)
    return written


def main():
    targets = [Path(p) for p in sys.argv[1:]] or sorted(JUDGE.glob("*.md"))
    for path in targets:
        if ".part" in path.name:
            continue
        parts = split(path)
        if parts == [path]:
            print(f"{path.name}: one grader ({len(path.read_bytes()) // 1024} KB)")
        else:
            items = sum(p.read_text(encoding='utf-8').count("\n## ") for p in parts)
            print(f"{path.name}: split into {len(parts)} ({items} items)")
            for p in parts:
                print(f"    {p.name}  {len(p.read_bytes()) // 1024} KB")


if __name__ == "__main__":
    main()
