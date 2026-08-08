from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

import config
from vault_notes import _sanitize_filename

from . import transcripts, vault_query, vault_write
from study_analysis import (
    AnalysisEngine,
    AnalysisMode,
    AnalysisRequest,
    CourseArchiveEngine,
    CourseArchiveRequest,
    CourseArchiveState,
    DiagnosticEngine,
    DiagnosticCorrection,
    DiagnosticObservation,
    DiagnosticRequest,
    DiagnosticSubmission,
    DiagnosticValidationError,
    EvidenceKind,
    Familiarity,
    KnowledgeCaptureRequest,
    KnowledgeRepository,
    KnowledgeRepositoryError,
    ObservationResult,
    ResearchEngine,
    ResearchRequest,
    audit_vault_links,
    adapter_from_env,
    default_research_cache_path,
    research_adapter_from_env,
    validate_research_cache_path,
)
from study_analysis.lifeos import export_assignment_signals, export_concept_signals
from study_analysis.sources import index_source

MODEL = "claude-sonnet-5"
_RUN_LOG = Path(__file__).resolve().parent.parent / "analysis_runs.log"
_MAX_CAPTURE_FILE_BYTES = 131_072


def _require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Missing ANTHROPIC_API_KEY. Get one from console.anthropic.com and add it to .env"
        )


def _extract_section(content: str, header: str) -> str:
    """Return the text under `## {header}` (up to the next `## ` or EOF), or "".

    Used to pull the Canvas-scraped assignment description back out of the note
    so prep can ground its output in the real task instead of just the title.
    Placeholder comment lines (`<!-- ... -->`) are dropped so an un-scraped
    section reads as empty.
    """
    target = f"## {header}"
    lines = content.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if line.strip() == target:
            capturing = True
            continue
        if capturing and line.strip().startswith("## "):
            break
        if capturing and not line.strip().startswith("<!--"):
            out.append(line)
    return "\n".join(out).strip()


