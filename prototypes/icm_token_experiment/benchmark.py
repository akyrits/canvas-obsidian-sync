from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
from dotenv import load_dotenv

from prototypes.icm_token_experiment.evaluation import BenchmarkCase, evaluate
from study_analysis.context import (
    CompiledContext,
    EvidenceLocator,
    PageScopedContext,
    SelectiveContext,
)
from study_analysis.engine import AnalysisEngine, _extract_section
from study_analysis.providers import AnthropicAdapter, ModelInvocationError, ModelUsage
from study_analysis.schema import (
    AnalysisMode,
    AnalysisResult,
    AnalysisValidationError,
    analysis_json_schema,
)
from study_analysis.sources import load_sources


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASE = Path(__file__).resolve().parent / "cases" / "assignment6.json"
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class GenerationPolicy:
    model: str
    thinking: str
    effort: str
    max_output_tokens: int


@dataclass(frozen=True)
class Arm:
    name: str
    prompt: str
    context: CompiledContext


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _vault_root(assignment_path: Path) -> Path:
    parents = assignment_path.resolve().parents
    if len(parents) < 3:
        raise ValueError("Assignment path must be inside <vault>/School/<course>/")
    return parents[2]


def _validate_output_dir(output_dir: Path, assignment_path: Path) -> None:
    resolved = output_dir.resolve()
    if _is_within(resolved, _vault_root(assignment_path)):
        raise ValueError("Benchmark output must be outside the Obsidian vault")
    if _is_within(resolved, PROJECT_ROOT):
        raise ValueError("Benchmark output must be outside the public code repository")


def _snapshot_vault(assignment_path: Path) -> dict[str, str]:
    vault_root = _vault_root(assignment_path)
    course = assignment_path.parent
    targets = [assignment_path, *sorted((course / "Sources").glob("*.md"))]
    for folder in (vault_root / "Knowledge" / "Concepts", course / "Solutions"):
        if folder.is_dir():
            targets.extend(sorted(folder.rglob("*.md")))
    return {
        path.relative_to(vault_root).as_posix(): _sha256_file(path)
        for path in targets
        if path.is_file()
    }


def _build_arms(
    assignment_path: Path,
    retrieval_chars: int,
    max_input_chars: int,
    case: BenchmarkCase,
) -> tuple[Arm, ...]:
    assignment = frontmatter.load(assignment_path)
    details = _extract_section(assignment.content, "Assignment Details")
    if not details:
        raise ValueError("Assignment Details is empty")
    sources = load_sources(assignment_path)
    if not sources:
        raise ValueError("No indexed analysis sources are linked")
    compilers = (
        PageScopedContext(),
        SelectiveContext(max_evidence_chars=retrieval_chars),
    )
    names = ("full-evidence-v1", f"retrieval-{retrieval_chars // 1000}k-v1")
    arms: list[Arm] = []
    for name, compiler in zip(names, compilers):
        context = compiler.compile(details, sources, max_input_chars)
        prompt = AnalysisEngine._build_prompt(
            assignment_title=assignment_path.stem,
            course=str(assignment.get("course") or "unknown"),
            due=str(assignment.get("due") or "unknown"),
            details=details,
            sources=context.text,
            mode=AnalysisMode.STUDY,
            include_json_shape=False,
        )
        coverage_contract = "\n".join(
            f"- {target.label}" for target in case.required_topics
        )
        prompt += (
            "\nBenchmark coverage contract:\n"
            "Produce one distinct durable concept for each item below. Do not merge "
            "two assessed skills into one concept. This names topics, not answers.\n"
            f"{coverage_contract}\n"
        )
        arms.append(Arm(name, prompt, context))
    return tuple(arms)


def _request_config(policy: GenerationPolicy) -> dict[str, Any]:
    return {
        "model": policy.model,
        "messages": [{"role": "user", "content": ""}],
        "thinking": {"type": policy.thinking},
        "output_config": {
            "effort": policy.effort,
            "format": {"type": "json_schema", "schema": analysis_json_schema()},
        },
    }


