from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping


class TransactionConflictError(RuntimeError):
    """A planned file changed before the guarded transaction could commit."""


def _current_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _require_stable_path(path: Path, scope: Path) -> None:
    current = path.resolve()
    if current != path:
        raise TransactionConflictError(f"Transaction path was redirected: {path}")
    try:
        current.relative_to(scope)
    except ValueError as exc:
        raise TransactionConflictError(
            f"Transaction path escaped lock scope {scope}: {path}"
        ) from exc


def _best_effort_cleanup(path: Path, *, contains_original: bool = False) -> None:
    try:
        path.unlink(missing_ok=True)
        return
    except OSError:
        pass
    if contains_original:
        try:
            path.write_bytes(b"")
            path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _scope_lock(root: Path) -> Iterator[None]:
    """Hold one non-blocking, process-safe lock for a vault transaction scope."""
    identity = str(root.resolve()).casefold().encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"canvas-obsidian-sync-{digest}.lock"
    stream = lock_path.open("a+b")
    locked = False
    try:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, OSError) as exc:
            raise TransactionConflictError(
                f"Another vault transaction is active for {root}"
            ) from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def commit_text_files(
    planned: Mapping[Path, str],
    *,
    lock_root: Path | None = None,
    expected_originals: Mapping[Path, bytes | None] | None = None,
    state_guard: Callable[[], None] | None = None,
    final_state_guard: Callable[[], None] | None = None,
) -> tuple[Path, ...]:
    """Guard and replace UTF-8 files, restoring replaced originals on failure.

    ``state_guard`` is an optional read-only transition validator. It runs while
    the transaction lock is held before and between replacements.
    ``final_state_guard`` runs after all replacements and can require the exact
    target state. If either raises, no planned change survives. This lets a
    caller protect read dependencies that are intentionally outside the write
    set, including transactions whose valid inventory changes mid-commit.
    """
    if not planned:
        if state_guard is None and final_state_guard is None:
            return ()
        if lock_root is None:
            raise ValueError("A guarded empty transaction requires lock_root")
        if not callable(state_guard):
            if state_guard is not None:
                raise TypeError("Transaction state_guard must be callable")
        if final_state_guard is not None and not callable(final_state_guard):
            raise TypeError("Transaction final_state_guard must be callable")
        with _scope_lock(Path(lock_root).resolve()):
            if state_guard is not None:
                state_guard()
            if final_state_guard is not None:
                final_state_guard()
        return ()

    if state_guard is not None and not callable(state_guard):
        raise TypeError("Transaction state_guard must be callable")
    if final_state_guard is not None and not callable(final_state_guard):
        raise TypeError("Transaction final_state_guard must be callable")

    normalized: dict[Path, str] = {}
    for raw_path, content in planned.items():
        path = Path(os.path.abspath(Path(raw_path)))
        if path in normalized:
            raise ValueError(f"Transaction repeats a resolved path: {path}")
        if not isinstance(content, str):
            raise TypeError(f"Transaction content must be text: {path}")
        normalized[path] = content

    if lock_root is None:
        scope = Path(os.path.commonpath([str(path.parent) for path in normalized]))
    else:
        scope = Path(lock_root).resolve()
        for path in normalized:
            try:
                path.relative_to(scope)
            except ValueError as exc:
                raise ValueError(f"Transaction path escapes lock root {scope}: {path}") from exc

    expected: dict[Path, bytes | None] = {}
    for raw_path, original in (expected_originals or {}).items():
        path = Path(os.path.abspath(Path(raw_path)))
        if path not in normalized:
            raise ValueError(f"Expected original is not a planned path: {path}")
        if original is not None and not isinstance(original, bytes):
            raise TypeError(f"Expected original must be bytes or None: {path}")
        expected[path] = original

    encoded = {path: content.encode("utf-8") for path, content in normalized.items()}
    with _scope_lock(scope):
        for path in normalized:
            _require_stable_path(path, scope)
        if state_guard is not None:
            state_guard()
        originals = {path: _current_bytes(path) for path in normalized}
        for path, wanted in expected.items():
            if originals[path] != wanted:
                raise TransactionConflictError(f"File changed before commit: {path}")

        staged: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        replaced: list[Path] = []
        created_directories: set[Path] = set()
        failed = False
        try:
            for path, content in normalized.items():
                _require_stable_path(path, scope)
                missing_parents: list[Path] = []
                current_parent = path.parent
                while current_parent != scope and not current_parent.exists():
                    missing_parents.append(current_parent)
                    current_parent = current_parent.parent
                path.parent.mkdir(parents=True, exist_ok=True)
                created_directories.update(
                    parent for parent in missing_parents if parent.is_dir()
                )
                handle, temp_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", dir=path.parent
                )
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
                staged[path] = Path(temp_name)

                original = originals[path]
                if original is not None:
                    handle, backup_name = tempfile.mkstemp(
                        prefix=f".{path.name}.backup.", dir=path.parent
                    )
                    with os.fdopen(handle, "wb") as backup:
                        backup.write(original)
                    backups[path] = Path(backup_name)

            if state_guard is not None:
                state_guard()
            for path, temp_path in staged.items():
                if state_guard is not None:
                    state_guard()
                _require_stable_path(path, scope)
                if _current_bytes(path) != originals[path]:
                    raise TransactionConflictError(f"File changed during commit: {path}")
                os.replace(temp_path, path)
                replaced.append(path)
            if state_guard is not None:
                state_guard()
            if final_state_guard is not None:
                final_state_guard()
        except Exception as exc:
            failed = True
            rollback_conflicts: list[Path] = []
            rollback_errors: list[tuple[Path, Exception]] = []
            for path in reversed(replaced):
                try:
                    if _current_bytes(path) != encoded[path]:
                        rollback_conflicts.append(path)
                        continue
                    original = originals[path]
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        os.replace(backups[path], path)
                except Exception as rollback_exc:
                    rollback_errors.append((path, rollback_exc))
            if rollback_conflicts or rollback_errors:
                details = [str(path) for path in rollback_conflicts]
                details.extend(str(path) for path, _ in rollback_errors)
                raise TransactionConflictError(
                    "Files could not be safely restored during rollback: "
                    + ", ".join(details)
                ) from exc
            raise
        finally:
            for temp_path in staged.values():
                _best_effort_cleanup(temp_path)
            for backup_path in backups.values():
                _best_effort_cleanup(backup_path, contains_original=True)
            if failed:
                for directory in sorted(
                    created_directories,
                    key=lambda candidate: len(candidate.parts),
                    reverse=True,
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
    return tuple(normalized)
