from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from study_analysis.transaction import commit_text_files


class TransactionStateGuardTests(unittest.TestCase):
    def test_state_guard_failure_after_first_replace_rolls_back_every_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("before first", encoding="utf-8")
            second.write_text("before second", encoding="utf-8")

            def guard() -> None:
                if first.read_text(encoding="utf-8") == "after first":
                    raise RuntimeError("read dependency changed")

            with self.assertRaisesRegex(RuntimeError, "read dependency changed"):
                commit_text_files(
                    {first: "after first", second: "after second"},
                    lock_root=root,
                    expected_originals={
                        first: b"before first",
                        second: b"before second",
                    },
                    state_guard=guard,
                )

            self.assertEqual(first.read_bytes(), b"before first")
            self.assertEqual(second.read_bytes(), b"before second")


if __name__ == "__main__":
    unittest.main()
