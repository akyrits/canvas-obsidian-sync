from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .sources import SourceRecord


_STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "answer",
    "are",
    "assignment",
    "been",
    "before",
    "being",
    "between",
    "course",
    "does",
    "each",
    "every",
    "from",
    "have",
    "into",
    "must",
    "other",
    "show",
    "should",
    "that",
    "their",
    "then",
    "these",
    "they",
    "this",
    "through",
    "using",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "your",
}


@dataclass(frozen=True)
class EvidenceLocator:
    source_title: str
    source_file: str
    page: int | None
    section: int | None = None

    @property
    def label(self) -> str:
        if self.page is not None:
            suffix = f", PDF page {self.page}"
        elif self.section is not None:
            suffix = f", text section {self.section}"
        else:
            suffix = ""
        return f"{self.source_title} ({self.source_file}){suffix}"

    def matches(self, citation: str) -> bool:
        locator = re.search(
            r",\s*(?P<kind>pdf page|text section)\s+(?P<number>\d+)\s*$",
            citation,
            re.IGNORECASE,
        )
        source_text = citation[: locator.start()] if locator else citation

        def normalized(value: str) -> str:
            return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))

        supplied_source = normalized(source_text)
        accepted_sources = {
            normalized(self.source_title),
            normalized(Path(self.source_file).stem),
            normalized(Path(self.source_file).name),
            # The evidence heading the model is shown renders as
            # "Title (file.pdf)", and the prompt tells it to copy that heading,
            # so accept it verbatim alongside the title- and file-only forms.
            normalized(f"{self.source_title} ({self.source_file})"),
            normalized(f"{self.source_title} ({Path(self.source_file).stem})"),
        }
        if supplied_source not in accepted_sources:
            return False
        if self.page is not None:
            return bool(
                locator
                and locator.group("kind").casefold() == "pdf page"
                and int(locator.group("number")) == self.page
            )
        if self.section is not None:
            return bool(
                locator
                and locator.group("kind").casefold() == "text section"
                and int(locator.group("number")) == self.section
            )
        return locator is None


@dataclass(frozen=True)
class CompiledContext:
    """Citation-preserving evidence plus compact, content-free telemetry."""

    text: str
    strategy: str
    selected: tuple[EvidenceLocator, ...]
    available_chunks: int
    truncated: bool
    source_hashes: tuple[str, ...]

    @property
    def evidence_chars(self) -> int:
        return len(self.text)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def resolves(self, citation: str) -> bool:
        return any(locator.matches(citation) for locator in self.selected)


class ContextCompiler(Protocol):
    """Internal seam for deciding which indexed evidence enters one model call."""

    name: str

    def compile(
        self,
        details: str,
        sources: Sequence[SourceRecord],
        max_chars: int,
    ) -> CompiledContext: ...


@dataclass(frozen=True)
class _EvidenceChunk:
    source_title: str
    source_file: str
    source_order: int
    page: int | None
    section: int | None
    text: str
    score: float = 0.0

    @property
    def key(self) -> tuple[int, int]:
        return (self.source_order, self.page or self.section or 0)

    @property
    def locator(self) -> EvidenceLocator:
        return EvidenceLocator(
            self.source_title,
            self.source_file,
            self.page,
            self.section,
        )

    def render(self) -> str:
        if self.page is not None:
            return f"\n[PDF page {self.page}]\n{self.text.strip()}\n"
        if self.section is not None:
            return f"\n[Text section {self.section}]\n{self.text.strip()}\n"
        return self.text.strip() + "\n"


def _source_header(chunk: _EvidenceChunk) -> str:
    return f"\n=== {chunk.source_title} ({chunk.source_file}) ===\n"


def _split_text_sections(text: str, target_chars: int = 2_400) -> tuple[str, ...]:
    """Split long text deterministically without cutting ordinary paragraphs."""

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    sections: list[str] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in paragraphs:
        marginal = len(paragraph) + (2 if current else 0)
        if current and current_chars + marginal > target_chars:
            sections.append("\n\n".join(current))
            current = []
            current_chars = 0
        if len(paragraph) > target_chars:
            if current:
                sections.append("\n\n".join(current))
                current = []
                current_chars = 0
            lines = paragraph.splitlines()
            line_group: list[str] = []
            line_chars = 0
            for line in lines:
                line_marginal = len(line) + (1 if line_group else 0)
                if line_group and line_chars + line_marginal > target_chars:
                    sections.append("\n".join(line_group))
                    line_group = []
                    line_chars = 0
                line_group.append(line)
                line_chars += line_marginal
            if line_group:
                sections.append("\n".join(line_group))
            continue
        current.append(paragraph)
        current_chars += marginal
    if current:
        sections.append("\n\n".join(current))
    return tuple(sections) or (text.strip(),)


