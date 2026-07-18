from __future__ import annotations

from pathlib import Path

import frontmatter


def set_section(note_path: Path, header: str, content: str) -> None:
    """Replace everything under `## {header}` (up to the next `## ` header or
    end of file) with `content`. Appends a new section if the header doesn't
    exist yet.

    Every other section - especially `## Notes` - is left exactly as-is. This
    is the same machine-owned/user-owned split the sync pipeline already uses
    for frontmatter vs. body, just scoped to one header instead of the whole
    body.
    """
    post = frontmatter.load(note_path)
    lines = post.content.splitlines(keepends=True)

    target = f"## {header}"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == target:
            start = i
            break

    new_section = [f"{target}\n", "\n", content.rstrip() + "\n", "\n"]

    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n"] + new_section
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].strip().startswith("## "):
                end = i
                break
        lines[start:end] = new_section

    post.content = "".join(lines)
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
