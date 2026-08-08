from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AnalysisValidationError(ValueError):
    """Raised before any vault file is changed when model output is invalid."""


class AnalysisMode(str, Enum):
    STUDY = "study"
    EXPERT = "expert"


ALLOWED_RELATIONSHIPS = {
    "prerequisite_of",
    "builds_on",
    "applies_to",
    "contrasts_with",
    "example_of",
    "used_in",
    "related_to",
}


def normalize_concept_name(name: str) -> str:
    """Return the single identity key used by validation and vault matching."""
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
ALLOWED_EFFORT = {"unknown", "small", "medium", "large", "very_large"}


def analysis_json_schema() -> dict[str, Any]:
    """JSON-output grammar; domain constraints remain enforced by ``parse`` below."""
    rated_signal = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "description": "Integer from 1 to 5."},
            "reason": {"type": "string"},
            "confidence": {"type": "number", "description": "Number from 0 to 1."},
        },
        "required": ["score", "reason", "confidence"],
        "additionalProperties": False,
    }
    resource = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "locator": {
                "type": "string",
                "description": "Exact locator copied from supplied indexed evidence; never a URL.",
            },
            "why_useful": {"type": "string"},
            "accessed_at": {"type": "string"},
        },
        "required": ["title", "locator", "why_useful", "accessed_at"],
        "additionalProperties": False,
    }
    relationship = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": sorted(ALLOWED_RELATIONSHIPS)},
            "target": {"type": "string"},
        },
        "required": ["type", "target"],
        "additionalProperties": False,
    }
    concept = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "summary": {"type": "string"},
            "why_this_matters": {
                "type": "object",
                "properties": {
                    "foundational": {"type": "string"},
                    "practical": {"type": "string"},
                    "decision_making": {"type": "string"},
                    "personal_curriculum": {"type": "string"},
                },
                "required": [
                    "foundational",
                    "practical",
                    "decision_making",
                    "personal_curriculum",
                ],
                "additionalProperties": False,
            },
            "difficulty": rated_signal,
            "relationships": {
                "type": "array",
                "items": relationship,
            },
            "examples": {
                "type": "array",
                "items": {"type": "string"},
            },
            "resources": {"type": "array", "items": resource},
            "source_citations": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "name",
            "summary",
            "why_this_matters",
            "difficulty",
            "relationships",
            "examples",
            "resources",
            "source_citations",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "assignment_difficulty": rated_signal,
            "assignment_effort": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": sorted(ALLOWED_EFFORT)},
                    "reason": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "description": "Number from 0 to 1.",
                    },
                },
                "required": ["level", "reason", "confidence"],
                "additionalProperties": False,
            },
            "concepts": {
                "type": "array",
                "items": concept,
            },
            "study_guidance": {
                "type": "object",
                "properties": {
                    "approach": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "diagnostic_offer": {"type": "string"},
                },
                "required": ["approach", "diagnostic_offer"],
                "additionalProperties": False,
            },
            "expert_solution_markdown": {"type": ["string", "null"]},
        },
        "required": [
            "assignment_difficulty",
            "assignment_effort",
            "concepts",
            "study_guidance",
            "expert_solution_markdown",
        ],
        "additionalProperties": False,
    }


def _required(mapping: dict[str, Any], key: str, expected: type, context: str) -> Any:
    value = mapping.get(key)
    if not isinstance(value, expected):
        raise AnalysisValidationError(f"{context}.{key} must be {expected.__name__}")
    return value


def _text(mapping: dict[str, Any], key: str, context: str) -> str:
    value = _required(mapping, key, str, context).strip()
    if not value:
        raise AnalysisValidationError(f"{context}.{key} cannot be empty")
    return value


def _confidence(mapping: dict[str, Any], context: str) -> float:
    value = mapping.get("confidence")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AnalysisValidationError(f"{context}.confidence must be a number")
    value = float(value)
    if not 0 <= value <= 1:
        raise AnalysisValidationError(f"{context}.confidence must be between 0 and 1")
    return value


@dataclass(frozen=True)
class RatedSignal:
    score: int
    reason: str
    confidence: float

    @classmethod
    def parse(cls, raw: Any, context: str) -> "RatedSignal":
        if not isinstance(raw, dict):
            raise AnalysisValidationError(f"{context} must be an object")
        score = raw.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
            raise AnalysisValidationError(f"{context}.score must be an integer from 1 to 5")
        return cls(score, _text(raw, "reason", context), _confidence(raw, context))


@dataclass(frozen=True)
class EffortSignal:
    level: str
    reason: str
    confidence: float

    @classmethod
    def parse(cls, raw: Any) -> "EffortSignal":
        if not isinstance(raw, dict):
            raise AnalysisValidationError("assignment_effort must be an object")
        level = _text(raw, "level", "assignment_effort")
        if level not in ALLOWED_EFFORT:
            raise AnalysisValidationError(
                f"assignment_effort.level must be one of {sorted(ALLOWED_EFFORT)}"
            )
        return cls(
            level,
            _text(raw, "reason", "assignment_effort"),
            _confidence(raw, "assignment_effort"),
        )


@dataclass(frozen=True)
class Relationship:
    kind: str
    target: str

    @classmethod
    def parse(cls, raw: Any, context: str) -> "Relationship":
        if not isinstance(raw, dict):
            raise AnalysisValidationError(f"{context} must be an object")
        kind = _text(raw, "type", context)
        if kind not in ALLOWED_RELATIONSHIPS:
            raise AnalysisValidationError(
                f"{context}.type must be one of {sorted(ALLOWED_RELATIONSHIPS)}"
            )
        return cls(kind, _text(raw, "target", context))


