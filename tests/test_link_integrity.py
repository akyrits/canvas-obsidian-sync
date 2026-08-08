from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from study_analysis.link_integrity import audit_vault_links


_REPORT_FIELDS = {
    "ok",
    "notes_scanned",
    "files_indexed",
    "references_checked",
    "resolved_references",
    "issues",
}


class LinkIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, content: str | bytes = "") -> Path:
        path = self.vault / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _hashes(root: Path) -> dict[str, str]:
        """Hash vault file bytes without following directory links."""
        hashes: dict[str, str] = {}
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            filenames.sort()
            parent = Path(directory)
            for filename in filenames:
                path = parent / filename
                if path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    def _report_dict(self, report: Any) -> dict[str, Any]:
        payload = report.to_dict()
        self.assertTrue(_REPORT_FIELDS.issubset(payload))
        self.assertEqual(payload["ok"], report.ok)
        self.assertEqual(payload["notes_scanned"], report.notes_scanned)
        self.assertEqual(payload["files_indexed"], report.files_indexed)
        self.assertEqual(payload["references_checked"], report.references_checked)
        self.assertEqual(payload["resolved_references"], report.resolved_references)
        self.assertEqual(len(payload["issues"]), len(report.issues))
        self.assertFalse(payload["model_attempted"])
        self.assertEqual(payload["input_tokens"], 0)
        self.assertEqual(payload["output_tokens"], 0)
        return payload

    def test_valid_links_alias_asset_relative_path_and_code_exclusions(self) -> None:
        self._write(
            "Knowledge/Concepts/Tree Traversal.md",
            """---
aliases:
  - Traversals
  - Tree Walk
---
# Tree Traversal

## Definition

Visit every node in a defined order. ^definition-block
""",
        )
        self._write("School/Course/_Course Info.md", "# Course Info\n")
        self._write("Attachments/diagram.png", b"not-a-real-png-but-a-real-asset")
        self._write(
            "School/Current/Assignment.md",
            """# Assignment

[[Knowledge/Concepts/Tree Traversal#Definition|displayed definition]]
[[Knowledge/Concepts/Tree Traversal#^definition-block]]
[[Traversals]]
![[Attachments/diagram.png]]
[[../Course/_Course Info]]

[External site](https://example.com) and ![remote](https://example.com/image.png).
<mailto:student@example.com>

`[[Ignored Inline Link]]`

<!-- [[Ignored HTML Comment]] -->
<!--
![[Ignored Multiline Comment.png]]
-->

```markdown
[[Ignored Fenced Link]]
![[Ignored Fenced Asset.png]]
```
""",
        )
        before = self._hashes(self.vault)

        report = audit_vault_links(self.vault)

        payload = self._report_dict(report)
        self.assertTrue(report.ok, payload["issues"])
        self.assertEqual(report.notes_scanned, 3)
        self.assertEqual(report.files_indexed, 4)
        self.assertEqual(report.references_checked, 5)
        self.assertEqual(report.resolved_references, 5)
        self.assertEqual(payload["issues"], [])
        self.assertEqual(self._hashes(self.vault), before)

    def test_reports_unresolved_and_ambiguous_basename_and_alias(self) -> None:
        self._write("One/Shared.md", "# First Shared\n")
        self._write("Two/Shared.md", "# Second Shared\n")
        self._write(
            "One/First.md",
            "---\naliases: [Duplicate Alias]\n---\n# First\n",
        )
        self._write(
            "Two/Second.md",
            "---\naliases: [Duplicate Alias]\n---\n# Second\n",
        )
        self._write(
            "Home.md",
            "[[Missing Note]]\n[[Shared]]\n[[Duplicate Alias]]\n",
        )

        report = audit_vault_links(self.vault)

        payload = self._report_dict(report)
        self.assertFalse(report.ok)
        self.assertEqual(report.references_checked, 3)
        self.assertEqual(report.resolved_references, 0)
        self.assertEqual(len(payload["issues"]), 3)
        by_target = {issue["target"]: issue for issue in payload["issues"]}
        self.assertEqual(by_target["Missing Note"]["kind"], "unresolved")
        self.assertEqual(by_target["Shared"]["kind"], "ambiguous")
        self.assertEqual(by_target["Duplicate Alias"]["kind"], "ambiguous")
        self.assertEqual(
            by_target["Shared"]["candidates"],
            ["One/Shared.md", "Two/Shared.md"],
        )
        self.assertEqual(
            by_target["Duplicate Alias"]["candidates"],
            ["One/First.md", "Two/Second.md"],
        )

    def test_reports_each_missing_managed_frontmatter_reference(self) -> None:
        self._write(
            "School/Course/Assignment.md",
            """---
analysis_sources:
  - Sources/Missing Source.md
---
# Assignment
""",
        )
        self._write(
            "Knowledge/Concepts/Tree Traversal.md",
            """---
diagnostic_records:
  - Knowledge/Diagnostics/Tree Traversal/missing-record.md
diagnostic_amendments:
  - Knowledge/Diagnostics/Tree Traversal/missing-amendment.md
---
# Tree Traversal
""",
        )

        payload = self._report_dict(audit_vault_links(self.vault))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["references_checked"], 3)
        self.assertEqual(payload["resolved_references"], 0)
        by_field = {
            issue["reference_kind"]: issue for issue in payload["issues"]
        }
        self.assertEqual(
            set(by_field),
            {"analysis_sources", "diagnostic_records", "diagnostic_amendments"},
        )
        self.assertTrue(
            all(issue["kind"] == "unresolved" for issue in by_field.values())
        )

    def test_path_traversal_outside_vault_is_an_escape_not_a_resolution(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        self._write("Source.md", "[[../outside]]\n")
        before = self._hashes(self.vault)

        payload = self._report_dict(audit_vault_links(self.vault))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["references_checked"], 1)
        self.assertEqual(payload["resolved_references"], 0)
        self.assertEqual(len(payload["issues"]), 1)
        self.assertEqual(payload["issues"][0]["kind"], "unsafe")
        self.assertEqual(payload["issues"][0]["target"], "../outside")
        self.assertEqual(self._hashes(self.vault), before)

    def test_symlink_escape_is_rejected_when_directory_links_are_supported(self) -> None:
        external = self.base / "external"
        external.mkdir()
        (external / "Outside.md").write_text("# Outside\n", encoding="utf-8")
        linked = self.vault / "Linked"
        try:
            linked.symlink_to(external, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            if os.name != "nt":
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(external)],
                capture_output=True,
                text=True,
                check=False,
            )
            if junction.returncode != 0:
                self.skipTest("directory symlinks and junctions are unavailable")
        self._write("Source.md", "[[Linked/Outside]]\n")
        before = self._hashes(self.vault)

        payload = self._report_dict(audit_vault_links(self.vault))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["resolved_references"], 0)
        self.assertTrue(
            any(issue["kind"] == "unsafe" for issue in payload["issues"]),
            payload["issues"],
        )
        self.assertEqual(payload["files_indexed"], 1)
        self.assertEqual(payload["notes_scanned"], 1)
        self.assertEqual(self._hashes(self.vault), before)

    def test_course_attachment_junction_is_a_narrow_allowed_adapter(self) -> None:
        external = self.base / "course-files"
        external.mkdir()
        (external / "slides.pdf").write_bytes(b"local course material")
        attachment_link = self.vault / "School" / "Course" / "Attachments"
        attachment_link.parent.mkdir(parents=True)
        try:
            attachment_link.symlink_to(external, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            if os.name != "nt":
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(attachment_link), str(external)],
                capture_output=True,
                text=True,
                check=False,
            )
            if junction.returncode != 0:
                self.skipTest("directory symlinks and junctions are unavailable")
        self._write(
            "School/Course/Lectures/Lecture.md",
            "# Lecture\n\n![[slides.pdf]]\n",
        )
        before = self._hashes(self.vault)

        payload = self._report_dict(audit_vault_links(self.vault))

        self.assertTrue(payload["ok"], payload["issues"])
        self.assertEqual(payload["references_checked"], 1)
        self.assertEqual(payload["resolved_references"], 1)
        self.assertEqual(payload["files_indexed"], 1)
        self.assertEqual(self._hashes(self.vault), before)

    def test_result_and_issue_order_are_deterministic_and_read_only(self) -> None:
        self._write(
            "Zeta.md",
            "[[Missing Z]]\n[[Missing A]]\n[[Missing M]]\n",
        )
        before = self._hashes(self.vault)

        first = audit_vault_links(self.vault)
        second = audit_vault_links(self.vault)
        first_payload = self._report_dict(first)
        second_payload = self._report_dict(second)

        self.assertEqual(first_payload, second_payload)
        self.assertEqual(
            json.dumps(first_payload, sort_keys=True),
            json.dumps(second_payload, sort_keys=True),
        )
        self.assertEqual(self._hashes(self.vault), before)


if __name__ == "__main__":
    unittest.main()
