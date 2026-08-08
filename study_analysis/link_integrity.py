from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import frontmatter


_IGNORED_DIRECTORIES = frozenset({".git", ".obsidian", ".trash"})
_MANAGED_REFERENCES = {
    "analysis_sources": "note",
    "diagnostic_records": "vault",
    "diagnostic_amendments": "vault",
}
_WIKILINK_RE = re.compile(r"(?P<embed>!)?\[\[(?P<target>[^\]\n]+)\]\]")
_MARKDOWN_LINK_RE = re.compile(
    r"(?P<embed>!)?\[[^\]\n]*\]\((?P<target><[^>\n]+>|[^)\n]+)\)"
)
_FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")
_EXTERNAL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)


class LinkIntegrityError(ValueError):
    """The vault root cannot be audited safely."""


@dataclass(frozen=True)
class LinkIssue:
    kind: str
    source: str
    reference_kind: str
    target: str
    line: int | None = None
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        payload = {
            "kind": self.kind,
            "source": self.source,
            "reference_kind": self.reference_kind,
            "target": self.target,
        }
        if self.line is not None:
            payload["line"] = self.line
        if self.candidates:
            payload["candidates"] = list(self.candidates)
        return payload


@dataclass(frozen=True)
class LinkIntegrityReport:
    notes_scanned: int
    files_indexed: int
    references_checked: int
    resolved_references: int
    issues: tuple[LinkIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self, *, include_issues: bool = True) -> dict:
        issue_counts = {
            kind: sum(issue.kind == kind for issue in self.issues)
            for kind in ("unresolved", "ambiguous", "unsafe", "invalid")
        }
        payload = {
            "ok": self.ok,
            "notes_scanned": self.notes_scanned,
            "files_indexed": self.files_indexed,
            "references_checked": self.references_checked,
            "resolved_references": self.resolved_references,
            "issue_counts": issue_counts,
            "model_attempted": False,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        if include_issues:
            payload["issues"] = [issue.to_dict() for issue in self.issues]
        return payload


@dataclass(frozen=True)
class _Resolution:
    status: str
    candidates: tuple[str, ...] = ()


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _safe_root(raw_root: Path) -> Path:
    root = Path(os.path.abspath(Path(raw_root)))
    if not root.is_dir():
        raise LinkIntegrityError(f"Vault root is not a directory: {root}")
    if _is_link_or_junction(root):
        raise LinkIntegrityError("Vault root cannot be a symbolic link or junction")
    resolved = root.resolve()
    if resolved != root.resolve(strict=False):
        raise LinkIntegrityError("Vault root could not be resolved consistently")
    return resolved


def _iter_safe_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
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
            if not _is_link_or_junction(path):
                files.append(path)
    return tuple(files)


def _aliases(metadata: dict) -> tuple[str, ...]:
    raw = metadata.get("aliases")
    if isinstance(raw, str):
        values = (raw,)
    elif isinstance(raw, list):
        values = tuple(item for item in raw if isinstance(item, str))
    else:
        values = ()
    return tuple(value.strip() for value in values if value.strip())


class _VaultIndex:
    def __init__(self, root: Path, files: tuple[Path, ...]):
        self.root = root
        self.by_path: dict[str, tuple[str, ...]] = {}
        self.by_name: dict[str, tuple[str, ...]] = {}
        self.by_stem: dict[str, tuple[str, ...]] = {}
        self.by_alias: dict[str, tuple[str, ...]] = {}

        paths: dict[str, set[str]] = {}
        names: dict[str, set[str]] = {}
        stems: dict[str, set[str]] = {}
        aliases: dict[str, set[str]] = {}
        for path in files:
            relative = path.relative_to(root).as_posix()
            paths.setdefault(relative.casefold(), set()).add(relative)
            names.setdefault(path.name.casefold(), set()).add(relative)
            stems.setdefault(path.stem.casefold(), set()).add(relative)
            if path.suffix.casefold() != ".md":
                continue
            try:
                metadata = frontmatter.load(path).metadata
            except Exception:
                continue
            for alias in _aliases(metadata):
                aliases.setdefault(alias.casefold(), set()).add(relative)

        self.by_path = self._freeze(paths)
        self.by_name = self._freeze(names)
        self.by_stem = self._freeze(stems)
        self.by_alias = self._freeze(aliases)

    @staticmethod
    def _freeze(values: dict[str, set[str]]) -> dict[str, tuple[str, ...]]:
        return {
            key: tuple(sorted(items, key=str.casefold))
            for key, items in values.items()
        }

    def _candidate_key(self, candidate: Path) -> tuple[str | None, bool]:
        absolute = Path(os.path.abspath(candidate))
        try:
            relative = absolute.relative_to(self.root)
        except ValueError:
            return None, True

        current = self.root
        for part in relative.parts:
            current = current / part
            if current.exists() and _is_link_or_junction(current):
                return None, True
        try:
            absolute.resolve(strict=False).relative_to(self.root)
        except ValueError:
            return None, True
        return relative.as_posix().casefold(), False

    @staticmethod
    def _merge(*groups: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({item for group in groups for item in group}, key=str.casefold))

    @staticmethod
    def _result(candidates: tuple[str, ...]) -> _Resolution:
        if len(candidates) == 1:
            return _Resolution("resolved", candidates)
        if len(candidates) > 1:
            return _Resolution("ambiguous", candidates)
        return _Resolution("unresolved")

    def _exact(self, candidate: Path, *, append_markdown: bool) -> _Resolution:
        keys: list[str] = []
        key, unsafe = self._candidate_key(candidate)
        if unsafe:
            return _Resolution("unsafe")
        if key is not None:
            keys.append(key)
        if append_markdown:
            markdown_key, markdown_unsafe = self._candidate_key(
                candidate.with_name(f"{candidate.name}.md")
            )
            if markdown_unsafe:
                return _Resolution("unsafe")
            if markdown_key is not None:
                keys.append(markdown_key)
        return self._result(self._merge(*(self.by_path.get(key, ()) for key in keys)))

    def resolve_explicit(self, target: str, *, base: Path) -> _Resolution:
        normalized = target.replace("\\", "/").strip()
        if not normalized:
            return _Resolution("invalid")
        if _WINDOWS_ABSOLUTE_RE.match(normalized):
            return _Resolution("unsafe")
        if normalized.startswith("/"):
            candidate = self.root / normalized.lstrip("/")
        else:
            candidate = base / normalized
        return self._exact(candidate, append_markdown=not Path(normalized).suffix)

    def _course_attachment(self, target: str, *, source: Path) -> _Resolution:
        if Path(target).name != target or not Path(target).suffix:
            return _Resolution("unresolved")
        try:
            source_relative = source.relative_to(self.root)
        except ValueError:
            return _Resolution("unsafe")
        if (
            len(source_relative.parts) < 3
            or source_relative.parts[0].casefold() != "school"
        ):
            return _Resolution("unresolved")
        attachment_link = (
            self.root / source_relative.parts[0] / source_relative.parts[1] / "Attachments"
        )
        if not attachment_link.is_dir() or not _is_link_or_junction(attachment_link):
            return _Resolution("unresolved")
        attachment_root = attachment_link.resolve()
        candidate = attachment_root / target
        try:
            candidate.resolve(strict=False).relative_to(attachment_root)
        except ValueError:
            return _Resolution("unsafe")
        if _is_link_or_junction(candidate):
            return _Resolution("unsafe")
        if not candidate.is_file():
            return _Resolution("unresolved")
        virtual_path = (attachment_link / target).relative_to(self.root).as_posix()
        return _Resolution("resolved", (virtual_path,))

    def resolve(self, target: str, *, source: Path) -> _Resolution:
        normalized = target.replace("\\", "/").strip()
        if not normalized:
            return self._exact(source, append_markdown=False)
        if _WINDOWS_ABSOLUTE_RE.match(normalized):
            return _Resolution("unsafe")

        starts_relative = normalized.startswith(("./", "../"))
        path_qualified = "/" in normalized or normalized.startswith("/")
        has_suffix = bool(Path(normalized).suffix)

        if starts_relative:
            return self.resolve_explicit(normalized, base=source.parent)

        if path_qualified:
            root_result = self.resolve_explicit(normalized, base=self.root)
            if root_result.status != "unresolved":
                return root_result
            local_result = self.resolve_explicit(normalized, base=source.parent)
            if local_result.status != "unresolved":
                return local_result
            suffixes = (normalized.casefold().lstrip("/"),)
            if not has_suffix:
                suffixes += (f"{suffixes[0]}.md",)
            candidates = tuple(
                relative
                for relative_group in self.by_path.values()
                for relative in relative_group
                if any(
                    relative.casefold() == suffix
                    or relative.casefold().endswith(f"/{suffix}")
                    for suffix in suffixes
                )
            )
            return self._result(tuple(sorted(set(candidates), key=str.casefold)))

        local_result = self.resolve_explicit(normalized, base=source.parent)
        if local_result.status != "unresolved":
            return local_result
        attachment_result = self._course_attachment(normalized, source=source)
        if attachment_result.status != "unresolved":
            return attachment_result
        if has_suffix:
            candidates = self.by_name.get(Path(normalized).name.casefold(), ())
        else:
            candidates = self._merge(
                self.by_stem.get(normalized.casefold(), ()),
                self.by_alias.get(normalized.casefold(), ()),
            )
        return self._result(candidates)


def _normalize_reference(raw: str, *, wiki: bool) -> tuple[str, bool]:
    target = raw.strip()
    if not wiki and target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if wiki:
        target = target.split("|", 1)[0].strip()
    target = unquote(target)
    target = target.split("#", 1)[0].strip()
    if target.startswith("^"):
        target = ""
    if _WINDOWS_ABSOLUTE_RE.match(target):
        return target, False
    if _EXTERNAL_SCHEME_RE.match(target) or target.startswith("//"):
        return target, True
    return target, False


def _markup_references(text: str):
    fence_character: str | None = None
    fence_length = 0
    in_html_comment = False
    for line_number, line in enumerate(text.splitlines(), 1):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            fence = fence_match.group("fence")
            if fence_character is None:
                fence_character = fence[0]
                fence_length = len(fence)
            elif fence[0] == fence_character and len(fence) >= fence_length:
                fence_character = None
                fence_length = 0
            continue
        if fence_character is not None:
            continue
        visible: list[str] = []
        remainder = line
        while remainder:
            if in_html_comment:
                comment_end = remainder.find("-->")
                if comment_end < 0:
                    remainder = ""
                    continue
                remainder = remainder[comment_end + 3 :]
                in_html_comment = False
                continue
            comment_start = remainder.find("<!--")
            if comment_start < 0:
                visible.append(remainder)
                remainder = ""
                continue
            visible.append(remainder[:comment_start])
            remainder = remainder[comment_start + 4 :]
            in_html_comment = True
        searchable = _INLINE_CODE_RE.sub("", "".join(visible))
        matches = [
            (match.start(), "embed" if match.group("embed") else "wikilink", match)
            for match in _WIKILINK_RE.finditer(searchable)
        ]
        matches.extend(
            (
                match.start(),
                "markdown_embed" if match.group("embed") else "markdown_link",
                match,
            )
            for match in _MARKDOWN_LINK_RE.finditer(searchable)
        )
        for _, reference_kind, match in sorted(matches, key=lambda item: item[0]):
            wiki = reference_kind in {"wikilink", "embed"}
            target, external = _normalize_reference(match.group("target"), wiki=wiki)
            if not external:
                yield line_number, reference_kind, target


def _managed_values(metadata: dict, field: str) -> tuple[tuple[str, ...], bool]:
    raw = metadata.get(field)
    if raw is None:
        return (), True
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return (), False
    return tuple(item.strip() for item in raw if item.strip()), True


def _issue(
    resolution: _Resolution,
    *,
    source: str,
    reference_kind: str,
    target: str,
    line: int | None,
) -> LinkIssue | None:
    if resolution.status == "resolved":
        return None
    return LinkIssue(
        kind=resolution.status,
        source=source,
        reference_kind=reference_kind,
        target=target,
        line=line,
        candidates=resolution.candidates,
    )


def audit_vault_links(vault_root: Path) -> LinkIntegrityReport:
    """Audit vault-local references without modifying the vault or using a model."""

    root = _safe_root(vault_root)
    files = _iter_safe_files(root)
    notes = tuple(path for path in files if path.suffix.casefold() == ".md")
    index = _VaultIndex(root, files)
    issues: list[LinkIssue] = []
    references_checked = 0
    resolved_references = 0

    for note_path in notes:
        relative_source = note_path.relative_to(root).as_posix()
        text = note_path.read_text(encoding="utf-8")
        for line, reference_kind, target in _markup_references(text):
            references_checked += 1
            resolution = index.resolve(target, source=note_path)
            issue = _issue(
                resolution,
                source=relative_source,
                reference_kind=reference_kind,
                target=target,
                line=line,
            )
            if issue is None:
                resolved_references += 1
            else:
                issues.append(issue)

        try:
            metadata = frontmatter.loads(text).metadata
        except Exception:
            metadata = {}
        for field, base_kind in _MANAGED_REFERENCES.items():
            values, valid = _managed_values(metadata, field)
            if not valid:
                references_checked += 1
                issues.append(
                    LinkIssue(
                        kind="invalid",
                        source=relative_source,
                        reference_kind=field,
                        target=field,
                    )
                )
                continue
            base = note_path.parent if base_kind == "note" else root
            for target in values:
                references_checked += 1
                resolution = index.resolve_explicit(target, base=base)
                issue = _issue(
                    resolution,
                    source=relative_source,
                    reference_kind=field,
                    target=target,
                    line=None,
                )
                if issue is None:
                    resolved_references += 1
                else:
                    issues.append(issue)

    ordered_issues = tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.source.casefold(),
                issue.line or 0,
                issue.reference_kind,
                issue.target.casefold(),
                issue.kind,
            ),
        )
    )
    return LinkIntegrityReport(
        notes_scanned=len(notes),
        files_indexed=len(files),
        references_checked=references_checked,
        resolved_references=resolved_references,
        issues=ordered_issues,
    )
