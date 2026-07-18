from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import frontmatter
from youtube_transcript_api import YouTubeTranscriptApi

from vault_notes import _sanitize_filename


def extract_video_id(url_or_id: str) -> str:
    parsed = urlparse(url_or_id)
    if not parsed.scheme:
        return url_or_id  # already a bare video ID

    if parsed.hostname == "youtu.be":
        return parsed.path.lstrip("/")

    if parsed.hostname and "youtube.com" in parsed.hostname:
        if parsed.path == "/watch":
            query_video_id = parse_qs(parsed.query).get("v")
            if query_video_id:
                return query_video_id[0]
        # /embed/<id> or /v/<id>
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            return parts[-1]

    raise ValueError(f"Could not extract a YouTube video ID from: {url_or_id}")


def _save_transcript(
    assignments_root: Path, course: str, title: str, video_url: str, transcript_text: str
) -> Path:
    course_folder = assignments_root / _sanitize_filename(course)
    lectures_folder = course_folder / "Lectures"
    lectures_folder.mkdir(parents=True, exist_ok=True)
    note_path = lectures_folder / f"{_sanitize_filename(title)} Transcript.md"

    frontmatter_fields = {
        "course": course,
        "source": "youtube",
        "source_url": video_url,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    body = f"# {title} - Transcript\n\n[Watch on YouTube]({video_url})\n\n## Transcript\n\n{transcript_text}\n"
    post = frontmatter.Post(body, **frontmatter_fields)
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return note_path


def fetch_and_save(assignments_root: Path, url_or_id: str, course: str, title: str) -> Path:
    video_id = extract_video_id(url_or_id)

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id)
    except Exception as e:
        raise RuntimeError(f"Could not fetch a transcript for this video: {e}") from e

    transcript_text = "\n".join(snippet.text for snippet in fetched)
    video_url = url_or_id if url_or_id.startswith("http") else f"https://www.youtube.com/watch?v={video_id}"
    return _save_transcript(assignments_root, course, title, video_url, transcript_text)
