"""Interactive shell and batch report for the throwaway context experiment."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from prototypes.icm_token_experiment.context_model import ContextVariant, build_variants
from study_analysis.providers import _parse_json
from study_analysis.schema import AnalysisMode, AnalysisResult, analysis_json_schema


MODEL = "claude-sonnet-5"
INPUT_PRICE_PER_MILLION = 2.0
OUTPUT_PRICE_PER_MILLION = 10.0


@dataclass(frozen=True)
class ViewerState:
    variants: tuple[ContextVariant, ...]
    active: int = 0
    reuse_count: int = 1
    exact_counts: tuple[int, ...] | None = None


def transition(state: ViewerState, action: str) -> ViewerState:
    if action == "n":
        return replace(state, active=(state.active + 1) % len(state.variants))
    if action == "p":
        return replace(state, active=(state.active - 1) % len(state.variants))
    if action == "r":
        values = (1, 3, 5, 10)
        index = values.index(state.reuse_count)
        return replace(state, reuse_count=values[(index + 1) % len(values)])
    return state


def _count_exact(variants: tuple[ContextVariant, ...]) -> tuple[int, ...]:
    import anthropic

    client = anthropic.Anthropic()
    output_config = {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": analysis_json_schema()},
    }
    return tuple(
        client.messages.count_tokens(
            model=MODEL,
            messages=[{"role": "user", "content": variant.prompt}],
            thinking={"type": "adaptive"},
            output_config=output_config,
        ).input_tokens
        for variant in variants
    )


def _rows(state: ViewerState) -> list[dict]:
    rows = []
    baseline = (
        state.exact_counts[0] if state.exact_counts else state.variants[0].rough_tokens
    )
    for index, variant in enumerate(state.variants):
        tokens = state.exact_counts[index] if state.exact_counts else variant.rough_tokens
        rows.append(
            {
                "variant": variant.name,
                "prompt_chars": variant.prompt_chars,
                "source_chars": variant.source_chars,
                "input_tokens": tokens,
                "count_kind": "anthropic" if state.exact_counts else "rough",
                "delta_tokens": tokens - baseline,
                "input_cost_usd": round(tokens * INPUT_PRICE_PER_MILLION / 1_000_000, 5),
                "selected_pages": len(variant.selected_pages),
            }
        )
    return rows


def _normalized_words(value: str) -> set[str]:
    return {
        word
        for word in value.casefold().replace("-", " ").replace(",", " ").split()
        if len(word) > 2
    }


def _concept_overlap(generated: tuple[str, ...], reference: tuple[str, ...]) -> int:
    available = list(reference)
    matches = 0
    for name in generated:
        left = _normalized_words(name)
        scored = []
        for candidate in available:
            right = _normalized_words(candidate)
            union = left | right
            scored.append((len(left & right) / len(union) if union else 0.0, candidate))
        if scored:
            score, candidate = max(scored)
            if score >= 0.5:
                matches += 1
                available.remove(candidate)
    return matches


def _run_live(variants: tuple[ContextVariant, ...], fixture_path: Path) -> list[dict]:
    import anthropic

    reference = AnalysisResult.parse(
        json.loads(fixture_path.read_text(encoding="utf-8")), AnalysisMode.STUDY
    )
    reference_names = tuple(concept.name for concept in reference.concepts)
    client = anthropic.Anthropic()
    output_config = {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": analysis_json_schema()},
    }
    selected_names = {"current", "icm-contract", "selective-retrieval"}
    results = []
    for variant in variants:
        if variant.name not in selected_names:
            continue
        response = client.messages.create(
            model=MODEL,
            max_tokens=8_000,
            messages=[{"role": "user", "content": variant.prompt}],
            thinking={"type": "disabled"},
            output_config=output_config,
        )
        usage = response.usage
        text = "\n".join(
            block.text for block in response.content if block.type == "text"
        )
        row = {
            "variant": variant.name,
            "stop_reason": response.stop_reason,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": round(
                usage.input_tokens * INPUT_PRICE_PER_MILLION / 1_000_000
                + usage.output_tokens * OUTPUT_PRICE_PER_MILLION / 1_000_000,
                5,
            ),
        }
        if response.stop_reason != "end_turn":
            row["valid"] = False
            row["error"] = f"incomplete response: {response.stop_reason}"
            results.append(row)
            continue
        try:
            parsed = AnalysisResult.parse(_parse_json(text), AnalysisMode.STUDY)
        except Exception as exc:
            row["valid"] = False
            row["error"] = str(exc)
            results.append(row)
            continue
        names = tuple(concept.name for concept in parsed.concepts)
        citations = sum(len(concept.source_citations) for concept in parsed.concepts)
        cited_sources = sorted(
            {
                source
                for source in (
                    "M11A - General Trees",
                    "M11B - Binary Trees",
                    "Goodrich - Chapter 8 Trees",
                )
                if any(
                    source.casefold() in citation.casefold()
                    for concept in parsed.concepts
                    for citation in concept.source_citations
                )
            }
        )
        row.update(
            {
                "valid": True,
                "concepts": list(names),
                "concept_count": len(names),
                "reference_concept_overlap": _concept_overlap(names, reference_names),
                "citation_count": citations,
                "cited_sources": cited_sources,
            }
        )
        results.append(row)
    return results


def render(state: ViewerState) -> str:
    variant = state.variants[state.active]
    rows = _rows(state)
    row = rows[state.active]
    pages = ", ".join(variant.selected_pages)
    if len(pages) > 500:
        pages = pages[:497] + "..."
    return f"""\x1b[2J\x1b[H\x1b[1mPROTOTYPE — ICM token experiment\x1b[0m

\x1b[1mVariant\x1b[0m: {variant.name}
\x1b[1mDescription\x1b[0m: {variant.description}
\x1b[1mPrompt chars\x1b[0m: {variant.prompt_chars:,}
\x1b[1mInput tokens\x1b[0m: {row['input_tokens']:,} ({row['count_kind']})
\x1b[1mDelta vs current\x1b[0m: {row['delta_tokens']:+,}
\x1b[1mInput cost/run\x1b[0m: ${row['input_cost_usd']:.5f}
\x1b[1mSelected pages\x1b[0m: {len(variant.selected_pages)}
\x1b[2m{pages}\x1b[0m

\x1b[1mReuse scenario\x1b[0m: {state.reuse_count} run(s)

[n] next variant  [p] previous variant  [r] cycle reuse  [q] quit
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="Print JSON and exit")
    parser.add_argument(
        "--count-api", action="store_true", help="Use Anthropic's free token counter"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Paid no-write generation for current, ICM contract, and retrieval",
    )
    args = parser.parse_args()

    assignment = (
        config.ASSIGNMENTS_ROOT / "COP3410C 042 12962" / "Assignment 6.md"
    )
    workspace = Path(__file__).resolve().parent / "icm_workspace"
    state = ViewerState(build_variants(assignment, workspace))
    if args.count_api:
        state = replace(state, exact_counts=_count_exact(state.variants))

    if args.live:
        fixture = ROOT / "tests" / "fixtures" / "assignment6_study_analysis.json"
        print(json.dumps(_run_live(state.variants, fixture), indent=2))
        return 0

    if args.report:
        print(json.dumps(_rows(state), indent=2))
        return 0

    while True:
        print(render(state), end="", flush=True)
        action = input("> ").strip().casefold()
        if action == "q":
            return 0
        state = transition(state, action)


if __name__ == "__main__":
    raise SystemExit(main())
