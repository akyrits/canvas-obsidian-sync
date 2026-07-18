from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from canvas_ics import CanvasAssignment

DEFAULT_COLUMNS = ["Backlog", "This Week", "In Progress", "Done"]

_BOARD_HEADER = "---\nkanban-plugin: board\n---\n\n"
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")


def ensure_board_exists(board_path: Path) -> None:
    if board_path.exists():
        return
    board_path.parent.mkdir(parents=True, exist_ok=True)
    body = _BOARD_HEADER + "\n\n".join(f"## {name}" for name in DEFAULT_COLUMNS) + "\n"
    board_path.write_text(body, encoding="utf-8")


def _existing_links(lines: list[str]) -> set[str]:
    links: set[str] = set()
    for line in lines:
        links.update(match.strip() for match in _LINK_RE.findall(line))
    return links


def _section_bounds(lines: list[str], column: str) -> Optional[tuple[int, int]]:
    header_re = re.compile(rf"^##\s+{re.escape(column)}\s*$")
    start = None
    for i, line in enumerate(lines):
        if header_re.match(line.strip()):
            start = i
            break
    if start is None:
        return None

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("## "):
            end = i
            break
    return start, end


def sync_cards(
    board_path: Path,
    assignments: list[CanvasAssignment],
    note_names: dict[str, str],
    due_soon_days: int,
) -> int:
    """Append a card for any assignment not already linked anywhere on the board.

    note_names maps canvas_uid -> the note's filename stem (the [[link]] target).
    Existing cards - including ones the user dragged to another column or typed
    in by hand - are never touched. Returns the number of cards added.
    """
    ensure_board_exists(board_path)
    lines = board_path.read_text(encoding="utf-8").splitlines(keepends=True)
    existing = _existing_links(lines)

    now = datetime.now(timezone.utc)
    added = 0
    for assignment in assignments:
        note_name = note_names.get(assignment.uid)
        if note_name is None or note_name in existing:
            continue

        due_str = assignment.due_date.strftime("%Y-%m-%d") if assignment.due_date else "no due date"
        card_line = f"- [ ] [[{note_name}]] (Due: {due_str})\n"

        is_due_soon = (
            assignment.due_date is not None
            and now <= assignment.due_date <= now + timedelta(days=due_soon_days)
        )
        column = "This Week" if is_due_soon else "Backlog"

        bounds = _section_bounds(lines, column)
        if bounds is None:
            # Column was renamed/removed by the user - recreate it rather than fail.
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"\n## {column}\n")
            lines.append(card_line)
        else:
            _, end = bounds
            lines.insert(end, card_line)

        existing.add(note_name)
        added += 1

    board_path.write_text("".join(lines), encoding="utf-8")
    return added
