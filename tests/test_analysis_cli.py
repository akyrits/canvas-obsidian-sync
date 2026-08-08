from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frontmatter


_TEST_VAULT = Path(tempfile.gettempdir()) / "canvas-obsidian-cli-test-vault"
with patch.dict(
    os.environ,
    {
        "CANVAS_ICS_URL": "https://example.invalid/calendar.ics",
        "VAULT_PATH": str(_TEST_VAULT),
    },
):
    # Importing the CLI normally loads project configuration. Keep this test
    # independent of (and unable to read) the developer's real .env file.
    with patch("dotenv.load_dotenv", return_value=False):
        from agent import cli

class AnalysisCliRoutingTests(unittest.TestCase):
    @staticmethod
    def _fake_outcome() -> SimpleNamespace:
        return SimpleNamespace(
            concepts=("Trees",),
            changed_files=(),
            solution_path=None,
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            input_truncated=False,
        )

    def test_cli_uses_internal_context_routing_and_removes_canary_flag(self) -> None:
        parser = cli.build_parser()
        assignment = Path("Assignment 6.md")

        with (
            patch.object(cli.vault_query, "find_note", return_value=assignment),
            patch.object(cli, "adapter_from_env") as adapter,
            patch.object(cli, "AnalysisEngine") as engine_type,
            patch("builtins.print"),
        ):
            engine_type.return_value.analyze.return_value = self._fake_outcome()
            adapter.return_value = object()

            normal = parser.parse_args(["analyze-concepts", "Assignment 6"])
            self.assertEqual(cli.cmd_analyze_concepts(normal), 0)
            self.assertNotIn("context_compiler", engine_type.call_args.kwargs)
            request = engine_type.return_value.analyze.call_args.args[0]
            self.assertFalse(hasattr(request, "require_contract"))

        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["analyze-concepts", "Assignment 6", "--selective-canary"]
                )

    def test_prep_routes_study_research_through_analysis_engine(self) -> None:
        parser = cli.build_parser()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assignments_root = root / "School"
            assignment = assignments_root / "Course 101" / "Assignment 6.md"
            assignment.parent.mkdir(parents=True)
            assignment.write_text("user-owned assignment bytes", encoding="utf-8")
            before = assignment.read_bytes()
            model_response = root / "model-response.json"
            research_response = root / "research-response.json"
            model_adapter = object()
            research_engine = object()

            with (
                patch.object(cli.config, "VAULT_PATH", root),
                patch.object(cli.config, "ASSIGNMENTS_ROOT", assignments_root),
                patch.object(
                    cli.vault_query, "find_note", return_value=assignment
                ) as find_note,
                patch.object(
                    cli, "adapter_from_env", return_value=model_adapter
                ) as model_factory,
                patch.object(
                    cli,
                    "_configured_research_engine",
                    return_value=research_engine,
                ) as research_factory,
                patch.object(cli, "research_adapter_from_env") as direct_research,
                patch.object(cli, "AnalysisEngine") as engine_type,
                patch.object(cli.vault_write, "set_section") as direct_vault_write,
                patch("builtins.print"),
            ):
                engine_type.return_value.analyze.return_value = self._fake_outcome()
                args = parser.parse_args(
                    [
                        "prep",
                        "Assignment 6",
                        "--response-file",
                        str(model_response),
                        "--research-response-file",
                        str(research_response),
                    ]
                )

                self.assertEqual(cli.cmd_prep(args), 0)

            find_note.assert_called_once_with(assignments_root, "Assignment 6")
            model_factory.assert_called_once_with(response_file=model_response)
            research_factory.assert_called_once_with(research_response)
            engine_type.assert_called_once_with(
                vault_root=root,
                adapter=model_adapter,
                research_engine=research_engine,
                log_path=cli._RUN_LOG,
            )
            request = engine_type.return_value.analyze.call_args.args[0]
            self.assertEqual(request.mode, cli.AnalysisMode.STUDY)
            self.assertTrue(request.include_research)
            direct_research.assert_not_called()
            direct_vault_write.assert_not_called()
            self.assertEqual(assignment.read_bytes(), before)

    def test_prep_open_hard_attempt_cap_counts_provider_failure(self) -> None:
        parser = cli.build_parser()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assignments_root = root / "School"
            course = assignments_root / "Course 101"
            course.mkdir(parents=True)
            for index in (1, 2):
                note = frontmatter.Post(
                    f"# Assignment {index}\n\n## Assignment Details\n\nPrompt.\n",
                    tags=["task"],
                    status="open",
                    due=f"2026-07-{index:02d}T12:00:00+00:00",
                )
                (course / f"Assignment {index}.md").write_text(
                    frontmatter.dumps(note), encoding="utf-8"
                )

            with (
                patch.object(cli.config, "VAULT_PATH", root),
                patch.object(cli.config, "ASSIGNMENTS_ROOT", assignments_root),
                patch.object(cli, "adapter_from_env", return_value=object()),
                patch.object(
                    cli, "_configured_research_engine", return_value=object()
                ),
                patch.object(cli, "AnalysisEngine") as engine_type,
                patch("builtins.print"),
            ):
                engine_type.return_value.analyze.side_effect = RuntimeError(
                    "provider failed after consuming one attempt"
                )
                args = parser.parse_args(["prep-open", "--max-attempts", "1"])

                self.assertEqual(cli.cmd_prep_open(args), 1)

            self.assertEqual(engine_type.return_value.analyze.call_count, 1)

        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["prep-open", "--max-attempts", "0"])

    def test_diagnostic_cli_adapters_do_not_construct_a_model(self) -> None:
        parser = cli.build_parser()
        prompt = SimpleNamespace(
            id="recognizes-v1",
            evidence_kind=SimpleNamespace(value="own_definition"),
        )
        plan = SimpleNamespace(
            id="plan-test",
            canonical_concept="Tree Traversal",
            prompts=(prompt,),
            to_dict=lambda: {"id": "plan-test", "prompts": []},
        )
        outcome = SimpleNamespace(
            to_dict=lambda: {
                "canonical_concept": "Tree Traversal",
                "familiarity": "recognizes",
            }
        )

        with (
            patch.object(cli, "DiagnosticEngine") as engine_type,
            patch.object(cli, "adapter_from_env") as model_factory,
            patch("builtins.print") as print_mock,
        ):
            engine_type.return_value.prepare.return_value = plan
            args = parser.parse_args(
                ["diagnostic-plan", "Tree Traversal", "--source", "voice"]
            )
            self.assertEqual(cli.cmd_diagnostic_plan(args), 0)
            model_factory.assert_not_called()
            payload = json.loads(print_mock.call_args.args[0])
            self.assertEqual(payload["plan"]["id"], "plan-test")
            self.assertEqual(
                payload["submission"]["observations"][0]["prompt_id"],
                "recognizes-v1",
            )
            self.assertNotIn("confirmed_by_user", payload["submission"])

        with tempfile.TemporaryDirectory() as temp:
            submission_path = Path(temp) / "submission.json"
            submission_path.write_text("{}", encoding="utf-8")
            with (
                patch.object(cli, "DiagnosticEngine") as engine_type,
                patch.object(
                    cli, "_parse_diagnostic_submission", return_value="submission"
                ),
                patch.object(cli, "adapter_from_env") as model_factory,
                patch("builtins.input", return_value="yes"),
                patch("builtins.print") as print_mock,
            ):
                engine_type.return_value.record.return_value = outcome
                args = parser.parse_args(
                    ["record-diagnostic", str(submission_path)]
                )
                self.assertEqual(cli.cmd_record_diagnostic(args), 0)
                engine_type.return_value.record.assert_called_once_with("submission")
                model_factory.assert_not_called()
                payload = json.loads(print_mock.call_args.args[0])
                self.assertEqual(payload["familiarity"], "recognizes")

        with self.assertRaisesRegex(
            cli.DiagnosticValidationError, "unsupported fields"
        ):
            cli._parse_diagnostic_submission(
                {
                    "plan_id": "plan-test",
                    "canonical_concept": "Tree Traversal",
                    "observations": [],
                    "transcript": "A raw answer that must not cross the adapter.",
                }
            )

    def test_check_vault_links_is_a_token_free_thin_adapter(self) -> None:
        parser = cli.build_parser()
        report = SimpleNamespace(
            ok=True,
            to_dict=lambda **_: {
                "ok": True,
                "references_checked": 4,
                "model_attempted": False,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )

        with (
            patch.object(cli, "audit_vault_links", return_value=report) as audit,
            patch.object(cli, "adapter_from_env") as model_factory,
            patch("builtins.print") as print_mock,
        ):
            args = parser.parse_args(["check-vault-links"])
            self.assertEqual(cli.cmd_check_vault_links(args), 0)

        audit.assert_called_once_with(cli.config.VAULT_PATH)
        model_factory.assert_not_called()
        payload = json.loads(print_mock.call_args.args[0])
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["model_attempted"])
        self.assertEqual(payload["input_tokens"], 0)
        self.assertEqual(payload["output_tokens"], 0)

    def test_refresh_knowledge_honors_explicit_vault_and_reports_zero_usage(self) -> None:
        parser = cli.build_parser()

        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "explicit-vault"
            changed = vault / "Knowledge" / "Knowledge Hub.md"
            expected = {
                "changed_files": [str(changed)],
                "concept_count": 3,
                "inbox_count": 2,
                "source_count": 1,
                "map_count": 1,
                "revision": "a" * 64,
                "model_attempted": False,
                "network_attempted": False,
                "provider_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
            }
            outcome = SimpleNamespace(to_dict=lambda: expected)

            with (
                patch.object(cli, "KnowledgeRepository") as repository_type,
                patch.object(cli, "adapter_from_env") as model_factory,
                patch.object(cli, "research_adapter_from_env") as research_factory,
                patch.object(cli, "AnalysisEngine") as analysis_engine,
                patch("builtins.print") as print_mock,
            ):
                repository_type.return_value.refresh.return_value = outcome
                args = parser.parse_args(
                    ["refresh-knowledge", "--vault", str(vault), "--pretty"]
                )

                self.assertEqual(args.func(args), 0)

            repository_type.assert_called_once_with(vault)
            repository_type.return_value.refresh.assert_called_once_with()
            model_factory.assert_not_called()
            research_factory.assert_not_called()
            analysis_engine.assert_not_called()
            payload = json.loads(print_mock.call_args.args[0])
            self.assertEqual(payload, expected)
            self.assertFalse(payload["model_attempted"])
            self.assertFalse(payload["network_attempted"])
            self.assertEqual(payload["provider_requests"], 0)
            self.assertEqual(payload["input_tokens"], 0)
            self.assertEqual(payload["output_tokens"], 0)
            self.assertEqual(payload["estimated_cost_usd"], 0.0)

    def test_capture_knowledge_text_uses_explicit_vault_and_reports_zero_usage(self) -> None:
        parser = cli.build_parser()

        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "explicit-vault"
            capture_path = vault / "Knowledge" / "Inbox" / "Decision Log.md"
            expected = {
                "capture_path": str(capture_path),
                "created": True,
                "changed_files": [str(capture_path)],
                "concept_count": 0,
                "inbox_count": 1,
                "source_count": 0,
                "map_count": 0,
                "revision": "b" * 64,
                "model_attempted": False,
                "network_attempted": False,
                "provider_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
            }
            outcome = SimpleNamespace(to_dict=lambda: expected)

            with (
                patch.object(cli, "KnowledgeRepository") as repository_type,
                patch.object(cli, "adapter_from_env") as model_factory,
                patch.object(cli, "research_adapter_from_env") as research_factory,
                patch.object(cli, "_configured_research_engine") as research_engine,
                patch("builtins.print") as print_mock,
            ):
                repository_type.return_value.capture.return_value = outcome
                args = parser.parse_args(
                    [
                        "capture-knowledge",
                        "Decision Log",
                        "--text",
                        "Prefer deterministic local indexes.",
                        "--vault",
                        str(vault),
                        "--pretty",
                    ]
                )

                self.assertEqual(args.func(args), 0)

            repository_type.assert_called_once_with(vault)
            request = repository_type.return_value.capture.call_args.args[0]
            self.assertIsInstance(request, cli.KnowledgeCaptureRequest)
            self.assertEqual(request.title, "Decision Log")
            self.assertEqual(request.content, "Prefer deterministic local indexes.")
            repository_type.return_value.capture.assert_called_once()
            self.assertEqual(json.loads(print_mock.call_args.args[0]), expected)
            model_factory.assert_not_called()
            research_factory.assert_not_called()
            research_engine.assert_not_called()

    def test_capture_knowledge_without_source_prompts_for_dictated_text(self) -> None:
        parser = cli.build_parser()
        outcome = SimpleNamespace(
            to_dict=lambda: {
                "capture_path": "synthetic.md",
                "created": True,
                "model_attempted": False,
                "network_attempted": False,
                "provider_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
            }
        )

        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "dictation-vault"
            with (
                patch.object(cli, "KnowledgeRepository") as repository_type,
                patch(
                    "builtins.input", return_value="Dictated capture line."
                ) as input_mock,
                patch("builtins.print"),
            ):
                repository_type.return_value.capture.return_value = outcome
                args = parser.parse_args(
                    ["capture-knowledge", "Voice Note", "--vault", str(vault)]
                )

                self.assertEqual(args.func(args), 0)

            input_mock.assert_called_once_with(
                "Capture text (type or dictate one line): "
            )
            request = repository_type.return_value.capture.call_args.args[0]
            self.assertEqual(request.title, "Voice Note")
            self.assertEqual(request.content, "Dictated capture line.")

    def test_capture_knowledge_invalid_utf8_file_never_constructs_repository(self) -> None:
        parser = cli.build_parser()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            source = root / "invalid-capture.md"
            source.write_bytes(b"\xff\xfeinvalid UTF-8")
            with (
                patch.object(cli, "KnowledgeRepository") as repository_type,
                patch.object(cli, "adapter_from_env") as model_factory,
                patch.object(cli, "research_adapter_from_env") as research_factory,
                patch("builtins.print") as print_mock,
            ):
                args = parser.parse_args(
                    [
                        "capture-knowledge",
                        "Invalid File",
                        "--file",
                        str(source),
                        "--vault",
                        str(vault),
                    ]
                )

                self.assertEqual(args.func(args), 1)

            repository_type.assert_not_called()
            model_factory.assert_not_called()
            research_factory.assert_not_called()
            self.assertIn("valid UTF-8", print_mock.call_args.args[0])

    def test_research_saved_response_is_token_free_and_never_touches_the_vault(self) -> None:
        parser = cli.build_parser()
        query = "PRIVATE-QUERY-MUST-NOT-BE-EMITTED"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            response_path = root / "research.json"
            request = cli.ResearchRequest(query)
            response_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "request_sha256": request.sha256,
                        "retrieved_at": "2026-07-01T12:00:00+00:00",
                        "results": [
                            {
                                "title": "Synthetic reference",
                                "url": "https://example.edu/reference",
                                "snippet": "Normalized replay data.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            vault_file = root / "School" / "Assignment.md"
            vault_file.parent.mkdir(parents=True)
            vault_file.write_text("user-owned vault bytes", encoding="utf-8")
            before = vault_file.read_bytes()

            with (
                patch.object(cli.config, "VAULT_PATH", root),
                patch.object(cli, "adapter_from_env") as model_factory,
                patch.object(cli, "AnalysisEngine") as analysis_engine,
                patch.object(cli.vault_write, "set_section") as vault_writer,
                patch("builtins.print") as print_mock,
            ):
                args = parser.parse_args(
                    [
                        "research",
                        query,
                        "--response-file",
                        str(response_path),
                        "--no-cache",
                        "--pretty",
                    ]
                )
                self.assertEqual(cli.cmd_research(args), 0)

            payload_text = print_mock.call_args.args[0]
            payload = json.loads(payload_text)
            self.assertNotIn(query, payload_text)
            self.assertEqual(payload["result_count"], 1)
            self.assertEqual(payload["usage"]["source"], "replay")
            self.assertFalse(payload["usage"]["model_attempted"])
            self.assertEqual(payload["usage"]["input_tokens"], 0)
            self.assertEqual(payload["usage"]["output_tokens"], 0)
            model_factory.assert_not_called()
            analysis_engine.assert_not_called()
            vault_writer.assert_not_called()
            self.assertEqual(vault_file.read_bytes(), before)

    def test_archive_course_previews_and_requires_confirmation_without_a_model(self) -> None:
        parser = cli.build_parser()
        engine = Mock()
        plan = SimpleNamespace(
            can_apply=True,
            blocking_reasons=(),
            to_dict=lambda: {
                "course": "Course 101",
                "target_state": "archived",
                "folders_moved": 0,
                "model_attempted": False,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        outcome = SimpleNamespace(
            to_dict=lambda: {
                "course": "Course 101",
                "state": "archived",
                "folders_moved": 0,
                "model_attempted": False,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        )
        engine.prepare.return_value = plan
        engine.apply.return_value = outcome

        with (
            patch.object(cli, "_course_archive_engine", return_value=engine),
            patch.object(cli, "adapter_from_env") as model_factory,
            patch("builtins.input", return_value="yes"),
            patch("builtins.print"),
        ):
            args = parser.parse_args(["archive-course", "Course 101"])
            self.assertEqual(cli.cmd_archive_course(args), 0)

        request = engine.prepare.call_args.args[0]
        self.assertEqual(request.course, "Course 101")
        self.assertEqual(request.target_state, cli.CourseArchiveState.ARCHIVED)
        engine.apply.assert_called_once_with(plan, confirmed_by_user=True)
        model_factory.assert_not_called()

    def test_terminal_diagnostic_confirms_once_and_never_persists_raw_answer(self) -> None:
        parser = cli.build_parser()

        def create_concept(root: Path) -> Path:
            path = root / "Knowledge" / "Concepts" / "Tree Traversal.md"
            path.parent.mkdir(parents=True)
            post = frontmatter.Post(
                "# Tree Traversal\n\n"
                "## Personal Notes\n\nPrivate note.\n\n"
                "## Definition\n\nVisit every node in an explicit order.\n\n"
                "## Why This Matters\n\nIt makes recursive structures usable.\n\n"
                "## Connections\n\n- [[General Trees]]\n\n"
                "## Examples\n\n- Preorder.\n",
                type="concept",
                canonical_name="Tree Traversal",
                aliases=[],
                familiarity="unknown",
            )
            path.write_text(frontmatter.dumps(post), encoding="utf-8")
            return path

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            concept_path = create_concept(root)
            log_path = root / "runs.log"
            raw_sentinel = "RAW-ANSWER-MUST-NOT-PERSIST"
            inputs = [
                raw_sentinel,
                "d",
                "0.9",
                "The learner defined traversal in current words.",
                raw_sentinel,
                "d",
                "0.8",
                "The learner explained its connection to general trees.",
                "yes",
            ]
            with (
                patch.object(cli.config, "VAULT_PATH", root),
                patch.object(cli, "_RUN_LOG", log_path),
                patch.object(cli, "adapter_from_env") as model_factory,
                patch("builtins.input", side_effect=inputs),
                patch("builtins.print"),
            ):
                args = parser.parse_args(
                    ["diagnose-concept", "Tree Traversal", "--source", "text"]
                )
                self.assertEqual(cli.cmd_diagnose_concept(args), 0)
                model_factory.assert_not_called()

            concept = frontmatter.load(concept_path)
            self.assertEqual(concept["familiarity"], "explains")
            records = list((root / "Knowledge" / "Diagnostics").rglob("diag-*.md"))
            self.assertEqual(len(records), 1)
            persisted = records[0].read_text(encoding="utf-8") + log_path.read_text(
                encoding="utf-8"
            )
            self.assertNotIn(raw_sentinel, persisted)
            self.assertIn("Private note.", concept.content)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            concept_path = create_concept(root)
            before = concept_path.read_bytes()
            inputs = [
                "discarded answer",
                "d",
                "0.9",
                "Current definition evidence.",
                "discarded answer",
                "d",
                "0.8",
                "Current explanation evidence.",
                "no",
            ]
            with (
                patch.object(cli.config, "VAULT_PATH", root),
                patch.object(cli, "_RUN_LOG", root / "runs.log"),
                patch.object(cli, "adapter_from_env") as model_factory,
                patch("builtins.input", side_effect=inputs),
                patch("builtins.print"),
            ):
                args = parser.parse_args(
                    ["diagnose-concept", "Tree Traversal", "--source", "text"]
                )
                with self.assertRaisesRegex(
                    cli.DiagnosticValidationError, "not explicitly confirmed"
                ):
                    cli.cmd_diagnose_concept(args)
                model_factory.assert_not_called()
            self.assertEqual(concept_path.read_bytes(), before)
            self.assertFalse((root / "Knowledge" / "Diagnostics").exists())

    def test_terminal_diagnostic_explains_choices_and_previews_assessment(self) -> None:
        raw_sentinel = "RAW-ANSWER-MUST-STAY-TRANSIENT"
        prompt = SimpleNamespace(
            id="recognizes-v1",
            capability=cli.Familiarity.RECOGNIZES,
            evidence_kind=cli.EvidenceKind.OWN_DEFINITION,
            question="Define Tree Traversal in your own words.",
            evidence_rule="Identify the concept and one defining feature.",
        )
        plan = SimpleNamespace(
            canonical_concept="Tree Traversal",
            prompts=(prompt,),
        )

        with (
            patch(
                "builtins.input",
                side_effect=[
                    raw_sentinel,
                    "demonstrated",
                    "0.8",
                    "Defined traversal and named its visit order.",
                    "yes",
                ],
            ) as input_mock,
            patch("builtins.print") as print_mock,
        ):
            conversation = cli._TerminalDiagnosticConversation("voice")
            observations = conversation.assess(plan)
            self.assertTrue(conversation.confirm(plan, observations))

        self.assertEqual(observations[0].result, cli.ObservationResult.DEMONSTRATED)
        printed = "\n".join(
            str(call.args[0]) for call in print_mock.call_args_list if call.args
        )
        prompts = "\n".join(
            str(call.args[0]) for call in input_mock.call_args_list if call.args
        )
        self.assertIn("d = demonstrated", printed)
        self.assertIn("p = partial", printed)
        self.assertIn("n = not yet", printed)
        self.assertIn("0.0 = unsure", prompts)
        self.assertIn("what the answer showed", prompts)
        self.assertIn("Assessment preview", printed)
        self.assertIn("Defined traversal and named its visit order.", printed)
        self.assertNotIn(raw_sentinel, printed)


if __name__ == "__main__":
    unittest.main()
