from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import frontmatter

from vault_notes import _sanitize_filename

from .schema import normalize_concept_name
from .transaction import TransactionConflictError, commit_text_files


class DiagnosticValidationError(ValueError):
    """The diagnostic request or evidence cannot be safely recorded."""


class DiagnosticConflictError(DiagnosticValidationError):
    """The concept or diagnostic state changed after the plan was prepared."""


class Familiarity(str, Enum):
    UNKNOWN = "unknown"
    RECOGNIZES = "recognizes"
    EXPLAINS = "explains"
    APPLIES = "applies"
    TRANSFERS = "transfers"


class ObservationResult(str, Enum):
    DEMONSTRATED = "demonstrated"
    PARTIAL = "partial"
    NOT_YET = "not_yet"


class EvidenceKind(str, Enum):
    OWN_DEFINITION = "own_definition"
    CAUSAL_EXPLANATION = "causal_explanation"
    NOVEL_APPLICATION = "novel_application"
    TRANSFER_OR_CORRECTION = "transfer_or_correction"


_CAPABILITIES = (
    Familiarity.RECOGNIZES,
    Familiarity.EXPLAINS,
    Familiarity.APPLIES,
    Familiarity.TRANSFERS,
)
_FAMILIARITY_RANK = {
    Familiarity.UNKNOWN: 0,
    **{level: index for index, level in enumerate(_CAPABILITIES, 1)},
}
_SEMANTIC_SECTIONS = (
    "Definition",
    "Why This Matters",
    "Connections",
    "Examples",
)
_RECORD_ID_RE = re.compile(r"diag-[0-9a-f]{24}")
_AMENDMENT_ID_RE = re.compile(r"amend-[0-9a-f]{24}")
_UNSAFE_MARKDOWN_RE = re.compile(
    r"javascript:|data:|file:|obsidian:|<script|<iframe|<object|<embed",
    re.IGNORECASE,
)
_ALLOWED_SOURCES = {"voice", "text", "scripted"}
_MIN_DEMONSTRATED_CONFIDENCE = 0.6
_PROHIBITED_EVIDENCE_RE = re.compile(
    r"\b(?:grades?|gpa|points?|instructor feedback|professor (?:said|gave)|"
    r"assignment (?:completion|completed)|completed (?:the|an|this) assignment|"
    r"got (?:an? )?[abcdf][+-]?|(?:scored?|earned|received) (?:an? )?"
    r"(?:\d{1,3}(?:\.\d+)?%|[abcdf][+-]?)|"
    r"(?:homework|quiz|exam|test|assignment) (?:score|grade))\b",
    re.IGNORECASE,
)
_PROHIBITED_SCORE_RE = re.compile(
    r"\b(?:scored?|earned|received)\s+(?:an?\s+)?\d{1,3}(?:\.\d+)?%"
    r"(?=\s|$)",
    re.IGNORECASE,
)
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _safe_directory_name(name: str) -> str:
    value = _sanitize_filename(name).strip().rstrip(". ")
    if (
        not value
        or value in {".", ".."}
        or value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
    ):
        raise DiagnosticValidationError(
            f"Concept name cannot form a safe diagnostic directory: {name!r}"
        )
    return value


def _bounded_text(value: Any, context: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiagnosticValidationError(f"{context} must be non-empty text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise DiagnosticValidationError(f"{context} cannot exceed {limit} characters")
    if _UNSAFE_MARKDOWN_RE.search(text):
        raise DiagnosticValidationError(f"{context} contains unsafe Markdown content")
    return text


def _evidence_summary(value: Any) -> str:
    text = _bounded_text(value, "diagnostic evidence summary", 300)
    if _PROHIBITED_EVIDENCE_RE.search(text) or _PROHIBITED_SCORE_RE.search(text):
        raise DiagnosticValidationError(
            "Diagnostic evidence must describe the learner's current answer, not "
            "grades, assignment completion, or instructor feedback"
        )
    return text


def _markdown_plaintext(value: str) -> str:
    """Render validated one-line evidence without enabling Markdown constructs."""
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", value)


def _confidence(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagnosticValidationError(f"{context} must be a number from 0 to 1")
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise DiagnosticValidationError(f"{context} must be a number from 0 to 1")
    return parsed


def _enum_value(enum_type: type[Enum], value: Any, context: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise DiagnosticValidationError(f"{context} is unsupported: {value!r}") from exc


@dataclass(frozen=True)
class DiagnosticRequest:
    concept: str
    target: Familiarity | None = None


@dataclass(frozen=True)
class DiagnosticPrompt:
    id: str
    capability: Familiarity
    evidence_kind: EvidenceKind
    question: str
    evidence_rule: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capability": self.capability.value,
            "evidence_kind": self.evidence_kind.value,
            "question": self.question,
            "evidence_rule": self.evidence_rule,
        }



@dataclass(frozen=True)
class DiagnosticPlan:
    id: str
    canonical_concept: str
    current_familiarity: Familiarity
    evidence_stale: bool
    prompts: tuple[DiagnosticPrompt, ...]
    target: Familiarity | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "canonical_concept": self.canonical_concept,
            "current_familiarity": self.current_familiarity.value,
            "evidence_stale": self.evidence_stale,
            "prompts": [prompt.to_dict() for prompt in self.prompts],
            "target": self.target.value if self.target else None,
        }


@dataclass(frozen=True)
class DiagnosticObservation:
    prompt_id: str
    evidence_kind: EvidenceKind
    result: ObservationResult
    confidence: float
    evidence_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "evidence_kind": self.evidence_kind.value,
            "result": self.result.value,
            "confidence": self.confidence,
            "evidence_summary": self.evidence_summary,
        }



@dataclass(frozen=True)
class DiagnosticSubmission:
    plan_id: str
    canonical_concept: str
    observations: tuple[DiagnosticObservation, ...]
    source: str = "voice"
    confirmed_by_user: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "canonical_concept": self.canonical_concept,
            "observations": [item.to_dict() for item in self.observations],
            "source": self.source,
            "confirmed_by_user": self.confirmed_by_user,
        }



