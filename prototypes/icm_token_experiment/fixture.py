from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import frontmatter

from study_analysis.sources import index_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DirectSource:
    title: str
    path: Path
    page_specs: tuple[str, ...] = ()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _file_manifest(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {"path": str(resolved), "sha256": _sha256(resolved)}


def build_isolated_fixture(
    assignment_path: Path,
    output_root: Path,
    source_records: Sequence[Path] = (),
    direct_sources: Sequence[DirectSource] = (),
) -> Path:
    """Clone one assignment and re-index evidence without changing its vault."""

    assignment_path = assignment_path.resolve()
    output_root = output_root.resolve()
    original_vault = _vault_root(assignment_path)
    for protected in (original_vault, PROJECT_ROOT):
        if _is_within(output_root, protected) or _is_within(protected, output_root):
            raise ValueError(
                "Fixture output must be separate from the live vault and code repository"
            )
    if output_root.exists():
        raise FileExistsError(f"Fixture destination already exists: {output_root}")
    if not source_records and not direct_sources:
        raise ValueError("At least one source record or direct source is required")

    source_assignment = frontmatter.load(assignment_path)
    metadata = dict(source_assignment.metadata)
    metadata.pop("analysis_sources", None)
    course_name = assignment_path.parent.name
    fixture_course = output_root / "vault" / "School" / course_name
    fixture_course.mkdir(parents=True)
    fixture_assignment = fixture_course / assignment_path.name
    cloned = frontmatter.Post(source_assignment.content, **metadata)
    fixture_assignment.write_text(frontmatter.dumps(cloned), encoding="utf-8")

    origin_records: list[dict[str, str]] = []
    evidence_files: list[dict[str, str]] = []
    indexed: list[dict[str, Any]] = []
    for record_path in source_records:
        record_path = record_path.resolve()
        record = frontmatter.load(record_path)
        source_path = Path(str(record.get("source_path") or "")).resolve()
        pages = tuple(int(page) for page in (record.get("relevant_pages") or ()))
        page_specs = (",".join(str(page) for page in pages),) if pages else ()
        index_source(
            fixture_course,
            source_path,
            record_path.stem,
            list(page_specs),
            fixture_assignment,
        )
        origin_records.append(_file_manifest(record_path))
        evidence_files.append(_file_manifest(source_path))
        indexed.append(
            {
                "title": record_path.stem,
                "source_path": str(source_path),
                "relevant_pages": list(pages),
                "origin_record": str(record_path),
            }
        )

    for source in direct_sources:
        source_path = source.path.resolve()
        index_source(
            fixture_course,
            source_path,
            source.title,
            list(source.page_specs),
            fixture_assignment,
        )
        evidence_files.append(_file_manifest(source_path))
        indexed.append(
            {
                "title": source.title,
                "source_path": str(source_path),
                "page_specs": list(source.page_specs),
                "origin_record": None,
            }
        )

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_assignment": _file_manifest(assignment_path),
        "source_records": origin_records,
        "evidence_files": evidence_files,
        "indexed_sources": indexed,
        "fixture_assignment": str(fixture_assignment),
    }
    (output_root / "fixture-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return fixture_assignment


def verify_fixture_origins(output_root: Path) -> dict[str, Any]:
    manifest_path = output_root.resolve() / "fixture-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    for group in ("source_assignment", "source_records", "evidence_files"):
        entries = manifest[group]
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            path = Path(entry["path"])
            current = _sha256(path) if path.is_file() else None
            checks.append(
                {
                    "path": str(path),
                    "expected_sha256": entry["sha256"],
                    "current_sha256": current,
                    "unchanged": current == entry["sha256"],
                }
            )
    return {"unchanged": all(check["unchanged"] for check in checks), "checks": checks}


def _prepare(args: argparse.Namespace) -> int:
    direct_sources = [
        DirectSource(title, Path(path))
        for title, path in (args.text_source or ())
    ]
    direct_sources.extend(
        DirectSource(title, Path(path), (pages,))
        for title, path, pages in (args.paged_source or ())
    )
    assignment = build_isolated_fixture(
        Path(args.assignment),
        Path(args.output),
        tuple(Path(path) for path in (args.source_record or ())),
        tuple(direct_sources),
    )
    print(json.dumps({"fixture_assignment": str(assignment)}, indent=2))
    return 0


def _verify(args: argparse.Namespace) -> int:
    report = verify_fixture_origins(Path(args.fixture))
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    print(json.dumps(report, indent=2))
    return 0 if report["unchanged"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare isolated benchmark vault fixtures")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--assignment", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--source-record", action="append")
    prepare.add_argument(
        "--text-source", action="append", nargs=2, metavar=("TITLE", "PATH")
    )
    prepare.add_argument(
        "--paged-source",
        action="append",
        nargs=3,
        metavar=("TITLE", "PATH", "PAGES"),
    )
    prepare.set_defaults(func=_prepare)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--fixture", required=True)
    verify.add_argument("--output")
    verify.set_defaults(func=_verify)
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
