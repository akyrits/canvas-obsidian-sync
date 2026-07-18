from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import frontmatter


@dataclass
class Task:
    note_name: str
    note_path: Path
    course: Optional[str]
    due_date: Optional[datetime]
    canvas_url: Optional[str]
    status: str


def find_note(assignments_root: Path, name: str) -> Optional[Path]:
    """Find a note by filename stem. Exact match first, then case-insensitive
    substring match so callers can pass a partial title.
    """
    if not assignments_root.exists():
        return None

    candidates = list(assignments_root.rglob("*.md"))
    for path in candidates:
        if path.stem == name:
            return path

    lowered = name.lower()
    partial_matches = [p for p in candidates if lowered in p.stem.lower()]
    if len(partial_matches) == 1:
        return partial_matches[0]
    return None


def load_tasks(assignments_root: Path) -> list[Task]:
    """Read every synced assignment note into a flat list of Task objects.
    Read-only - never writes.

    `status` comes from the note's own frontmatter (TaskNotes' field - see
    vault_notes.py's create_only_fields), not the Kanban board's column
    placement. The board is still synced as a visual overview, but frontmatter
    is the single source of truth for status so a change made in TaskNotes is
    always what the agent sees.
    """
    if not assignments_root.exists():
        return []

    tasks = []
    for path in assignments_root.rglob("*.md"):
        try:
            post = frontmatter.load(path)
        except Exception:
            continue

        due_date_str = post.get("due")
        due_date = datetime.fromisoformat(due_date_str) if due_date_str else None

        tasks.append(
            Task(
                note_name=path.stem,
                note_path=path,
                course=post.get("course"),
                due_date=due_date,
                canvas_url=post.get("canvas_url"),
                status=post.get("status", "open"),
            )
        )

    return tasks
