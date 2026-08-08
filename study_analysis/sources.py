from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import frontmatter
from pypdf import PdfReader

from vault_notes import _sanitize_filename


_SOURCE_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SUPPORTED_SOURCE_SUFFIXES = {".md", ".markdown", ".pdf", ".txt"}


def _validate_source_file(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"Indexed sources cannot be symbolic links: {path}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    if resolved.suffix.casefold() not in _SUPPORTED_SOURCE_SUFFIXES:
        supported = ", ".join(sorted(_SUPPORTED_SOURCE_SUFFIXES))
        raise ValueError(f"Unsupported source type {resolved.suffix!r}; expected {supported}")
    return resolved


def _source_record_path(course_path: Path, reference: object) -> Path:
    relative = Path(str(reference))
    if relative.is_absolute():
        raise ValueError("Source record references must be relative to the course")
    sources_root = (course_path / "Sources").resolve()
    candidate = (course_path / relative).resolve()
    try:
        candidate.relative_to(sources_root)
    except ValueError as exc:
        raise ValueError(
            f"Source record must stay inside the course Sources folder: {reference}"
        ) from exc
    return candidate


def parse_pages(specs: list[str]) -> tuple[int, ...]:
    pages: set[int] = set()
    for spec in specs:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start < 1 or end < start:
                    raise ValueError(f"Invalid page range: {part}")
                pages.update(range(start, end + 1))
            else:
                page = int(part)
                if page < 1:
                    raise ValueError(f"Invalid page number: {part}")
                pages.add(page)
    return tuple(sorted(pages))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceRecord:
    title: str
    note_path: Path
    source_path: Path
    pages: tuple[int, ...]
    file_hash: str
    note_original: bytes = field(repr=False)
    source_original: bytes = field(repr=False)

    def pdf_reader(self) -> PdfReader:
        if self.source_path.suffix.casefold() != ".pdf":
            raise ValueError(f"Not a PDF source: {self.source_path}")
        return PdfReader(BytesIO(self.source_original))

    def text(self) -> str:
        return self.source_original.decode("utf-8")

    def is_current(self) -> bool:
        """Check that the indexed record and evidence still match this snapshot."""
        try:
            if self.note_path.is_symlink() or self.source_path.is_symlink():
                return False
            if self.note_path.read_bytes() != self.note_original:
                return False
            return _sha256(self.source_path) == self.file_hash
        except OSError:
            return False

    def extract_text(self, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        suffix = self.source_path.suffix.lower()
        if suffix == ".pdf":
            reader = self.pdf_reader()
            chunks: list[str] = []
            remaining = max_chars
            selected = self.pages or tuple(range(1, len(reader.pages) + 1))
            for page_number in selected:
                if page_number > len(reader.pages):
                    raise ValueError(
                        f"{self.title}: page {page_number} exceeds {len(reader.pages)} pages"
                    )
                text = (reader.pages[page_number - 1].extract_text() or "").strip()
                chunk = f"\n[PDF page {page_number}]\n{text}\n"
                chunks.append(chunk[:remaining])
                remaining -= len(chunk)
                if remaining <= 0:
                    break
            return "".join(chunks)
        return self.text()[:max_chars]


def index_source(
    course_path: Path,
    source_path: Path,
    title: str,
    page_specs: list[str],
    assignment_path: Path | None = None,
) -> Path:
    source_path = _validate_source_file(source_path)
    pages = parse_pages(page_specs)
    source_folder = course_path / "Sources"
    source_folder.mkdir(parents=True, exist_ok=True)
    note_path = source_folder / f"{_sanitize_filename(title)}.md"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metadata = {
        "type": "source-record",
        "course": course_path.name,
        "source_path": source_path.as_posix(),
        "file_hash": _sha256(source_path),
        "indexed_at": now,
        "relevant_pages": list(pages),
        "tags": ["source"],
    }
    if note_path.exists():
        post = frontmatter.load(note_path)
        post.metadata.update(metadata)
    else:
        body = (
            f"# {title}\n\n"
            "## Relevance\n\n"
            "Indexed source material for concept analysis.\n\n"
            "## Notes\n"
        )
        post = frontmatter.Post(body, **metadata)
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    if assignment_path is not None:
        assignment = frontmatter.load(assignment_path)
        references = list(assignment.get("analysis_sources") or [])
        relative = note_path.relative_to(course_path).as_posix()
        if relative not in references:
            references.append(relative)
        assignment["analysis_sources"] = references
        assignment_path.write_text(frontmatter.dumps(assignment), encoding="utf-8")
    return note_path


def load_sources(
    assignment_path: Path,
    assignment_snapshot: frontmatter.Post | None = None,
) -> list[SourceRecord]:
    assignment = (
        assignment_snapshot
        if assignment_snapshot is not None
        else frontmatter.load(assignment_path)
    )
    course_path = assignment_path.parent
    records = []
    for reference in assignment.get("analysis_sources") or []:
        note_path = _source_record_path(course_path, reference)
        if not note_path.is_file():
            raise FileNotFoundError(f"Source record not found: {note_path}")
        note_original = note_path.read_bytes()
        post = frontmatter.loads(note_original.decode("utf-8"))
        if post.get("type") != "source-record":
            raise ValueError(f"Not a source record: {note_path}")
        if str(post.get("course") or "") != course_path.name:
            raise ValueError(f"Source record course does not match assignment: {note_path}")
        source_path = _validate_source_file(Path(str(post.get("source_path", ""))))
        recorded_hash = str(post.get("file_hash", "")).strip().casefold()
        if not _SOURCE_HASH_RE.fullmatch(recorded_hash):
            raise ValueError(f"Source record lacks a valid recorded SHA-256: {note_path}")
        source_original = source_path.read_bytes()
        current_hash = hashlib.sha256(source_original).hexdigest()
        if current_hash != recorded_hash:
            raise ValueError(f"Indexed source changed since indexing: {source_path}")
        pages = tuple(int(page) for page in (post.get("relevant_pages") or []))
        if any(page < 1 for page in pages):
            raise ValueError(f"Source record contains an invalid page number: {note_path}")
        records.append(
            SourceRecord(
                title=note_path.stem,
                note_path=note_path,
                source_path=source_path,
                pages=pages,
                file_hash=current_hash,
                note_original=note_original,
                source_original=source_original,
            )
        )
    return records
