"""Tests for datastore-path parsing and building."""

import unittest

from vsphere_download.dspath import DatastorePath, parse_datastore_path
from vsphere_download.errors import ExtractionError


class ParseTest(unittest.TestCase):
    def test_splits_datastore_and_rel_path(self) -> None:
        ds = parse_datastore_path("[FASTRAID1] vm/vm.vmdk")
        self.assertEqual(ds, DatastorePath("FASTRAID1", "vm/vm.vmdk"))

    def test_rejects_missing_datastore(self) -> None:
        with self.assertRaises(ExtractionError):
            parse_datastore_path("vm/vm.vmdk")

    def test_rejects_missing_bracket(self) -> None:
        with self.assertRaises(ExtractionError):
            parse_datastore_path("ds] vm/vm.vmdk")


class BuildTest(unittest.TestCase):
    def test_str_round_trips(self) -> None:
        path = "[ds] vm/vm.vmdk"
        self.assertEqual(str(parse_datastore_path(path)), path)

    def test_sibling_stays_in_same_directory(self) -> None:
        ds = parse_datastore_path("[ds] vm/vm.vmdk")
        self.assertEqual(str(ds.sibling("copy.vmdk")), "[ds] vm/copy.vmdk")

    def test_sibling_at_datastore_root(self) -> None:
        ds = parse_datastore_path("[ds] disk.vmdk")
        self.assertEqual(str(ds.sibling("copy.vmdk")), "[ds] copy.vmdk")


if __name__ == "__main__":
    unittest.main()
