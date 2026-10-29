"""Tests for output-directory naming."""

import unittest
from pathlib import Path

from vsphere_download.workflow import output_dir

TS = "2026-10-07_11-00-13"


TARGET = Path("/out")


class OutputDirTest(unittest.TestCase):
    def test_plain_name_is_kept(self) -> None:
        self.assertEqual(output_dir(TARGET, "dc01", TS), TARGET / f"dc01_{TS}")

    def test_dots_and_hyphens_are_kept(self) -> None:
        self.assertEqual(
            output_dir(TARGET, "web-01.corp", TS), TARGET / f"web-01.corp_{TS}"
        )

    def test_unsafe_characters_become_underscores(self) -> None:
        # Slashes, colons, spaces and stars are not valid in file names.
        self.assertEqual(
            output_dir(TARGET, "AD/2:DC* 01", TS), TARGET / f"AD_2_DC__01_{TS}"
        )

    def test_distinct_names_do_not_collide(self) -> None:
        self.assertNotEqual(
            output_dir(TARGET, "a b", TS), output_dir(TARGET, "a-b", TS)
        )

    def test_subfolder_is_made_in_the_target(self) -> None:
        self.assertEqual(output_dir(TARGET, "dc01", TS).parent, TARGET)


if __name__ == "__main__":
    unittest.main()