@dataclass(frozen=True)
class DiagnosticOutcome:
    canonical_concept: str
    familiarity: Familiarity
    confidence: float | None
    assessed_at: str | None
    evidence_stale: bool
    record_id: str
    changed_files: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_concept": self.canonical_concept,
            "familiarity": self.familiarity.value,
            "confidence": self.confidence,
            "assessed_at": self.assessed_at,
            "evidence_stale": self.evidence_stale,
            "record_id": self.record_id,
            "changed_file_count": len(self.changed_files),
        }


@dataclass(frozen=True)
class DiagnosticCorrection:
    canonical_concept: str
    record_id: str
    correction: str
    confirmed_by_user: bool = False


@dataclass(frozen=True)
class DiagnosticCorrectionOutcome:
    canonical_concept: str
    record_id: str
    amendment_id: str
    reassessment_due: bool
    changed_files: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_concept": self.canonical_concept,
            "record_id": self.record_id,
            "amendment_id": self.amendment_id,
            "reassessment_due": self.reassessment_due,
            "changed_file_count": len(self.changed_files),
        }


class DiagnosticConversation(Protocol):
    """Trusted human-facing adapter; raw answers stay inside the adapter."""

    source: str

    def assess(self, plan: DiagnosticPlan) -> Sequence[DiagnosticObservation]: ...

    def confirm(
        self,
        plan: DiagnosticPlan,
        observations: Sequence[DiagnosticObservation],
    ) -> bool: ...


@dataclass(frozen=True)
class ScriptedDiagnosticConversation:
    """Deterministic adapter for tests and pre-reviewed human assessments."""

    observations: tuple[DiagnosticObservation, ...]
    confirmed_by_user: bool = False
    source: str = "scripted"

    def assess(self, plan: DiagnosticPlan) -> Sequence[DiagnosticObservation]:
        del plan
        return self.observations

    def confirm(
        self,
        plan: DiagnosticPlan,
        observations: Sequence[DiagnosticObservation],
    ) -> bool:
        del plan, observations
        return self.confirmed_by_user


def _extract_section(content: str, header: str) -> str:
    lines = content.splitlines()
    target = f"## {header}"
    start = next((i for i, line in enumerate(lines) if line.strip() == target), None)
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start + 1 : end]).strip()


