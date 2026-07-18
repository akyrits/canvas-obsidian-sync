from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter

from canvas_ics import CanvasAssignment

_INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def _sanitize_filename(name: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS_RE.sub("-", name).strip()
    return cleaned or "Untitled"


def _merge_task_tag(existing) -> list[str]:
    """Return a tags list that always contains "task" (so TaskNotes recognizes
    the note) while preserving any tags the user added themselves.
    """
    tags: list[str] = []
    if isinstance(existing, str):
        tags = [t.strip() for t in existing.split(",") if t.strip()]
    elif isinstance(existing, list):
        tags = [str(t).strip() for t in existing if str(t).strip()]
    if "task" not in tags:
        tags.insert(0, "task")
    return tags


def _find_existing_note(assignments_root: Path, canvas_uid: str) -> Optional[Path]:
    """Look up a note by canvas_uid in its frontmatter, not by filename/path.

    This is what lets the user freely rename or move a note without breaking
    dedup on the next sync.
    """
    if not assignments_root.exists():
        return None
    for path in assignments_root.rglob("*.md"):
        try:
            post = frontmatter.load(path)
        except Exception:
            continue
        if post.get("canvas_uid") == canvas_uid:
            return path
    return None


def _render_body(assignment: CanvasAssignment) -> str:
    canvas_link = (
        f"[Open in Canvas]({assignment.url})"
        if assignment.url
        else "_(no Canvas link found in the calendar feed)_"
    )
    return (
        f"# {assignment.title}\n\n"
        f"{canvas_link}\n\n"
        f"## How to Approach This\n"
        f"<!-- Filled in by `python agent.py prep` -->\n\n"
        f"## Key Concepts\n"
        f"<!-- Filled in by `python agent.py prep` -->\n\n"
        f"## Resources\n"
        f"<!-- Drop professor-provided PDFs in this course's Attachments/ folder and embed them here, e.g. ![[Lecture 5 Slides.pdf]] -->\n\n"
        f"## Notes\n"
    )


def upsert_note(assignments_root: Path, assignment: CanvasAssignment) -> tuple[Path, bool]:
    """Create or update the note for one assignment.

    Frontmatter is fully rewritten every call (it's machine-owned). The body
    is written once, at creation, and never touched again so the user's own
    notes are never at risk from a re-sync.

    Returns (note_path, created).
    """
    existing_path = _find_existing_note(assignments_root, assignment.uid)

    # Machine-owned fields: refreshed on every sync. `due`/`tags`/etc. use the
    # exact frontmatter keys TaskNotes expects by default so its calendar and
    # views work with no per-view configuration.
    always_fields = {
        "course": assignment.course,
        "due": assignment.due_date.isoformat() if assignment.due_date else None,
        "canvas_uid": assignment.uid,
        "canvas_url": assignment.url,
        "source": "canvas",
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # User-owned once created: set on creation, never overwritten again, so a
    # status change or scheduling the user makes in TaskNotes survives re-syncs.
    create_only_fields = {
        "status": "open",
        "priority": "normal",
    }

    if existing_path is not None:
        post = frontmatter.load(existing_path)
        post.metadata.update(always_fields)
        post.metadata["tags"] = _merge_task_tag(post.metadata.get("tags"))
        post.metadata.pop("due_date", None)  # drop the pre-migration key
        # setdefault: backfill status/priority if missing, but never overwrite
        # a value the user has since changed in TaskNotes.
        for key, value in create_only_fields.items():
            post.metadata.setdefault(key, value)
        existing_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return existing_path, False

    course_folder = assignments_root / _sanitize_filename(assignment.course or "Uncategorized")
    course_folder.mkdir(parents=True, exist_ok=True)

    note_path = course_folder / f"{_sanitize_filename(assignment.title)}.md"
    base_stem = note_path.stem
    suffix = 2
    while note_path.exists():
        note_path = course_folder / f"{base_stem} ({suffix}).md"
        suffix += 1

    new_metadata = {
        **always_fields,
        **create_only_fields,
        "tags": _merge_task_tag(None),
    }
    post = frontmatter.Post(_render_body(assignment), **new_metadata)
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return note_path, True
