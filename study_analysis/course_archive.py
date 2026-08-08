from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

import frontmatter

from .link_integrity import LinkIntegrityReport, audit_vault_links
from .transaction import TransactionConflictError, commit_text_files


_COMPLETED_STATUSES = frozenset({"done", "completed", "cancelled", "canceled"})
_IGNORED_DIRECTORIES = frozenset({".git", ".obsidian", ".trash"})


class CourseArchiveError(ValueError):
    """Base error for a rejected course lifecycle operation."""


class CourseArchiveValidationError(CourseArchiveError):
    """The requested lifecycle change does not satisfy the archive contract."""


class CourseArchiveConflictError(CourseArchiveError):
    """Course state changed after the lifecycle plan was prepared."""


class CourseArchiveIntegrityError(CourseArchiveError):
    """Vault link integrity did not hold across the lifecycle change."""


class CourseArchiveState(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class CourseArchiveRequest:
    course: str
    target_state: CourseArchiveState


@dataclass(frozen=True)
class CourseArchivePlan:
    id: str
    course: str
    current_state: CourseArchiveState
    target_state: CourseArchiveState
    assignment_count: int
    open_assignments: tuple[str, ...]
    references_checked: int
    link_issue_count: int

    @property
    def files_to_change(self) -> int:
        return int(self.current_state is not self.target_state)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.link_issue_count:
            reasons.append("vault_link_issues")
        if (
            self.target_state is CourseArchiveState.ARCHIVED
            and self.open_assignments
        ):
            reasons.append("open_assignments")
        return tuple(reasons)

    @property
    def can_apply(self) -> bool:
        return not self.blocking_reasons

    def to_dict(self) -> dict:
        return {
            "plan_id": self.id,
            "course": self.course,
            "current_state": self.current_state.value,
            "target_state": self.target_state.value,
            "assignment_count": self.assignment_count,
            "open_assignment_count": len(self.open_assignments),
            "open_assignments": list(self.open_assignments),
            "references_checked": self.references_checked,
            "link_issue_count": self.link_issue_count,
            "files_to_change": self.files_to_change,
            "folders_moved": 0,
            "can_apply": self.can_apply,
            "blocking_reasons": list(self.blocking_reasons),
            "model_attempted": False,
            "input_tokens": 0,
            "output_tokens": 0,
        }


@dataclass(frozen=True)
class CourseArchiveOutcome:
    course: str
    state: CourseArchiveState
    assignment_count: int
    references_checked: int
    changed_files: tuple[Path, ...]

    def to_dict(self) -> dict:
        return {
            "course": self.course,
            "state": self.state.value,
            "assignment_count": self.assignment_count,
            "references_checked": self.references_checked,
            "changed_files": [str(path) for path in self.changed_files],
            "folders_moved": 0,
            "model_attempted": False,
            "input_tokens": 0,
            "output_tokens": 0,
        }


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _safe_directory(path: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_dir():
        raise CourseArchiveValidationError(f"{label} is not a directory: {absolute}")
    if _is_link_or_junction(absolute):
        raise CourseArchiveValidationError(
            f"{label} cannot be a symbolic link or junction: {absolute}"
        )
    return absolute.resolve()


def _course_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise CourseArchiveValidationError("Course name must be text")
    course = raw.strip()
    if (
        not course
        or course in {".", ".."}
        or Path(course).is_absolute()
        or "/" in course
        or "\\" in course
    ):
        raise CourseArchiveValidationError("Course name must be one direct folder name")
    return course


def _course_state(metadata: dict) -> CourseArchiveState:
    raw = metadata.get("course_archive")
    if raw is None:
        return CourseArchiveState.ACTIVE
    if not isinstance(raw, dict):
        raise CourseArchiveValidationError("course_archive metadata must be an object")
    try:
        state = CourseArchiveState(raw.get("status"))
    except (TypeError, ValueError) as exc:
        raise CourseArchiveValidationError(
            "course_archive.status must be active or archived"
        ) from exc
    changed_at = raw.get("changed_at")
    if not isinstance(changed_at, str) or not changed_at.strip():
        raise CourseArchiveValidationError(
            "course_archive.changed_at must be a timestamp"
        )
    return state


def _iter_markdown_files(root: Path):
    for current_text, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_text)
        directory_names[:] = [
            name
            for name in sorted(directory_names, key=str.casefold)
            if name not in _IGNORED_DIRECTORIES
            and not _is_link_or_junction(current / name)
        ]
        for name in sorted(file_names, key=str.casefold):
            path = current / name
            if path.suffix.casefold() == ".md" and not _is_link_or_junction(path):
                yield path


def _canvas_assignment(post: frontmatter.Post) -> bool:
    uid = post.get("canvas_uid")
    return not isinstance(uid, bool) and isinstance(uid, (str, int))


def _status(post: frontmatter.Post) -> str:
    raw = post.get("status")
    return raw.strip().casefold() if isinstance(raw, str) else ""


def _digest_payload(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:24]


class CourseArchiveEngine:
    """Plan and apply one guarded metadata-only course lifecycle change."""

    def __init__(
        self,
        vault_root: Path,
        assignments_root: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.vault_root = _safe_directory(Path(vault_root), label="Vault root")
        raw_assignments = (
            Path(assignments_root)
            if assignments_root is not None
            else self.vault_root / "School"
        )
        self.assignments_root = _safe_directory(
            raw_assignments, label="Assignments root"
        )
        try:
            self.assignments_root.relative_to(self.vault_root)
        except ValueError as exc:
            raise CourseArchiveValidationError(
                "Assignments root must stay inside the vault"
            ) from exc
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _course_paths(self, raw_course: object) -> tuple[str, Path, Path]:
        course = _course_name(raw_course)
        course_path = self.assignments_root / course
        if not course_path.is_dir():
            raise CourseArchiveValidationError(f"Course folder not found: {course}")
        if _is_link_or_junction(course_path) or course_path.resolve() != course_path:
            raise CourseArchiveValidationError(
                f"Course folder cannot be a symbolic link or junction: {course}"
            )
        info_path = course_path / "_Course Info.md"
        if not info_path.is_file() or _is_link_or_junction(info_path):
            raise CourseArchiveValidationError(
                f"Course info note not found: {course}/_Course Info.md"
            )
        if info_path.resolve().parent != course_path:
            raise CourseArchiveValidationError("Course info note escaped its course folder")
        return course, course_path, info_path

    def _snapshot(self, request: CourseArchiveRequest):
        try:
            target_state = CourseArchiveState(request.target_state)
        except (TypeError, ValueError) as exc:
            raise CourseArchiveValidationError(
                "Target state must be active or archived"
            ) from exc
        course, course_path, info_path = self._course_paths(request.course)
        info_bytes = info_path.read_bytes()
        try:
            course_info = frontmatter.loads(info_bytes.decode("utf-8"))
        except Exception as exc:
            raise CourseArchiveValidationError(
                f"Course info note is not valid UTF-8 Markdown/YAML: {course}"
            ) from exc
        if course_info.get("type") != "course-info":
            raise CourseArchiveValidationError(
                f"Course info note has the wrong type: {course}"
            )
        if course_info.get("course") != course:
            raise CourseArchiveValidationError(
                f"Course info identity does not match its folder: {course}"
            )
        current_state = _course_state(course_info.metadata)

        assignments: list[tuple[str, str, str]] = []
        open_assignments: list[str] = []
        for path in _iter_markdown_files(course_path):
            if path == info_path:
                continue
            try:
                post = frontmatter.load(path)
            except Exception:
                continue
            if not _canvas_assignment(post):
                continue
            relative = path.relative_to(course_path).as_posix()
            status = _status(post)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assignments.append((relative, status, digest))
            if status not in _COMPLETED_STATUSES:
                open_assignments.append(relative)

        link_report = audit_vault_links(self.vault_root)
        identity = {
            "course": course,
            "target_state": target_state.value,
            "current_state": current_state.value,
            "course_info_sha256": hashlib.sha256(info_bytes).hexdigest(),
            "assignments": assignments,
            "link_report": link_report.to_dict(),
        }
        return (
            course,
            info_path,
            info_bytes,
            course_info,
            current_state,
            target_state,
            tuple(assignments),
            tuple(sorted(open_assignments, key=str.casefold)),
            link_report,
            _digest_payload(identity),
        )

    def prepare(self, request: CourseArchiveRequest) -> CourseArchivePlan:
        (
            course,
            _,
            _,
            _,
            current_state,
            target_state,
            assignments,
            open_assignments,
            link_report,
            plan_id,
        ) = self._snapshot(request)
        return CourseArchivePlan(
            id=plan_id,
            course=course,
            current_state=current_state,
            target_state=target_state,
            assignment_count=len(assignments),
            open_assignments=open_assignments,
            references_checked=link_report.references_checked,
            link_issue_count=len(link_report.issues),
        )

    def apply(
        self,
        plan: CourseArchivePlan,
        confirmed_by_user: bool,
    ) -> CourseArchiveOutcome:
        if not confirmed_by_user:
            raise CourseArchiveValidationError(
                "Course lifecycle change requires explicit user confirmation"
            )
        request = CourseArchiveRequest(plan.course, plan.target_state)
        current_plan = self.prepare(request)
        if current_plan.id != plan.id:
            raise CourseArchiveConflictError(
                "Course or vault state changed after the archive plan was prepared"
            )
        if current_plan.link_issue_count:
            raise CourseArchiveIntegrityError(
                "Vault link issues must be resolved before changing course state"
            )
        if (
            current_plan.target_state is CourseArchiveState.ARCHIVED
            and current_plan.open_assignments
        ):
            raise CourseArchiveValidationError(
                "Course cannot be archived while Canvas assignments remain open"
            )
        if current_plan.current_state is current_plan.target_state:
            return CourseArchiveOutcome(
                course=current_plan.course,
                state=current_plan.target_state,
                assignment_count=current_plan.assignment_count,
                references_checked=current_plan.references_checked,
                changed_files=(),
            )

        (
            _,
            info_path,
            info_bytes,
            course_info,
            _,
            _,
            _,
            _,
            _,
            snapshot_id,
        ) = self._snapshot(request)
        if snapshot_id != plan.id:
            raise CourseArchiveConflictError(
                "Course or vault state changed before the archive commit"
            )
        timestamp = self.now()
        if not isinstance(timestamp, datetime):
            raise CourseArchiveValidationError("Archive clock must return a datetime")
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        course_info["course_archive"] = {
            "status": plan.target_state.value,
            "changed_at": timestamp.isoformat(timespec="seconds"),
        }
        rendered = frontmatter.dumps(course_info)
        try:
            changed_files = commit_text_files(
                {info_path: rendered},
                lock_root=self.vault_root,
                expected_originals={info_path: info_bytes},
            )
        except TransactionConflictError as exc:
            raise CourseArchiveConflictError(str(exc)) from exc

        try:
            post_report: LinkIntegrityReport = audit_vault_links(self.vault_root)
            if not post_report.ok:
                raise CourseArchiveIntegrityError(
                    "Vault link integrity changed during the archive commit"
                )
        except Exception as exc:
            committed_bytes = info_path.read_bytes()
            try:
                commit_text_files(
                    {info_path: info_bytes.decode("utf-8")},
                    lock_root=self.vault_root,
                    expected_originals={info_path: committed_bytes},
                )
            except Exception as rollback_exc:
                raise CourseArchiveIntegrityError(
                    "Archive verification failed and the course note could not be restored"
                ) from rollback_exc
            if isinstance(exc, CourseArchiveIntegrityError):
                raise
            raise CourseArchiveIntegrityError(
                "Archive verification failed; the course note was restored"
            ) from exc

        return CourseArchiveOutcome(
            course=plan.course,
            state=plan.target_state,
            assignment_count=current_plan.assignment_count,
            references_checked=post_report.references_checked,
            changed_files=changed_files,
        )