@dataclass(frozen=True)
class Resource:
    title: str
    locator: str
    why_useful: str
    accessed_at: str

    @classmethod
    def parse(cls, raw: Any, context: str) -> "Resource":
        if not isinstance(raw, dict):
            raise AnalysisValidationError(f"{context} must be an object")
        return cls(
            _text(raw, "title", context),
            _text(raw, "locator", context),
            _text(raw, "why_useful", context),
            _text(raw, "accessed_at", context),
        )


@dataclass(frozen=True)
class ConceptAnalysis:
    name: str
    summary: str
    why_foundational: str
    why_practical: str
    why_decision_making: str
    why_personal_curriculum: str
    difficulty: RatedSignal
    relationships: tuple[Relationship, ...]
    examples: tuple[str, ...]
    resources: tuple[Resource, ...]
    source_citations: tuple[str, ...]

    @classmethod
    def parse(cls, raw: Any, index: int) -> "ConceptAnalysis":
        context = f"concepts[{index}]"
        if not isinstance(raw, dict):
            raise AnalysisValidationError(f"{context} must be an object")
        why = _required(raw, "why_this_matters", dict, context)
        relationships = _required(raw, "relationships", list, context)
        examples = _required(raw, "examples", list, context)
        resources = _required(raw, "resources", list, context)
        citations = _required(raw, "source_citations", list, context)
        if len(relationships) > 4:
            raise AnalysisValidationError(f"{context}.relationships cannot exceed 4")
        if len(examples) > 2:
            raise AnalysisValidationError(f"{context}.examples cannot exceed 2")
        if len(resources) > 3:
            raise AnalysisValidationError(f"{context}.resources cannot exceed 3")
        if len(citations) > 5:
            raise AnalysisValidationError(
                f"{context}.source_citations cannot exceed 5"
            )
        if not all(isinstance(item, str) and item.strip() for item in examples):
            raise AnalysisValidationError(f"{context}.examples must contain non-empty strings")
        if not all(isinstance(item, str) and item.strip() for item in citations):
            raise AnalysisValidationError(
                f"{context}.source_citations must contain non-empty strings"
            )
        return cls(
            name=_text(raw, "name", context),
            summary=_text(raw, "summary", context),
            why_foundational=_text(why, "foundational", f"{context}.why_this_matters"),
            why_practical=_text(why, "practical", f"{context}.why_this_matters"),
            why_decision_making=_text(
                why, "decision_making", f"{context}.why_this_matters"
            ),
            why_personal_curriculum=_text(
                why, "personal_curriculum", f"{context}.why_this_matters"
            ),
            difficulty=RatedSignal.parse(raw.get("difficulty"), f"{context}.difficulty"),
            relationships=tuple(
                Relationship.parse(item, f"{context}.relationships[{i}]")
                for i, item in enumerate(relationships)
            ),
            examples=tuple(item.strip() for item in examples),
            resources=tuple(
                Resource.parse(item, f"{context}.resources[{i}]")
                for i, item in enumerate(resources)
            ),
            source_citations=tuple(item.strip() for item in citations),
        )


@dataclass(frozen=True)
class AnalysisResult:
    assignment_difficulty: RatedSignal
    assignment_effort: EffortSignal
    concepts: tuple[ConceptAnalysis, ...]
    approach: tuple[str, ...]
    diagnostic_offer: str
    expert_solution_markdown: str | None

    @classmethod
    def parse(cls, raw: Any, mode: AnalysisMode) -> "AnalysisResult":
        if not isinstance(raw, dict):
            raise AnalysisValidationError("analysis output must be a JSON object")
        concept_items = _required(raw, "concepts", list, "analysis")
        if not concept_items:
            raise AnalysisValidationError("analysis.concepts cannot be empty")
        if len(concept_items) > 6:
            raise AnalysisValidationError("analysis.concepts cannot exceed 6")
        guidance = _required(raw, "study_guidance", dict, "analysis")
        approach = _required(guidance, "approach", list, "study_guidance")
        if not approach or not all(isinstance(item, str) and item.strip() for item in approach):
            raise AnalysisValidationError(
                "study_guidance.approach must contain non-empty strings"
            )
        if len(approach) > 10:
            raise AnalysisValidationError("study_guidance.approach cannot exceed 10")
        solution = raw.get("expert_solution_markdown")
        if mode is AnalysisMode.STUDY and solution not in (None, ""):
            raise AnalysisValidationError("study mode cannot contain a completed solution")
        if mode is AnalysisMode.EXPERT and not isinstance(solution, str):
            raise AnalysisValidationError("expert mode requires expert_solution_markdown")
        if isinstance(solution, str):
            solution = solution.strip() or None
        if mode is AnalysisMode.EXPERT and not solution:
            raise AnalysisValidationError("expert mode requires a non-empty completed solution")

        concepts = tuple(
            ConceptAnalysis.parse(item, i) for i, item in enumerate(concept_items)
        )
        normalized = [normalize_concept_name(c.name) for c in concepts]
        if len(normalized) != len(set(normalized)):
            raise AnalysisValidationError("analysis contains duplicate concept names")

        return cls(
            assignment_difficulty=RatedSignal.parse(
                raw.get("assignment_difficulty"), "assignment_difficulty"
            ),
            assignment_effort=EffortSignal.parse(raw.get("assignment_effort")),
            concepts=concepts,
            approach=tuple(item.strip() for item in approach),
            diagnostic_offer=_text(guidance, "diagnostic_offer", "study_guidance"),
            expert_solution_markdown=solution,
        )
