from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import anthropic
import frontmatter

import config

from . import transcripts, vault_query, vault_write
from .tools import get_tasks

MODEL = "claude-sonnet-5"


def _require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Missing ANTHROPIC_API_KEY. Get one from console.anthropic.com and add it to .env"
        )


def _split_response(text: str) -> tuple[str, str]:
    """Splits the model's response on the `## Outline` / `## Concepts`
    markers the prep prompt asks for. These are distinct from the note's own
    `## How to Approach This` / `## Key Concepts` headers - kept separate so
    parsing the response never gets confused with the note structure.
    """
    sections = {"outline": "", "concepts": ""}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## Outline":
            current = "outline"
            continue
        if stripped == "## Concepts":
            current = "concepts"
            continue
        if current:
            sections[current] += line + "\n"
    return sections["outline"].strip(), sections["concepts"].strip()


def cmd_setup_course(args: argparse.Namespace) -> int:
    course_path = config.ASSIGNMENTS_ROOT / args.course
    if not course_path.exists():
        print(f"No course folder found at {course_path}")
        return 1

    print(f"Setting up course info for: {args.course}")
    textbook = input("Textbook(s) / main resources for this course: ").strip()
    topics = input("General topics/focus of this course: ").strip()
    other = input("Anything else relevant (optional): ").strip()

    body_parts = [f"# {args.course}\n"]
    if textbook:
        body_parts.append(f"## Textbook / Resources\n{textbook}\n")
    if topics:
        body_parts.append(f"## Topics\n{topics}\n")
    if other:
        body_parts.append(f"## Other Notes\n{other}\n")

    info_path = course_path / "_Course Info.md"
    post = frontmatter.Post(
        "\n".join(body_parts),
        course=args.course,
        type="course-info",
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    info_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    print(f"Saved: {info_path}")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    _require_api_key()

    note_path = vault_query.find_note(config.ASSIGNMENTS_ROOT, args.assignment)
    if note_path is None:
        print(f"Could not find a note matching: {args.assignment}")
        return 1

    post = frontmatter.load(note_path)
    course = post.get("course")

    course_info = ""
    if course:
        info_path = config.ASSIGNMENTS_ROOT / course / "_Course Info.md"
        if info_path.exists():
            course_info = frontmatter.load(info_path).content

    prompt = f"""I need help preparing for this assignment.

Assignment: {note_path.stem}
Course: {course or "unknown"}
Due: {post.get("due") or "unknown"}

Current note content:
---
{post.content}
---

Course context (textbook/topics), if known:
---
{course_info or "(no course info recorded yet - run setup-course for this class)"}
---

Give me two things:
1. A concrete step-by-step outline for how to accomplish this assignment.
2. A short "Key Concepts" explanation of the underlying lesson this assignment is
   testing, tied to the course's textbook/topics where relevant. Where a real
   external resource (Khan Academy, a specific YouTube lecture, documentation)
   would genuinely help, search for one and link it - don't invent a URL from memory.

Explain concepts entirely in your own words - do not use quotation marks or
present any text as a direct quotation anywhere in this response. This
matters especially for the course textbook: you have no way to verify its
exact wording (web search cannot surface the actual text of a paid textbook),
so treat it as off-limits for quotation entirely, not just something to hedge
on. If you reference a specific source found via web search, describe what it
says rather than quoting it, and name/link the source instead.

Format your response exactly as:
## Outline
...
## Concepts
...
"""

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    text = "\n".join(block.text for block in response.content if block.type == "text")
    outline, concepts = _split_response(text)

    if not outline and not concepts:
        # Model didn't follow the requested format - keep the output rather
        # than silently losing it.
        outline = text
        concepts = None

    vault_write.set_section(note_path, "How to Approach This", outline)
    if concepts:
        vault_write.set_section(note_path, "Key Concepts", concepts)

    print(f"Updated: {note_path}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    _require_api_key()

    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=2000,
        thinking={"type": "adaptive"},
        tools=[get_tasks],
        messages=[{"role": "user", "content": args.question}],
    )

    final_message = None
    for message in runner:
        final_message = message

    if final_message:
        for block in final_message.content:
            if block.type == "text":
                print(block.text)
    return 0


def cmd_transcript(args: argparse.Namespace) -> int:
    note_path = transcripts.fetch_and_save(
        config.ASSIGNMENTS_ROOT, args.url, args.course, args.title
    )
    print(f"Saved transcript: {note_path}")
    return 0


def cmd_check_files(args: argparse.Namespace) -> int:
    course_path = config.ASSIGNMENTS_ROOT / args.course
    listing_path = course_path / "canvas_file_listing.txt"
    if not listing_path.exists():
        print(f"No listing found at {listing_path}")
        print("Copy the file names from Canvas's Files page into that file (one per line) and re-run.")
        return 1

    canvas_files = {
        line.strip() for line in listing_path.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    local_files = {p.name for p in course_path.rglob("*") if p.is_file()}

    missing = sorted(canvas_files - local_files)
    if not missing:
        print("Nothing missing - every file in the listing is already saved locally.")
    else:
        print("Missing (not found locally):")
        for name in missing:
            print(f"  - {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py", description="Study/concept agent over the synced Obsidian vault."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_setup = subparsers.add_parser("setup-course", help="One-time per-course context setup")
    p_setup.add_argument("course", help="Course folder name under School/")
    p_setup.set_defaults(func=cmd_setup_course)

    p_prep = subparsers.add_parser("prep", help="Generate an outline + key concepts for an assignment")
    p_prep.add_argument("assignment", help="Assignment note name (exact or partial)")
    p_prep.set_defaults(func=cmd_prep)

    p_ask = subparsers.add_parser("ask", help="Ask a question about your synced tasks")
    p_ask.add_argument("question")
    p_ask.set_defaults(func=cmd_ask)

    p_transcript = subparsers.add_parser("transcript", help="Fetch a YouTube lecture transcript")
    p_transcript.add_argument("url", help="YouTube video URL or ID")
    p_transcript.add_argument("--course", required=True, help="Course folder name under School/")
    p_transcript.add_argument("--title", required=True, help="Lecture title")
    p_transcript.set_defaults(func=cmd_transcript)

    p_check = subparsers.add_parser(
        "check-files", help="Diff a Canvas file listing against what's saved locally"
    )
    p_check.add_argument("course", help="Course folder name under School/")
    p_check.set_defaults(func=cmd_check_files)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        print(f"Error: {e}")
        return 1
