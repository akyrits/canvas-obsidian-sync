from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import frontmatter

from vault_notes import _sanitize_filename

from .schema import (
    AnalysisMode,
    AnalysisResult,
    AnalysisValidationError,
    ConceptAnalysis,
    normalize_concept_name,
)
from .research import ResearchHit
from .transaction import commit_text_files


def _extract_assignment_details(content: str) -> str:
    lines = content.splitlines()
    target = "## Assignment Details"
    start = next((i for i, line in enumerate(lines) if line.strip() == target), None)
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start + 1 : end]).strip()


def _set_section(content: str, header: str, value: str) -> str:
    lines = content.splitlines(keepends=True)
    target = f"## {header}"
    matches = [i for i, line in enumerate(lines) if line.strip() == target]
    # Older commands could append a second copy of a managed section. Remove
    # duplicates from the end first so the earliest section keeps its position.
    for duplicate in reversed(matches[1:]):
        end = next(
            (
                i
                for i in range(duplicate + 1, len(lines))
                if lines[i].strip().startswith("## ")
            ),
            len(lines),
        )
        del lines[duplicate:end]
    start = next((i for i, line in enumerate(lines) if line.strip() == target), None)
    replacement = [f"{target}\n", "\n", value.rstrip() + "\n", "\n"]
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        return "".join(lines + ["\n"] + replacement)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("## ")),
        len(lines),
    )
    lines[start:end] = replacement
    return "".join(lines)


def _confidence_label(value: float) -> str:
    if value >= 0.8:
        return "high"
    if value >= 0.55:
        return "medium"
    return "low"


def _wikilink(name: str) -> str:
    return f"[[{_sanitize_filename(name)}]]"


def _research_links(hits: Sequence[ResearchHit]) -> str:
    if not hits:
        return "_None recorded._"
    lines = [
        "_Discovered by local search from reviewed public topics; page contents "
        "were not fetched or independently verified._"
    ]
    seen: set[str] = set()
    for hit in hits:
        if not hit.url.startswith("https://") or hit.url in seen:
            raise AnalysisValidationError(
                "Helpful Links must come from unique normalized HTTPS research hits"
            )
        seen.add(hit.url)
        title = hit.title.replace("\\", "").replace("[", "\\[").replace("]", "\\]")
        lines.append(f"- [{title}](<{hit.url}>)")
    return "\n".join(lines)


def _concept_content(existing: frontmatter.Post | None, concept: ConceptAnalysis) -> str:
    content = existing.content if existing else f"# {concept.name}\n\n## Personal Notes\n"
    content = _set_section(content, "Definition", concept.summary)
    content = _set_section(
        content,
        "Why This Matters",
        "\n".join(
            [
                f"### Foundational\n{concept.why_foundational}",
                f"### Practical\n{concept.why_practical}",
                f"### Decision-making\n{concept.why_decision_making}",
                f"### Personal curriculum\n{concept.why_personal_curriculum}",
            ]
        ),
    )
    relationships = (
        "\n".join(f"- `{item.kind}` → {_wikilink(item.target)}" for item in concept.relationships)
        or "_No validated relationships yet._"
    )
    content = _set_section(content, "Connections", relationships)
    content = _set_section(
        content, "Examples", "\n".join(f"- {item}" for item in concept.examples)
    )
    resources = (
        "\n".join(
            f"- [{item.title}]({item.locator}) - {item.why_useful} "
            f"(accessed {item.accessed_at})"
            if item.locator.startswith(("http://", "https://"))
            else f"- **{item.title}:** `{item.locator}` - {item.why_useful} "
            f"(indexed {item.accessed_at})"
            for item in concept.resources
        )
        or "_None recorded._"
    )
    content = _set_section(content, "Resources", resources)
    content = _set_section(
        content,
        "Source Trail",
        "\n".join(f"- {citation}" for citation in concept.source_citations),
    )
    return content


@dataclass(frozen=True)
class CommitResult:
    changed_files: tuple[Path, ...]
    solution_path: Path | None


