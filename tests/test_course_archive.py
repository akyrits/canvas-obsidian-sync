from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from study_analysis.course_archive import (
    CourseArchiveConflictError,
    CourseArchiveEngine,
    CourseArchiveIntegrityError,
    CourseArchiveRequest,
    CourseArchiveState,
    CourseArchiveValidationError,
)
from study_analysis.lifeos import export_assignment_signals


class CourseArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.school = self.vault / "School"
        self.course = "Course 101"
        self.course_root = self.school / self.course
        self.course_root.mkdir(parents=True)
        self.now = datetime(2026, 7, 26, 16, 0, tzinfo=timezone.utc)
        self.info_path = self.course_root / "_Course Info.md"
        self._write_course_info()
        self.engine = CourseArchiveEngine(
            self.vault,
            now=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_course_info(
        self,
        *,
        state: CourseArchiveState | None = None,
        changed_at: str = "2026-07-01T12:00:00+00:00",
    ) -> None:
        archive = ""
        if state is not None:
            archive = (
                "course_archive:\n"
                f"  status: {state.value}\n"
                f"  changed_at: '{changed_at}'\n"
            )
        self.info_path.write_text(
            "---\n"
            f"course: {self.course}\n"
            "type: course-info\n"
            "owner_note: Keep this metadata exactly.\n"
            "nested:\n"
            "  label: Personal metadata\n"
            f"{archive}"
            "---\n\n"
            f"# {self.course}\n\n"
            "## Textbook / Resources\n"
            "A user-authored resource.\n\n"
            "## Personal Notes\n"
            "Keep this body exactly, including spacing.\n",
            encoding="utf-8",
        )

    def _write_assignment(
        self,
        name: str,
        status: str,
        *,
        canvas_uid: str | None = None,
    ) -> Path:
        path = self.course_root / f"{name}.md"
        uid = f"canvas_uid: {canvas_uid}\n" if canvas_uid is not None else ""
        path.write_text(
            "---\n"
            f"{uid}"
            f"course: {self.course}\n"
            f"status: {status}\n"
            "---\n\n"
            f"# {name}\n\n"
            "User-authored assignment notes stay byte-for-byte identical.\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _physical_file_hashes(root: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            parent = Path(directory)
            for filename in filenames:
                path = parent / filename
                if path.is_symlink():
                    continue
                hashes[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return hashes

    def _non_info_hashes(self) -> dict[str, str]:
        hashes = self._physical_file_hashes(self.vault)
        hashes.pop(f"School/{self.course}/_Course Info.md", None)
        return hashes

    def test_archive_and_restore_only_change_course_archive_metadata(self) -> None:
        for index, status in enumerate(("done", "completed", "cancelled"), 1):
            self._write_assignment(
                f"Assignment {index}",
                status,
                canvas_uid=f"assignment-{index}",
            )
        self._write_assignment("Local Checklist", "open")
        binary = self.course_root / "local-resource.bin"
        binary.write_bytes(b"\x00\x01private course bytes\xff")
        original_info = frontmatter.load(self.info_path)
        original_body = original_info.content
        original_metadata = dict(original_info.metadata)
        non_info_before = self._non_info_hashes()

        archive_plan = self.engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )
        archived = self.engine.apply(archive_plan, confirmed_by_user=True)

        archived_info = frontmatter.load(self.info_path)
        self.assertEqual(
            archived_info["course_archive"],
            {
                "status": "archived",
                "changed_at": "2026-07-26T16:00:00+00:00",
            },
        )
        self.assertEqual(archived_info.content, original_body)
        for key, value in original_metadata.items():
            self.assertEqual(archived_info[key], value)
        self.assertEqual(self._non_info_hashes(), non_info_before)
        self.assertEqual(archived.course, self.course)
        self.assertEqual(archived.state, CourseArchiveState.ARCHIVED)
        self.assertEqual(archived.changed_files, (self.info_path,))
        self._assert_zero_token_outcome(archived)
        self.assertTrue(
            all(
                signal["course_archived"]
                for signal in export_assignment_signals(self.school)
            )
        )

        # Restoring is allowed even when a Canvas assignment has become open.
        self._write_assignment(
            "Assignment 1", "open", canvas_uid="assignment-1"
        )
        non_info_before_restore = self._non_info_hashes()
        restore_time = datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)
        restore_engine = CourseArchiveEngine(
            self.vault,
            now=lambda: restore_time,
        )
        restore_plan = restore_engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ACTIVE)
        )
        restored = restore_engine.apply(restore_plan, confirmed_by_user=True)

        restored_info = frontmatter.load(self.info_path)
        self.assertEqual(
            restored_info["course_archive"],
            {
                "status": "active",
                "changed_at": "2026-07-27T09:30:00+00:00",
            },
        )
        self.assertEqual(restored_info.content, original_body)
        for key, value in original_metadata.items():
            self.assertEqual(restored_info[key], value)
        self.assertEqual(self._non_info_hashes(), non_info_before_restore)
        self.assertEqual(restored.state, CourseArchiveState.ACTIVE)
        self.assertEqual(restored.changed_files, (self.info_path,))
        self._assert_zero_token_outcome(restored)
        self.assertFalse(
            any(
                signal["course_archived"]
                for signal in export_assignment_signals(self.school)
            )
        )

    def test_archive_rejects_open_canvas_assignment_without_changing_vault(self) -> None:
        self._write_assignment("Finished", "done", canvas_uid="finished")
        self._write_assignment("Still Open", "open", canvas_uid="still-open")
        before = self._physical_file_hashes(self.vault)

        plan = self.engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )
        self.assertFalse(plan.can_apply)
        self.assertEqual(plan.blocking_reasons, ("open_assignments",))
        self.assertEqual(plan.open_assignments, ("Still Open.md",))
        with self.assertRaisesRegex(CourseArchiveValidationError, "open|complete"):
            self.engine.apply(plan, confirmed_by_user=True)

        self.assertEqual(self._physical_file_hashes(self.vault), before)
        self.assertNotIn("course_archive", frontmatter.load(self.info_path).metadata)

    def test_unconfirmed_apply_changes_nothing(self) -> None:
        self._write_assignment("Finished", "done", canvas_uid="finished")
        plan = self.engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )
        before = self._physical_file_hashes(self.vault)

        with self.assertRaisesRegex(CourseArchiveValidationError, "confirm"):
            self.engine.apply(plan, confirmed_by_user=False)

        self.assertEqual(self._physical_file_hashes(self.vault), before)

    def test_stale_plan_preserves_concurrent_course_info_edit(self) -> None:
        self._write_assignment("Finished", "done", canvas_uid="finished")
        plan = self.engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )
        concurrent = self.info_path.read_text(encoding="utf-8").replace(
            "Keep this body exactly, including spacing.",
            "A concurrent personal edit must survive.",
        )
        self.info_path.write_text(concurrent, encoding="utf-8")
        after_edit = self._physical_file_hashes(self.vault)

        with self.assertRaises(CourseArchiveConflictError):
            self.engine.apply(plan, confirmed_by_user=True)

        self.assertEqual(self._physical_file_hashes(self.vault), after_edit)
        self.assertIn("concurrent personal edit", self.info_path.read_text())
        self.assertNotIn("course_archive", frontmatter.load(self.info_path).metadata)

    def test_archive_is_idempotent_and_does_not_rewrite_changed_at(self) -> None:
        self._write_assignment("Finished", "completed", canvas_uid="finished")
        first_plan = self.engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )
        first = self.engine.apply(first_plan, confirmed_by_user=True)
        first_bytes = self.info_path.read_bytes()

        later_engine = CourseArchiveEngine(
            self.vault,
            now=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
        )
        second_plan = later_engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )
        second = later_engine.apply(second_plan, confirmed_by_user=True)

        self.assertEqual(self.info_path.read_bytes(), first_bytes)
        self.assertEqual(second.changed_files, ())
        self.assertEqual(second.state, CourseArchiveState.ARCHIVED)
        self._assert_zero_token_outcome(first)
        self._assert_zero_token_outcome(second)

    def test_broken_vault_link_blocks_archive_without_changes(self) -> None:
        self._write_assignment("Finished", "cancelled", canvas_uid="finished")
        broken = self.vault / "Home.md"
        broken.write_text("# Home\n\n[[Missing Note]]\n", encoding="utf-8")
        before = self._physical_file_hashes(self.vault)

        plan = self.engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )
        self.assertFalse(plan.can_apply)
        self.assertEqual(plan.blocking_reasons, ("vault_link_issues",))
        with self.assertRaises(CourseArchiveIntegrityError):
            self.engine.apply(plan, confirmed_by_user=True)

        self.assertEqual(self._physical_file_hashes(self.vault), before)

    def test_post_commit_link_failure_restores_course_info(self) -> None:
        self._write_assignment("Finished", "done", canvas_uid="finished")
        original_info = self.info_path.read_bytes()
        plan = self.engine.prepare(
            CourseArchiveRequest(self.course, CourseArchiveState.ARCHIVED)
        )

        def concurrent_breakage() -> datetime:
            (self.vault / "Concurrent.md").write_text(
                "# Concurrent\n\n[[Missing During Commit]]\n",
                encoding="utf-8",
            )
            return self.now

        engine = CourseArchiveEngine(self.vault, now=concurrent_breakage)

        with self.assertRaises(CourseArchiveIntegrityError):
            engine.apply(plan, confirmed_by_user=True)

        self.assertEqual(self.info_path.read_bytes(), original_info)
        self.assertNotIn("course_archive", frontmatter.load(self.info_path).metadata)
        self.assertTrue((self.vault / "Concurrent.md").is_file())

    def test_course_path_escape_is_rejected_without_touching_outside_file(self) -> None:
        outside = self.base / "Outside Course" / "_Course Info.md"
        outside.parent.mkdir()
        outside.write_text("# Outside\n", encoding="utf-8")
        outside_before = outside.read_bytes()
        vault_before = self._physical_file_hashes(self.vault)

        with self.assertRaises(CourseArchiveValidationError):
            self.engine.prepare(
                CourseArchiveRequest("../Outside Course", CourseArchiveState.ARCHIVED)
            )

        self.assertEqual(outside.read_bytes(), outside_before)
        self.assertEqual(self._physical_file_hashes(self.vault), vault_before)

    def test_linked_course_directory_is_rejected_when_supported(self) -> None:
        external_course = self.base / "external-course"
        external_course.mkdir()
        external_info = external_course / "_Course Info.md"
        external_info.write_text("---\ncourse: Linked\n---\n\n# Linked\n", encoding="utf-8")
        linked_course = self.school / "Linked"
        try:
            linked_course.symlink_to(external_course, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            if os.name != "nt":
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked_course), str(external_course)],
                capture_output=True,
                text=True,
                check=False,
            )
            if junction.returncode != 0:
                self.skipTest("directory symlinks and junctions are unavailable")
        external_before = external_info.read_bytes()

        with self.assertRaises(CourseArchiveValidationError):
            self.engine.prepare(
                CourseArchiveRequest("Linked", CourseArchiveState.ARCHIVED)
            )

        self.assertEqual(external_info.read_bytes(), external_before)

    def _assert_zero_token_outcome(self, outcome) -> None:
        first = outcome.to_dict()
        second = outcome.to_dict()
        self.assertEqual(first, second)
        self.assertFalse(first["model_attempted"])
        self.assertEqual(first["input_tokens"], 0)
        self.assertEqual(first["output_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
