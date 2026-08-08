from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .schema import (
    ALLOWED_EFFORT,
    AnalysisResult,
    AnalysisValidationError,
    normalize_concept_name,
)


def _concept_names(raw: Any, context: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise AnalysisValidationError(f"{context} must be a non-empty list")
    if len(raw) > 6:
        raise AnalysisValidationError(f"{context} cannot exceed 6 concepts")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AnalysisValidationError(f"{context} must contain non-empty strings")
        name = item.strip()
        if "\n" in name or "\r" in name:
            raise AnalysisValidationError(f"{context} concept names must be single-line")
        names.append(name)
    normalized = [normalize_concept_name(name) for name in names]
    if len(normalized) != len(set(normalized)):
        raise AnalysisValidationError(f"{context} contains duplicate concept names")
    return tuple(names)


def _difficulty_values(raw: Any) -> tuple[int, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise AnalysisValidationError(
            "analysis_contract.assignment_difficulty must be a non-empty list"
        )
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 5
        for value in raw
    ):
        raise AnalysisValidationError(
            "analysis_contract.assignment_difficulty values must be integers from 1 to 5"
        )
    return tuple(dict.fromkeys(raw))


def _effort_values(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise AnalysisValidationError(
            "analysis_contract.assignment_effort must be a non-empty list"
        )
    if any(not isinstance(value, str) or value not in ALLOWED_EFFORT for value in raw):
        raise AnalysisValidationError(
            "analysis_contract.assignment_effort contains an unsupported value"
        )
    return tuple(dict.fromkeys(raw))


@dataclass(frozen=True)
class AnalysisContract:
    """Trusted, compact acceptance criteria for one assignment analysis."""

    required_concepts: tuple[str, ...]
    allowed_difficulty: tuple[int, ...] = ()
    allowed_effort: tuple[str, ...] = ()
    source: str = "explicit"

    @classmethod
    def from_assignment(
        cls, metadata: Mapping[str, Any]
    ) -> "AnalysisContract | None":
        explicit = metadata.get("analysis_contract")
        if explicit is None:
            return None
        if not isinstance(explicit, dict):
            raise AnalysisValidationError("analysis_contract must be an object")
        if explicit.get("version") != 1:
            raise AnalysisValidationError("analysis_contract.version must be 1")
        return cls(
            required_concepts=_concept_names(
                explicit.get("required_concepts"),
                "analysis_contract.required_concepts",
            ),
            allowed_difficulty=_difficulty_values(
                explicit.get("assignment_difficulty")
            ),
            allowed_effort=_effort_values(explicit.get("assignment_effort")),
            source="explicit",
        )

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            {
                "version": 1,
                "required_concepts": self.required_concepts,
                "assignment_difficulty": self.allowed_difficulty,
                "assignment_effort": self.allowed_effort,
                "source": self.source,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def prompt_block(self) -> str:
        lines = [
            "Trusted assignment analysis contract:",
            "- Return exactly one concept for each canonical name below.",
            "- Copy each canonical name exactly; do not rename, merge, split, or omit it.",
            "- Use exact canonical names for relationship targets; do not abbreviate them.",
        ]
        lines.extend(f"  - {name}" for name in self.required_concepts)
        if self.allowed_difficulty:
            lines.append(
                "- Allowed assignment difficulty scores: "
                + ", ".join(str(value) for value in self.allowed_difficulty)
            )
        if self.allowed_effort:
            lines.append(
                "- Allowed assignment effort levels: "
                + ", ".join(self.allowed_effort)
            )
        return "\n".join(lines)

    def validate(
        self, result: AnalysisResult, resolved_concepts: Sequence[str]
    ) -> None:
        expected = {
            normalize_concept_name(name): name for name in self.required_concepts
        }
        actual = {normalize_concept_name(name): name for name in resolved_concepts}
        missing = [expected[key] for key in expected.keys() - actual.keys()]
        unexpected = [actual[key] for key in actual.keys() - expected.keys()]
        if missing or unexpected or len(resolved_concepts) != len(self.required_concepts):
            raise AnalysisValidationError(
                "analysis violates the curated analysis contract concept set; "
                f"missing={sorted(missing)}; "
                f"unexpected={sorted(unexpected)}"
            )
        if (
            self.allowed_difficulty
            and result.assignment_difficulty.score not in self.allowed_difficulty
        ):
            raise AnalysisValidationError(
                "assignment difficulty is outside the analysis contract"
            )
        if self.allowed_effort and result.assignment_effort.level not in self.allowed_effort:
            raise AnalysisValidationError(
                "assignment effort is outside the analysis contract"
            )
