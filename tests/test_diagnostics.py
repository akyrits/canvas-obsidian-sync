from __future__ import annotations

import os
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import frontmatter

from study_analysis.diagnostics import (
    DiagnosticConflictError,
    DiagnosticCorrection,
    DiagnosticEngine,
    DiagnosticObservation,
    DiagnosticRequest,
    DiagnosticSubmission,
    DiagnosticValidationError,
    EvidenceKind,
    Familiarity,
    ObservationResult,
    ScriptedDiagnosticConversation,
)
from study_analysis.lifeos import export_assignment_signals, export_concept_signals
from study_analysis.transaction import commit_text_files


class DiagnosticEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        concepts = self.root / "Knowledge" / "Concepts"
        concepts.mkdir(parents=True)
        self.concept_path = concepts / "Tree Traversal.md"
        note = frontmatter.Post(
            "# Tree Traversal\n\n"
            "## Personal Notes\n\nKeep my private note.\n\n"
            "## Definition\n\nVisit every node in a defined order.\n\n"
            "## Why This Matters\n\nTraversal makes recursive structure usable.\n\n"
            "## Connections\n\n- `builds_on` → [[General Trees]]\n\n"
            "## Examples\n\n- Preorder visits a root before its subtrees.\n\n"
            "## Resources\n\n_None._\n\n"
            "## Source Trail\n\n- Course source, text section 1\n",
            type="concept",
            canonical_name="Tree Traversal",
            aliases=["Traversals"],
            familiarity="unknown",
            tags=["concept", "mine"],
            difficulty=3,
            difficulty_reason="Requires recursive reasoning.",
        )
        self.concept_path.write_text(frontmatter.dumps(note), encoding="utf-8")
        self.now = datetime(2026, 7, 26, 15, 0, tzinfo=timezone.utc)
        self.engine = DiagnosticEngine(self.root, now=lambda: self.now)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _submission(
        plan,
        results: tuple[ObservationResult, ...] | None = None,
    ) -> DiagnosticSubmission:
        selected = results or tuple(
            ObservationResult.DEMONSTRATED for _ in plan.prompts
        )
        observations = tuple(
            DiagnosticObservation(
                prompt_id=prompt.id,
                evidence_kind=prompt.evidence_kind,
                result=result,
                confidence=0.9 - index * 0.1,
                evidence_summary=f"Concise evidence for {prompt.capability.value}.",
            )
            for index, (prompt, result) in enumerate(zip(plan.prompts, selected))
        )
        return DiagnosticSubmission(
            plan_id=plan.id,
            canonical_concept=plan.canonical_concept,
            observations=observations,
            source="voice",
            confirmed_by_user=True,
        )

    def test_prepare_resolves_alias_without_writes_and_uses_short_voice_plan(self) -> None:
        before = self.concept_path.read_bytes()

        plan = self.engine.prepare(DiagnosticRequest("Traversals"))

        self.assertEqual(plan.canonical_concept, "Tree Traversal")
        self.assertEqual(
            [prompt.capability for prompt in plan.prompts],
            [Familiarity.RECOGNIZES, Familiarity.EXPLAINS],
        )
        self.assertIn("General Trees", plan.prompts[1].question)
        self.assertFalse(plan.evidence_stale)
        self.assertEqual(self.concept_path.read_bytes(), before)
        self.assertFalse((self.root / "Knowledge" / "Diagnostics").exists())

    def test_record_promotes_contiguously_and_preserves_private_notes(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        submission = self._submission(plan)

        outcome = self.engine.record(submission)

        self.assertEqual(outcome.familiarity, Familiarity.EXPLAINS)
        self.assertEqual(outcome.confidence, 0.8)
        self.assertFalse(outcome.evidence_stale)
        self.assertEqual(len(outcome.changed_files), 2)
        concept = frontmatter.load(self.concept_path)
        self.assertEqual(concept["familiarity"], "explains")
        self.assertEqual(concept["familiarity_confidence"], 0.8)
        self.assertIn("Keep my private note.", concept.content)
        self.assertEqual(concept["aliases"], ["Traversals"])
        self.assertEqual(concept["tags"], ["concept", "mine"])
        record_path = self.root / concept["diagnostic_records"][0]
        record = frontmatter.load(record_path)
        self.assertEqual(record["contiguous_level"], "explains")
        self.assertNotIn("raw transcript", record.content.casefold())

    def test_replay_is_idempotent(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        submission = self._submission(plan)
        first = self.engine.record(submission)
        concept_after_first = self.concept_path.read_bytes()
        record_after_first = first.changed_files[0].read_bytes()

        second = self.engine.record(submission)

        self.assertEqual(second.record_id, first.record_id)
        self.assertEqual(second.changed_files, ())
        self.assertEqual(self.concept_path.read_bytes(), concept_after_first)
        self.assertEqual(first.changed_files[0].read_bytes(), record_after_first)
        self.assertEqual(
            len(frontmatter.load(self.concept_path)["diagnostic_records"]), 1
        )

    def test_skipped_rung_records_evidence_without_false_promotion(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        submission = self._submission(
            plan,
            (ObservationResult.NOT_YET, ObservationResult.DEMONSTRATED),
        )

        outcome = self.engine.record(submission)

        self.assertEqual(outcome.familiarity, Familiarity.UNKNOWN)
        self.assertIsNone(outcome.confidence)
        self.assertEqual(
            frontmatter.load(self.concept_path)["familiarity"], "unknown"
        )
        self.assertEqual(len(outcome.changed_files), 2)

    def test_concurrent_semantic_change_fails_without_diagnostic_write(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        post = frontmatter.load(self.concept_path)
        post.content = post.content.replace(
            "Visit every node in a defined order.",
            "Visit every node exactly once in a defined order.",
        )
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        before = self.concept_path.read_bytes()

        with self.assertRaisesRegex(
            DiagnosticConflictError, "Concept or diagnostic evidence changed"
        ):
            self.engine.record(self._submission(plan))

        self.assertEqual(self.concept_path.read_bytes(), before)
        self.assertFalse((self.root / "Knowledge" / "Diagnostics").exists())

    def test_staleness_ignores_personal_notes_but_detects_semantic_change(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.engine.record(self._submission(plan))
        self.assertFalse(
            self.engine.prepare(DiagnosticRequest("Tree Traversal")).evidence_stale
        )

        post = frontmatter.load(self.concept_path)
        post.content = post.content.replace(
            "Keep my private note.", "Keep my revised private note."
        )
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        self.assertFalse(
            self.engine.prepare(DiagnosticRequest("Tree Traversal")).evidence_stale
        )

        post = frontmatter.load(self.concept_path)
        post.content = post.content.replace(
            "Visit every node in a defined order.",
            "Visit nodes according to an explicit recursive order.",
        )
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        stale = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.assertTrue(stale.evidence_stale)
        self.assertEqual(stale.prompts[0].capability, Familiarity.RECOGNIZES)

    def test_invalid_submission_leaves_vault_unchanged(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        before = self.concept_path.read_bytes()
        invalid = self._submission(plan)
        invalid_observation = DiagnosticObservation(
            prompt_id=invalid.observations[0].prompt_id,
            evidence_kind=invalid.observations[0].evidence_kind,
            result=ObservationResult.DEMONSTRATED,
            confidence=2.0,
            evidence_summary="Invalid confidence.",
        )
        invalid = DiagnosticSubmission(
            plan_id=plan.id,
            canonical_concept=plan.canonical_concept,
            observations=(invalid_observation, *invalid.observations[1:]),
            source="voice",
            confirmed_by_user=True,
        )
        with self.assertRaisesRegex(DiagnosticValidationError, "number from 0 to 1"):
            self.engine.record(invalid)

        self.assertEqual(self.concept_path.read_bytes(), before)
        self.assertFalse((self.root / "Knowledge" / "Diagnostics").exists())

    def test_atomic_commit_failure_leaves_concept_unchanged(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        before = self.concept_path.read_bytes()

        with patch(
            "study_analysis.diagnostics.commit_text_files",
            side_effect=OSError("simulated commit failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated commit failure"):
                self.engine.record(self._submission(plan))

        self.assertEqual(self.concept_path.read_bytes(), before)
        self.assertFalse((self.root / "Knowledge" / "Diagnostics").exists())

    def test_lifeos_projection_is_compact_and_marks_reassessment(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.engine.record(self._submission(plan))

        signals = export_concept_signals(self.root)

        self.assertEqual(len(signals), 1)
        self.assertEqual(
            set(signals[0]),
            {
                "concept",
                "familiarity",
                "confidence",
                "familiarity_as_of",
                "reassessment_due",
                "diagnostic_count",
            },
        )
        self.assertEqual(signals[0]["familiarity"], "explains")
        self.assertFalse(signals[0]["reassessment_due"])
        self.assertNotIn("Concise evidence", repr(signals))

        post = frontmatter.load(self.concept_path)
        post.content = post.content.replace(
            "Visit every node in a defined order.",
            "Visit nodes according to an explicit recursive order.",
        )
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        self.assertTrue(export_concept_signals(self.root)[0]["reassessment_due"])

    def test_lifeos_projection_rejects_nested_or_malformed_scalars(self) -> None:
        post = frontmatter.load(self.concept_path)
        post["familiarity_confidence"] = {"private": "nested"}
        post["familiarity_assessed_at"] = ["private"]
        post["diagnostic_records"] = "not-a-list"
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")

        signal = export_concept_signals(self.root)[0]

        self.assertIsNone(signal["confidence"])
        self.assertIsNone(signal["familiarity_as_of"])
        self.assertEqual(signal["diagnostic_count"], 0)
        self.assertNotIn("private", repr(signal))

    def test_lifeos_assignment_projection_is_strict_and_link_ancestors_fail_closed(self) -> None:
        school = self.root / "School" / "Course"
        school.mkdir(parents=True)
        assignment = frontmatter.Post(
            "# Assignment\n",
            canvas_uid="uid-1",
            course={"private": "nested"},
            due=["private"],
            status={"private": "nested"},
            analysis={
                "concepts": {"private": "nested"},
                "concept_difficulty_max": [5],
                "assignment_difficulty": {"private": 5},
                "effort": {"private": "large"},
                "assignment_difficulty_confidence": {"private": 1},
                "effort_confidence": [1],
                "analyzed_at": {"private": "now"},
            },
        )
        (school / "Assignment.md").write_text(
            frontmatter.dumps(assignment), encoding="utf-8"
        )

        signal = export_assignment_signals(self.root / "School")[0]

        self.assertIsNone(signal["course"])
        self.assertIsNone(signal["due"])
        self.assertEqual(signal["status"], "open")
        self.assertEqual(signal["concepts"], [])
        self.assertIsNone(signal["concept_difficulty"])
        self.assertEqual(signal["effort"], "unknown")
        self.assertNotIn("private", repr(signal))

        with patch(
            "study_analysis.lifeos._is_link_or_junction",
            side_effect=lambda path: path.name == "Knowledge",
        ):
            self.assertEqual(export_concept_signals(self.root), [])

    def test_next_plan_advances_to_application_and_transfer(self) -> None:
        first = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.engine.record(self._submission(first))

        second = self.engine.prepare(DiagnosticRequest("Tree Traversal"))

        self.assertEqual(
            [prompt.capability for prompt in second.prompts],
            [Familiarity.APPLIES, Familiarity.TRANSFERS],
        )

    def test_plan_id_reconstructs_custom_target_without_submission_state(self) -> None:
        plan = self.engine.prepare(
            DiagnosticRequest("Tree Traversal", Familiarity.APPLIES)
        )
        submission = self._submission(plan)

        outcome = self.engine.record(submission)

        self.assertEqual(plan.target, Familiarity.APPLIES)
        self.assertNotIn("target", submission.to_dict())
        self.assertEqual(outcome.familiarity, Familiarity.APPLIES)

    def test_one_call_scripted_adapter_records_only_after_confirmation(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        submission = self._submission(plan)
        conversation = ScriptedDiagnosticConversation(
            submission.observations, confirmed_by_user=True
        )

        outcome = self.engine.diagnose(
            DiagnosticRequest("Tree Traversal"), conversation
        )

        self.assertEqual(outcome.familiarity, Familiarity.EXPLAINS)

        other_plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        other = self._submission(other_plan)
        unconfirmed = ScriptedDiagnosticConversation(
            other.observations, confirmed_by_user=False
        )
        with self.assertRaisesRegex(DiagnosticValidationError, "not explicitly confirmed"):
            self.engine.diagnose(DiagnosticRequest("Tree Traversal"), unconfirmed)

    def test_low_confidence_and_prohibited_signals_cannot_promote(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        low = DiagnosticSubmission(
            plan_id=plan.id,
            canonical_concept=plan.canonical_concept,
            observations=tuple(
                DiagnosticObservation(
                    prompt_id=prompt.id,
                    evidence_kind=prompt.evidence_kind,
                    result=ObservationResult.DEMONSTRATED,
                    confidence=0.0,
                    evidence_summary=f"Current answer attempted {prompt.capability.value}.",
                )
                for prompt in plan.prompts
            ),
            source="voice",
            confirmed_by_user=True,
        )

        outcome = self.engine.record(low)

        self.assertEqual(outcome.familiarity, Familiarity.UNKNOWN)

        next_plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        valid = self._submission(next_plan)
        prohibited = DiagnosticObservation(
            prompt_id=valid.observations[0].prompt_id,
            evidence_kind=valid.observations[0].evidence_kind,
            result=ObservationResult.DEMONSTRATED,
            confidence=0.9,
            evidence_summary="I scored 95% on the homework.",
        )
        invalid = DiagnosticSubmission(
            plan_id=next_plan.id,
            canonical_concept=next_plan.canonical_concept,
            observations=(prohibited, *valid.observations[1:]),
            source="voice",
            confirmed_by_user=True,
        )
        with self.assertRaisesRegex(DiagnosticValidationError, "current answer"):
            self.engine.record(invalid)

    def test_typed_evidence_must_match_prompt_capability(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        valid = self._submission(plan)
        wrong = DiagnosticObservation(
            prompt_id=valid.observations[0].prompt_id,
            evidence_kind=EvidenceKind.NOVEL_APPLICATION,
            result=ObservationResult.DEMONSTRATED,
            confidence=0.9,
            evidence_summary="The learner supplied a current definition.",
        )
        submission = DiagnosticSubmission(
            plan_id=plan.id,
            canonical_concept=plan.canonical_concept,
            observations=(wrong, *valid.observations[1:]),
            source="voice",
            confirmed_by_user=True,
        )

        with self.assertRaisesRegex(DiagnosticValidationError, "requires own_definition"):
            self.engine.record(submission)

    def test_direct_constructor_text_is_normalized_and_markdown_escaped(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        valid = self._submission(plan)
        injected = DiagnosticObservation(
            prompt_id=valid.observations[0].prompt_id,
            evidence_kind=valid.observations[0].evidence_kind,
            result=ObservationResult.DEMONSTRATED,
            confidence=0.9,
            evidence_summary="Current definition.\n\n## Forged Section",
        )
        submission = DiagnosticSubmission(
            plan_id=plan.id,
            canonical_concept=plan.canonical_concept,
            observations=(injected, *valid.observations[1:]),
            source="voice",
            confirmed_by_user=True,
        )

        outcome = self.engine.record(submission)
        record = frontmatter.load(outcome.changed_files[0])

        self.assertNotIn("\n## Forged Section", record.content)
        self.assertIn(r"\#\# Forged Section", record.content)

    def test_provenance_and_difficulty_changes_do_not_stale_familiarity(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.engine.record(self._submission(plan))
        post = frontmatter.load(self.concept_path)
        post["difficulty"] = 5
        post["difficulty_reason"] = "Newly calibrated."
        post.content = post.content.replace(
            "- Course source, text section 1",
            "- Course source, text section 2",
        ).replace("_None._", "- A new external resource")
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")

        self.assertFalse(
            self.engine.prepare(DiagnosticRequest("Tree Traversal")).evidence_stale
        )

    def test_concurrent_records_from_one_plan_do_not_lose_references(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        first = self._submission(plan)
        second_observations = tuple(
            DiagnosticObservation(
                prompt_id=item.prompt_id,
                evidence_kind=item.evidence_kind,
                result=item.result,
                confidence=item.confidence,
                evidence_summary=item.evidence_summary + " Second assessor.",
            )
            for item in first.observations
        )
        second = DiagnosticSubmission(
            plan_id=plan.id,
            canonical_concept=plan.canonical_concept,
            observations=second_observations,
            source="voice",
            confirmed_by_user=True,
        )
        barrier = __import__("threading").Barrier(2)
        real_commit = commit_text_files

        def coordinated_commit(*args, **kwargs):
            barrier.wait(timeout=5)
            return real_commit(*args, **kwargs)

        with patch(
            "study_analysis.diagnostics.commit_text_files",
            side_effect=coordinated_commit,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda item: self._record_or_error(item),
                        (first, second),
                    )
                )

        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        self.assertEqual(
            sum(isinstance(item, DiagnosticConflictError) for item in results), 1
        )
        concept = frontmatter.load(self.concept_path)
        self.assertEqual(len(concept["diagnostic_records"]), 1)
        records = list((self.root / "Knowledge" / "Diagnostics").rglob("diag-*.md"))
        self.assertEqual(len(records), 1)

    def _record_or_error(self, submission):
        try:
            return self.engine.record(submission)
        except Exception as exc:
            return exc

    def test_unsafe_canonical_directory_is_rejected(self) -> None:
        post = frontmatter.load(self.concept_path)
        post["canonical_name"] = ".."
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        plan = self.engine.prepare(DiagnosticRequest(".."))

        with self.assertRaisesRegex(DiagnosticValidationError, "safe diagnostic directory"):
            self.engine.record(self._submission(plan))

        self.assertFalse((self.root / "Knowledge" / "diag-").exists())

    def test_canonical_name_fallback_does_not_immediately_go_stale(self) -> None:
        post = frontmatter.load(self.concept_path)
        del post["canonical_name"]
        self.concept_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))

        outcome = self.engine.record(self._submission(plan))

        self.assertFalse(outcome.evidence_stale)
        self.assertFalse(
            self.engine.prepare(DiagnosticRequest("Tree Traversal")).evidence_stale
        )

    def test_correction_is_append_only_and_requires_reassessment(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        recorded = self.engine.record(self._submission(plan))
        concept = frontmatter.load(self.concept_path)
        record_path = self.root / concept["diagnostic_records"][0]
        record_before = record_path.read_bytes()

        request = DiagnosticCorrection(
            canonical_concept="Tree Traversal",
            record_id=recorded.record_id,
            correction="The causal connection summary overstated my answer.",
            confirmed_by_user=True,
        )
        corrected = self.engine.correct(request)

        self.assertTrue(corrected.reassessment_due)
        self.assertEqual(record_path.read_bytes(), record_before)
        concept = frontmatter.load(self.concept_path)
        self.assertTrue(concept["familiarity_review_required"])
        self.assertEqual(len(concept["diagnostic_amendments"]), 1)
        amendment = frontmatter.load(self.root / concept["diagnostic_amendments"][0])
        self.assertEqual(amendment["record_id"], recorded.record_id)
        self.assertIn("overstated my answer", amendment.content)
        self.assertTrue(export_concept_signals(self.root)[0]["reassessment_due"])

        replay = self.engine.correct(request)
        self.assertEqual(replay.changed_files, ())

        reassessment = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.assertEqual(
            [prompt.capability for prompt in reassessment.prompts],
            [Familiarity.RECOGNIZES, Familiarity.EXPLAINS],
        )
        refreshed = self.engine.record(self._submission(reassessment))
        self.assertFalse(refreshed.evidence_stale)
        self.assertFalse(
            frontmatter.load(self.concept_path)["familiarity_review_required"]
        )

    def test_unconfirmed_correction_changes_nothing(self) -> None:
        plan = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        recorded = self.engine.record(self._submission(plan))
        before = self.concept_path.read_bytes()

        with self.assertRaisesRegex(DiagnosticValidationError, "explicitly confirmed"):
            self.engine.correct(
                DiagnosticCorrection(
                    "Tree Traversal",
                    recorded.record_id,
                    "A correction that was not confirmed.",
                )
            )

        self.assertEqual(self.concept_path.read_bytes(), before)

    def test_transfer_reassessment_accumulates_across_short_review_sessions(self) -> None:
        foundation = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.engine.record(self._submission(foundation))
        advanced = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        transferred = self.engine.record(self._submission(advanced))
        self.assertEqual(transferred.familiarity, Familiarity.TRANSFERS)

        self.engine.correct(
            DiagnosticCorrection(
                "Tree Traversal",
                transferred.record_id,
                "The evidence needs a fresh review across the whole ladder.",
                confirmed_by_user=True,
            )
        )
        first_review = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.assertEqual(
            [prompt.capability for prompt in first_review.prompts],
            [Familiarity.RECOGNIZES, Familiarity.EXPLAINS],
        )
        still_stale = self.engine.record(self._submission(first_review))
        self.assertTrue(still_stale.evidence_stale)

        second_review = self.engine.prepare(DiagnosticRequest("Tree Traversal"))
        self.assertEqual(
            [prompt.capability for prompt in second_review.prompts],
            [Familiarity.APPLIES, Familiarity.TRANSFERS],
        )
        refreshed = self.engine.record(self._submission(second_review))

        self.assertEqual(refreshed.familiarity, Familiarity.TRANSFERS)
        self.assertFalse(refreshed.evidence_stale)

    def test_diagnostic_telemetry_is_content_free_and_reports_zero_tokens(self) -> None:
        log_path = self.root / "runs.log"
        engine = DiagnosticEngine(
            self.root, now=lambda: self.now, log_path=log_path
        )
        plan = engine.prepare(DiagnosticRequest("Tree Traversal"))
        submission = self._submission(plan)

        engine.record(submission)
        invalid = DiagnosticSubmission(
            plan_id=plan.id,
            canonical_concept=plan.canonical_concept,
            observations=submission.observations,
            source="voice",
            confirmed_by_user=False,
        )
        with self.assertRaises(DiagnosticValidationError):
            engine.record(invalid)

        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        self.assertEqual([entry["status"] for entry in entries], ["success", "failure"])
        self.assertTrue(all(entry["model_attempted"] is False for entry in entries))
        self.assertTrue(all(entry["input_tokens"] == 0 for entry in entries))
        self.assertTrue(all(entry["output_tokens"] == 0 for entry in entries))
        serialized = repr(entries)
        self.assertNotIn("Tree Traversal", serialized)
        self.assertNotIn("Concise evidence", serialized)


class AtomicTextTransactionTests(unittest.TestCase):
    def test_second_replace_failure_restores_first_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("before", encoding="utf-8")
            real_replace = os.replace
            calls = 0

            def fail_second(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second replace failed")
                return real_replace(source, target)

            with patch("study_analysis.transaction.os.replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "second replace failed"):
                    commit_text_files({first: "after", second: "new"})

            self.assertEqual(first.read_text(encoding="utf-8"), "before")
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
