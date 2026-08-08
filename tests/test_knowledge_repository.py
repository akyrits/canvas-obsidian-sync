from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import frontmatter

from study_analysis.knowledge import (
    KnowledgeCaptureOutcome,
    KnowledgeCaptureRequest,
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from study_analysis.transaction import commit_text_files as real_commit_text_files


class KnowledgeRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        self.vault.mkdir()
        self.repository = KnowledgeRepository(self.vault)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_concept(self, filename: str, canonical_name: str) -> Path:
        path = self.vault / "Knowledge" / "Concepts" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "type: concept\n"
            f"canonical_name: {canonical_name}\n"
            "aliases: []\n"
            "familiarity: unknown\n"
            "---\n\n"
            f"# {canonical_name}\n\n"
            "## Personal Notes\n\n"
            "User-owned concept detail.\n",
            encoding="utf-8",
        )
        return path

    def _write_knowledge_note(self, folder: str, name: str) -> Path:
        path = self.vault / "Knowledge" / folder / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {name}\n\nUser-owned knowledge note.\n",
            encoding="utf-8",
        )
        return path

    def _snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.vault).as_posix(): path.read_bytes()
            for path in sorted(self.vault.rglob("*.md"), key=lambda item: item.as_posix().casefold())
        }

    def _directories(self) -> set[str]:
        return {
            path.relative_to(self.vault).as_posix()
            for path in self.vault.rglob("*")
            if path.is_dir()
        }

    @staticmethod
    def _section(content: str, heading: str) -> str:
        lines = content.splitlines()
        start = None
        for index, line in enumerate(lines):
            if line.strip() == f"## {heading}":
                start = index + 1
                break
        if start is None:
            return ""
        end = len(lines)
        for index in range(start, len(lines)):
            if lines[index].startswith("## "):
                end = index
                break
        return "\n".join(lines[start:end]).strip()

    def _assert_zero_model_usage(self, outcome) -> None:
        self.assertFalse(outcome.model_attempted)
        self.assertEqual(outcome.input_tokens, 0)
        self.assertEqual(outcome.output_tokens, 0)

    def _assert_zero_external_usage(self, outcome) -> None:
        payload = outcome.to_dict()
        self.assertFalse(payload["model_attempted"])
        self.assertFalse(payload["network_attempted"])
        self.assertEqual(payload["provider_requests"], 0)
        self.assertEqual(payload["input_tokens"], 0)
        self.assertEqual(payload["output_tokens"], 0)
        self.assertEqual(payload["estimated_cost_usd"], 0.0)

    def test_refresh_creates_landings_and_indexes_concepts_deterministically(self) -> None:
        zebra = self._write_concept("01-first-created.md", "Zebra Tree")
        alpha = self._write_concept("99-second-created.md", "Alpha Tree")
        concept_bytes = {zebra: zebra.read_bytes(), alpha: alpha.read_bytes()}

        outcome = self.repository.refresh()

        hub = self.vault / "Knowledge" / "Knowledge Hub.md"
        inbox = self.vault / "Knowledge" / "Inbox" / "_Inbox.md"
        sources = self.vault / "Knowledge" / "Sources" / "_Sources.md"
        maps = self.vault / "Knowledge" / "Maps" / "_Maps.md"
        self.assertEqual(set(outcome.changed_files), {hub, inbox, sources, maps})
        self.assertIsInstance(outcome.changed_files, tuple)
        for path in (hub, inbox, sources, maps):
            self.assertTrue(path.is_file(), path)
        hub_text = hub.read_text(encoding="utf-8")
        self.assertIn("Alpha Tree", hub_text)
        self.assertIn("Zebra Tree", hub_text)
        self.assertLess(hub_text.index("Alpha Tree"), hub_text.index("Zebra Tree"))
        self.assertIn("Inbox", hub_text)
        self.assertIn("Sources", hub_text)
        self.assertIn("Maps", hub_text)
        self.assertEqual(outcome.concept_count, 2)
        self.assertEqual(outcome.inbox_count, 0)
        self.assertEqual(outcome.source_count, 0)
        self.assertEqual(outcome.map_count, 0)
        self.assertRegex(outcome.revision, r"^[0-9a-f]{64}$")
        self._assert_zero_model_usage(outcome)
        self.assertEqual(
            {path: path.read_bytes() for path in concept_bytes}, concept_bytes
        )

    def test_refresh_is_idempotent_and_preserves_personal_hub_content(self) -> None:
        self._write_concept("Tree Traversal.md", "Tree Traversal")
        hub = self.vault / "Knowledge" / "Knowledge Hub.md"
        hub.parent.mkdir(parents=True, exist_ok=True)
        hub.write_text(
            "---\n"
            "owner_note: Keep this metadata exactly.\n"
            "nested:\n"
            "  label: Personal metadata\n"
            "---\n\n"
            "# Knowledge Hub\n\n"
            "## Personal Navigation\n\n"
            "- [[Personal Dashboard]]\n"
            "- Keep this custom route.\n\n"
            "## Concepts\n\n"
            "Old generated index.\n",
            encoding="utf-8",
        )

        first = self.repository.refresh()
        after_first = self._snapshot()
        second = self.repository.refresh()

        updated = frontmatter.load(hub)
        self.assertEqual(updated["owner_note"], "Keep this metadata exactly.")
        self.assertEqual(updated["nested"], {"label": "Personal metadata"})
        self.assertEqual(
            self._section(updated.content, "Personal Navigation"),
            "- [[Personal Dashboard]]\n- Keep this custom route.",
        )
        self.assertIn("Tree Traversal", updated.content)
        self.assertIn(hub, first.changed_files)
        self.assertEqual(second.changed_files, ())
        self.assertEqual(self._snapshot(), after_first)
        self.assertEqual(second.concept_count, 1)
        self.assertEqual(second.inbox_count, 0)
        self.assertEqual(second.source_count, 0)
        self.assertEqual(second.map_count, 0)
        self.assertEqual(second.revision, first.revision)
        self._assert_zero_model_usage(first)
        self._assert_zero_model_usage(second)

    def test_duplicate_canonical_concepts_are_rejected_before_any_write(self) -> None:
        first = self._write_concept("Traversal A.md", "Tree Traversal")
        second = self._write_concept("Traversal B.md", "Tree Traversal")
        before_files = self._snapshot()
        before_directories = self._directories()

        with self.assertRaisesRegex(
            KnowledgeRepositoryError, r"(?i)duplicate.*canonical|canonical.*duplicate"
        ):
            self.repository.refresh()

        self.assertEqual(self._snapshot(), before_files)
        self.assertEqual(self._directories(), before_directories)
        self.assertEqual(first.read_bytes(), before_files["Knowledge/Concepts/Traversal A.md"])
        self.assertEqual(second.read_bytes(), before_files["Knowledge/Concepts/Traversal B.md"])
        self.assertFalse((self.vault / "Knowledge" / "Knowledge Hub.md").exists())

    def test_hostile_markdown_filename_is_rejected_before_managed_writes(self) -> None:
        hostile = self._write_knowledge_note("Inbox", "Topic]] [[Injected")
        hostile_original = hostile.read_bytes()
        before_files = self._snapshot()
        before_directories = self._directories()

        with self.assertRaisesRegex(
            KnowledgeRepositoryError, r"(?i)unsafe.*link|link.*delimiter"
        ):
            self.repository.refresh()

        self.assertEqual(hostile.read_bytes(), hostile_original)
        self.assertEqual(self._snapshot(), before_files)
        self.assertEqual(self._directories(), before_directories)
        self.assertFalse((self.vault / "Knowledge" / "Knowledge Hub.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Inbox" / "_Inbox.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Sources" / "_Sources.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Maps" / "_Maps.md").exists())

    def test_in_lock_inventory_guard_preserves_external_concept_edit(self) -> None:
        concept = self._write_concept("Tree Traversal.md", "Tree Traversal")
        external_edit = concept.read_bytes() + b"\nConcurrent external concept edit.\n"

        def edit_then_commit(planned, **kwargs):
            concept.write_bytes(external_edit)
            return real_commit_text_files(planned, **kwargs)

        with patch(
            "study_analysis.knowledge.commit_text_files",
            side_effect=edit_then_commit,
        ) as commit:
            with self.assertRaisesRegex(
                KnowledgeRepositoryError, r"(?i)inventory changed|commit safely"
            ):
                self.repository.refresh()

        commit.assert_called_once()
        self.assertEqual(concept.read_bytes(), external_edit)
        self.assertEqual(
            self._snapshot(),
            {"Knowledge/Concepts/Tree Traversal.md": external_edit},
        )
        self.assertFalse((self.vault / "Knowledge" / "Knowledge Hub.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Inbox" / "_Inbox.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Sources" / "_Sources.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Maps" / "_Maps.md").exists())

    def test_refresh_indexes_non_landing_notes_by_knowledge_area(self) -> None:
        self._write_knowledge_note("Inbox", "Zeta Capture")
        self._write_knowledge_note("Inbox", "Alpha Capture")
        self._write_knowledge_note("Sources", "Algorithms Book")
        self._write_knowledge_note("Maps", "Learning Roadmap")

        outcome = self.repository.refresh()

        inbox_text = (
            self.vault / "Knowledge" / "Inbox" / "_Inbox.md"
        ).read_text(encoding="utf-8")
        source_text = (
            self.vault / "Knowledge" / "Sources" / "_Sources.md"
        ).read_text(encoding="utf-8")
        map_text = (
            self.vault / "Knowledge" / "Maps" / "_Maps.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Alpha Capture", inbox_text)
        self.assertIn("Zeta Capture", inbox_text)
        self.assertLess(
            inbox_text.index("Alpha Capture"), inbox_text.index("Zeta Capture")
        )
        self.assertIn("Algorithms Book", source_text)
        self.assertIn("Learning Roadmap", map_text)
        self.assertNotIn("_Inbox", inbox_text)
        self.assertNotIn("_Sources", source_text)
        self.assertNotIn("_Maps", map_text)
        self.assertEqual(outcome.concept_count, 0)
        self.assertEqual(outcome.inbox_count, 2)
        self.assertEqual(outcome.source_count, 1)
        self.assertEqual(outcome.map_count, 1)
        self._assert_zero_model_usage(outcome)

    def test_capture_creates_inbox_note_and_refreshes_repository_atomically(self) -> None:
        request = KnowledgeCaptureRequest(
            title="Local Search Follow-up",
            content="Compare deterministic retrieval options before choosing one.",
        )

        outcome = self.repository.capture(request)

        self.assertIsInstance(outcome, KnowledgeCaptureOutcome)
        self.assertTrue(outcome.created)
        capture = outcome.capture_path
        inbox_root = self.vault / "Knowledge" / "Inbox"
        hub = self.vault / "Knowledge" / "Knowledge Hub.md"
        inbox = inbox_root / "_Inbox.md"
        sources = self.vault / "Knowledge" / "Sources" / "_Sources.md"
        maps = self.vault / "Knowledge" / "Maps" / "_Maps.md"
        self.assertEqual(capture.parent, inbox_root)
        self.assertEqual(
            set(outcome.changed_files), {capture, hub, inbox, sources, maps}
        )
        captures = tuple(
            path for path in inbox_root.glob("*.md") if path.name != "_Inbox.md"
        )
        self.assertEqual(captures, (capture,))
        captured = frontmatter.load(capture)
        self.assertEqual(captured["type"], "knowledge-capture")
        self.assertIn("Local Search Follow-up", captured.content)
        self.assertIn(
            "Compare deterministic retrieval options before choosing one.",
            captured.content,
        )
        for path in (hub, inbox, sources, maps):
            self.assertTrue(path.is_file(), path)
        self.assertIn("Local Search Follow-up", hub.read_text(encoding="utf-8"))
        self.assertIn("Local Search Follow-up", inbox.read_text(encoding="utf-8"))
        self.assertEqual(outcome.concept_count, 0)
        self.assertEqual(outcome.inbox_count, 1)
        self.assertEqual(outcome.source_count, 0)
        self.assertEqual(outcome.map_count, 0)
        self.assertRegex(outcome.revision, r"^[0-9a-f]{64}$")
        self._assert_zero_external_usage(outcome)

    def test_exact_capture_retry_is_idempotent(self) -> None:
        request = KnowledgeCaptureRequest(
            title="Reusable Capture",
            content="These exact bytes should identify one durable capture.",
        )

        first = self.repository.capture(request)
        after_first = self._snapshot()
        second = self.repository.capture(request)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.capture_path, first.capture_path)
        self.assertEqual(second.changed_files, ())
        self.assertEqual(second.inbox_count, 1)
        self.assertEqual(second.revision, first.revision)
        self.assertEqual(self._snapshot(), after_first)
        self._assert_zero_external_usage(first)
        self._assert_zero_external_usage(second)

    def test_exact_capture_retry_follows_unchanged_note_moved_to_sources(self) -> None:
        request = KnowledgeCaptureRequest(
            title="Move-safe Capture",
            content="This identity should survive a user-controlled move.",
        )
        first = self.repository.capture(request)
        original_path = first.capture_path
        original_bytes = original_path.read_bytes()
        moved_path = self.vault / "Knowledge" / "Sources" / "Renamed Capture.md"

        original_path.rename(moved_path)
        self.assertEqual(moved_path.read_bytes(), original_bytes)

        retry = self.repository.capture(request)

        self.assertFalse(retry.created)
        self.assertEqual(retry.capture_path, moved_path)
        self.assertEqual(moved_path.read_bytes(), original_bytes)
        self.assertFalse(original_path.exists())
        inbox_captures = tuple(
            path
            for path in (self.vault / "Knowledge" / "Inbox").glob("*.md")
            if path.name != "_Inbox.md"
        )
        self.assertEqual(inbox_captures, ())
        self.assertNotIn(moved_path, retry.changed_files)
        self.assertEqual(retry.inbox_count, 0)
        self.assertEqual(retry.source_count, 1)
        self.assertIn(
            "Renamed Capture",
            (
                self.vault / "Knowledge" / "Sources" / "_Sources.md"
            ).read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "Move-safe Capture",
            (self.vault / "Knowledge" / "Inbox" / "_Inbox.md").read_text(
                encoding="utf-8"
            ),
        )
        self._assert_zero_external_usage(retry)

    def test_same_capture_title_with_different_content_creates_distinct_notes(self) -> None:
        first = self.repository.capture(
            KnowledgeCaptureRequest(
                title="Shared Topic",
                content="First independent observation.",
            )
        )
        second = self.repository.capture(
            KnowledgeCaptureRequest(
                title="Shared Topic",
                content="Second independent observation.",
            )
        )

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotEqual(second.capture_path, first.capture_path)
        self.assertIn(
            "First independent observation.",
            first.capture_path.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Second independent observation.",
            second.capture_path.read_text(encoding="utf-8"),
        )
        captures = tuple(
            path
            for path in (self.vault / "Knowledge" / "Inbox").glob("*.md")
            if path.name != "_Inbox.md"
        )
        self.assertEqual(set(captures), {first.capture_path, second.capture_path})
        self.assertEqual(second.inbox_count, 2)
        self._assert_zero_external_usage(second)

    def test_invalid_captures_fail_before_writing(self) -> None:
        invalid = (
            ("", "Valid body"),
            ("Valid title", ""),
            ("Valid title", "x" * 1_000_001),
            ("Valid title", "Body with a null control\x00character"),
            ("Topic]] [[Injected", "Valid body"),
            (123, "Valid body"),
            ("Valid title", None),
        )

        for title, content in invalid:
            with self.subTest(title=title, content_type=type(content).__name__):
                before_files = self._snapshot()
                before_directories = self._directories()
                with self.assertRaises(KnowledgeRepositoryError):
                    self.repository.capture(
                        KnowledgeCaptureRequest(title=title, content=content)
                    )
                self.assertEqual(self._snapshot(), before_files)
                self.assertEqual(self._directories(), before_directories)

    def test_capture_inventory_race_rolls_back_capture_and_indexes(self) -> None:
        concept = self._write_concept("Tree Traversal.md", "Tree Traversal")
        external_edit = concept.read_bytes() + b"\nConcurrent external concept edit.\n"
        guard_observation = {"calls": 0, "race_at": None}

        def race_at_final_guard(planned, **kwargs):
            original_guard = kwargs["state_guard"]
            race_at = len(planned) + 3
            guard_observation["race_at"] = race_at

            def racing_guard():
                guard_observation["calls"] += 1
                if guard_observation["calls"] == race_at:
                    concept.write_bytes(external_edit)
                original_guard()

            return real_commit_text_files(
                planned,
                **{**kwargs, "state_guard": racing_guard},
            )

        with patch(
            "study_analysis.knowledge.commit_text_files",
            side_effect=race_at_final_guard,
        ) as commit:
            with self.assertRaisesRegex(
                KnowledgeRepositoryError, r"(?i)inventory changed|commit safely"
            ):
                self.repository.capture(
                    KnowledgeCaptureRequest(
                        title="Capture During Race",
                        content="This planned note must be rolled back.",
                    )
                )

        commit.assert_called_once()
        self.assertEqual(guard_observation["calls"], guard_observation["race_at"])
        self.assertEqual(concept.read_bytes(), external_edit)
        self.assertEqual(
            self._snapshot(),
            {"Knowledge/Concepts/Tree Traversal.md": external_edit},
        )
        self.assertFalse((self.vault / "Knowledge" / "Knowledge Hub.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Inbox" / "_Inbox.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Sources" / "_Sources.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Maps" / "_Maps.md").exists())

    def test_capture_final_guard_preserves_external_deletion_and_cleans_rollback(self) -> None:
        before_files = self._snapshot()
        before_directories = self._directories()
        observed = {"capture_path": None, "existed_before_delete": False}

        def delete_capture_at_final_guard(planned, **kwargs):
            capture_path = next(
                path
                for path in planned
                if path.parent.name == "Inbox" and path.name != "_Inbox.md"
            )
            original_final_guard = kwargs["final_state_guard"]
            observed["capture_path"] = capture_path

            def deleting_final_guard():
                observed["existed_before_delete"] = capture_path.is_file()
                capture_path.unlink()
                original_final_guard()

            return real_commit_text_files(
                planned,
                **{**kwargs, "final_state_guard": deleting_final_guard},
            )

        with patch(
            "study_analysis.knowledge.commit_text_files",
            side_effect=delete_capture_at_final_guard,
        ) as commit:
            with self.assertRaisesRegex(
                KnowledgeRepositoryError,
                r"(?i)commit safely|inventory changed|safely restored",
            ):
                self.repository.capture(
                    KnowledgeCaptureRequest(
                        title="Deleted During Commit",
                        content="The final-state guard must reject this deletion.",
                    )
                )

        commit.assert_called_once()
        self.assertTrue(observed["existed_before_delete"])
        capture_path = observed["capture_path"]
        self.assertIsInstance(capture_path, Path)
        self.assertFalse(capture_path.exists())
        self.assertEqual(self._snapshot(), before_files)
        self.assertEqual(self._directories(), before_directories)
        self.assertFalse((self.vault / "Knowledge" / "Knowledge Hub.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Inbox" / "_Inbox.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Sources" / "_Sources.md").exists())
        self.assertFalse((self.vault / "Knowledge" / "Maps" / "_Maps.md").exists())


if __name__ == "__main__":
    unittest.main()