def _extract_chunks(sources: Sequence[SourceRecord]) -> list[_EvidenceChunk]:
    chunks: list[_EvidenceChunk] = []
    for source_order, source in enumerate(sources):
        if source.source_path.suffix.lower() != ".pdf":
            for section, text in enumerate(
                _split_text_sections(
                    source.text()
                ),
                start=1,
            ):
                chunks.append(
                    _EvidenceChunk(
                        source_title=source.title,
                        source_file=source.source_path.name,
                        source_order=source_order,
                        page=None,
                        section=section,
                        text=text,
                    )
                )
            continue

        reader = source.pdf_reader()
        pages = source.pages or tuple(range(1, len(reader.pages) + 1))
        for page in pages:
            if page < 1 or page > len(reader.pages):
                raise ValueError(
                    f"{source.title}: page {page} exceeds {len(reader.pages)} pages"
                )
            chunks.append(
                _EvidenceChunk(
                    source_title=source.title,
                    source_file=source.source_path.name,
                    source_order=source_order,
                    page=page,
                    section=None,
                    text=reader.pages[page - 1].extract_text() or "",
                )
            )
    return chunks


class PageScopedContext:
    """Current behavior: concatenate every indexed range until the hard budget."""

    name = "page-scoped-v1"

    def compile(
        self,
        details: str,
        sources: Sequence[SourceRecord],
        max_chars: int,
    ) -> CompiledContext:
        del details
        if max_chars <= 0:
            raise ValueError("max_input_chars must be positive")

        def collect(budget: int) -> tuple[list[str], list[EvidenceLocator], int, int]:
            remaining = budget
            blocks: list[str] = []
            selected: list[EvidenceLocator] = []
            included_sources = 0
            for source in sources:
                header = f"\n=== {source.title} ({source.source_path.name}) ===\n"
                remaining -= len(header)
                if remaining <= 0:
                    break
                text = source.extract_text(remaining)
                blocks.append(header + text)
                remaining -= len(text)
                included_sources += 1
                if source.source_path.suffix.lower() == ".pdf":
                    supplied_pages = tuple(
                        int(page)
                        for page in re.findall(r"\[PDF page (\d+)\]", text)
                    )
                    selected.extend(
                        EvidenceLocator(source.title, source.source_path.name, page)
                        for page in supplied_pages
                    )
                else:
                    selected.append(
                        EvidenceLocator(source.title, source.source_path.name, None)
                    )
            return blocks, selected, included_sources, remaining

        blocks, selected, included_sources, remaining = collect(max_chars)
        truncated = remaining <= 0 or included_sources < len(sources)
        marker = (
            "\n[INPUT BUDGET REACHED: some indexed source content was not supplied. "
            "Do not claim it was analyzed.]\n"
        )
        if truncated:
            evidence_budget = max(0, max_chars - len(marker))
            blocks, selected, _, _ = collect(evidence_budget)
            text = ("".join(blocks) + marker)[:max_chars]
        else:
            text = "".join(blocks)
        return CompiledContext(
            text=text,
            strategy=self.name,
            selected=tuple(selected),
            available_chunks=sum(
                len(source.pages) if source.pages else 1 for source in sources
            ),
            truncated=truncated,
            source_hashes=tuple(source.file_hash for source in sources),
        )


def _terms(text: str) -> list[str]:
    return [
        word
        for word in re.findall(r"[a-z][a-z0-9_-]{2,}", text.casefold())
        if word not in _STOP_WORDS
    ]


def _score_chunks(chunks: list[_EvidenceChunk], details: str) -> list[_EvidenceChunk]:
    query = Counter(_terms(details))
    query_terms = set(query)
    documents = [Counter(_terms(chunk.text)) for chunk in chunks]
    document_count = max(len(documents), 1)
    average_length = sum(sum(document.values()) for document in documents) / document_count
    average_length = max(average_length, 1.0)
    document_frequency = Counter(
        term for document in documents for term in document.keys()
    )
    k1 = 1.5
    b = 0.75
    scored: list[_EvidenceChunk] = []
    for chunk, document in zip(chunks, documents):
        length = max(sum(document.values()), 1)
        score = 0.0
        for term, query_count in query.items():
            frequency = document.get(term, 0)
            if not frequency:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (document_count - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
            )
            normalized_frequency = frequency * (k1 + 1) / (
                frequency + k1 * (1 - b + b * length / average_length)
            )
            score += inverse_document_frequency * normalized_frequency * (
                1 + math.log(query_count)
            )
        # End-of-chapter exercise banks often repeat assignment wording exactly
        # while providing little explanatory evidence. Keep them eligible, but
        # prefer definitions and worked exposition when both match the query.
        exercise_markers = len(
            re.findall(r"\b(?:r|c|p)-\d+\.\d+\b", chunk.text.casefold())
        )
        exercise_markers += sum(
            marker in chunk.text.casefold()
            for marker in ("reinforcement exercises", "creativity exercises")
        )
        if exercise_markers:
            score /= 1 + exercise_markers
        # Reference pages contain maintenance/security appendices that repeat an
        # API name but are poor teaching evidence for an unrelated assignment.
        # Keep them eligible when the assignment asks for those concerns.
        lowered = chunk.text.casefold()
        opening = lowered[:300]
        if not query_terms.intersection({"security", "injection", "xss", "csp", "trusted"}):
            if any(
                marker in opening
                for marker in (
                    "security considerations",
                    "content-security-policy",
                    "trusted types",
                    "trusted-types",
                )
            ):
                score *= 0.2
        if not query_terms.intersection({"binding", "context", "globalthis"}):
            if "globalthis" in lowered or "`this` context" in lowered:
                score *= 0.35
        if not query_terms.intersection({"compatibility", "specification", "support"}):
            if any(
                marker in lowered
                for marker in ("## browser compatibility", "## specifications", "## see also")
            ):
                score *= 0.35
        scored.append(
            _EvidenceChunk(
                source_title=chunk.source_title,
                source_file=chunk.source_file,
                source_order=chunk.source_order,
                page=chunk.page,
                section=chunk.section,
                text=chunk.text,
                score=score,
            )
        )
    return scored