def _count_tokens(arms: tuple[Arm, ...], policy: GenerationPolicy) -> dict[str, int]:
    import anthropic

    client = anthropic.Anthropic()
    base = _request_config(policy)
    counts: dict[str, int] = {}
    for arm in arms:
        request = dict(base)
        request["messages"] = [{"role": "user", "content": arm.prompt}]
        counts[arm.name] = client.messages.count_tokens(**request).input_tokens
    return counts


def _manifest(
    assignment_path: Path,
    case_path: Path,
    output_dir: Path,
    arms: tuple[Arm, ...],
    policy: GenerationPolicy,
    pairs: int,
    order_seed: int,
    exact_counts: dict[str, int] | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assignment_path": str(assignment_path.resolve()),
        "assignment_sha256": _sha256_file(assignment_path),
        "case_path": str(case_path.resolve()),
        "case_file": "case-spec.json",
        "case_sha256": _sha256_file(case_path),
        "output_dir": str(output_dir.resolve()),
        "pairs": pairs,
        "order_seed": order_seed,
        "generation_policy": asdict(policy),
        "schema_sha256": _sha256_bytes(
            json.dumps(analysis_json_schema(), sort_keys=True).encode("utf-8")
        ),
        "arms": [
            {
                "name": arm.name,
                "context_strategy": arm.context.strategy,
                "context_chars": arm.context.evidence_chars,
                "context_sha256": arm.context.sha256,
                "prompt_chars": len(arm.prompt),
                "prompt_sha256": _sha256_bytes(arm.prompt.encode("utf-8")),
                "input_tokens": exact_counts.get(arm.name) if exact_counts else None,
                "rough_chars_div_4": math.ceil(len(arm.prompt) / 4),
                "selected_evidence": [
                    locator.label for locator in arm.context.selected
                ],
                "selected_locators": [
                    asdict(locator) for locator in arm.context.selected
                ],
                "available_evidence": arm.context.available_chunks,
                "truncated": arm.context.truncated,
                "source_hashes": list(arm.context.source_hashes),
            }
            for arm in arms
        ],
    }


