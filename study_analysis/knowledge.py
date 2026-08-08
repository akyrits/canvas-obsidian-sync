from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import frontmatter

from .diagnostics import Familiarity
from .schema import normalize_concept_name
from .transaction import TransactionConflictError, commit_text_files


class KnowledgeRepositoryError(ValueError):
    """The knowledge repository cannot change without risking vault data."""


@dataclass(frozen=True)
class KnowledgeCaptureRequest:
    title: str
    content: str


@dataclass(frozen=True)
class KnowledgeCaptureOutcome:
    capture_path: Path
    created: bool
    changed_files: tuple[Path, ...]
    concept_count: int
    inbox_count: int
    source_count: int
    map_count: int
    revision: str
    model_attempted: bool = False
    network_attempted: bool = False
    provider_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_path": str(self.capture_path),
            "created": self.created,
            "changed_files": [str(path) for path in self.changed_files],
            "concept_count": self.concept_count,
            "inbox_count": self.inbox_count,
            "source_count": self.source_count,
            "map_count": self.map_count,
            "revision": self.revision,
            "model_attempted": self.model_attempted,
            "network_attempted": self.network_attempted,
            "provider_requests": self.provider_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class KnowledgeRefreshOutcome:
    changed_files: tuple[Path, ...]
    concept_count: int
    inbox_count: int
    source_count: int
    map_count: int
    revision: str
    model_attempted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_files": [str(path) for path in self.changed_files],
            "concept_count": self.concept_count,
            "inbox_count": self.inbox_count,
            "source_count": self.source_count,
            "map_count": self.map_count,
            "revision": self.revision,
            "model_attempted": self.model_attempted,
            "network_attempted": False,
            "provider_requests": 0,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": 0.0,
        }


@dataclass(frozen=True)
class _Entry:
    area: str
    path: Path
    vault_path: str
    title: str
    content_digest: str
    familiarity: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def link_target(self) -> str:
        return self.vault_path[:-3] if self.vault_path.casefold().endswith(".md") else self.vault_path

    def revision_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "area": self.area,
            "path": self.vault_path,
            "title": self.title,
            "sha256": self.content_digest,
        }
        if self.familiarity is not None:
            record["familiarity"] = self.familiarity
            record["aliases"] = list(self.aliases)
        return record


_SCHEMA_VERSION = 1
_MAX_CAPTURE_BYTES = 131_072
_AREA_CONFIG = {
    "inbox": ("Inbox", "_Inbox.md", "Inbox"),
    "sources": ("Sources", "_Sources.md", "Sources"),
    "maps": ("Maps", "_Maps.md", "Maps"),
}


def _capture_content(value: object) -> str:
    if not isinstance(value, str):
        raise KnowledgeRepositoryError("Knowledge capture content must be text")
    content = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        raise KnowledgeRepositoryError("Knowledge capture content cannot be empty")
    if any(ord(character) < 32 and character not in "\n\t" for character in content):
        raise KnowledgeRepositoryError(
            "Knowledge capture content contains an unsafe control character"
        )
    if len(content.encode("utf-8")) > _MAX_CAPTURE_BYTES:
        raise KnowledgeRepositoryError(
            f"Knowledge capture content exceeds {_MAX_CAPTURE_BYTES} UTF-8 bytes"
        )
    return content


def _capture_title(value: object) -> str:
    title = _clean_text(value, label="Knowledge capture title")
    if any(
        character in "[]|#^/\\\r\n" or ord(character) < 32
        for character in title
    ):
        raise KnowledgeRepositoryError(
            "Knowledge capture title contains an unsafe path or link delimiter"
        )
    return title


