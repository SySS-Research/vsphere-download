"""Tests for VMDK descriptor parsing and extent validation."""

import unittest

from vsphere_download.errors import ExtractionError
from vsphere_download.vmdk import EXTENT_RE, open_vmdk

HEADER = "# Disk DescriptorFile\nversion=1\nCID=fffffffe\n\n"


class ExtentRegexTest(unittest.TestCase):
    def test_extent_without_offset(self) -> None:
        match = EXTENT_RE.search('RW 293601280 VMFS "vm-flat.vmdk"')
        assert match is not None
        self.assertEqual(
            match.groups(), ("RW", "293601280", "VMFS", "vm-flat.vmdk", None)
        )

    def test_extent_with_offset(self) -> None:
        match = EXTENT_RE.search('RW 1024 FLAT "disk-flat.vmdk" 2048')
        assert match is not None
        self.assertEqual(match.group(5), "2048")

    def test_access_modes(self) -> None:
        modes = [
            m.group(1)
            for m in EXTENT_RE.finditer(
                'RW 1 VMFS "a.vmdk"\nRDONLY 2 VMFS "b.vmdk"\nNOACCESS 3 VMFS "c.vmdk"\n'
            )
        ]
        self.assertEqual(modes, ["RW", "RDONLY", "NOACCESS"])


class FakeReader:
    """A reader whose descriptor fetch returns canned bytes."""

    def __init__(self, descriptor: str) -> None:
        self._data = descriptor.encode()

    def fetch(self, url: str, offset: int, length: int) -> tuple[bytes, int]:
        return self._data, len(self._data)


def open_with(descriptor: str) -> object:
    reader = FakeReader(descriptor)
    return open_vmdk(reader, "DC", "[ds] vm/vm.vmdk")  # type: ignore[arg-type]


class OpenVmdkErrorsTest(unittest.TestCase):
    def test_not_a_descriptor(self) -> None:
        with self.assertRaises(ExtractionError):
            open_with('RW 2048 VMFS "vm-flat.vmdk"\n')

    def test_encrypted_descriptor(self) -> None:
        with self.assertRaises(ExtractionError):
            open_with(HEADER + 'encryption = "aes-xts-256"\nRW 2048 VMFS "f.vmdk"\n')

    def test_no_extents(self) -> None:
        with self.assertRaises(ExtractionError):
            open_with(HEADER)

    def test_unsupported_extent_kind(self) -> None:
        with self.assertRaises(ExtractionError):
            open_with(HEADER + 'RW 2048 SPARSE "vm-s001.vmdk"\n')

    def test_noaccess_extent(self) -> None:
        with self.assertRaises(ExtractionError):
            open_with(HEADER + 'NOACCESS 2048 VMFS "vm-flat.vmdk"\n')

    def test_absolute_extent_path(self) -> None:
        with self.assertRaises(ExtractionError):
            open_with(HEADER + 'RW 2048 VMFS "/abs/vm-flat.vmdk"\n')


if __name__ == "__main__":
    unittest.main()
