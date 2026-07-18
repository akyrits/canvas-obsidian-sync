from __future__ import annotations

from datetime import datetime, timedelta, timezone

from anthropic import beta_tool

import config

from . import vault_query


@beta_tool
def get_tasks(status: str = "", course: str = "", due_within_days: int = 0) -> str:
    """Look up synced Canvas assignments from the Obsidian vault.

    Args:
        status: Filter to this exact TaskNotes status value (e.g. "open",
            "in-progress", "done" - whatever the user's TaskNotes setup uses;
            new assignments default to "open"). Leave empty for every status.
        course: Filter to tasks whose course contains this text (case-insensitive
            substring match, e.g. "COP3410"). Leave empty for every course.
        due_within_days: If greater than 0, only include tasks due on or before
            that many days from now (this also includes anything already
            overdue). Leave 0 to skip due-date filtering.
    """
    tasks = vault_query.load_tasks(config.ASSIGNMENTS_ROOT)

    if status:
        tasks = [t for t in tasks if t.status == status]
    if course:
        needle = course.lower()
        tasks = [t for t in tasks if t.course and needle in t.course.lower()]
    if due_within_days > 0:
        cutoff = datetime.now(timezone.utc) + timedelta(days=due_within_days)
        tasks = [t for t in tasks if t.due_date and t.due_date <= cutoff]

    if not tasks:
        return "No matching tasks found."

    tasks.sort(key=lambda t: (t.due_date is None, t.due_date))
    lines = []
    for t in tasks:
        due_str = t.due_date.strftime("%Y-%m-%d") if t.due_date else "no due date"
        lines.append(f"- [{t.status}] {t.note_name} ({t.course or 'no course'}) - due {due_str}")
    return "\n".join(lines)
