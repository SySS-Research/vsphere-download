"""Tests for parsing Windows guest paths."""

import unittest

from vsphere_download.guestpath import GuestPath, parse_guest_path


class ParseGuestPathTest(unittest.TestCase):
    def test_backslash_path_with_drive(self) -> None:
        p = parse_guest_path(r"C:\Windows\System32\drivers\etc\hosts")
        self.assertEqual(p.parts, ("Windows", "System32", "drivers", "etc", "hosts"))
        self.assertEqual(p.name, "hosts")
        self.assertEqual(p.ntfs_path, "Windows/System32/drivers/etc/hosts")

    def test_drive_letter_is_case_insensitive(self) -> None:
        self.assertEqual(parse_guest_path(r"c:\Users").parts, ("Users",))

    def test_forward_slashes_are_accepted(self) -> None:
        self.assertEqual(
            parse_guest_path("C:/Windows/System32/drivers/etc/hosts").parts,
            ("Windows", "System32", "drivers", "etc", "hosts"),
        )

    def test_drive_is_optional(self) -> None:
        expected = ("Windows", "win.ini")
        self.assertEqual(parse_guest_path(r"\Windows\win.ini").parts, expected)
        self.assertEqual(parse_guest_path("Windows/win.ini").parts, expected)

    def test_leading_and_repeated_separators_are_collapsed(self) -> None:
        self.assertEqual(parse_guest_path(r"C:\\Users\\").parts, ("Users",))

    def test_dot_components_are_dropped(self) -> None:
        self.assertEqual(
            parse_guest_path(r"C:\Users\.\admin").parts, ("Users", "admin")
        )

    def test_other_drive_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_guest_path(r"D:\data")

    def test_parent_components_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_guest_path(r"C:\Windows\..\Users")

    def test_root_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_guest_path("C:\\")
        with self.assertRaises(ValueError):
            parse_guest_path("")


class GuestPathTest(unittest.TestCase):
    def test_str_round_trips_to_a_windows_path(self) -> None:
        p = GuestPath(("Windows", "System32"))
        self.assertEqual(str(p), r"C:\Windows\System32")

    def test_child_appends_a_component(self) -> None:
        p = GuestPath(("Users",)).child("admin")
        self.assertEqual(p.parts, ("Users", "admin"))


if __name__ == "__main__":
    unittest.main()