def concept_revision(post: frontmatter.Post, canonical_name: str | None = None) -> str:
    """Hash only managed semantic evidence, excluding Personal Notes and timestamps."""
    canonical = str(canonical_name or post.get("canonical_name") or "").strip()
    payload = {
        "version": 2,
        "canonical_name": canonical,
        "sections": {
            header: _extract_section(post.content, header)
            for header in _SEMANTIC_SECTIONS
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def familiarity_is_stale(
    post: frontmatter.Post, canonical_name: str | None = None
) -> bool:
    level = _familiarity(post)
    if level is Familiarity.UNKNOWN:
        return False
    if post.get("familiarity_review_required") is True:
        return True
    basis = str(post.get("familiarity_basis_sha256") or "").strip().casefold()
    return basis != concept_revision(post, canonical_name)


def _familiarity(post: frontmatter.Post) -> Familiarity:
    value = str(post.get("familiarity") or Familiarity.UNKNOWN.value).strip().casefold()
    return _enum_value(Familiarity, value, "concept familiarity")


def _review_epoch(post: frontmatter.Post) -> str | None:
    if post.get("familiarity_review_required") is not True:
        return None
    value = post.get("familiarity_review_epoch")
    if not isinstance(value, str) or not _AMENDMENT_ID_RE.fullmatch(value):
        raise DiagnosticValidationError(
            "A familiarity review requires a valid correction epoch"
        )
    return value


class DiagnosticEngine:
    """Prepare and persist one token-free, evidence-backed concept diagnostic."""

    def __init__(
        self,
        vault_root: Path,
        now: Callable[[], datetime] | None = None,
        log_path: Path | None = None,
    ):
        self.vault_root = Path(vault_root).resolve()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.log_path = Path(log_path) if log_path is not None else None

    def prepare(self, request: DiagnosticRequest) -> DiagnosticPlan:
        if not isinstance(request, DiagnosticRequest):
            raise DiagnosticValidationError("Diagnostic request is invalid")
        if request.target is not None and not isinstance(request.target, Familiarity):
            raise DiagnosticValidationError("Diagnostic request has an invalid target")
        _, _, post, canonical = self._resolve_concept(request.concept)
        return self._prepare_from_post(post, canonical, request.target)

    def diagnose(
        self,
        request: DiagnosticRequest,
        conversation: DiagnosticConversation,
    ) -> DiagnosticOutcome:
        """Run one confirmed diagnostic without exposing persistence mechanics."""
        plan: DiagnosticPlan | None = None
        try:
            plan = self.prepare(request)
            source = str(getattr(conversation, "source", "")).strip().casefold()
            if source not in _ALLOWED_SOURCES:
                raise DiagnosticValidationError(
                    f"diagnostic source must be one of {sorted(_ALLOWED_SOURCES)}"
                )
            observations = tuple(conversation.assess(plan))
            confirmed = conversation.confirm(plan, observations)
            if confirmed is not True:
                raise DiagnosticValidationError(
                    "Diagnostic evidence was not explicitly confirmed by the user"
                )
            outcome = self._record(
                DiagnosticSubmission(
                    plan_id=plan.id,
                    canonical_concept=plan.canonical_concept,
                    observations=observations,
                    source=source,
                    confirmed_by_user=True,
                )
            )
        except Exception as exc:
            self._log(
                "diagnose-concept",
                "failure",
                plan.canonical_concept
                if plan
                else str(getattr(request, "concept", "invalid")),
                plan_id=plan.id if plan else None,
                error=exc,
            )
            raise
        self._log(
            "diagnose-concept",
            "success",
            outcome.canonical_concept,
            plan_id=plan.id,
            record_id=outcome.record_id,
            outcome=outcome,
        )
        return outcome

    def _prepare_from_post(
        self,
        post: frontmatter.Post,
        canonical: str,
        target: Familiarity | None,
    ) -> DiagnosticPlan:
        revision = concept_revision(post, canonical)
        state_sha256 = self._diagnostic_state_sha256(post, canonical)
        current = _familiarity(post)
        stale = familiarity_is_stale(post, canonical)
        review_epoch = _review_epoch(post)
        fresh, _ = self._fresh_contiguous(
            post,
            canonical,
            revision,
            review_epoch=review_epoch,
        )
        if review_epoch is not None:
            baseline_rank = _FAMILIARITY_RANK[fresh]
        else:
            baseline_rank = (
                _FAMILIARITY_RANK[fresh]
                if stale
                else max(_FAMILIARITY_RANK[current], _FAMILIARITY_RANK[fresh])
            )
        capabilities = self._select_capabilities(baseline_rank, target)
        connection = self._connection_hint(post)
        prompts = tuple(
            self._prompt_for(capability, canonical, connection)
            for capability in capabilities
        )
        plan_id = self._plan_id(
            canonical,
            revision,
            state_sha256,
            current,
            stale,
            prompts,
            target,
        )
        return DiagnosticPlan(
            id=plan_id,
            canonical_concept=canonical,
            current_familiarity=current,
            evidence_stale=stale,
            prompts=prompts,
            target=target,
        )

    def _plan_from_id(
        self,
        post: frontmatter.Post,
        canonical: str,
        plan_id: str,
    ) -> DiagnosticPlan | None:
        matches: list[DiagnosticPlan] = []
        for target in (None, *_CAPABILITIES):
            try:
                candidate = self._prepare_from_post(post, canonical, target)
            except DiagnosticValidationError:
                continue
            if candidate.id == plan_id:
                matches.append(candidate)
        if len(matches) > 1:
            raise DiagnosticValidationError(
                "Diagnostic plan identity is ambiguous"
            )
        return matches[0] if matches else None

    def record(self, submission: DiagnosticSubmission) -> DiagnosticOutcome:
        try:
            outcome = self._record(submission)
        except Exception as exc:
            self._log(
                "record-diagnostic",
                "failure",
                str(getattr(submission, "canonical_concept", "invalid")),
                plan_id=str(getattr(submission, "plan_id", "")) or None,
                error=exc,
            )
            raise
        self._log(
            "record-diagnostic",
            "success",
            outcome.canonical_concept,
            plan_id=submission.plan_id,
            record_id=outcome.record_id,
            outcome=outcome,
        )
        return outcome

    def _record(self, submission: DiagnosticSubmission) -> DiagnosticOutcome:
        submission = self._normalized_submission(submission)
        concept_path, concept_original, concept, canonical = self._resolve_concept(
            submission.canonical_concept
        )
        submission_sha256 = self._submission_sha256(submission)
        record_id = f"diag-{submission_sha256[:24]}"
        diagnostics_root = self._confined_root("Knowledge", "Diagnostics")
        record_path = (
            diagnostics_root
            / _safe_directory_name(canonical)
            / f"{record_id}.md"
        ).resolve()
        try:
            record_path.relative_to(diagnostics_root)
        except ValueError as exc:
            raise DiagnosticValidationError(
                f"Diagnostic record path escapes {diagnostics_root}: {record_path}"
            ) from exc
        existing_refs = self._record_references(concept)
        relative_record = record_path.relative_to(self.vault_root).as_posix()
        if record_path.exists():
            record = frontmatter.load(record_path)
            if (
                record.get("type") != "diagnostic-record"
                or record.get("record_id") != record_id
                or record.get("submission_sha256") != submission_sha256
                or record.get("concept") != canonical
                or relative_record not in existing_refs
            ):
                raise DiagnosticConflictError(
                    f"Diagnostic record identity conflicts with {record_path}"
                )
            return self._outcome(
                concept,
                canonical,
                record_id,
                (),
            )

        expected_plan = self._plan_from_id(
            concept, canonical, submission.plan_id
        )
        if expected_plan is None:
            raise DiagnosticConflictError(
                "Concept or diagnostic evidence changed after the plan was prepared"
            )
        self._validate_observations(expected_plan, submission.observations)

        current_revision = concept_revision(concept, canonical)
        prompt_by_id = {prompt.id: prompt for prompt in expected_plan.prompts}
        demonstrated = {
            prompt_by_id[item.prompt_id].capability.value: item.confidence
            for item in submission.observations
            if item.result is ObservationResult.DEMONSTRATED
            and item.confidence >= _MIN_DEMONSTRATED_CONFIDENCE
        }
        review_epoch = _review_epoch(concept)
        fresh_level, fresh_confidence = self._fresh_contiguous(
            concept,
            canonical,
            current_revision,
            demonstrated,
            review_epoch=review_epoch,
        )
        old_level = _familiarity(concept)
        old_rank = _FAMILIARITY_RANK[old_level]
        fresh_rank = _FAMILIARITY_RANK[fresh_level]
        old_stale = familiarity_is_stale(concept, canonical)
        observed_at = self._timestamp()

        metadata = dict(concept.metadata)
        metadata["diagnostic_records"] = [*existing_refs, relative_record]
        metadata["diagnostic_last_at"] = observed_at
        if fresh_rank > old_rank or (fresh_rank == old_rank > 0 and old_stale):
            metadata["familiarity"] = fresh_level.value
            metadata["familiarity_confidence"] = fresh_confidence
            metadata["familiarity_assessed_at"] = observed_at
            metadata["familiarity_basis_sha256"] = current_revision
            metadata["familiarity_review_required"] = False
            metadata.pop("familiarity_review_epoch", None)
        else:
            metadata.setdefault("familiarity", old_level.value)

        updated_concept = frontmatter.Post(concept.content, **metadata)
        record_post = self._record_post(
            submission,
            expected_plan,
            canonical,
            record_id,
            submission_sha256,
            current_revision,
            observed_at,
            demonstrated,
            fresh_level,
            fresh_confidence,
            review_epoch,
        )
        try:
            changed = commit_text_files(
                {
                    record_path: frontmatter.dumps(record_post),
                    concept_path: frontmatter.dumps(updated_concept),
                },
                lock_root=self.vault_root,
                expected_originals={
                    record_path: None,
                    concept_path: concept_original,
                },
            )
        except TransactionConflictError as exc:
            raise DiagnosticConflictError(str(exc)) from exc
        return self._outcome(
            updated_concept,
            canonical,
            record_id,
            changed,
        )

    def correct(
        self, correction: DiagnosticCorrection
    ) -> DiagnosticCorrectionOutcome:
        try:
            outcome = self._correct(correction)
        except Exception as exc:
            self._log(
                "correct-diagnostic",
                "failure",
                str(getattr(correction, "canonical_concept", "invalid")),
                record_id=str(getattr(correction, "record_id", "")) or None,
                error=exc,
            )
            raise
        self._log(
            "correct-diagnostic",
            "success",
            outcome.canonical_concept,
            record_id=outcome.record_id,
            correction_outcome=outcome,
        )
        return outcome

    def _correct(
        self, correction: DiagnosticCorrection
    ) -> DiagnosticCorrectionOutcome:
        """Append a confirmed correction without rewriting diagnostic evidence."""
        if not isinstance(correction, DiagnosticCorrection):
            raise DiagnosticValidationError("Diagnostic correction is invalid")
        canonical_input = _bounded_text(
            correction.canonical_concept, "diagnostic canonical concept", 160
        )
        record_id = _bounded_text(
            correction.record_id, "diagnostic record id", 80
        )
        if not _RECORD_ID_RE.fullmatch(record_id):
            raise DiagnosticValidationError("Diagnostic record id is invalid")
        if correction.confirmed_by_user is not True:
            raise DiagnosticValidationError(
                "Diagnostic correction must be explicitly confirmed by the user"
            )
        text = _bounded_text(correction.correction, "diagnostic correction", 400)

        concept_path, concept_original, concept, canonical = self._resolve_concept(
            canonical_input
        )
        if canonical_input != canonical:
            raise DiagnosticValidationError(
                "Diagnostic correction must use the exact canonical concept name"
            )
        record_path = None
        for candidate in self._record_paths(concept):
            record = frontmatter.load(candidate)
            if record.get("record_id") == record_id:
                if (
                    record.get("type") != "diagnostic-record"
                    or record.get("concept") != canonical
                ):
                    raise DiagnosticValidationError(
                        f"Diagnostic record identity is invalid: {candidate}"
                    )
                record_path = candidate
                break
        if record_path is None:
            raise DiagnosticValidationError(
                f"Diagnostic record is not linked to {canonical}: {record_id}"
            )

        correction_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "version": 1,
                    "concept": canonical,
                    "record_id": record_id,
                    "correction": text,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        amendment_id = f"amend-{correction_sha256[:24]}"
        amendment_path = (
            record_path.parent / f"{record_id}-{amendment_id}.md"
        ).resolve()
        diagnostics_root = self._confined_root("Knowledge", "Diagnostics")
        try:
            amendment_path.relative_to(diagnostics_root)
        except ValueError as exc:
            raise DiagnosticValidationError(
                f"Diagnostic amendment path escapes {diagnostics_root}"
            ) from exc
        relative_amendment = amendment_path.relative_to(self.vault_root).as_posix()
        amendment_refs = self._amendment_references(concept)
        if amendment_path.exists():
            amendment = frontmatter.load(amendment_path)
            if (
                amendment.get("type") != "diagnostic-amendment"
                or amendment.get("amendment_id") != amendment_id
                or amendment.get("correction_sha256") != correction_sha256
                or amendment.get("record_id") != record_id
                or amendment.get("concept") != canonical
                or relative_amendment not in amendment_refs
            ):
                raise DiagnosticConflictError(
                    f"Diagnostic amendment identity conflicts with {amendment_path}"
                )
            return DiagnosticCorrectionOutcome(
                canonical,
                record_id,
                amendment_id,
                familiarity_is_stale(concept, canonical),
                (),
            )

        corrected_at = self._timestamp()
        metadata = dict(concept.metadata)
        metadata["diagnostic_amendments"] = [
            *amendment_refs,
            relative_amendment,
        ]
        metadata["diagnostic_correction_last_at"] = corrected_at
        metadata["familiarity_review_required"] = True
        metadata["familiarity_review_epoch"] = amendment_id
        updated_concept = frontmatter.Post(concept.content, **metadata)
        amendment = frontmatter.Post(
            f"# {_markdown_plaintext(canonical)} Diagnostic Amendment\n\n"
            f"## Correction\n\n{_markdown_plaintext(text)}\n",
            type="diagnostic-amendment",
            amendment_id=amendment_id,
            correction_sha256=correction_sha256,
            concept=canonical,
            record_id=record_id,
            corrected_at=corrected_at,
            tags=["diagnostic", "amendment"],
        )
        try:
            changed = commit_text_files(
                {
                    amendment_path: frontmatter.dumps(amendment),
                    concept_path: frontmatter.dumps(updated_concept),
                },
                lock_root=self.vault_root,
                expected_originals={
                    amendment_path: None,
                    concept_path: concept_original,
                },
            )
        except TransactionConflictError as exc:
            raise DiagnosticConflictError(str(exc)) from exc
        return DiagnosticCorrectionOutcome(
            canonical,
            record_id,
            amendment_id,
            familiarity_is_stale(updated_concept, canonical),
            changed,
        )

    def _resolve_concept(
        self, name: str
    ) -> tuple[Path, bytes, frontmatter.Post, str]:
        wanted = normalize_concept_name(
            _bounded_text(name, "diagnostic concept", 160)
        )
        concepts_root = self._confined_root("Knowledge", "Concepts")
        matches: list[tuple[Path, bytes, frontmatter.Post, str]] = []
        if concepts_root.is_dir():
            for path in concepts_root.glob("*.md"):
                if path.is_symlink():
                    raise DiagnosticValidationError(
                        f"Concept notes cannot be symbolic links: {path}"
                    )
                try:
                    original = path.read_bytes()
                    post = frontmatter.loads(original.decode("utf-8"))
                    raw_canonical = post.get("canonical_name")
                    if raw_canonical is not None and not isinstance(
                        raw_canonical, str
                    ):
                        raise DiagnosticValidationError(
                            f"Concept canonical_name must be text: {path}"
                        )
                    canonical = _bounded_text(
                        raw_canonical or path.stem,
                        "canonical concept name",
                        160,
                    )
                    aliases = post.get("aliases") or []
                    if not isinstance(aliases, list) or any(
                        not isinstance(item, str) for item in aliases
                    ):
                        raise DiagnosticValidationError(
                            f"Concept aliases must be a string list: {path}"
                        )
                    names = [
                        canonical,
                        *(
                            _bounded_text(item, "concept alias", 160)
                            for item in aliases
                        ),
                    ]
                except Exception:
                    continue
                if wanted in {normalize_concept_name(item) for item in names}:
                    matches.append((path, original, post, canonical))
        if not matches:
            raise DiagnosticValidationError(f"Concept does not exist: {name}")
        if len(matches) > 1:
            raise DiagnosticValidationError(
                f"Ambiguous concept name {name!r}: {[item[0] for item in matches]}"
            )
        return matches[0]

    def _confined_root(self, *parts: str) -> Path:
        lexical = self.vault_root.joinpath(*parts)
        current = self.vault_root
        for part in parts:
            current = current / part
            if current.exists() and _is_link_or_junction(current):
                raise DiagnosticValidationError(
                    f"Vault diagnostic paths cannot traverse a link or junction: {current}"
                )
        resolved = lexical.resolve()
        try:
            resolved.relative_to(self.vault_root)
        except ValueError as exc:
            raise DiagnosticValidationError(
                f"Vault diagnostic path escapes the vault: {lexical}"
            ) from exc
        return resolved

    @staticmethod
    def _references(post: frontmatter.Post, field: str) -> list[str]:
        references = post.get(field) or []
        if not isinstance(references, list) or any(
            not isinstance(reference, str) or not reference.strip()
            for reference in references
        ):
            raise DiagnosticValidationError(
                f"Concept {field} must be a list of vault-relative paths"
            )
        return list(references)

    @classmethod
    def _record_references(cls, post: frontmatter.Post) -> list[str]:
        return cls._references(post, "diagnostic_records")

    @classmethod
    def _amendment_references(cls, post: frontmatter.Post) -> list[str]:
        return cls._references(post, "diagnostic_amendments")

    def _record_paths(self, post: frontmatter.Post) -> tuple[Path, ...]:
        return self._linked_paths(post, "diagnostic_records")

    def _amendment_paths(self, post: frontmatter.Post) -> tuple[Path, ...]:
        return self._linked_paths(post, "diagnostic_amendments")

    def _linked_paths(
        self, post: frontmatter.Post, field: str
    ) -> tuple[Path, ...]:
        root = self._confined_root("Knowledge", "Diagnostics")
        paths: list[Path] = []
        references = self._references(post, field)
        for reference in references:
            relative = Path(str(reference))
            if relative.is_absolute():
                raise DiagnosticValidationError(
                    "Diagnostic record references must be vault-relative"
                )
            parts = relative.parts
            if len(parts) < 3 or tuple(part.casefold() for part in parts[:2]) != (
                "knowledge",
                "diagnostics",
            ):
                raise DiagnosticValidationError(
                    f"Diagnostic record is outside Knowledge/Diagnostics: {reference}"
                )
            lexical = self.vault_root / relative
            current = self.vault_root
            for part in parts:
                current = current / part
                if current.exists() and _is_link_or_junction(current):
                    raise DiagnosticValidationError(
                        f"Diagnostic record traverses a link or junction: {reference}"
                    )
            path = lexical.resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise DiagnosticValidationError(
                    f"Diagnostic record escapes Knowledge/Diagnostics: {reference}"
                ) from exc
            if not path.is_file():
                raise DiagnosticValidationError(
                    f"Diagnostic record is missing or unsafe: {path}"
                )
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise DiagnosticValidationError(
                f"Concept {field} contains duplicate paths"
            )
        return tuple(paths)

    def _diagnostic_state_sha256(
        self, post: frontmatter.Post, canonical: str
    ) -> str:
        records = [
            {
                "path": path.relative_to(self.vault_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in self._record_paths(post)
        ]
        amendments = [
            {
                "path": path.relative_to(self.vault_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in self._amendment_paths(post)
        ]
        payload = {
            "version": 2,
            "canonical": canonical,
            "familiarity": post.get("familiarity", "unknown"),
            "familiarity_confidence": post.get("familiarity_confidence"),
            "familiarity_assessed_at": post.get("familiarity_assessed_at"),
            "familiarity_basis_sha256": post.get("familiarity_basis_sha256"),
            "diagnostic_last_at": post.get("diagnostic_last_at"),
            "familiarity_review_required": post.get(
                "familiarity_review_required", False
            ),
            "familiarity_review_epoch": post.get("familiarity_review_epoch"),
            "records": records,
            "amendments": amendments,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _fresh_contiguous(
        self,
        post: frontmatter.Post,
        canonical: str,
        revision: str,
        extra: Mapping[str, float] | None = None,
        review_epoch: str | None = None,
    ) -> tuple[Familiarity, float | None]:
        confidences: dict[Familiarity, float] = {}
        for path in self._record_paths(post):
            record = frontmatter.load(path)
            if record.get("type") != "diagnostic-record":
                raise DiagnosticValidationError(f"Not a diagnostic record: {path}")
            if record.get("concept") != canonical:
                raise DiagnosticValidationError(
                    f"Diagnostic record concept mismatch: {path}"
                )
            if record.get("concept_revision_sha256") != revision:
                continue
            if review_epoch is not None and record.get("review_epoch") != review_epoch:
                continue
            raw = record.get("demonstrated_confidence") or {}
            if not isinstance(raw, dict):
                raise DiagnosticValidationError(
                    f"Diagnostic record has invalid evidence metadata: {path}"
                )
            for key, value in raw.items():
                level = _enum_value(Familiarity, key, "diagnostic evidence level")
                if level is Familiarity.UNKNOWN:
                    raise DiagnosticValidationError(
                        f"Diagnostic record cannot demonstrate unknown: {path}"
                    )
                confidence = _confidence(value, "diagnostic evidence confidence")
                if confidence < _MIN_DEMONSTRATED_CONFIDENCE:
                    continue
                confidences[level] = max(confidences.get(level, 0.0), confidence)
        for key, value in (extra or {}).items():
            level = _enum_value(Familiarity, key, "diagnostic evidence level")
            confidence = _confidence(value, "diagnostic evidence confidence")
            if confidence < _MIN_DEMONSTRATED_CONFIDENCE:
                continue
            confidences[level] = max(confidences.get(level, 0.0), confidence)

        contiguous: list[Familiarity] = []
        for capability in _CAPABILITIES:
            if capability not in confidences:
                break
            contiguous.append(capability)
        if not contiguous:
            return Familiarity.UNKNOWN, None
        return contiguous[-1], min(confidences[level] for level in contiguous)

    @staticmethod
    def _select_capabilities(
        baseline_rank: int, target: Familiarity | None
    ) -> tuple[Familiarity, ...]:
        if target is not None and not isinstance(target, Familiarity):
            target = _enum_value(Familiarity, target, "diagnostic target")
        if target is Familiarity.UNKNOWN:
            raise DiagnosticValidationError("Diagnostic target cannot be unknown")
        if target is None:
            if baseline_rank >= len(_CAPABILITIES):
                return (Familiarity.TRANSFERS,)
            return _CAPABILITIES[baseline_rank : baseline_rank + 2]
        target_rank = _FAMILIARITY_RANK[target]
        if target_rank <= baseline_rank:
            return (target,)
        capabilities = _CAPABILITIES[baseline_rank:target_rank]
        if len(capabilities) > 3:
            raise DiagnosticValidationError(
                "A voice-friendly diagnostic cannot span more than three levels"
            )
        return capabilities

    @staticmethod
    def _connection_hint(post: frontmatter.Post) -> str:
        connections = _extract_section(post.content, "Connections")
        match = re.search(r"\[\[([^\]|#]+)", connections)
        return match.group(1) if match else "a nearby concept or practical use"

    @staticmethod
    def _prompt_for(
        capability: Familiarity, canonical: str, connection: str
    ) -> DiagnosticPrompt:
        prompts = {
            Familiarity.RECOGNIZES: (
                f"In one or two sentences, define {canonical} in your own words and name its distinguishing feature.",
                "The response identifies the concept and at least one defining feature without relying on a copied definition.",
            ),
            Familiarity.EXPLAINS: (
                f"Explain why {canonical} matters and how it connects to {connection}.",
                "The response gives a causal explanation and a meaningful connection, not only two definitions.",
            ),
            Familiarity.APPLIES: (
                f"Give a new example where you would apply {canonical}, and walk through your reasoning.",
                "The response selects and applies the concept correctly in an analogous situation with visible reasoning.",
            ),
            Familiarity.TRANSFERS: (
                f"Use {canonical} in a different domain, or teach it by correcting a plausible misconception.",
                "The response transfers the concept to a novel context or accurately repairs a misconception.",
            ),
        }
        if capability not in prompts:
            raise DiagnosticValidationError(
                f"Unsupported diagnostic capability: {capability.value}"
            )
        question, rule = prompts[capability]
        evidence_kinds = {
            Familiarity.RECOGNIZES: EvidenceKind.OWN_DEFINITION,
            Familiarity.EXPLAINS: EvidenceKind.CAUSAL_EXPLANATION,
            Familiarity.APPLIES: EvidenceKind.NOVEL_APPLICATION,
            Familiarity.TRANSFERS: EvidenceKind.TRANSFER_OR_CORRECTION,
        }
        return DiagnosticPrompt(
            id=f"{capability.value}-v1",
            capability=capability,
            evidence_kind=evidence_kinds[capability],
            question=question,
            evidence_rule=rule,
        )

    @staticmethod
    def _plan_id(
        canonical: str,
        revision: str,
        state_sha256: str,
        current: Familiarity,
        stale: bool,
        prompts: Sequence[DiagnosticPrompt],
        target: Familiarity | None,
    ) -> str:
        payload = {
            "version": 1,
            "canonical": canonical,
            "revision": revision,
            "state": state_sha256,
            "current": current.value,
            "stale": stale,
            "prompts": [prompt.to_dict() for prompt in prompts],
            "target": target.value if target else None,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return f"plan-{hashlib.sha256(encoded).hexdigest()[:24]}"

    @staticmethod
    def _validate_submission_shape(submission: DiagnosticSubmission) -> None:
        if not isinstance(submission, DiagnosticSubmission):
            raise DiagnosticValidationError("Diagnostic submission is invalid")
        _bounded_text(submission.plan_id, "diagnostic plan id", 80)
        _bounded_text(
            submission.canonical_concept, "diagnostic canonical concept", 160
        )
        if not isinstance(submission.source, str) or submission.source not in _ALLOWED_SOURCES:
            raise DiagnosticValidationError(
                f"diagnostic source must be one of {sorted(_ALLOWED_SOURCES)}"
            )
        if submission.confirmed_by_user is not True:
            raise DiagnosticValidationError(
                "Diagnostic evidence must be explicitly confirmed by the user"
            )
        if any(
            not isinstance(item, DiagnosticObservation)
            for item in submission.observations
        ):
            raise DiagnosticValidationError(
                "Diagnostic submission contains an invalid observation"
            )
        for item in submission.observations:
            _bounded_text(item.prompt_id, "observation prompt id", 80)
            if not isinstance(item.result, ObservationResult):
                raise DiagnosticValidationError(
                    "Diagnostic observation has an invalid result"
                )
            if not isinstance(item.evidence_kind, EvidenceKind):
                raise DiagnosticValidationError(
                    "Diagnostic observation has an invalid evidence kind"
                )
            _confidence(item.confidence, "diagnostic confidence")
            _evidence_summary(item.evidence_summary)

    @classmethod
    def _normalized_submission(
        cls, submission: DiagnosticSubmission
    ) -> DiagnosticSubmission:
        cls._validate_submission_shape(submission)
        observations = tuple(
            DiagnosticObservation(
                prompt_id=_bounded_text(
                    item.prompt_id, "observation prompt id", 80
                ),
                evidence_kind=item.evidence_kind,
                result=item.result,
                confidence=_confidence(item.confidence, "diagnostic confidence"),
                evidence_summary=_evidence_summary(item.evidence_summary),
            )
            for item in submission.observations
        )
        return DiagnosticSubmission(
            plan_id=_bounded_text(submission.plan_id, "diagnostic plan id", 80),
            canonical_concept=_bounded_text(
                submission.canonical_concept,
                "diagnostic canonical concept",
                160,
            ),
            observations=observations,
            source=submission.source,
            confirmed_by_user=True,
        )

    @staticmethod
    def _validate_observations(
        plan: DiagnosticPlan,
        observations: tuple[DiagnosticObservation, ...],
    ) -> None:
        expected_ids = [prompt.id for prompt in plan.prompts]
        actual_ids = [item.prompt_id for item in observations]
        if actual_ids != expected_ids:
            raise DiagnosticValidationError(
                "Diagnostic observations must match planned prompt order exactly"
            )
        for prompt, observation in zip(plan.prompts, observations):
            if observation.evidence_kind is not prompt.evidence_kind:
                raise DiagnosticValidationError(
                    f"{prompt.id} requires {prompt.evidence_kind.value} evidence"
                )

    @staticmethod
    def _submission_sha256(submission: DiagnosticSubmission) -> str:
        encoded = json.dumps(
            submission.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _record_post(
        submission: DiagnosticSubmission,
        plan: DiagnosticPlan,
        canonical: str,
        record_id: str,
        submission_sha256: str,
        revision: str,
        observed_at: str,
        demonstrated: Mapping[str, float],
        fresh_level: Familiarity,
        fresh_confidence: float | None,
        review_epoch: str | None,
    ) -> frontmatter.Post:
        prompt_by_id = {prompt.id: prompt for prompt in plan.prompts}
        evidence_lines: list[str] = []
        for item in submission.observations:
            capability = prompt_by_id[item.prompt_id].capability.value
            evidence_lines.append(
                f"- **{capability}** (`{item.evidence_kind.value}`): "
                f"`{item.result.value}` at "
                f"{item.confidence:.2f} confidence - "
                f"{_markdown_plaintext(item.evidence_summary)}"
            )
        body = (
            f"# {_markdown_plaintext(canonical)} Diagnostic\n\n"
            "## Evidence\n\n"
            + "\n".join(evidence_lines)
            + "\n"
        )
        return frontmatter.Post(
            body,
            type="diagnostic-record",
            record_id=record_id,
            submission_sha256=submission_sha256,
            concept=canonical,
            concept_revision_sha256=revision,
            plan_id=plan.id,
            observed_at=observed_at,
            source=submission.source,
            demonstrated_confidence=dict(demonstrated),
            promotion_min_confidence=_MIN_DEMONSTRATED_CONFIDENCE,
            contiguous_level=fresh_level.value,
            contiguous_confidence=fresh_confidence,
            review_epoch=review_epoch,
            tags=["diagnostic"],
        )

    def _outcome(
        self,
        post: frontmatter.Post,
        canonical: str,
        record_id: str,
        changed_files: tuple[Path, ...],
    ) -> DiagnosticOutcome:
        confidence = post.get("familiarity_confidence")
        if confidence is not None:
            confidence = _confidence(confidence, "concept familiarity confidence")
        assessed_at = post.get("familiarity_assessed_at")
        return DiagnosticOutcome(
            canonical_concept=canonical,
            familiarity=_familiarity(post),
            confidence=confidence,
            assessed_at=str(assessed_at) if assessed_at else None,
            evidence_stale=familiarity_is_stale(post, canonical),
            record_id=record_id,
            changed_files=changed_files,
        )

    def _log(
        self,
        operation: str,
        status: str,
        concept: str,
        *,
        plan_id: str | None = None,
        record_id: str | None = None,
        outcome: DiagnosticOutcome | None = None,
        correction_outcome: DiagnosticCorrectionOutcome | None = None,
        error: Exception | None = None,
    ) -> None:
        if self.log_path is None:
            return
        concept_key = normalize_concept_name(str(concept)) or "invalid"
        entry = {
            "schema_version": 1,
            "timestamp": self._timestamp(),
            "operation": operation,
            "status": status,
            "concept_sha256": hashlib.sha256(
                concept_key.encode("utf-8")
            ).hexdigest(),
            "plan_id": plan_id,
            "record_id": record_id,
            "familiarity": outcome.familiarity.value if outcome else None,
            "reassessment_due": (
                correction_outcome.reassessment_due
                if correction_outcome is not None
                else outcome.evidence_stale if outcome is not None else None
            ),
            "changed_file_count": (
                len(correction_outcome.changed_files)
                if correction_outcome is not None
                else len(outcome.changed_files) if outcome is not None else 0
            ),
            "model_attempted": False,
            "input_tokens": 0,
            "output_tokens": 0,
            "error_type": type(error).__name__ if error is not None else None,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(entry, separators=(",", ":"), ensure_ascii=False)
                    + "\n"
                )
        except OSError:
            pass

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