def cmd_setup_course(args: argparse.Namespace) -> int:
    course_path = config.ASSIGNMENTS_ROOT / args.course
    if not course_path.exists():
        print(f"No course folder found at {course_path}")
        return 1

    print(f"Setting up course info for: {args.course}")
    textbook = input("Textbook(s) / main resources for this course: ").strip()
    topics = input("General topics/focus of this course: ").strip()
    other = input("Anything else relevant (optional): ").strip()

    body_parts = [f"# {args.course}\n"]
    if textbook:
        body_parts.append(f"## Textbook / Resources\n{textbook}\n")
    if topics:
        body_parts.append(f"## Topics\n{topics}\n")
    if other:
        body_parts.append(f"## Other Notes\n{other}\n")

    info_path = course_path / "_Course Info.md"
    post = frontmatter.Post(
        "\n".join(body_parts),
        course=args.course,
        type="course-info",
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    info_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    print(f"Saved: {info_path}")
    return 0


def cmd_new_lecture(args: argparse.Namespace) -> int:
    course_path = config.ASSIGNMENTS_ROOT / args.course
    pdf_path = course_path / "Attachments" / args.pdf
    if not pdf_path.exists():
        print(f"No PDF found at {pdf_path}")
        print(f"(looking for '{args.pdf}' inside {course_path / 'Attachments'})")
        return 1

    lectures_folder = course_path / "Lectures"
    lectures_folder.mkdir(parents=True, exist_ok=True)

    title = args.title or pdf_path.stem
    note_path = lectures_folder / f"{title}.md"
    if note_path.exists():
        print(f"Lecture note already exists: {note_path}")
        return 1

    post = frontmatter.Post(
        f"# {title}\n\n![[{pdf_path.name}]]\n\n## Study Notes\n",
        course=args.course,
        source="pdf",
        source_file=pdf_path.name,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    print(f"Created: {note_path}")
    return 0


def _material_line(filename: str) -> str:
    """Embed PDFs (Obsidian renders them inline for PDF++ annotation);
    everything else - docx, pptx, txt - just gets a clickable link, since
    Obsidian can't embed those formats.
    """
    return f"![[{filename}]]" if filename.lower().endswith(".pdf") else f"[[{filename}]]"


def cmd_new_module(args: argparse.Namespace) -> int:
    course_path = config.ASSIGNMENTS_ROOT / args.course
    if not course_path.exists():
        print(f"No course folder found at {course_path}")
        return 1

    modules_folder = course_path / "Modules"
    modules_folder.mkdir(parents=True, exist_ok=True)

    title = args.title or args.module
    note_path = modules_folder / f"{_sanitize_filename(title)}.md"

    requested = [f.strip() for f in (args.files or "").split(",") if f.strip()]
    for filename in requested:
        file_path = course_path / "Attachments" / filename
        if not file_path.exists():
            print(f"Note: '{filename}' isn't in {course_path / 'Attachments'} yet - listing it anyway")

    if note_path.exists():
        post = frontmatter.load(note_path)
        existing = post.get("source_files") or []
        new_files = [f for f in requested if f not in existing]
        if not new_files:
            print(f"Already up to date: {note_path}")
            return 0
        all_files = existing + new_files
        materials = "\n".join(f"- {_material_line(f)}" for f in all_files)
        vault_write.set_section(note_path, "Materials", materials)
        post = frontmatter.load(note_path)
        post["source_files"] = all_files
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        print(f"Updated: {note_path} (added {', '.join(new_files)})")
        return 0

    materials = (
        "\n".join(f"- {_material_line(f)}" for f in requested)
        if requested
        else "*(nothing uploaded yet - re-run with --files once this module opens)*"
    )
    post = frontmatter.Post(
        f"# {title}\n\n## Materials\n{materials}\n\n## Study Notes\n",
        course=args.course,
        type="module",
        module=args.module,
        source_files=requested,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    print(f"Created: {note_path}")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    """Compatibility command over the sole production analysis interface."""
    return _run_analysis_command(
        args,
        mode=AnalysisMode.STUDY,
        include_research=True,
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _note_is_prepped(post) -> bool:
    """A validated analysis, not arbitrary prose, is the preparation receipt."""
    analysis = post.get("analysis")
    return isinstance(analysis, dict) and analysis.get("status") == "complete"


def cmd_prep_open(args: argparse.Namespace) -> int:
    """Prep open assignments in due order behind a hard provider-call cap.

    Dollar pricing is provider-specific and can change, so this command limits
    attempts plus per-call input/output ceilings instead of claiming a billing cap.
    """
    max_attempts = args.max_attempts

    candidates = []
    for p in config.ASSIGNMENTS_ROOT.rglob("*.md"):
        try:
            post = frontmatter.load(p)
        except Exception:
            continue
        if "task" not in [str(t) for t in (post.get("tags") or [])]:
            continue
        if post.get("status") != "open":
            continue
        if not args.force and _note_is_prepped(post):
            continue
        if getattr(args, "only_grounded", False) and not _extract_section(
            post.content, "Assignment Details"
        ):
            continue
        candidates.append((post.get("due") or "", p))
    candidates.sort(key=lambda x: x[0])

    model_adapter = adapter_from_env()
    research_engine = _configured_research_engine()
    engine = AnalysisEngine(
        vault_root=config.VAULT_PATH,
        adapter=model_adapter,
        research_engine=research_engine,
        log_path=_RUN_LOG,
    )

    print(
        f"{len(candidates)} open assignment(s) to prep; "
        f"hard attempt cap {max_attempts}"
    )
    attempted = 0
    done = 0
    failed = 0
    input_tokens = 0
    output_tokens = 0
    provider_requests = 0
    for due, p in candidates:
        if attempted >= max_attempts:
            print(
                f"Stopping before '{p.stem}': the hard {max_attempts}-attempt "
                "cap has been reached."
            )
            break
        attempted += 1
        print(f"[{attempted}] {p.stem}  (due {str(due)[:10]})")
        try:
            outcome = engine.analyze(
                AnalysisRequest(
                    assignment_path=p,
                    mode=AnalysisMode.STUDY,
                    max_output_tokens=args.max_output_tokens,
                    max_input_chars=args.max_input_chars,
                    include_research=True,
                    max_research_results=args.max_research_results,
                    refresh_research=args.refresh_research,
                )
            )
            done += 1
            input_tokens += outcome.usage.total_input_tokens
            output_tokens += outcome.usage.output_tokens
            if outcome.research_usage is not None:
                provider_requests += outcome.research_usage.provider_requests
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
    print(
        f"Done: attempted {attempted}, prepped {done}, failed {failed}; "
        f"{input_tokens:,} model input + "
        f"{output_tokens:,} output tokens; {provider_requests} live research request(s); "
        "review provider billing separately from these measured usage counters."
    )
    return 0 if failed == 0 else 1


def cmd_ask(args: argparse.Namespace) -> int:
    import anthropic

    from .tools import get_tasks

    _require_api_key()

    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=2000,
        thinking={"type": "adaptive"},
        tools=[get_tasks],
        messages=[{"role": "user", "content": args.question}],
    )

    final_message = None
    for message in runner:
        final_message = message

    if final_message:
        for block in final_message.content:
            if block.type == "text":
                print(block.text)
    return 0


def cmd_transcript(args: argparse.Namespace) -> int:
    note_path = transcripts.fetch_and_save(
        config.ASSIGNMENTS_ROOT, args.url, args.course, args.title
    )
    print(f"Saved transcript: {note_path}")
    return 0


def cmd_check_files(args: argparse.Namespace) -> int:
    course_path = config.ASSIGNMENTS_ROOT / args.course
    listing_path = course_path / "canvas_file_listing.txt"
    if not listing_path.exists():
        print(f"No listing found at {listing_path}")
        print("Copy the file names from Canvas's Files page into that file (one per line) and re-run.")
        return 1

    canvas_files = {
        line.strip() for line in listing_path.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    local_files = {p.name for p in course_path.rglob("*") if p.is_file()}

    missing = sorted(canvas_files - local_files)
    if not missing:
        print("Nothing missing - every file in the listing is already saved locally.")
    else:
        print("Missing (not found locally):")
        for name in missing:
            print(f"  - {name}")
    return 0


def cmd_index_source(args: argparse.Namespace) -> int:
    course_path = config.ASSIGNMENTS_ROOT / args.course
    if not course_path.is_dir():
        print(f"No course folder found at {course_path}")
        return 1
    assignment_path = None
    if args.assignment:
        assignment_path = vault_query.find_note(config.ASSIGNMENTS_ROOT, args.assignment)
        if assignment_path is None:
            print(f"Could not find a note matching: {args.assignment}")
            return 1
        if assignment_path.parent != course_path:
            print(f"Assignment is not inside course folder: {args.course}")
            return 1
    note_path = index_source(
        course_path=course_path,
        source_path=Path(args.file),
        title=args.title,
        page_specs=args.pages or [],
        assignment_path=assignment_path,
    )
    print(f"Indexed: {note_path}")
    return 0


def _configured_research_engine(
    response_file: Path | None = None, *, no_cache: bool = False
) -> ResearchEngine:
    research_adapter = research_adapter_from_env(response_file)
    cache_dir = None
    if not no_cache and research_adapter.cacheable:
        cache_dir = validate_research_cache_path(
            default_research_cache_path(),
            protected_roots=(
                Path(__file__).resolve().parent.parent,
                config.VAULT_PATH,
            ),
        )
    return ResearchEngine(research_adapter, cache_dir=cache_dir)


def _run_analysis_command(
    args: argparse.Namespace,
    *,
    mode: AnalysisMode,
    include_research: bool,
) -> int:
    note_path = vault_query.find_note(config.ASSIGNMENTS_ROOT, args.assignment)
    if note_path is None:
        print(f"Could not find a note matching: {args.assignment}")
        return 1
    response_file = Path(args.response_file) if args.response_file else None
    adapter = adapter_from_env(response_file=response_file)
    research_engine = None
    if include_research:
        research_response = getattr(args, "research_response_file", None)
        research_engine = _configured_research_engine(
            Path(research_response) if research_response else None
        )
    engine = AnalysisEngine(
        vault_root=config.VAULT_PATH,
        adapter=adapter,
        research_engine=research_engine,
        log_path=_RUN_LOG,
    )
    outcome = engine.analyze(
        AnalysisRequest(
            assignment_path=note_path,
            mode=mode,
            max_output_tokens=args.max_output_tokens,
            max_input_chars=args.max_input_chars,
            include_research=include_research,
            max_research_results=getattr(args, "max_research_results", 5),
            refresh_research=getattr(args, "refresh_research", False),
        )
    )
    print(f"Analyzed: {note_path}")
    print(f"Concepts: {', '.join(outcome.concepts)}")
    print(f"Updated {len(outcome.changed_files)} file(s).")
    if outcome.solution_path:
        print(f"Solution archive: {outcome.solution_path}")
    if outcome.usage.input_tokens or outcome.usage.output_tokens:
        print(
            f"Usage: {outcome.usage.total_input_tokens:,} input + "
            f"{outcome.usage.output_tokens:,} output tokens"
        )
        if outcome.usage.thinking_tokens is not None:
            print(
                f"Reasoning: {outcome.usage.thinking_tokens:,} thinking + "
                f"{outcome.usage.non_thinking_output_tokens:,} non-thinking output tokens"
            )
    research_usage = getattr(outcome, "research_usage", None)
    if research_usage is not None:
        research_cost = research_usage.estimated_cost_usd
        cost_text = "unknown" if research_cost is None else f"${research_cost:.4f}"
        print(
            f"Research: {getattr(outcome, 'research_result_count', 0)} discovered link candidate(s), "
            f"source={research_usage.source}, "
            f"provider_requests={research_usage.provider_requests}, "
            f"cost={cost_text}; research_model_tokens=0."
        )
    if outcome.input_truncated:
        print("Input budget reached; some indexed source content was not analyzed.")
    return 0


def cmd_analyze_concepts(args: argparse.Namespace) -> int:
    return _run_analysis_command(
        args,
        mode=AnalysisMode(args.mode),
        include_research=args.with_research,
    )


def cmd_export_lifeos(args: argparse.Namespace) -> int:
    records = export_assignment_signals(config.ASSIGNMENTS_ROOT)
    if args.pretty:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(records, separators=(",", ":"), ensure_ascii=False))
    return 0


class _TerminalDiagnosticConversation:
    """Human-confirmed terminal/dictation adapter; answers are never persisted."""

    def __init__(self, source: str):
        self.source = source

    def assess(self, plan):
        observations = []
        result_choices = {
            "d": ObservationResult.DEMONSTRATED,
            "demonstrated": ObservationResult.DEMONSTRATED,
            "p": ObservationResult.PARTIAL,
            "partial": ObservationResult.PARTIAL,
            "n": ObservationResult.NOT_YET,
            "not yet": ObservationResult.NOT_YET,
            "not-yet": ObservationResult.NOT_YET,
            "not_yet": ObservationResult.NOT_YET,
        }
        print("\nAssessment choices:")
        print("  d = demonstrated - fully meets the evidence rule")
        print("  p = partial - shows some correct understanding, but not enough")
        print("  n = not yet - does not provide enough correct evidence")
        print("Use a confidence below 0.60 when you are unsure; it will not promote familiarity.")
        for prompt in plan.prompts:
            print(f"\nQuestion: {prompt.question}")
            print(f"Evidence rule: {prompt.evidence_rule}")
            answer = input("Answer aloud or type it here (not saved): ")
            del answer
            result_key = input(
                "Assessment [d = demonstrated, p = partial, n = not yet]: "
            ).strip().casefold()
            if result_key not in result_choices:
                raise DiagnosticValidationError(
                    "Assessment must be demonstrated (d), partial (p), or not yet (n)"
                )
            try:
                confidence = float(
                    input(
                        "Assessment confidence [0.0 = unsure, 1.0 = certain]: "
                    ).strip()
                )
            except ValueError as exc:
                raise DiagnosticValidationError(
                    "Assessment confidence must be a number from 0 to 1"
                ) from exc
            summary = input(
                "Evidence summary (one sentence describing what the answer showed or "
                "missed; example: 'Defined traversal and named its visit order.'): "
            )
            observations.append(
                DiagnosticObservation(
                    prompt_id=prompt.id,
                    evidence_kind=prompt.evidence_kind,
                    result=result_choices[result_key],
                    confidence=confidence,
                    evidence_summary=summary,
                )
            )
        return tuple(observations)

    def confirm(self, plan, observations):
        print("\nAssessment preview (raw answers were discarded):")
        for index, (prompt, observation) in enumerate(
            zip(plan.prompts, observations), 1
        ):
            capability = prompt.capability.value.replace("_", " ").title()
            result = observation.result.value.replace("_", " ").title()
            print(f"{index}. {capability} - {result}")
            print(f"   Confidence: {observation.confidence:.2f}")
            print(f"   Evidence: {observation.evidence_summary}")
        return input(
            "Record this assessment exactly as shown? [y/N]: "
        ).strip().casefold() in {
            "y",
            "yes",
        }


def _diagnostic_engine() -> DiagnosticEngine:
    return DiagnosticEngine(config.VAULT_PATH, log_path=_RUN_LOG)


def cmd_diagnose_concept(args: argparse.Namespace) -> int:
    target = Familiarity(args.target) if args.target else None
    outcome = _diagnostic_engine().diagnose(
        DiagnosticRequest(args.concept, target),
        _TerminalDiagnosticConversation(args.source),
    )
    payload = outcome.to_dict()
    print(
        json.dumps(
            payload,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


def cmd_diagnostic_plan(args: argparse.Namespace) -> int:
    target = Familiarity(args.target) if args.target else None
    plan = _diagnostic_engine().prepare(
        DiagnosticRequest(args.concept, target)
    )
    payload = {
        "plan": plan.to_dict(),
        "submission": {
            "plan_id": plan.id,
            "canonical_concept": plan.canonical_concept,
            "observations": [
                {
                    "prompt_id": prompt.id,
                    "evidence_kind": prompt.evidence_kind.value,
                    "result": "replace_with_demonstrated_partial_or_not_yet",
                    "confidence": 0.0,
                    "evidence_summary": "",
                }
                for prompt in plan.prompts
            ],
            "source": args.source,
        },
    }
    if args.pretty:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0


def _diagnostic_object(raw, context: str, required: set[str], optional=None):
    if not isinstance(raw, dict):
        raise DiagnosticValidationError(f"{context} must be an object")
    allowed = required | (optional or set())
    missing = required - raw.keys()
    extra = raw.keys() - allowed
    if missing:
        raise DiagnosticValidationError(f"{context} is missing {sorted(missing)}")
    if extra:
        raise DiagnosticValidationError(
            f"{context} contains unsupported fields {sorted(extra)}"
        )
    return raw


def _parse_diagnostic_submission(
    raw, *, confirmed_by_user: bool = False
) -> DiagnosticSubmission:
    data = _diagnostic_object(
        raw,
        "diagnostic submission",
        {
            "plan_id",
            "canonical_concept",
            "observations",
        },
        {"source"},
    )
    if not isinstance(data["observations"], list):
        raise DiagnosticValidationError("diagnostic observations must be a list")
    observations = []
    for raw_observation in data["observations"]:
        item = _diagnostic_object(
            raw_observation,
            "diagnostic observation",
            {
                "prompt_id",
                "evidence_kind",
                "result",
                "confidence",
                "evidence_summary",
            },
            set(),
        )
        try:
            evidence_kind = EvidenceKind(item["evidence_kind"])
            result = ObservationResult(item["result"])
        except (TypeError, ValueError) as exc:
            raise DiagnosticValidationError(
                "diagnostic observation has an unsupported enum value"
            ) from exc
        observations.append(
            DiagnosticObservation(
                prompt_id=item["prompt_id"],
                evidence_kind=evidence_kind,
                result=result,
                confidence=item["confidence"],
                evidence_summary=item["evidence_summary"],
            )
        )
    return DiagnosticSubmission(
        plan_id=data["plan_id"],
        canonical_concept=data["canonical_concept"],
        observations=tuple(observations),
        source=data.get("source") or "voice",
        confirmed_by_user=confirmed_by_user,
    )


def cmd_record_diagnostic(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.submission_file).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and set(payload) == {"plan", "submission"}:
        payload = payload["submission"]
    confirmed = input(
        "I reviewed these classifications against the current learner answers. "
        "Record them? [y/N]: "
    ).strip().casefold() in {"y", "yes"}
    if not confirmed:
        raise DiagnosticValidationError(
            "Diagnostic evidence was not explicitly confirmed by the user"
        )
    outcome = _diagnostic_engine().record(
        _parse_diagnostic_submission(payload, confirmed_by_user=True)
    )
    if args.pretty:
        print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(outcome.to_dict(), separators=(",", ":"), ensure_ascii=False))
    return 0


def cmd_correct_diagnostic(args: argparse.Namespace) -> int:
    correction = input("Correction to append (the original record stays immutable): ")
    confirmed = input("Append this correction and require reassessment? [y/N]: ")
    outcome = _diagnostic_engine().correct(
        DiagnosticCorrection(
            canonical_concept=args.concept,
            record_id=args.record_id,
            correction=correction,
            confirmed_by_user=confirmed.strip().casefold() in {"y", "yes"},
        )
    )
    print(
        json.dumps(
            outcome.to_dict(),
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


def cmd_export_concepts(args: argparse.Namespace) -> int:
    records = export_concept_signals(config.VAULT_PATH)
    if args.pretty:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(records, separators=(",", ":"), ensure_ascii=False))
    return 0


def cmd_check_vault_links(args: argparse.Namespace) -> int:
    report = audit_vault_links(config.VAULT_PATH)
    payload = report.to_dict(include_issues=args.pretty or not report.ok)
    print(
        json.dumps(
            payload,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    return 0 if report.ok else 1


def cmd_refresh_knowledge(args: argparse.Namespace) -> int:
    """Refresh deterministic knowledge indexes for one vault."""
    try:
        outcome = KnowledgeRepository(Path(args.vault)).refresh()
    except KnowledgeRepositoryError as exc:
        print(f"Error: {exc}")
        return 1
    print(
        json.dumps(
            outcome.to_dict(),
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


def cmd_capture_knowledge(args: argparse.Namespace) -> int:
    """Capture one bounded note through the knowledge repository boundary."""
    try:
        if args.text is not None:
            capture_text = args.text
        elif args.file is not None:
            source_path = Path(args.file)
            if source_path.is_symlink() or not source_path.is_file():
                raise KnowledgeRepositoryError(
                    f"Capture input is not an existing regular file: {source_path}"
                )
            try:
                if source_path.stat().st_size > _MAX_CAPTURE_FILE_BYTES:
                    raise KnowledgeRepositoryError(
                        f"Capture input cannot exceed {_MAX_CAPTURE_FILE_BYTES} bytes"
                    )
                with source_path.open("rb") as stream:
                    raw_capture = stream.read(_MAX_CAPTURE_FILE_BYTES + 1)
            except KnowledgeRepositoryError:
                raise
            except OSError as exc:
                raise KnowledgeRepositoryError(
                    f"Capture input could not be read: {source_path}"
                ) from exc
            if len(raw_capture) > _MAX_CAPTURE_FILE_BYTES:
                raise KnowledgeRepositoryError(
                    f"Capture input cannot exceed {_MAX_CAPTURE_FILE_BYTES} bytes"
                )
            try:
                capture_text = raw_capture.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise KnowledgeRepositoryError(
                    f"Capture input is not valid UTF-8: {source_path}"
                ) from exc
        else:
            capture_text = input("Capture text (type or dictate one line): ")

        outcome = KnowledgeRepository(Path(args.vault)).capture(
            KnowledgeCaptureRequest(title=args.title, content=capture_text)
        )
    except KnowledgeRepositoryError as exc:
        print(f"Error: {exc}")
        return 1
    print(
        json.dumps(
            outcome.to_dict(),
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    """Run bounded web discovery without invoking a model or touching the vault."""
    response_file = Path(args.response_file) if args.response_file else None
    engine = _configured_research_engine(response_file, no_cache=args.no_cache)
    outcome = engine.search(
        ResearchRequest(args.query, max_results=args.max_results),
        refresh=args.refresh,
    )
    print(
        json.dumps(
            outcome.to_dict(),
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


def _course_archive_engine() -> CourseArchiveEngine:
    return CourseArchiveEngine(
        config.VAULT_PATH,
        assignments_root=config.ASSIGNMENTS_ROOT,
    )


def _cmd_course_archive(args: argparse.Namespace, target: CourseArchiveState) -> int:
    engine = _course_archive_engine()
    plan = engine.prepare(CourseArchiveRequest(args.course, target))
    print(
        json.dumps(
            plan.to_dict(),
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    if not plan.can_apply:
        print(
            "Course lifecycle change is blocked: "
            + ", ".join(plan.blocking_reasons)
        )
        return 1
    action = "Archive" if target is CourseArchiveState.ARCHIVED else "Restore"
    confirmed = input(
        f"{action} this course by metadata only (0 folders moved)? [y/N]: "
    ).strip().casefold() in {"y", "yes"}
    outcome = engine.apply(plan, confirmed_by_user=confirmed)
    print(
        json.dumps(
            outcome.to_dict(),
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


def cmd_archive_course(args: argparse.Namespace) -> int:
    return _cmd_course_archive(args, CourseArchiveState.ARCHIVED)


def cmd_restore_course(args: argparse.Namespace) -> int:
    return _cmd_course_archive(args, CourseArchiveState.ACTIVE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py", description="Study/concept agent over the synced Obsidian vault."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_setup = subparsers.add_parser("setup-course", help="One-time per-course context setup")
    p_setup.add_argument("course", help="Course folder name under School/")
    p_setup.set_defaults(func=cmd_setup_course)

    p_prep = subparsers.add_parser(
        "prep",
        help="Run validated Study analysis plus local research in one guarded commit",
    )
    p_prep.add_argument("assignment", help="Assignment note name (exact or partial)")
    p_prep.add_argument(
        "--response-file",
        help="Use a saved model JSON response instead of spending model tokens",
    )
    p_prep.add_argument(
        "--research-response-file",
        help="Use a query-bound saved research bundle instead of a live search",
    )
    p_prep.add_argument("--max-output-tokens", type=int, default=8000)
    p_prep.add_argument("--max-input-chars", type=int, default=48000)
    p_prep.add_argument("--max-research-results", type=int, default=5)
    p_prep.add_argument("--refresh-research", action="store_true")
    p_prep.set_defaults(func=cmd_prep)

    p_prep_open = subparsers.add_parser(
        "prep-open",
        help="Prep open assignments in due order behind a hard attempt cap",
    )
    p_prep_open.add_argument(
        "--max-attempts",
        type=_positive_int,
        default=1,
        help="Maximum provider attempts in this run, including failed calls (default: 1)",
    )
    p_prep_open.add_argument(
        "--force", action="store_true",
        help="Re-prep notes that were already prepped (e.g. to pick up newly-scraped descriptions)",
    )
    p_prep_open.add_argument(
        "--only-grounded", action="store_true",
        help="Only prep notes that have a scraped Assignment Details section (skips quizzes / thin ones)",
    )
    p_prep_open.add_argument("--max-output-tokens", type=int, default=8000)
    p_prep_open.add_argument("--max-input-chars", type=int, default=48000)
    p_prep_open.add_argument("--max-research-results", type=int, default=5)
    p_prep_open.add_argument("--refresh-research", action="store_true")
    p_prep_open.set_defaults(func=cmd_prep_open)

    p_ask = subparsers.add_parser("ask", help="Ask a question about your synced tasks")
    p_ask.add_argument("question")
    p_ask.set_defaults(func=cmd_ask)

    p_transcript = subparsers.add_parser("transcript", help="Fetch a YouTube lecture transcript")
    p_transcript.add_argument("url", help="YouTube video URL or ID")
    p_transcript.add_argument("--course", required=True, help="Course folder name under School/")
    p_transcript.add_argument("--title", required=True, help="Lecture title")
    p_transcript.set_defaults(func=cmd_transcript)

    p_check = subparsers.add_parser(
        "check-files", help="Diff a Canvas file listing against what's saved locally"
    )
    p_check.add_argument("course", help="Course folder name under School/")
    p_check.set_defaults(func=cmd_check_files)

    p_lecture = subparsers.add_parser("new-lecture", help="Create a study note for a PDF lecture")
    p_lecture.add_argument("pdf", help="PDF filename inside this course's Attachments/ folder")
    p_lecture.add_argument("--course", required=True, help="Course folder name under School/")
    p_lecture.add_argument("--title", help="Lecture note title (defaults to the PDF's filename)")
    p_lecture.set_defaults(func=cmd_new_lecture)

    p_module = subparsers.add_parser(
        "new-module",
        help="Create (or update) a study note for a Canvas module - any mix of readings, or none yet",
    )
    p_module.add_argument("module", help="Module name, e.g. 'Module 9'")
    p_module.add_argument("--course", required=True, help="Course folder name under School/")
    p_module.add_argument(
        "--files",
        help="Comma-separated filenames inside this course's Attachments/ folder (optional - "
        "omit if the module hasn't opened yet). Re-run with --files later to add newly "
        "uploaded material without touching your existing Study Notes.",
    )
    p_module.add_argument("--title", help="Module note title (defaults to the module name)")
    p_module.set_defaults(func=cmd_new_module)

    p_source = subparsers.add_parser(
        "index-source", help="Index a local source file and optionally link it to an assignment"
    )
    p_source.add_argument("file", help="Absolute or relative path to the source file")
    p_source.add_argument("--course", required=True, help="Course folder name under School/")
    p_source.add_argument("--title", required=True, help="Stable source-record title")
    p_source.add_argument(
        "--pages",
        action="append",
        help="Relevant PDF page or range, e.g. --pages 2-9 --pages 14 (repeatable)",
    )
    p_source.add_argument(
        "--assignment", help="Assignment note to link this source to (exact or partial)"
    )
    p_source.set_defaults(func=cmd_index_source)

    p_analyze = subparsers.add_parser(
        "analyze-concepts",
        help="Analyze an assignment through the provider-neutral validated pipeline",
    )
    p_analyze.add_argument("assignment", help="Assignment note name (exact or partial)")
    p_analyze.add_argument("--mode", choices=["study", "expert"], default="study")
    p_analyze.add_argument(
        "--response-file",
        help="Use a saved JSON response instead of spending model tokens",
    )
    p_analyze.add_argument("--max-output-tokens", type=int, default=8000)
    p_analyze.add_argument("--max-input-chars", type=int, default=48000)
    p_analyze.add_argument(
        "--with-research",
        action="store_true",
        help="Add local-search candidates from reviewed public topics in the same commit",
    )
    p_analyze.add_argument(
        "--research-response-file",
        help="Use a query-bound saved research bundle instead of a live search",
    )
    p_analyze.add_argument("--max-research-results", type=int, default=5)
    p_analyze.add_argument("--refresh-research", action="store_true")
    p_analyze.set_defaults(func=cmd_analyze_concepts)

    p_diagnose = subparsers.add_parser(
        "diagnose-concept",
        help="Run one human-confirmed, token-free terminal or dictation diagnostic",
    )
    p_diagnose.add_argument("concept", help="Canonical concept name or alias")
    p_diagnose.add_argument(
        "--target",
        choices=[
            Familiarity.RECOGNIZES.value,
            Familiarity.EXPLAINS.value,
            Familiarity.APPLIES.value,
            Familiarity.TRANSFERS.value,
        ],
        help="Optional highest familiarity level to assess",
    )
    p_diagnose.add_argument(
        "--source",
        choices=["voice", "text"],
        default="voice",
        help="Use voice when dictating into the terminal, otherwise text",
    )
    p_diagnose.add_argument("--pretty", action="store_true")
    p_diagnose.set_defaults(func=cmd_diagnose_concept)

    p_diagnostic_plan = subparsers.add_parser(
        "diagnostic-plan",
        help="Create a token-free, voice-friendly plan for one canonical concept",
    )
    p_diagnostic_plan.add_argument("concept", help="Canonical concept name or alias")
    p_diagnostic_plan.add_argument(
        "--target",
        choices=[
            Familiarity.RECOGNIZES.value,
            Familiarity.EXPLAINS.value,
            Familiarity.APPLIES.value,
            Familiarity.TRANSFERS.value,
        ],
        help="Optional highest familiarity level to assess",
    )
    p_diagnostic_plan.add_argument(
        "--source",
        choices=["voice", "text", "scripted"],
        default="voice",
        help="Conversation channel recorded with the eventual evidence",
    )
    p_diagnostic_plan.add_argument("--pretty", action="store_true")
    p_diagnostic_plan.set_defaults(func=cmd_diagnostic_plan)

    p_record_diagnostic = subparsers.add_parser(
        "record-diagnostic",
        help="Validate and transactionally record a completed diagnostic submission",
    )
    p_record_diagnostic.add_argument(
        "submission_file",
        help="Confirmed compact JSON observations; raw transcripts are rejected",
    )
    p_record_diagnostic.add_argument("--pretty", action="store_true")
    p_record_diagnostic.set_defaults(func=cmd_record_diagnostic)

    p_correct_diagnostic = subparsers.add_parser(
        "correct-diagnostic",
        help="Append a confirmed correction and mark familiarity for reassessment",
    )
    p_correct_diagnostic.add_argument("concept", help="Exact canonical concept name")
    p_correct_diagnostic.add_argument("record_id", help="Diagnostic record id to amend")
    p_correct_diagnostic.add_argument("--pretty", action="store_true")
    p_correct_diagnostic.set_defaults(func=cmd_correct_diagnostic)

    p_export = subparsers.add_parser(
        "export-lifeos", help="Print the compact read-only assignment signal contract"
    )
    p_export.add_argument("--pretty", action="store_true", help="Human-readable JSON")
    p_export.set_defaults(func=cmd_export_lifeos)

    p_export_concepts = subparsers.add_parser(
        "export-concepts",
        help="Print compact familiarity signals without diagnostic evidence or notes",
    )
    p_export_concepts.add_argument("--pretty", action="store_true")
    p_export_concepts.set_defaults(func=cmd_export_concepts)

    p_check_links = subparsers.add_parser(
        "check-vault-links",
        help="Audit internal vault links and managed references without writing",
    )
    p_check_links.add_argument("--pretty", action="store_true")
    p_check_links.set_defaults(func=cmd_check_vault_links)

    p_refresh_knowledge = subparsers.add_parser(
        "refresh-knowledge",
        help="Refresh deterministic general-knowledge indexes for a vault",
    )
    p_refresh_knowledge.add_argument(
        "--vault",
        type=Path,
        default=config.VAULT_PATH,
        help="Vault root (defaults to VAULT_PATH)",
    )
    p_refresh_knowledge.add_argument("--pretty", action="store_true")
    p_refresh_knowledge.set_defaults(func=cmd_refresh_knowledge)

    p_capture_knowledge = subparsers.add_parser(
        "capture-knowledge",
        help="Capture one note into the general-knowledge inbox",
    )
    p_capture_knowledge.add_argument("title", help="Short capture title")
    capture_source = p_capture_knowledge.add_mutually_exclusive_group()
    capture_source.add_argument("--text", help="Inline capture text")
    capture_source.add_argument(
        "--file",
        type=Path,
        help=f"UTF-8 capture file (maximum {_MAX_CAPTURE_FILE_BYTES} bytes)",
    )
    p_capture_knowledge.add_argument(
        "--vault",
        type=Path,
        default=config.VAULT_PATH,
        help="Vault root (defaults to VAULT_PATH)",
    )
    p_capture_knowledge.add_argument("--pretty", action="store_true")
    p_capture_knowledge.set_defaults(func=cmd_capture_knowledge)

    p_research = subparsers.add_parser(
        "research",
        help="Search through the provider-neutral, model-free research adapter",
    )
    p_research.add_argument("query", help="Compact public search query")
    p_research.add_argument("--max-results", type=int, default=5)
    p_research.add_argument(
        "--response-file",
        help="Replay a normalized saved JSON result bundle with no network spend",
    )
    p_research.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass any reusable live-provider cache entry",
    )
    p_research.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the live-provider cache",
    )
    p_research.add_argument("--pretty", action="store_true")
    p_research.set_defaults(func=cmd_research)

    p_archive_course = subparsers.add_parser(
        "archive-course",
        help="Mark one completed course archived without moving files",
    )
    p_archive_course.add_argument("course", help="Exact course folder name")
    p_archive_course.add_argument("--pretty", action="store_true")
    p_archive_course.set_defaults(func=cmd_archive_course)

    p_restore_course = subparsers.add_parser(
        "restore-course",
        help="Restore one archived course to active without moving files",
    )
    p_restore_course.add_argument("course", help="Exact course folder name")
    p_restore_course.add_argument("--pretty", action="store_true")
    p_restore_course.set_defaults(func=cmd_restore_course)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        print(f"Error: {e}")
        return 1