def _capture_stem(title: str) -> str:
    stem = re.sub(r'[<>:"/?*]+', "-", title)
    stem = re.sub(r"\s+", " ", stem).strip(" .-") or "Capture"
    encoded = stem.encode("utf-8")
    if len(encoded) > 120:
        stem = encoded[:120].decode("utf-8", errors="ignore")
    return stem.rstrip(" .-") or "Capture"


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _clean_text(value: object, *, label: str, limit: int = 160) -> str:
    if not isinstance(value, str):
        raise KnowledgeRepositoryError(f"{label} must be text")
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise KnowledgeRepositoryError(
            f"{label} must contain between 1 and {limit} characters"
        )
    return text


def _set_section(content: str, header: str, value: str) -> str:
    """Replace one managed H2 section while preserving every other section."""
    lines = content.splitlines(keepends=True)
    target = f"## {header}"
    matches = [index for index, line in enumerate(lines) if line.strip() == target]
    for duplicate in reversed(matches[1:]):
        end = next(
            (
                index
                for index in range(duplicate + 1, len(lines))
                if lines[index].strip().startswith("## ")
            ),
            len(lines),
        )
        del lines[duplicate:end]
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == target), None
    )
    replacement = [f"{target}\n", "\n", value.rstrip() + "\n", "\n"]
    if start is None:
        existing = "".join(lines)
        if existing.strip():
            if not existing.endswith("\n"):
                existing += "\n"
            if not existing.endswith("\n\n"):
                existing += "\n"
        return existing + "".join(replacement)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip().startswith("## ")
        ),
        len(lines),
    )
    lines[start:end] = replacement
    return "".join(lines)


def _merge_tags(raw: object, *required: str) -> list[str]:
    if raw is None:
        tags: list[str] = []
    elif isinstance(raw, str):
        tags = [_clean_text(raw, label="Knowledge note tag", limit=80)]
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        tags = [_clean_text(item, label="Knowledge note tag", limit=80) for item in raw]
    else:
        raise KnowledgeRepositoryError("Knowledge note tags must be text or a text list")
    seen = {tag.casefold() for tag in tags}
    for tag in required:
        if tag.casefold() not in seen:
            tags.append(tag)
            seen.add(tag.casefold())
    return tags


def _wikilink(entry: _Entry) -> str:
    display = entry.title.replace("|", "-").replace("[", "(").replace("]", ")")
    return f"[[{entry.link_target}|{display}]]"


def _safe_link_path(vault_path: str, *, source: Path) -> str:
    forbidden = "[]|#^\r\n"
    if any(character in forbidden or ord(character) < 32 for character in vault_path):
        raise KnowledgeRepositoryError(
            "Knowledge note path contains an unsafe Obsidian link delimiter: "
            f"{source}"
        )
    return vault_path


def _render_entries(entries: Sequence[_Entry], *, empty: str) -> str:
    if not entries:
        return f"_{empty}_"
    lines = []
    for entry in entries:
        suffix = (
            f" — familiarity: `{entry.familiarity}`"
            if entry.familiarity is not None
            else ""
        )
        lines.append(f"- {_wikilink(entry)}{suffix}")
    return "\n".join(lines)


