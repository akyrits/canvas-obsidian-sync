from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from study_analysis.context import CompiledContext, EvidenceLocator
from study_analysis.schema import AnalysisMode, AnalysisResult, ConceptAnalysis


@dataclass(frozen=True)
class CoverageTarget:
    id: str
    label: str
    term_groups: tuple[tuple[str, ...], ...]

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "CoverageTarget":
        return cls(
            id=str(raw["id"]),
            label=str(raw["label"]),
            term_groups=tuple(
                tuple(str(alias) for alias in group) for group in raw["all"]
            ),
        )


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    minimum_concepts: int
    maximum_concepts: int
    required_topics: tuple[CoverageTarget, ...]
    assignment_difficulty: tuple[int, ...]
    assignment_effort: tuple[str, ...]
    forbidden_solution_phrases: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> "BenchmarkCase":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=str(raw["name"]),
            minimum_concepts=int(raw.get("minimum_concepts", 1)),
            maximum_concepts=int(raw.get("maximum_concepts", 6)),
            required_topics=tuple(
                CoverageTarget.parse(item) for item in raw["required_topics"]
            ),
            assignment_difficulty=tuple(
                int(value) for value in raw.get("assignment_difficulty", [])
            ),
            assignment_effort=tuple(
                str(value) for value in raw.get("assignment_effort", [])
            ),
            forbidden_solution_phrases=tuple(
                str(value) for value in raw.get("forbidden_solution_phrases", [])
            ),
        )


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class EvaluationReport:
    passed: bool
    gates: tuple[GateResult, ...]
    topic_matches: dict[str, str | None]
    resolved_citations: int
    unresolved_citations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "gates": [asdict(gate) for gate in self.gates],
            "topic_matches": self.topic_matches,
            "resolved_citations": self.resolved_citations,
            "unresolved_citations": list(self.unresolved_citations),
        }


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _concept_text(concept: ConceptAnalysis) -> str:
    return _normalized(
        " ".join(
            (
                concept.name,
                concept.summary,
                concept.why_foundational,
                concept.why_practical,
                concept.why_decision_making,
                concept.why_personal_curriculum,
                *concept.examples,
            )
        )
    )


def _target_matches(target: CoverageTarget, concept: ConceptAnalysis) -> bool:
    text = _concept_text(concept)
    return all(
        any(_normalized(alias) in text for alias in aliases)
        for aliases in target.term_groups
    )


def _distinct_topic_matches(
    targets: tuple[CoverageTarget, ...], concepts: tuple[ConceptAnalysis, ...]
) -> dict[str, str | None]:
    candidates = {
        target.id: [
            index
            for index, concept in enumerate(concepts)
            if _target_matches(target, concept)
        ]
        for target in targets
    }
    ordered = sorted(targets, key=lambda target: len(candidates[target.id]))
    assignment: dict[str, int] = {}

    def assign(position: int, used: set[int]) -> bool:
        if position == len(ordered):
            return True
        target = ordered[position]
        for index in candidates[target.id]:
            if index in used:
                continue
            assignment[target.id] = index
            if assign(position + 1, used | {index}):
                return True
            assignment.pop(target.id, None)
        return False

    complete = assign(0, set())
    if not complete:
        assignment.clear()
        used: set[int] = set()
        for target in ordered:
            available = [index for index in candidates[target.id] if index not in used]
            if available:
                assignment[target.id] = available[0]
                used.add(available[0])

    return {
        target.id: (
            concepts[assignment[target.id]].name if target.id in assignment else None
        )
        for target in targets
    }


def _citation_resolves(citation: str, locators: tuple[EvidenceLocator, ...]) -> bool:
    return any(locator.matches(citation) for locator in locators)


def _word_count(value: str) -> int:
    return len(re.findall(r"\b\w+[\w'-]*\b", value))