def _usage_dict(usage: ModelUsage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    value = asdict(usage)
    value["total_input_tokens"] = usage.total_input_tokens
    value["non_thinking_output_tokens"] = usage.non_thinking_output_tokens
    return value


def _cost(usage: ModelUsage, input_price: float, output_price: float) -> float:
    return (
        usage.total_input_tokens * input_price / 1_000_000
        + usage.output_tokens * output_price / 1_000_000
    )


def _summarize(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    arms: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        arms.setdefault(attempt["arm"], []).append(attempt)
    arm_summaries = {}
    for name, rows in arms.items():
        successful = [row for row in rows if row["status"] == "success"]
        costs = [row["cost_usd"] for row in successful]
        inputs = [row["usage"]["total_input_tokens"] for row in successful]
        outputs = [row["usage"]["output_tokens"] for row in successful]
        totals = [
            input_tokens + output_tokens
            for input_tokens, output_tokens in zip(inputs, outputs)
        ]
        durations = [row["duration_seconds"] for row in successful]
        thinking = [row["usage"]["thinking_tokens"] for row in successful]
        arm_summaries[name] = {
            "attempts": len(rows),
            "successful": len(successful),
            "automatic_gate_passes": sum(
                bool(row.get("evaluation", {}).get("passed")) for row in successful
            ),
            "median_input_tokens": statistics.median(inputs) if inputs else None,
            "median_output_tokens": statistics.median(outputs) if outputs else None,
            "median_total_tokens": statistics.median(totals) if totals else None,
            "median_thinking_tokens": statistics.median(thinking) if thinking else None,
            "median_duration_seconds": statistics.median(durations) if durations else None,
            "median_cost_usd": statistics.median(costs) if costs else None,
        }
    summary: dict[str, Any] = {"arms": arm_summaries, "production_enabled": False}
    baseline = arm_summaries.get("full-evidence-v1")
    candidate_name = next(
        (name for name in arm_summaries if name.startswith("retrieval-")), None
    )
    candidate = arm_summaries.get(candidate_name) if candidate_name else None
    if baseline and candidate:
        savings: dict[str, Any] = {"candidate": candidate_name}
        for key in (
            "median_input_tokens",
            "median_output_tokens",
            "median_total_tokens",
            "median_cost_usd",
            "median_duration_seconds",
        ):
            before = baseline[key]
            after = candidate[key]
            if before is None or after is None:
                continue
            label = key.removeprefix("median_")
            savings[f"{label}_delta"] = before - after
            savings[f"{label}_percent"] = (
                round((before - after) / before * 100, 2) if before else None
            )
        summary["candidate_savings"] = savings
    return summary


def _render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Context benchmark report",
        "",
        "Automatic results only. Human blind review remains required.",
        "",
        "| Arm | Success | Auto gates | Median input | Median output | Median total | Median latency | Median cost |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, values in summary["arms"].items():
        cost = values["median_cost_usd"]
        lines.append(
            "| {name} | {successful}/{attempts} | {passes}/{successful} | {input} | "
            "{output} | {total} | {latency} | {cost} |".format(
                name=name,
                successful=values["successful"],
                attempts=values["attempts"],
                passes=values["automatic_gate_passes"],
                input=values["median_input_tokens"] or "—",
                output=values["median_output_tokens"] or "—",
                total=values["median_total_tokens"] or "—",
                latency=(
                    f'{values["median_duration_seconds"]:.3f}s'
                    if values["median_duration_seconds"] is not None
                    else "—"
                ),
                cost=f"${cost:.5f}" if cost is not None else "—",
            )
        )
    savings = summary.get("candidate_savings")
    if savings:
        lines.extend(
            (
                "",
                "Candidate savings versus full evidence:",
                "",
                f'- Input tokens: {savings.get("input_tokens_percent", "—")}% ',
                f'- Total tokens: {savings.get("total_tokens_percent", "—")}% ',
                f'- Cost: {savings.get("cost_usd_percent", "—")}% ',
                f'- Latency: {savings.get("duration_seconds_percent", "—")}% ',
            )
        )
    lines.extend(
        (
            "",
            "Production retrieval remains disabled until multiple assignments pass",
            "automatic and human quality gates.",
        )
    )
    return "\n".join(lines) + "\n"


def plan(args: argparse.Namespace) -> int:
    assignment = Path(args.assignment).resolve()
    case_path = Path(args.case).resolve()
    output = Path(args.output).resolve()
    _validate_output_dir(output, assignment)
    policy = GenerationPolicy(
        args.model, args.thinking, args.effort, args.max_output_tokens
    )
    case = BenchmarkCase.load(case_path)
    arms = _build_arms(
        assignment, args.retrieval_chars, args.max_input_chars, case
    )
    counts = _count_tokens(arms, policy) if args.count_api else None
    output.mkdir(parents=True, exist_ok=True)
    (output / "case-spec.json").write_bytes(case_path.read_bytes())
    _write_json(
        output / "run-manifest.json",
        _manifest(
            assignment,
            case_path,
            output,
            arms,
            policy,
            args.pairs,
            args.order_seed,
            counts,
        ),
    )
    before = _snapshot_vault(assignment)
    after = _snapshot_vault(assignment)
    _write_json(
        output / "vault-integrity.json",
        {"before": before, "after": after, "unchanged": before == after},
    )
    _write_json(
        output / "blind-review.json",
        {
            "status": "pending",
            "instructions": "Review response files without consulting arm mapping.",
            "reviews": [],
        },
    )
    print(json.dumps({"output": str(output), "input_tokens": counts}, indent=2))
    return 0


def compare(args: argparse.Namespace) -> int:
    if not args.execute_paid:
        raise ValueError("Paid comparison requires --execute-paid")
    assignment = Path(args.assignment).resolve()
    case_path = Path(args.case).resolve()
    output = Path(args.output).resolve()
    _validate_output_dir(output, assignment)
    policy = GenerationPolicy(
        args.model, args.thinking, args.effort, args.max_output_tokens
    )
    case = BenchmarkCase.load(case_path)
    arms = _build_arms(
        assignment, args.retrieval_chars, args.max_input_chars, case
    )
    if args.arm == "full":
        arms = tuple(arm for arm in arms if arm.name == "full-evidence-v1")
    elif args.arm == "retrieval":
        arms = tuple(arm for arm in arms if arm.name.startswith("retrieval-"))
    counts = _count_tokens(arms, policy)
    worst_case = args.pairs * sum(
        count * args.input_price / 1_000_000
        + policy.max_output_tokens * args.output_price / 1_000_000
        for count in counts.values()
    )
    if worst_case > args.max_cost_usd:
        raise ValueError(
            f"Worst-case ${worst_case:.4f} exceeds --max-cost-usd ${args.max_cost_usd:.4f}"
        )

    output.mkdir(parents=True, exist_ok=True)
    (output / "case-spec.json").write_bytes(case_path.read_bytes())
    before = _snapshot_vault(assignment)
    _write_json(
        output / "run-manifest.json",
        _manifest(
            assignment,
            case_path,
            output,
            arms,
            policy,
            args.pairs,
            args.order_seed,
            counts,
        ),
    )
    adapter = AnthropicAdapter(
        policy.model,
        os.environ.get("ANTHROPIC_API_KEY"),
        effort=policy.effort,
        thinking=policy.thinking,
    )
    attempts: list[dict[str, Any]] = []
    for pair in range(1, args.pairs + 1):
        order = list(arms)
        random.Random(args.order_seed + pair).shuffle(order)
        for position, arm in enumerate(order, start=1):
            attempt_id = f"pair-{pair:03d}-{position}"
            started = time.monotonic()
            usage: ModelUsage | None = None
            row: dict[str, Any] = {
                "attempt_id": attempt_id,
                "pair": pair,
                "order": position,
                "arm": arm.name,
                "prompt_sha256": _sha256_bytes(arm.prompt.encode("utf-8")),
            }
            try:
                reply = adapter.generate_json(arm.prompt, policy.max_output_tokens)
                usage = reply.usage
                response_path = output / "responses" / f"{attempt_id}.json"
                _write_json(response_path, reply.payload)
                row.update(
                    {
                        "response_file": str(response_path.relative_to(output)),
                        "response_sha256": _sha256_file(response_path),
                    }
                )
                result = AnalysisResult.parse(reply.payload, AnalysisMode.STUDY)
                evaluation = evaluate(result, AnalysisMode.STUDY, arm.context, case)
                row.update(
                    {
                        "status": "success",
                        "evaluation": evaluation.to_dict(),
                    }
                )
            except Exception as exc:
                if isinstance(exc, ModelInvocationError):
                    usage = exc.usage
                row.update(
                    {
                        "status": "failure",
                        "error_type": type(exc).__name__,
                        "error_kind": getattr(exc, "kind", "unknown"),
                    }
                )
                if isinstance(exc, AnalysisValidationError):
                    row["error_detail"] = str(exc)
            row["duration_seconds"] = round(time.monotonic() - started, 3)
            row["usage"] = _usage_dict(usage)
            row["cost_usd"] = (
                round(_cost(usage, args.input_price, args.output_price), 6)
                if usage is not None
                else None
            )
            attempts.append(row)
            _append_jsonl(output / "attempts.jsonl", row)
            if row.get("evaluation"):
                _append_jsonl(
                    output / "gate-results.jsonl",
                    {"attempt_id": attempt_id, **row["evaluation"]},
                )

    after = _snapshot_vault(assignment)
    integrity = {"before": before, "after": after, "unchanged": before == after}
    _write_json(output / "vault-integrity.json", integrity)
    if before != after:
        raise RuntimeError("Benchmark detected an unexpected canonical vault change")
    summary = _summarize(attempts)
    summary["actual_cost_usd"] = round(
        sum(row["cost_usd"] or 0 for row in attempts), 6
    )
    summary["worst_case_cap_usd"] = round(worst_case, 6)
    _write_json(output / "summary.json", summary)
    (output / "report.md").write_text(_render_report(summary), encoding="utf-8")
    _write_json(
        output / "blind-review.json",
        {
            "status": "pending",
            "instructions": "Review each response for factual support and Study-mode leakage before reading run-manifest.json.",
            "response_ids": [row["attempt_id"] for row in attempts if row["status"] == "success"],
            "reviews": [],
        },
    )
    print(json.dumps(summary, indent=2))
    return 0


def replay(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    if args.case_override:
        case_path = Path(args.case_override).resolve()
    else:
        frozen_case = run_dir / manifest.get("case_file", "case-spec.json")
        case_path = frozen_case if frozen_case.is_file() else Path(manifest["case_path"])
        if _sha256_file(case_path) != manifest["case_sha256"]:
            raise ValueError("Benchmark case hash no longer matches the run manifest")
    case = BenchmarkCase.load(case_path)
    policy = GenerationPolicy(**manifest["generation_policy"])
    if all("selected_locators" in arm for arm in manifest["arms"]):
        contexts = {
            arm["name"]: CompiledContext(
                text="",
                strategy=arm["context_strategy"],
                selected=tuple(
                    EvidenceLocator(**locator) for locator in arm["selected_locators"]
                ),
                available_chunks=arm["available_evidence"],
                truncated=bool(arm["truncated"]),
                source_hashes=tuple(arm["source_hashes"]),
            )
            for arm in manifest["arms"]
        }
    else:
        assignment = Path(manifest["assignment_path"])
        retrieval_chars = next(
            int(arm["name"].split("-")[1][:-1]) * 1000
            for arm in manifest["arms"]
            if arm["name"].startswith("retrieval-")
        )
        contexts = {
            arm.name: arm.context
            for arm in _build_arms(
                assignment, retrieval_chars, args.max_input_chars, case
            )
        }
    rescored = []
    for line in (run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines():
        attempt = json.loads(line)
        response_file = attempt.get("response_file")
        if not response_file:
            continue
        payload = json.loads((run_dir / response_file).read_text(encoding="utf-8"))
        result = AnalysisResult.parse(payload, AnalysisMode.STUDY)
        report = evaluate(result, AnalysisMode.STUDY, contexts[attempt["arm"]], case)
        rescored.append({"attempt_id": attempt["attempt_id"], **report.to_dict()})
    output_name = (
        "replay-gate-results.override.json"
        if args.case_override
        else "replay-gate-results.json"
    )
    _write_json(run_dir / output_name, rescored)
    if args.case_override:
        _write_json(
            run_dir / "replay-case-override.json",
            {
                "case_path": str(case_path),
                "case_sha256": _sha256_file(case_path),
                "reason": "Explicit rubric recalibration; original frozen case remains unchanged.",
            },
        )
    print(json.dumps({"model": policy.model, "rescored": len(rescored)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paired no-vault-write context benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def shared(command: argparse.ArgumentParser) -> None:
        command.add_argument("--assignment", required=True)
        command.add_argument("--case", default=str(DEFAULT_CASE))
        command.add_argument("--output", required=True)
        command.add_argument("--pairs", type=int, default=1)
        command.add_argument("--order-seed", type=int, default=3410)
        command.add_argument("--model", default=os.environ.get("MODEL_NAME") or "claude-sonnet-5")
        command.add_argument("--thinking", choices=["adaptive", "disabled"], default="adaptive")
        command.add_argument("--effort", choices=["low", "medium", "high", "max"], default="medium")
        command.add_argument("--max-output-tokens", type=int, default=8000)
        command.add_argument("--max-input-chars", type=int, default=48000)
        command.add_argument("--retrieval-chars", type=int, default=12000)

    plan_parser = subparsers.add_parser("plan", help="Build contexts and manifests without paid generation")
    shared(plan_parser)
    plan_parser.add_argument("--count-api", action="store_true", help="Use Anthropic's free exact token counter")
    plan_parser.set_defaults(func=plan)

    compare_parser = subparsers.add_parser("compare", help="Run a bounded paid paired comparison")
    shared(compare_parser)
    compare_parser.add_argument("--execute-paid", action="store_true")
    compare_parser.add_argument("--max-cost-usd", type=float, required=True)
    compare_parser.add_argument("--input-price", type=float, default=2.0)
    compare_parser.add_argument("--output-price", type=float, default=10.0)
    compare_parser.add_argument(
        "--arm",
        choices=["all", "full", "retrieval"],
        default="all",
        help="Use a single arm only for an explicit diagnostic retry.",
    )
    compare_parser.set_defaults(func=compare)

    replay_parser = subparsers.add_parser("replay", help="Rescore saved responses with zero API calls")
    replay_parser.add_argument("--run-dir", required=True)
    replay_parser.add_argument("--max-input-chars", type=int, default=48000)
    replay_parser.add_argument("--case-override")
    replay_parser.set_defaults(func=replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
