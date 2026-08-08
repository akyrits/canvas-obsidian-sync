"""Pure context construction for the throwaway ICM comparison."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from study_analysis.engine import AnalysisEngine, _extract_section
from study_analysis.schema import AnalysisMode
from study_analysis.sources import SourceRecord, load_sources


STOP_WORDS = {
    "about", "after", "again", "also", "and", "are", "assignment", "been",
    "before", "being", "between", "course", "does", "each", "from", "have",
    "into", "must", "other", "should", "that", "their", "then", "these",
    "they", "this", "through", "using", "what", "when", "where", "which",
    "with", "would", "your",
}


@dataclass(frozen=True)
class PageChunk:
    source_title: str
    source_name: str
    source_order: int
    page: int
    text: str
    score: float = 0.0

    def render(self) -> str:
        return (
            f"\n=== {self.source_title} ({self.source_name}) ===\n"
            f"[PDF page {self.page}]\n{self.text.strip()}\n"
        )


@dataclass(frozen=True)
class ContextVariant:
    name: str
    prompt: str
    source_chars: int
    selected_pages: tuple[str, ...]
    description: str

    @property
    def prompt_chars(self) -> int:
        return len(self.prompt)

    @property
    def rough_tokens(self) -> int:
        return math.ceil(len(self.prompt) / 4)


def _raw_source_text(sources: list[SourceRecord], max_chars: int) -> str:
    remaining = max_chars
    blocks: list[str] = []
    for source in sources:
        header = f"\n=== {source.title} ({source.source_path.name}) ===\n"
        remaining -= len(header)
        if remaining <= 0:
            break
        text = source.extract_text(remaining)
        blocks.append(header + text)
        remaining -= len(text)
    if remaining <= 0 or len(blocks) < len(sources):
        blocks.append(
            "\n[INPUT BUDGET REACHED: some indexed source content was not supplied. "
            "Do not claim it was analyzed.]\n"
        )
    return "".join(blocks)


def _page_chunks(sources: list[SourceRecord]) -> list[PageChunk]:
    chunks: list[PageChunk] = []
    for source_order, source in enumerate(sources):
        if source.source_path.suffix.lower() != ".pdf":
            continue
        reader = source.pdf_reader()
        pages = source.pages or tuple(range(1, len(reader.pages) + 1))
        for page in pages:
            text = (reader.pages[page - 1].extract_text() or "").strip()
            chunks.append(
                PageChunk(
                    source_title=source.title,
                    source_name=source.source_path.name,
                    source_order=source_order,
                    page=page,
                    text=text,
                )
            )
    return chunks


def _terms(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z][a-z0-9_-]{2,}", text.casefold())
        if word not in STOP_WORDS
    }


def _select_pages(
    sources: list[SourceRecord], details: str, char_budget: int
) -> list[PageChunk]:
    query = _terms(details)
    scored: list[PageChunk] = []
    for chunk in _page_chunks(sources):
        words = re.findall(r"[a-z][a-z0-9_-]{2,}", chunk.text.casefold())
        overlap = sum(1 for word in words if word in query)
        score = overlap / math.sqrt(max(len(words), 1))
        scored.append(
            PageChunk(
                chunk.source_title,
                chunk.source_name,
                chunk.source_order,
                chunk.page,
                chunk.text,
                score,
            )
        )

    chosen: list[PageChunk] = []
    used = 0
    # Keep one page from every indexed source before filling by relevance.
    for source_order in sorted({chunk.source_order for chunk in scored}):
        candidates = [chunk for chunk in scored if chunk.source_order == source_order]
        if candidates:
            best = max(candidates, key=lambda chunk: (chunk.score, -chunk.page))
            rendered = best.render()
            if used + len(rendered) <= char_budget:
                chosen.append(best)
                used += len(rendered)

    for chunk in sorted(scored, key=lambda item: (-item.score, item.source_order, item.page)):
        if chunk in chosen:
            continue
        rendered = chunk.render()
        if used + len(rendered) > char_budget:
            continue
        chosen.append(chunk)
        used += len(rendered)

    return sorted(chosen, key=lambda chunk: (chunk.source_order, chunk.page))


def _icm_catalog(workspace: Path) -> str:
    paths = [
        workspace / "AGENTS.md",
        workspace / "CONTEXT.md",
        workspace / "stages" / "02_analyze" / "CONTEXT.md",
        workspace / "_shared" / "study-policy.md",
    ]
    return "\n\n".join(
        f"=== {path.relative_to(workspace).as_posix()} ===\n{path.read_text(encoding='utf-8')}"
        for path in paths
    )


def build_variants(
    assignment_path: Path,
    workspace: Path,
    max_source_chars: int = 48_000,
    retrieval_chars: int = 12_000,
) -> tuple[ContextVariant, ...]:
    assignment = frontmatter.load(assignment_path)
    details = _extract_section(assignment.content, "Assignment Details")
    sources = load_sources(assignment_path)
    raw_sources = _raw_source_text(sources, max_source_chars)
    direct_prompt = AnalysisEngine._build_prompt(
        assignment_title=assignment_path.stem,
        course=str(assignment.get("course") or "unknown"),
        due=str(assignment.get("due") or "unknown"),
        details=details,
        sources=raw_sources,
        mode=AnalysisMode.STUDY,
    )
    all_pages = tuple(
        f"{source.title}: {page}" for source in sources for page in source.pages
    )
    catalog = _icm_catalog(workspace)
    structure_only = f"{catalog}\n\n{direct_prompt}"
    icm_contract = f"""Execute only the `02_analyze` contract below. The JSON transport schema is authoritative for output shape.

{catalog}

=== Working assignment ===
Title: {assignment_path.stem}
Course: {assignment.get('course') or 'unknown'}
Due: {assignment.get('due') or 'unknown'}

## Assignment Details
{details}

=== Verified evidence packet ===
{raw_sources}
"""

    selected = _select_pages(sources, details, retrieval_chars)
    routed_sources = "".join(chunk.render() for chunk in selected)
    retrieval_prompt = AnalysisEngine._build_prompt(
        assignment_title=assignment_path.stem,
        course=str(assignment.get("course") or "unknown"),
        due=str(assignment.get("due") or "unknown"),
        details=details,
        sources=routed_sources,
        mode=AnalysisMode.STUDY,
    )
    selected_pages = tuple(f"{chunk.source_title}: {chunk.page}" for chunk in selected)

    return (
        ContextVariant(
            "current",
            direct_prompt,
            len(raw_sources),
            all_pages,
            "Production prompt and every indexed page-scoped source.",
        ),
        ContextVariant(
            "icm-structure-only",
            structure_only,
            len(raw_sources),
            all_pages,
            "Current prompt plus ICM catalogs/contracts; isolates structural overhead.",
        ),
        ContextVariant(
            "icm-contract",
            icm_contract,
            len(raw_sources),
            all_pages,
            "Concise ICM stage contract, same evidence, transport schema not duplicated.",
        ),
        ContextVariant(
            "selective-retrieval",
            retrieval_prompt,
            len(routed_sources),
            selected_pages,
            "Deterministic page routing under a smaller evidence budget.",
        ),
    )