def _render_selected(chunks: Sequence[_EvidenceChunk]) -> str:
    blocks: list[str] = []
    current_source: int | None = None
    for chunk in sorted(chunks, key=lambda item: item.key):
        if current_source != chunk.source_order:
            blocks.append(_source_header(chunk))
            current_source = chunk.source_order
        blocks.append(chunk.render())
    return "".join(blocks)


class SelectiveContext:
    """Local BM25-style page routing with source coverage and neighbor continuity."""

    name = "selective-v1"

    def __init__(self, max_evidence_chars: int = 12_000):
        if max_evidence_chars <= 0:
            raise ValueError("max_evidence_chars must be positive")
        self.max_evidence_chars = max_evidence_chars

    def compile(
        self,
        details: str,
        sources: Sequence[SourceRecord],
        max_chars: int,
    ) -> CompiledContext:
        if max_chars <= 0:
            raise ValueError("max_input_chars must be positive")
        if not sources:
            raise ValueError("Selective context requires at least one source")

        budget = min(max_chars, self.max_evidence_chars)
        chunks = _score_chunks(_extract_chunks(sources), details)
        if not chunks:
            raise ValueError("Indexed sources produced no readable evidence")

        priorities = {chunk.key: chunk.score for chunk in chunks}
        by_key = {chunk.key: chunk for chunk in chunks}
        for chunk in chunks:
            if chunk.score <= 0:
                continue
            ordinal = chunk.page or chunk.section
            if ordinal is None:
                continue
            for neighbor_ordinal in (ordinal - 1, ordinal + 1):
                neighbor_key = (chunk.source_order, neighbor_ordinal)
                if neighbor_key in by_key:
                    priorities[neighbor_key] = max(
                        priorities[neighbor_key], chunk.score * 0.35
                    )

        ranked = sorted(
            chunks,
            key=lambda item: (
                -priorities[item.key],
                item.source_order,
                item.page or item.section or 0,
            ),
        )
        seeds: list[_EvidenceChunk] = []
        for source_order in sorted({chunk.source_order for chunk in chunks}):
            candidates = [
                chunk for chunk in ranked if chunk.source_order == source_order
            ]
            if candidates[0].section is not None:
                # Text references usually state their core contract near the
                # beginning. Preserve that context alongside the best match so
                # a repeated term in an appendix cannot displace the definition.
                first = min(candidates, key=lambda item: item.section or 0)
                seeds.append(candidates[0])
                if first != candidates[0]:
                    seeds.append(first)
            else:
                # Two pages per multi-page source prevents a large textbook from
                # crowding out distinct lecture evidence under a tight budget.
                seeds.extend(candidates[: min(2, len(candidates))])
        ordered_candidates = seeds + [chunk for chunk in ranked if chunk not in seeds]

        selected: list[_EvidenceChunk] = []
        selected_keys: set[tuple[int, int]] = set()
        selected_sources: set[int] = set()
        used = 0
        for chunk in ordered_candidates:
            if chunk.key in selected_keys:
                continue
            header_chars = (
                0 if chunk.source_order in selected_sources else len(_source_header(chunk))
            )
            marginal = header_chars + len(chunk.render())
            if used + marginal > budget:
                continue
            selected.append(chunk)
            selected_keys.add(chunk.key)
            selected_sources.add(chunk.source_order)
            used += marginal

        if not selected:
            first = ranked[0]
            header = _source_header(first)
            if first.page is not None:
                locator_header = f"\n[PDF page {first.page}]\n"
            elif first.section is not None:
                locator_header = f"\n[Text section {first.section}]\n"
            else:
                locator_header = ""
            remaining = budget - len(header) - len(locator_header) - 1
            if remaining <= 0:
                raise ValueError("Evidence budget is too small for a source header")
            selected = [
                _EvidenceChunk(
                    source_title=first.source_title,
                    source_file=first.source_file,
                    source_order=first.source_order,
                    page=first.page,
                    section=first.section,
                    text=first.text[:remaining],
                    score=first.score,
                )
            ]

        text = _render_selected(selected)
        return CompiledContext(
            text=text,
            strategy=self.name,
            selected=tuple(chunk.locator for chunk in sorted(selected, key=lambda item: item.key)),
            available_chunks=len(chunks),
            truncated=len(selected_keys) < len(chunks),
            source_hashes=tuple(source.file_hash for source in sources),
        )
