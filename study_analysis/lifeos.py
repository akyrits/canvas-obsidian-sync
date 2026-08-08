from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter

from .diagnostics import Familiarity, familiarity_is_stale
from .schema import ALLOWED_EFFORT


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _safe_root(raw_root: Path, *parts: str) -> Path | None:
    root = Path(os.path.abspath(Path(raw_root)))
    current = root
    if _is_link_or_junction(current):
        return None
    for part in parts:
        current = current / part
        if current.exists() and _is_link_or_junction(current):
            return None
    resolved_root = root.resolve()
    resolved = current.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


def _safe_file(root: Path, path: Path) -> Path | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_or_junction(current):
            return None
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved if resolved.is_file() else None


def _text(value: Any, *, limit: int, default: str | None = None) -> str | None:
    if not isinstance(value, str):
        return default
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        return default
    return normalized


def _date_text(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return _text(value, limit=64)


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if 0 <= parsed <= 1 else None


def _difficulty(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        return None
    return value


def _concept_names(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 24:
        return []
    names = [_text(item, limit=160) for item in value]
    return [name for name in names if name is not None]


def _course_archive_states(root: Path) -> dict[str, bool]:
    states: dict[str, bool] = {}
    try:
        courses = tuple(root.iterdir())
    except OSError:
        return states
    for course_path in courses:
        if not course_path.is_dir() or _is_link_or_junction(course_path):
            continue
        info_path = _safe_file(root, course_path / "_Course Info.md")
        if info_path is None:
            continue
        try:
            post = frontmatter.load(info_path)
        except Exception:
            continue
        archive = post.get("course_archive")
        states[course_path.name] = bool(
            isinstance(archive, dict) and archive.get("status") == "archived"
        )
    return states


def export_assignment_signals(assignments_root: Path) -> list[dict]:
    """Return a strict compact assignment contract without traversing linked data."""
    root = _safe_root(assignments_root)
    if root is None or not root.is_dir():
        return []
    archive_states = _course_archive_states(root)
    records = []
    for candidate in root.rglob("*.md"):
        path = _safe_file(root, candidate)
        if path is None:
            continue
        try:
            post = frontmatter.load(path)
        except Exception:
            continue
        raw_uid = post.get("canvas_uid")
        if isinstance(raw_uid, bool) or not isinstance(raw_uid, (str, int)):
            continue
        uid = _text(str(raw_uid), limit=160)
        if uid is None:
            continue
        raw_analysis = post.get("analysis")
        analysis = raw_analysis if isinstance(raw_analysis, dict) else {}
        effort = _text(analysis.get("effort"), limit=24, default="unknown")
        if effort not in ALLOWED_EFFORT:
            effort = "unknown"
        course = _text(post.get("course"), limit=200)
        records.append(
            {
                "id": uid,
                "title": _text(path.stem, limit=240, default="Untitled"),
                "course": course,
                "course_archived": archive_states.get(course or "", False),
                "due": _date_text(post.get("due")),
                "status": _text(post.get("status"), limit=40, default="open"),
                "concepts": _concept_names(analysis.get("concepts")),
                "concept_difficulty": _difficulty(
                    analysis.get("concept_difficulty_max")
                ),
                "assignment_difficulty": _difficulty(
                    analysis.get("assignment_difficulty")
                ),
                "effort": effort,
                "confidence": {
                    "assignment_difficulty": _confidence(
                        analysis.get("assignment_difficulty_confidence")
                    ),
                    "effort": _confidence(analysis.get("effort_confidence")),
                },
                "analyzed_at": _date_text(analysis.get("analyzed_at")),
            }
        )
    records.sort(
        key=lambda item: (item["due"] is None, item["due"] or "", item["title"])
    )
    return records


def export_concept_signals(vault_root: Path) -> list[dict]:
    """Return strict familiarity projections without evidence or linked data."""
    root = _safe_root(vault_root)
    concepts_root = _safe_root(vault_root, "Knowledge", "Concepts")
    if root is None or concepts_root is None or not concepts_root.is_dir():
        return []
    records = []
    for candidate in concepts_root.glob("*.md"):
        path = _safe_file(concepts_root, candidate)
        if path is None:
            continue
        try:
            post = frontmatter.load(path)
            raw_canonical = post.get("canonical_name")
            canonical = _text(raw_canonical, limit=160) or _text(
                path.stem, limit=160, default="Untitled"
            )
            familiarity = Familiarity(
                str(post.get("familiarity") or Familiarity.UNKNOWN.value)
                .strip()
                .casefold()
            )
            stale = familiarity_is_stale(post, canonical)
        except Exception:
            continue
        references = post.get("diagnostic_records")
        diagnostic_count = (
            len(references)
            if isinstance(references, list)
            and len(references) <= 1000
            and all(isinstance(reference, str) for reference in references)
            else 0
        )
        records.append(
            {
                "concept": canonical,
                "familiarity": familiarity.value,
                "confidence": _confidence(post.get("familiarity_confidence")),
                "familiarity_as_of": _date_text(
                    post.get("familiarity_assessed_at")
                ),
                "reassessment_due": stale,
                "diagnostic_count": diagnostic_count,
            }
        )
    records.sort(key=lambda item: item["concept"].casefold())
    return records