def _contract_errors(result: AnalysisResult) -> list[str]:
    errors: list[str] = []
    for concept in result.concepts:
        if len(concept.examples) > 2:
            errors.append(f"{concept.name}: more than 2 examples")
        if len(concept.relationships) > 4:
            errors.append(f"{concept.name}: more than 4 relationships")
        if len(concept.resources) > 3:
            errors.append(f"{concept.name}: more than 3 resources")
        if len(concept.source_citations) > 5:
            errors.append(f"{concept.name}: more than 5 citations")
        prose = (
            concept.summary,
            concept.why_foundational,
            concept.why_practical,
            concept.why_decision_making,
            concept.why_personal_curriculum,
            concept.difficulty.reason,
            *concept.examples,
            *(resource.why_useful for resource in concept.resources),
        )
        if any(_word_count(value) > 45 for value in prose):
            errors.append(f"{concept.name}: a prose field exceeds 45 words")
    if any(_word_count(step) > 45 for step in result.approach):
        errors.append("study guidance contains a step over 45 words")
    if _word_count(result.diagnostic_offer) > 45:
        errors.append("diagnostic offer exceeds 45 words")
    return errors


def evaluate(
    result: AnalysisResult,
    mode: AnalysisMode,
    compiled: CompiledContext,
    case: BenchmarkCase,
) -> EvaluationReport:
    gates: list[GateResult] = []
    concept_count_ok = case.minimum_concepts <= len(result.concepts) <= case.maximum_concepts
    gates.append(
        GateResult(
            "concept_count",
            concept_count_ok,
            f"{len(result.concepts)} concepts; expected {case.minimum_concepts}-{case.maximum_concepts}",
        )
    )

    contract_errors = _contract_errors(result)
    gates.append(
        GateResult(
            "output_contract",
            not contract_errors,
            "; ".join(contract_errors) if contract_errors else "bounded output contract passed",
        )
    )

    topic_matches = _distinct_topic_matches(case.required_topics, result.concepts)
    missing_topics = [
        target.label for target in case.required_topics if topic_matches[target.id] is None
    ]
    gates.append(
        GateResult(
            "topic_coverage",
            not missing_topics,
            "; ".join(missing_topics) if missing_topics else "all required topics matched distinctly",
        )
    )

    citations = [
        citation
        for concept in result.concepts
        for citation in concept.source_citations
    ]
    unresolved = tuple(
        citation
        for citation in citations
        if not _citation_resolves(citation, compiled.selected)
    )
    concepts_with_resolved_citation = sum(
        any(_citation_resolves(citation, compiled.selected) for citation in concept.source_citations)
        for concept in result.concepts
    )
    citation_ok = (
        bool(citations)
        and not unresolved
        and concepts_with_resolved_citation == len(result.concepts)
    )
    gates.append(
        GateResult(
            "citation_integrity",
            citation_ok,
            (
                f"{len(citations) - len(unresolved)}/{len(citations)} citations resolved; "
                f"{concepts_with_resolved_citation}/{len(result.concepts)} concepts grounded"
            ),
        )
    )

    serialized = _normalized(json.dumps(asdict(result), ensure_ascii=False))
    forbidden = [
        phrase
        for phrase in case.forbidden_solution_phrases
        if _normalized(phrase) in serialized
    ]
    study_safe = (
        mode is not AnalysisMode.STUDY
        or (result.expert_solution_markdown is None and not forbidden)
    )
    gates.append(
        GateResult(
            "study_safety",
            study_safe,
            f"forbidden solution phrases: {forbidden}" if forbidden else "no direct-solution marker found",
        )
    )

    difficulty_ok = (
        not case.assignment_difficulty
        or result.assignment_difficulty.score in case.assignment_difficulty
    )
    effort_ok = (
        not case.assignment_effort
        or result.assignment_effort.level in case.assignment_effort
    )
    gates.append(
        GateResult(
            "signal_calibration",
            difficulty_ok and effort_ok,
            (
                f"difficulty={result.assignment_difficulty.score}; "
                f"effort={result.assignment_effort.level}"
            ),
        )
    )

    return EvaluationReport(
        passed=all(gate.passed for gate in gates),
        gates=tuple(gates),
        topic_matches=topic_matches,
        resolved_citations=len(citations) - len(unresolved),
        unresolved_citations=unresolved,
    )
