from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Iterable

import requests

from canvas_ics import CanvasAssignment


def _parse_due_at(due_at: str, local_tz: tzinfo) -> datetime:
    """Parse Canvas's `due_at` (ISO 8601 UTC, e.g. "2026-06-24T03:59:00Z") into a
    timezone-aware datetime in local_tz. `.replace("Z", ...)` keeps this working
    on Python < 3.11, whose fromisoformat rejects a trailing "Z".
    """
    return datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(local_tz)


def enrich_due_times(
    assignments: Iterable[CanvasAssignment],
    base_url: str,
    token: str,
    local_tz: tzinfo,
) -> int:
    """Overwrite each assignment's due_date with the exact due_at from the Canvas
    REST API (the ICS feed carries only the date). Returns the number refined.

    Best-effort per assignment: anything we can't resolve - missing ids, an HTTP
    error, or a null due_at (e.g. an assignment with only section-specific
    overrides) - is left with its 23:59-local fallback rather than failing the
    whole sync. A student token's `due_at` already reflects that student's
    applicable override.
    """
    base_url = base_url.rstrip("/")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    refined = 0
    for assignment in assignments:
        if not assignment.course_id or not assignment.assignment_id:
            continue
        url = (
            f"{base_url}/api/v1/courses/{assignment.course_id}"
            f"/assignments/{assignment.assignment_id}"
        )
        try:
            response = session.get(url, timeout=30)
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        try:
            due_at = response.json().get("due_at")
        except ValueError:
            continue
        if not due_at:
            continue
        assignment.due_date = _parse_due_at(due_at, local_tz)
        refined += 1

    return refined
