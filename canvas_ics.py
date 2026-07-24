from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
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
# direct link (and to call the REST API for the exact due time, which the ICS
# feed omits). Falls back to the calendar URL unchanged if the expected pattern
# isn't found, rather than guessing.
_CALENDAR_URL_RE = re.compile(
    r"include_contexts=course_(?P<course_id>\d+).*#assignment_(?P<assignment_id>\d+)"
)


def _parse_calendar_url(calendar_url: str) -> tuple[Optional[str], Optional[str], str]:
    """Return (course_id, assignment_id, direct_url) from a Canvas calendar URL.

    If the expected pattern isn't present we can't recover the ids, so both come
    back None and direct_url is the original calendar URL unchanged.
    """
    match = _CALENDAR_URL_RE.search(calendar_url)
    if not match:
        return None, None, calendar_url
    course_id = match.group("course_id")
    assignment_id = match.group("assignment_id")
    parsed = urlparse(calendar_url)
    direct_url = (
        f"{parsed.scheme}://{parsed.netloc}"
        f"/courses/{course_id}/assignments/{assignment_id}"
    )
    return course_id, assignment_id, direct_url


@dataclass
class CanvasAssignment:
    uid: str
    title: str
    course: Optional[str]
    due_date: Optional[datetime]
    url: Optional[str]
    # Recovered from the calendar URL so canvas_api can fetch the exact due time
    # the ICS feed leaves out. Either may be None if the URL didn't match.
    course_id: Optional[str] = None
    assignment_id: Optional[str] = None


def _split_title_and_course(summary: str) -> tuple[str, Optional[str]]:
    match = _COURSE_SUFFIX_RE.search(summary)
    if not match:
        return summary.strip(), None
    course = match.group("course").strip()
    title = summary[: match.start()].strip()
    return title, course


def _as_local_datetime(prop, local_tz: tzinfo) -> Optional[datetime]:
    """Return a timezone-aware datetime in `local_tz`, so callers can compare due
    dates against each other and format the correct local date/time.

    Canvas's "upcoming assignments" ICS feed represents every assignment as an
    all-day event: DTSTART is a bare DATE with no time (e.g. VALUE=DATE:20260623),
    so the real due time (almost always 11:59 PM) is simply not in the feed. We
    anchor those date-only values to 23:59 local time, which matches how Canvas
    assignments are actually due and keeps the calendar date correct. When a
    token is configured, canvas_api overwrites this with the exact due_at.

    Timed values (rare in this feed) are converted from their own zone - or from
    UTC if the feed left them naive - into local_tz.
    """
    if prop is None:
        return None
    value = prop.dt
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(local_tz)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, tzinfo=local_tz)
    return None


def fetch_assignments(ics_url: str, local_tz: tzinfo) -> list[CanvasAssignment]:
    """Fetch the Canvas ICS feed and return only assignment-type events.

    The feed also contains plain calendar events; Canvas's own UID scheme
    (e.g. "event-assignment-123...") is used to filter those out. Due times are
    a best-effort 23:59 local (see _as_local_datetime); canvas_api can refine
    them to the exact due_at afterward.
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
        course_id = assignment_id = None
        direct_url = None
        if url_prop:
            course_id, assignment_id, direct_url = _parse_calendar_url(str(url_prop))

        assignments.append(
            CanvasAssignment(
                uid=uid,
                title=title,
                course=course,
                due_date=_as_local_datetime(component.get("dtstart"), local_tz),
                url=direct_url,
                course_id=course_id,
                assignment_id=assignment_id,
            )
        )

    return assignments
