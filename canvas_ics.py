from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from icalendar import Calendar

# Canvas writes calendar titles as "Assignment Title [COURSE CODE]" - there's no
# separate machine-readable course field in the ICS feed, so we recover it from
# that trailing bracket suffix.
_COURSE_SUFFIX_RE = re.compile(r"\s*\[(?P<course>[^\[\]]+)\]\s*$")

# The feed's URL property points at a calendar month view
# (".../calendar?include_contexts=course_195198&month=07&year=2026#assignment_2668571")
# rather than the assignment page itself - this recovers the ids to build a
# direct link instead. Falls back to the calendar URL unchanged if the
# expected pattern isn't found, rather than guessing.
_CALENDAR_URL_RE = re.compile(
    r"include_contexts=course_(?P<course_id>\d+).*#assignment_(?P<assignment_id>\d+)"
)


def _to_direct_url(calendar_url: str) -> str:
    match = _CALENDAR_URL_RE.search(calendar_url)
    if not match:
        return calendar_url
    parsed = urlparse(calendar_url)
    return (
        f"{parsed.scheme}://{parsed.netloc}"
        f"/courses/{match.group('course_id')}/assignments/{match.group('assignment_id')}"
    )


@dataclass
class CanvasAssignment:
    uid: str
    title: str
    course: Optional[str]
    due_date: Optional[datetime]
    url: Optional[str]


def _split_title_and_course(summary: str) -> tuple[str, Optional[str]]:
    match = _COURSE_SUFFIX_RE.search(summary)
    if not match:
        return summary.strip(), None
    course = match.group("course").strip()
    title = summary[: match.start()].strip()
    return title, course


def _as_datetime(prop) -> Optional[datetime]:
    """Always returns a timezone-aware datetime (UTC), so callers never have
    to guess whether a given due_date can be compared to another datetime.
    Canvas's feed uses UTC timestamps for timed events; date-only values
    (rare, but allowed by the format) are anchored to UTC midnight.
    """
    if prop is None:
        return None
    value = prop.dt
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def fetch_assignments(ics_url: str) -> list[CanvasAssignment]:
    """Fetch the Canvas ICS feed and return only assignment-type events.

    The feed also contains plain calendar events; Canvas's own UID scheme
    (e.g. "event-assignment-123...") is used to filter those out.
    """
    response = requests.get(ics_url, timeout=30)
    response.raise_for_status()
    calendar = Calendar.from_ical(response.text)

    assignments = []
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("uid", ""))
        if "assignment" not in uid.lower():
            continue

        summary = str(component.get("summary", "Untitled"))
        title, course = _split_title_and_course(summary)
        url_prop = component.get("url")

        assignments.append(
            CanvasAssignment(
                uid=uid,
                title=title,
                course=course,
                due_date=_as_datetime(component.get("dtstart")),
                url=_to_direct_url(str(url_prop)) if url_prop else None,
            )
        )

    return assignments