class KnowledgeRepository:
    """Maintain one deterministic, model-free navigation layer over Knowledge/."""

    def __init__(self, vault_root: Path):
        absolute = Path(os.path.abspath(Path(vault_root)))
        if not absolute.is_dir():
            raise KnowledgeRepositoryError(f"Vault root is not a directory: {absolute}")
        if _is_link_or_junction(absolute):
            raise KnowledgeRepositoryError(
                f"Vault root cannot be a symbolic link or junction: {absolute}"
            )
        self.vault_root = absolute.resolve()

    def _validate_path(self, path: Path, *, kind: str | None = None) -> Path:
        try:
            relative = path.relative_to(self.vault_root)
        except ValueError as exc:
            raise KnowledgeRepositoryError(
                f"Knowledge path escapes the vault: {path}"
            ) from exc
        current = self.vault_root
        for part in relative.parts:
            current = current / part
            if _is_link_or_junction(current):
                raise KnowledgeRepositoryError(
                    f"Knowledge path cannot traverse a link or junction: {current}"
                )
        resolved = path.resolve()
        try:
            resolved.relative_to(self.vault_root)
        except ValueError as exc:
            raise KnowledgeRepositoryError(
                f"Knowledge path resolves outside the vault: {path}"
            ) from exc
        if kind == "directory" and path.exists() and not path.is_dir():
            raise KnowledgeRepositoryError(f"Knowledge folder is not a directory: {path}")
        if kind == "file" and path.exists() and not path.is_file():
            raise KnowledgeRepositoryError(f"Knowledge note is not a file: {path}")
        return resolved

    def _category_root(self, name: str) -> Path:
        knowledge = self._validate_path(
            self.vault_root / "Knowledge", kind="directory"
        )
        return self._validate_path(knowledge / name, kind="directory")

    def _markdown_files(self, root: Path, *, landing_name: str | None = None) -> tuple[Path, ...]:
        if not root.exists():
            return ()
        found: list[Path] = []
        for raw_current, dirnames, filenames in os.walk(root, followlinks=False):
            current = self._validate_path(Path(raw_current), kind="directory")
            for dirname in dirnames:
                self._validate_path(current / dirname, kind="directory")
            dirnames.sort(key=lambda name: (name.casefold(), name))
            for filename in sorted(filenames, key=lambda name: (name.casefold(), name)):
                if not filename.casefold().endswith(".md"):
                    continue
                path = self._validate_path(current / filename, kind="file")
                if (
                    landing_name is not None
                    and path.parent == root
                    and path.name.casefold() == landing_name.casefold()
                ):
                    continue
                found.append(path)
        return tuple(
            sorted(
                found,
                key=lambda path: (
                    path.relative_to(self.vault_root).as_posix().casefold(),
                    path.relative_to(self.vault_root).as_posix(),
                ),
            )
        )

    def _read(self, path: Path) -> bytes:
        path = self._validate_path(path, kind="file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise KnowledgeRepositoryError(f"Could not read knowledge note: {path}") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise KnowledgeRepositoryError(
                    f"Knowledge note is not a regular file: {path}"
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._validate_path(path, kind="file")
        try:
            after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise KnowledgeRepositoryError(
                f"Knowledge note changed while it was read: {path}"
            ) from exc
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_after != identity_before:
            raise KnowledgeRepositoryError(
                f"Knowledge note changed while it was read: {path}"
            )
        return content

    @staticmethod
    def _parse_post(path: Path, original: bytes) -> frontmatter.Post:
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise KnowledgeRepositoryError(
                f"Knowledge note is not valid UTF-8: {path}"
            ) from exc
        try:
            return frontmatter.loads(text)
        except Exception as exc:
            raise KnowledgeRepositoryError(
                f"Knowledge note has invalid frontmatter: {path}"
            ) from exc

    def _concept_entries(self) -> tuple[tuple[_Entry, ...], dict[Path, bytes]]:
        root = self._category_root("Concepts")
        entries: list[_Entry] = []
        originals: dict[Path, bytes] = {}
        identities: dict[str, Path] = {}
        for path in self._markdown_files(root, landing_name="_Concepts.md"):
            original = self._read(path)
            originals[path] = original
            post = self._parse_post(path, original)
            raw_canonical = post.get("canonical_name")
            canonical = _clean_text(
                path.stem if raw_canonical is None else raw_canonical,
                label=f"Concept canonical_name in {path}",
            )
            identity = normalize_concept_name(canonical)
            if not identity:
                raise KnowledgeRepositoryError(
                    f"Concept canonical_name has no usable identity: {path}"
                )
            previous = identities.get(identity)
            if previous is not None:
                raise KnowledgeRepositoryError(
                    "Duplicate canonical concept identity "
                    f"{canonical!r}: {previous} and {path}"
                )
            identities[identity] = path

            raw_aliases = post.get("aliases") or []
            if not isinstance(raw_aliases, list) or len(raw_aliases) > 64:
                raise KnowledgeRepositoryError(
                    f"Concept aliases must be a bounded text list: {path}"
                )
            aliases = tuple(
                _clean_text(alias, label=f"Concept alias in {path}")
                for alias in raw_aliases
            )
            raw_familiarity = post.get("familiarity") or Familiarity.UNKNOWN.value
            try:
                familiarity = Familiarity(str(raw_familiarity).strip().casefold()).value
            except ValueError as exc:
                raise KnowledgeRepositoryError(
                    f"Concept familiarity is invalid in {path}: {raw_familiarity!r}"
                ) from exc
            relative = _safe_link_path(
                path.relative_to(self.vault_root).as_posix(), source=path
            )
            entries.append(
                _Entry(
                    area="concepts",
                    path=path,
                    vault_path=relative,
                    title=canonical,
                    content_digest=hashlib.sha256(original).hexdigest(),
                    familiarity=familiarity,
                    aliases=aliases,
                )
            )
        entries.sort(
            key=lambda entry: (
                entry.title.casefold(),
                entry.title,
                entry.vault_path.casefold(),
                entry.vault_path,
            )
        )
        return tuple(entries), originals

    def _area_entries(
        self, area: str, folder: str, landing_name: str
    ) -> tuple[tuple[_Entry, ...], dict[Path, bytes]]:
        root = self._category_root(folder)
        entries: list[_Entry] = []
        originals: dict[Path, bytes] = {}
        for path in self._markdown_files(root, landing_name=landing_name):
            original = self._read(path)
            originals[path] = original
            post = self._parse_post(path, original)
            raw_title = post.get("title")
            title = _clean_text(
                path.stem if raw_title is None else raw_title,
                label=f"Knowledge note title for {path}",
            )
            relative = _safe_link_path(
                path.relative_to(self.vault_root).as_posix(), source=path
            )
            entries.append(
                _Entry(
                    area=area,
                    path=path,
                    vault_path=relative,
                    title=title,
                    content_digest=hashlib.sha256(original).hexdigest(),
                )
            )
        entries.sort(
            key=lambda entry: (
                entry.title.casefold(),
                entry.title,
                entry.vault_path.casefold(),
                entry.vault_path,
            )
        )
        return tuple(entries), originals

    def _optional_original(self, path: Path) -> bytes | None:
        path = self._validate_path(path, kind="file")
        return self._read(path) if path.exists() else None

    def _managed_post(
        self, path: Path, original: bytes | None, *, initial_content: str
    ) -> frontmatter.Post:
        if original is None:
            return frontmatter.Post(initial_content)
        return self._parse_post(path, original)

    @staticmethod
    def _dump(post: frontmatter.Post) -> str:
        rendered = frontmatter.dumps(post)
        return rendered if rendered.endswith("\n") else rendered + "\n"

    def _hub_text(
        self,
        path: Path,
        original: bytes | None,
        *,
        concepts: Sequence[_Entry],
        areas: dict[str, tuple[_Entry, ...]],
        revision: str,
    ) -> str:
        initial = (
            "# Knowledge Hub\n\n"
            "## Repository Status\n\n"
            "## Concepts\n\n"
            "## Maps\n\n"
            "## Sources\n\n"
            "## Inbox\n\n"
            "## Personal Navigation\n\n"
            "_Add your own dashboards, maps, and frequently used routes here._\n"
        )
        post = self._managed_post(path, original, initial_content=initial)
        post["type"] = "knowledge-hub"
        post["schema_version"] = _SCHEMA_VERSION
        post["knowledge_revision"] = revision
        post["knowledge_counts"] = {
            "concepts": len(concepts),
            "inbox": len(areas["inbox"]),
            "sources": len(areas["sources"]),
            "maps": len(areas["maps"]),
        }
        post["tags"] = _merge_tags(post.get("tags"), "knowledge")
        status = "\n".join(
            [
                f"Repository revision: `{revision}`",
                "",
                f"- Concepts: **{len(concepts)}**",
                f"- Inbox: **{len(areas['inbox'])}** — [[Knowledge/Inbox/_Inbox|open Inbox]]",
                f"- Sources: **{len(areas['sources'])}** — [[Knowledge/Sources/_Sources|open Sources]]",
                f"- Maps: **{len(areas['maps'])}** — [[Knowledge/Maps/_Maps|open Maps]]",
                "- Model calls: **0**",
                "- Network requests: **0**",
            ]
        )
        post.content = _set_section(post.content, "Repository Status", status)
        post.content = _set_section(
            post.content,
            "Concepts",
            _render_entries(concepts, empty="No concept notes yet."),
        )
        for area, header in (("maps", "Maps"), ("sources", "Sources"), ("inbox", "Inbox")):
            folder, landing_name, label = _AREA_CONFIG[area]
            landing_target = f"Knowledge/{folder}/{landing_name[:-3]}"
            listing = _render_entries(areas[area], empty=f"No {label.lower()} notes yet.")
            post.content = _set_section(
                post.content,
                header,
                f"Entry point: [[{landing_target}|{label}]]\n\n{listing}",
            )
        return self._dump(post)

    def _landing_text(
        self,
        path: Path,
        original: bytes | None,
        *,
        area: str,
        label: str,
        entries: Sequence[_Entry],
        revision: str,
    ) -> str:
        descriptions = {
            "inbox": "Capture unprocessed ideas and references here before organizing them.",
            "sources": "Keep durable global reference notes here; course evidence stays with its course.",
            "maps": "Use maps to connect concepts, sources, and active lines of inquiry.",
        }
        initial = (
            f"# {label}\n\n{descriptions[area]}\n\n"
            "## Index\n\n"
            "## Notes\n\n"
            "_Add personal notes about how you use this area here._\n"
        )
        post = self._managed_post(path, original, initial_content=initial)
        post["type"] = "knowledge-index"
        post["schema_version"] = _SCHEMA_VERSION
        post["knowledge_area"] = area
        post["knowledge_revision"] = revision
        post["note_count"] = len(entries)
        post["tags"] = _merge_tags(post.get("tags"), "knowledge", area)
        post.content = _set_section(
            post.content,
            "Index",
            _render_entries(entries, empty=f"No {label.lower()} notes yet."),
        )
        return self._dump(post)

    @staticmethod
    def _revision(entries: Sequence[_Entry]) -> str:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "entries": [entry.revision_record() for entry in entries],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _revision_for(
        self,
        concepts: Sequence[_Entry],
        areas: dict[str, tuple[_Entry, ...]],
    ) -> str:
        all_entries = tuple(
            sorted(
                (*concepts, *(entry for values in areas.values() for entry in values)),
                key=lambda entry: (
                    entry.area,
                    entry.title.casefold(),
                    entry.title,
                    entry.vault_path.casefold(),
                    entry.vault_path,
                ),
            )
        )
        return self._revision(all_entries)

    def _navigation_plan(
        self,
        *,
        concepts: Sequence[_Entry],
        areas: dict[str, tuple[_Entry, ...]],
        revision: str,
    ) -> tuple[dict[Path, str], dict[Path, bytes | None]]:
        knowledge_root = self._category_root("")
        hub_path = self._validate_path(
            knowledge_root / "Knowledge Hub.md", kind="file"
        )
        originals: dict[Path, bytes | None] = {
            hub_path: self._optional_original(hub_path)
        }
        landing_paths: dict[str, Path] = {}
        for area, (folder, landing_name, _label) in _AREA_CONFIG.items():
            path = self._validate_path(
                self._category_root(folder) / landing_name, kind="file"
            )
            landing_paths[area] = path
            originals[path] = self._optional_original(path)

        rendered: dict[Path, str] = {
            hub_path: self._hub_text(
                hub_path,
                originals[hub_path],
                concepts=concepts,
                areas=areas,
                revision=revision,
            )
        }
        for area, (_folder, _landing_name, label) in _AREA_CONFIG.items():
            path = landing_paths[area]
            rendered[path] = self._landing_text(
                path,
                originals[path],
                area=area,
                label=label,
                entries=areas[area],
                revision=revision,
            )
        planned = {
            path: content
            for path, content in rendered.items()
            if originals[path] != content.encode("utf-8")
        }
        return planned, {path: originals[path] for path in planned}

    def _inventory_guard(self, *allowed_revisions: str) -> Callable[[], None]:
        allowed = frozenset(allowed_revisions)

        def guard() -> None:
            _concepts, _areas, _originals, current_revision = self._inventory()
            if current_revision not in allowed:
                raise KnowledgeRepositoryError(
                    "Knowledge inventory changed during the transaction; retry "
                    "against the new vault state"
                )

        return guard

    def _commit(
        self,
        planned: dict[Path, str],
        expected: dict[Path, bytes | None],
        *,
        state_guard: Callable[[], None],
        final_state_guard: Callable[[], None],
        action: str,
    ) -> tuple[Path, ...]:
        try:
            return commit_text_files(
                planned,
                lock_root=self.vault_root,
                expected_originals=expected,
                state_guard=state_guard,
                final_state_guard=final_state_guard,
            )
        except (TransactionConflictError, OSError, ValueError) as exc:
            raise KnowledgeRepositoryError(
                f"Knowledge {action} could not commit safely: {exc}"
            ) from exc

    def _verify_sources(self, originals: dict[Path, bytes], *, action: str) -> None:
        for path, original in originals.items():
            if self._read(path) != original:
                raise KnowledgeRepositoryError(
                    f"Knowledge note changed during {action}: {path}"
                )

    def _inventory(
        self,
    ) -> tuple[
        tuple[_Entry, ...],
        dict[str, tuple[_Entry, ...]],
        dict[Path, bytes],
        str,
    ]:
        concepts, concept_originals = self._concept_entries()
        areas: dict[str, tuple[_Entry, ...]] = {}
        source_originals: dict[Path, bytes] = dict(concept_originals)
        for area, (folder, landing_name, _label) in _AREA_CONFIG.items():
            entries, originals = self._area_entries(area, folder, landing_name)
            areas[area] = entries
            source_originals.update(originals)

        revision = self._revision_for(concepts, areas)
        return concepts, areas, source_originals, revision

    def _find_capture_by_id(
        self,
        originals: dict[Path, bytes],
        *,
        capture_id: str,
        capture_revision: str,
    ) -> Path | None:
        matches: list[Path] = []
        for path, original in originals.items():
            post = self._parse_post(path, original)
            if post.get("capture_id") != capture_id:
                continue
            if post.get("capture_revision") != capture_revision:
                raise KnowledgeRepositoryError(
                    f"Knowledge capture identity metadata conflicts in {path}"
                )
            matches.append(path)
        if len(matches) > 1:
            raise KnowledgeRepositoryError(
                "Duplicate knowledge capture identity appears in: "
                + ", ".join(str(path) for path in matches)
            )
        return matches[0] if matches else None

    def capture(self, request: KnowledgeCaptureRequest) -> KnowledgeCaptureOutcome:
        if not isinstance(request, KnowledgeCaptureRequest):
            raise KnowledgeRepositoryError(
                "Knowledge capture requires a KnowledgeCaptureRequest"
            )
        title = _capture_title(request.title)
        content = _capture_content(request.content)
        identity_payload = json.dumps(
            {
                "schema_version": _SCHEMA_VERSION,
                "title": title,
                "content": content,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        capture_revision = hashlib.sha256(identity_payload).hexdigest()
        capture_id = f"capture-{capture_revision[:24]}"

        concepts, current_areas, source_originals, current_revision = self._inventory()
        existing_capture = self._find_capture_by_id(
            source_originals,
            capture_id=capture_id,
            capture_revision=capture_revision,
        )
        if existing_capture is None:
            inbox_root = self._category_root("Inbox")
            filename = f"{_capture_stem(title)} - {capture_revision[:16]}.md"
            capture_path = self._validate_path(inbox_root / filename, kind="file")
            capture_original = self._optional_original(capture_path)
            created = capture_original is None
        else:
            capture_path = existing_capture
            capture_original = source_originals[capture_path]
            created = False
        capture_text: str | None = None

        if created:
            post = frontmatter.Post(
                f"# {title}\n\n"
                f"## Captured Material\n\n{content}\n\n"
                "## Notes\n\n"
            )
            post["type"] = "knowledge-capture"
            post["schema_version"] = _SCHEMA_VERSION
            post["capture_id"] = capture_id
            post["capture_revision"] = capture_revision
            post["captured_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            post["title"] = title
            post["tags"] = ["knowledge", "inbox", "capture"]
            capture_text = self._dump(post)
        else:
            existing = self._parse_post(capture_path, capture_original)
            if existing.get("capture_id") != capture_id or existing.get(
                "capture_revision"
            ) != capture_revision:
                raise KnowledgeRepositoryError(
                    "Knowledge capture path collides with a different existing note: "
                    f"{capture_path}"
                )

        target_areas = dict(current_areas)
        if created:
            assert capture_text is not None
            vault_path = _safe_link_path(
                capture_path.relative_to(self.vault_root).as_posix(),
                source=capture_path,
            )
            capture_entry = _Entry(
                area="inbox",
                path=capture_path,
                vault_path=vault_path,
                title=title,
                content_digest=hashlib.sha256(
                    capture_text.encode("utf-8")
                ).hexdigest(),
            )
            target_areas["inbox"] = tuple(
                sorted(
                    (*current_areas["inbox"], capture_entry),
                    key=lambda entry: (
                        entry.title.casefold(),
                        entry.title,
                        entry.vault_path.casefold(),
                        entry.vault_path,
                    ),
                )
            )
        target_revision = self._revision_for(concepts, target_areas)
        navigation, navigation_expected = self._navigation_plan(
            concepts=concepts,
            areas=target_areas,
            revision=target_revision,
        )

        planned: dict[Path, str] = {}
        expected: dict[Path, bytes | None] = {}
        if created:
            assert capture_text is not None
            planned[capture_path] = capture_text
            expected[capture_path] = None
        planned.update(navigation)
        expected.update(navigation_expected)

        self._verify_sources(source_originals, action="capture")
        changed = self._commit(
            planned,
            expected,
            state_guard=self._inventory_guard(
                current_revision,
                target_revision,
            ),
            final_state_guard=self._inventory_guard(target_revision),
            action="capture",
        )
        return KnowledgeCaptureOutcome(
            capture_path=capture_path,
            created=created,
            changed_files=changed,
            concept_count=len(concepts),
            inbox_count=len(target_areas["inbox"]),
            source_count=len(target_areas["sources"]),
            map_count=len(target_areas["maps"]),
            revision=target_revision,
        )

    def refresh(self) -> KnowledgeRefreshOutcome:
        concepts, areas, source_originals, revision = self._inventory()
        planned, expected = self._navigation_plan(
            concepts=concepts,
            areas=areas,
            revision=revision,
        )
        self._verify_sources(source_originals, action="refresh")
        changed = self._commit(
            planned,
            expected,
            state_guard=self._inventory_guard(revision),
            final_state_guard=self._inventory_guard(revision),
            action="refresh",
        )

        return KnowledgeRefreshOutcome(
            changed_files=changed,
            concept_count=len(concepts),
            inbox_count=len(areas["inbox"]),
            source_count=len(areas["sources"]),
            map_count=len(areas["maps"]),
            revision=revision,
        )