class AnalysisVault:
    """Plans and commits one validated analysis as a reversible file transaction."""

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root

    def _find_concept(self, concepts_root: Path, name: str) -> Path | None:
        wanted = normalize_concept_name(name)
        if not concepts_root.exists():
            return None
        matches: list[Path] = []
        for path in concepts_root.glob("*.md"):
            try:
                post = frontmatter.load(path)
            except Exception:
                continue
            names = [str(post.get("canonical_name") or path.stem)]
            names.extend(str(alias) for alias in (post.get("aliases") or []))
            if wanted in {normalize_concept_name(item) for item in names}:
                matches.append(path)
        if len(matches) > 1:
            raise AnalysisValidationError(
                f"Ambiguous canonical concept match for {name}: {matches}"
            )
        return matches[0] if matches else None

    def _resolve_targets(
        self, concepts: Sequence[ConceptAnalysis]
    ) -> tuple[
        tuple[ConceptAnalysis, Path, frontmatter.Post | None, dict, str, bytes | None],
        ...,
    ]:
        concepts_root = self.vault_root / "Knowledge" / "Concepts"
        targets: list[
            tuple[
                ConceptAnalysis,
                Path,
                frontmatter.Post | None,
                dict,
                str,
                bytes | None,
            ]
        ] = []
        seen_paths: dict[str, str] = {}
        for concept in concepts:
            path = self._find_concept(concepts_root, concept.name)
            if path is None:
                path = concepts_root / f"{_sanitize_filename(concept.name)}.md"
                existing = None
                original = None
                metadata = {
                    "type": "concept",
                    "canonical_name": concept.name,
                    "aliases": [],
                    "familiarity": "unknown",
                    "tags": ["concept"],
                }
            else:
                original = path.read_bytes()
                existing = frontmatter.loads(original.decode("utf-8"))
                metadata = dict(existing.metadata)
            canonical_name = str(metadata.get("canonical_name") or path.stem)
            identity = str(path.absolute()).casefold()
            if identity in seen_paths:
                raise AnalysisValidationError(
                    f"{concept.name!r} and {seen_paths[identity]!r} resolve to the "
                    f"same canonical concept {canonical_name!r}"
                )
            seen_paths[identity] = concept.name
            metadata.setdefault("canonical_name", canonical_name)
            metadata.update(
                {
                    "difficulty": concept.difficulty.score,
                    "difficulty_reason": concept.difficulty.reason,
                    "difficulty_confidence": concept.difficulty.confidence,
                }
            )
            metadata.setdefault("familiarity", "unknown")
            targets.append(
                (concept, path, existing, metadata, canonical_name, original)
            )
        return tuple(targets)

    def resolve_concept_names(
        self, concepts: Sequence[ConceptAnalysis]
    ) -> tuple[str, ...]:
        """Resolve exact canonical names without creating or changing vault files."""
        return tuple(target[4] for target in self._resolve_targets(concepts))

    def validate_contract_targets(self, canonical_names: Sequence[str]) -> None:
        """Fail closed when a production canary names an absent or alias-only target."""
        concepts_root = self.vault_root / "Knowledge" / "Concepts"
        seen_paths: set[str] = set()
        for canonical_name in canonical_names:
            path = self._find_concept(concepts_root, canonical_name)
            if path is None:
                raise AnalysisValidationError(
                    f"Required canonical concept does not exist: {canonical_name}"
                )
            post = frontmatter.load(path)
            stored_name = str(post.get("canonical_name") or path.stem)
            if normalize_concept_name(stored_name) != normalize_concept_name(
                canonical_name
            ):
                raise AnalysisValidationError(
                    f"Analysis contract must name the canonical concept {stored_name!r}, "
                    f"not alias {canonical_name!r}"
                )
            identity = str(path.absolute()).casefold()
            if identity in seen_paths:
                raise AnalysisValidationError(
                    f"Analysis contract repeats canonical concept: {stored_name}"
                )
            seen_paths.add(identity)

    def validate_relationship_targets(
        self,
        concepts: Sequence[ConceptAnalysis],
        resolved_concepts: Sequence[str],
    ) -> None:
        """Require contract-backed links to use resolvable canonical note names."""
        concepts_root = self.vault_root / "Knowledge" / "Concepts"
        generated = {
            normalize_concept_name(concept.name): canonical_name
            for concept, canonical_name in zip(concepts, resolved_concepts)
        }
        for concept in concepts:
            for relationship in concept.relationships:
                target = relationship.target
                canonical_name = generated.get(normalize_concept_name(target))
                if canonical_name is None:
                    path = self._find_concept(concepts_root, target)
                    if path is None:
                        raise AnalysisValidationError(
                            f"Unresolved relationship target {target!r} in {concept.name!r}"
                        )
                    post = frontmatter.load(path)
                    canonical_name = str(post.get("canonical_name") or path.stem)
                if target != canonical_name:
                    raise AnalysisValidationError(
                        f"Noncanonical relationship target {target!r} in {concept.name!r}; "
                        f"use {canonical_name!r}"
                    )

    def commit(
        self,
        assignment_path: Path,
        result: AnalysisResult,
        mode: AnalysisMode,
        analyzed_at: str,
        *,
        assignment_original: bytes,
        research_hits: Sequence[ResearchHit] | None = None,
    ) -> CommitResult:
        targets = self._resolve_targets(result.concepts)
        planned: dict[Path, str] = {}

        for _, _, _, metadata, _, _ in targets:
            metadata["analysis_date"] = analyzed_at

        for concept, path, existing, metadata, _, _ in targets:
            post = frontmatter.Post(_concept_content(existing, concept), **metadata)
            planned[path] = frontmatter.dumps(post)

        assignment = frontmatter.loads(assignment_original.decode("utf-8"))
        assignment["analysis"] = {
            "status": "complete",
            "mode": mode.value,
            "analyzed_at": analyzed_at,
            "concepts": [
                canonical_name for _, _, _, _, canonical_name, _ in targets
            ],
            "concept_difficulty_max": max(c.difficulty.score for c in result.concepts),
            "assignment_difficulty": result.assignment_difficulty.score,
            "assignment_difficulty_confidence": result.assignment_difficulty.confidence,
            "effort": result.assignment_effort.level,
            "effort_confidence": result.assignment_effort.confidence,
        }
        approach = "\n".join(
            f"{index}. {item}" for index, item in enumerate(result.approach, 1)
        )
        assignment.content = _set_section(
            assignment.content,
            "Assignment Details",
            _extract_assignment_details(assignment.content),
        )
        assignment.content = _set_section(assignment.content, "How to Approach This", approach)
        concept_summary = []
        for concept, path, _, metadata, _, _ in targets:
            concept_summary.append(
                f"- {_wikilink(path.stem)} - difficulty {concept.difficulty.score}/5 "
                f"({_confidence_label(concept.difficulty.confidence)} confidence); "
                f"familiarity: {metadata.get('familiarity', 'unknown')}"
            )
        concept_summary.append(f"\n{result.diagnostic_offer}")
        assignment.content = _set_section(
            assignment.content, "Concept Analysis", "\n".join(concept_summary)
        )
        key_concepts = "\n".join(
            f"- {_wikilink(path.stem)} - {concept.summary}"
            for concept, path, _, _, _, _ in targets
        )
        assignment.content = _set_section(
            assignment.content, "Key Concepts", key_concepts
        )
        if research_hits is not None:
            assignment.content = _set_section(
                assignment.content,
                "Helpful Links",
                _research_links(research_hits),
            )
        planned[assignment_path] = frontmatter.dumps(assignment)

        solution_path = None
        solution_original = None
        if mode is AnalysisMode.EXPERT:
            solution_folder = assignment_path.parent / "Solutions"
            solution_path = solution_folder / f"{assignment_path.stem} Solution.md"
            solution_original = (
                solution_path.read_bytes() if solution_path.exists() else None
            )
            metadata = {
                "type": "solution-archive",
                "assignment": assignment_path.stem,
                "course": assignment.get("course"),
                "analysis_date": analyzed_at,
                "sources": list(assignment.get("analysis_sources") or []),
                "tags": ["solution-archive"],
            }
            body = f"# {assignment_path.stem} - Expert Solution\n\n{result.expert_solution_markdown}\n"
            planned[solution_path] = frontmatter.dumps(frontmatter.Post(body, **metadata))

        expected_originals = {
            path: original for _, path, _, _, _, original in targets
        }
        expected_originals[assignment_path] = assignment_original
        if solution_path is not None:
            expected_originals[solution_path] = solution_original
        commit_text_files(
            planned,
            lock_root=self.vault_root,
            expected_originals=expected_originals,
        )
        return CommitResult(tuple(planned), solution_path)
