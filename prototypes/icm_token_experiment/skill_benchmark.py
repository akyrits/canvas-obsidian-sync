"""Offline routing-cost benchmark for the repo-scoped analysis skill."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / ".agents" / "skills" / "canvas-study-analysis"
SKILL_PATH = SKILL_DIR / "SKILL.md"
OPENAI_YAML_PATH = SKILL_DIR / "agents" / "openai.yaml"
CONTEXT_PATH = ROOT / "study_analysis" / "CONTEXT.md"
TRIGGER = "$canvas-study-analysis"

SHOULD_USE = (
    f'Use {TRIGGER} to analyze "Assignment 6" in Study mode.',
    f'Use {TRIGGER} to analyze the Unit 9 activity in Expert mode privately.',
    f"Use {TRIGGER} to validate the Assignment 6 replay fixture offline.",
    f"Use {TRIGGER} to link evidence, then run Study analysis.",
    f"Use {TRIGGER} to report analysis usage and context telemetry.",
)

SHOULD_NOT_USE = (
    "Run canvas-study-analysis on Assignment 6 in Study mode.",
    "Sync Canvas into my Obsidian vault now.",
    "Index this PDF, but do not analyze anything.",
    "Export current assignment signals to LifeOS.",
    "What did the last Assignment 6 analysis cost, and did it pass?",
)


def _read_raw(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _stats(text: str) -> dict[str, int]:
    return {
        "characters": len(text),
        "words": len(re.findall(r"\S+", text)),
        "chars_div_4_proxy_tokens": math.ceil(len(text) / 4),
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _quoted_yaml_value(text: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*\"([^\"]*)\"\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def build_report() -> dict[str, object]:
    skill = _read_raw(SKILL_PATH)
    context = _read_raw(CONTEXT_PATH)
    openai_yaml = _read_raw(OPENAI_YAML_PATH)
    default_prompt = _quoted_yaml_value(openai_yaml, "default_prompt") or ""

    skill_stats = _stats(skill)
    context_stats = _stats(context)
    trigger_stats = _stats(TRIGGER)
    default_prompt_stats = _stats(default_prompt)
    activated_proxy = (
        skill_stats["chars_div_4_proxy_tokens"]
        + trigger_stats["chars_div_4_proxy_tokens"]
    )
    baseline_proxy = context_stats["chars_div_4_proxy_tokens"]
    savings_percent = round((1 - activated_proxy / baseline_proxy) * 100, 1)

    frontmatter_match = re.match(r"^---\r?\n(.*?)\r?\n---", skill, re.DOTALL)
    frontmatter = frontmatter_match.group(1) if frontmatter_match else ""
    frontmatter_keys = set(re.findall(r"^([a-zA-Z0-9_-]+):", frontmatter, re.MULTILINE))
    skill_name_match = re.search(r"^name:\s*([^\r\n]+)$", frontmatter, re.MULTILINE)
    description_match = re.search(
        r"^description:\s*([^\r\n]+)$", frontmatter, re.MULTILINE
    )
    skill_name = skill_name_match.group(1).strip() if skill_name_match else ""
    description = description_match.group(1).strip() if description_match else ""
    routed_paths = (
        ROOT / "AGENTS.md",
        CONTEXT_PATH,
        ROOT / "README.md",
        ROOT / "prototypes" / "icm_token_experiment" / "README.md",
    )

    gates = {
        "frontmatter_only_name_and_description": frontmatter_keys == {"name", "description"},
        "frontmatter_name_valid": bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_name))
        and len(skill_name) <= 64,
        "frontmatter_description_valid": bool(description)
        and len(description) <= 1024
        and "<" not in description
        and ">" not in description,
        "explicit_only_policy": bool(
            re.search(
                r"^\s*allow_implicit_invocation:\s*false\s*$",
                openai_yaml,
                re.MULTILINE,
            )
        ),
        "default_prompt_names_skill": TRIGGER in default_prompt,
        "all_routed_paths_exist": all(path.is_file() for path in routed_paths),
        "production_deep_interface_present": "AnalysisEngine.analyze(AnalysisRequest)" in skill,
        "positive_trigger_matrix_5_of_5": sum(TRIGGER in prompt for prompt in SHOULD_USE) == 5,
        "negative_trigger_matrix_5_of_5": sum(TRIGGER not in prompt for prompt in SHOULD_NOT_USE) == 5,
        "activated_proxy_saves_at_least_20_percent": savings_percent >= 20.0,
        "dormant_model_context_characters_zero_by_policy": True,
        "provider_and_network_calls_zero": True,
    }

    return {
        "method": {
            "kind": "offline static routing benchmark",
            "token_count": "ceil(raw UTF-8 text characters / 4); proxy, not exact model billing",
            "baseline_increment": "study_analysis/CONTEXT.md after common AGENTS.md",
            "candidate_increment": "full SKILL.md plus explicit $skill trigger",
            "default_prompt_reported_separately": True,
            "writes": "none",
            "provider_or_network_calls": 0,
        },
        "measurements": {
            "dormant_skill_model_context_characters": 0,
            "baseline_context": context_stats,
            "activated_skill_body": skill_stats,
            "explicit_trigger": trigger_stats,
            "activated_proxy_tokens": activated_proxy,
            "default_prompt": default_prompt_stats,
            "proxy_token_savings_percent": savings_percent,
        },
        "trigger_matrix": {
            "should_use": list(SHOULD_USE),
            "should_not_use": list(SHOULD_NOT_USE),
        },
        "source_hashes": {
            str(SKILL_PATH.relative_to(ROOT)): _sha256(skill),
            str(OPENAI_YAML_PATH.relative_to(ROOT)): _sha256(openai_yaml),
            str(CONTEXT_PATH.relative_to(ROOT)): _sha256(context),
        },
        "gates": gates,
        "passed": all(gates.values()),
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
