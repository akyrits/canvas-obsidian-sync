from __future__ import annotations

import sys

import config
from canvas_ics import fetch_assignments
from kanban_board import sync_cards
from vault_notes import upsert_note


def main() -> int:
    print("Fetching Canvas ICS feed...")
    assignments = fetch_assignments(config.CANVAS_ICS_URL)
    print(f"Found {len(assignments)} assignment(s) in the feed.")

    note_names: dict[str, str] = {}
    created_count = 0
    for assignment in assignments:
        path, created = upsert_note(config.ASSIGNMENTS_ROOT, assignment)
        note_names[assignment.uid] = path.stem
        if created:
            created_count += 1
            print(f"  + created note: {path}")

    print(f"Created {created_count} new note(s); refreshed frontmatter on the rest.")

    added = sync_cards(config.BOARD_PATH, assignments, note_names, config.DUE_SOON_DAYS)
    print(f"Added {added} new card(s) to the Kanban board at {config.BOARD_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
