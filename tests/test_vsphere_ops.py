"""Tests for the disk-copy size estimate."""

import unittest
from types import SimpleNamespace as NS

from vsphere_download.vsphere_ops import estimate_copy_size


def disk_device(backing: object, key: int, by_bytes: int = 0, by_kb: int = 0) -> object:
    return NS(backing=backing, key=key, capacityInBytes=by_bytes, capacityInKB=by_kb)


class EstimateCopySizeTest(unittest.TestCase):
    def test_without_layout_uses_capacity_in_bytes(self) -> None:
        device = disk_device(NS(fileName="base.vmdk"), key=2000, by_bytes=777)
        self.assertEqual(estimate_copy_size(NS(layoutEx=None), device), 777)

    def test_without_layout_falls_back_to_kb(self) -> None:
        device = disk_device(NS(fileName="base.vmdk"), key=2000, by_kb=100)
        self.assertEqual(estimate_copy_size(NS(layoutEx=None), device), 100 * 1024)

    def test_layout_sums_only_the_snapshot_chain(self) -> None:
        layout = NS(
            file=[
                NS(key=1, name="base.vmdk", size=100),
                NS(key=2, name="snap.vmdk", size=20),
                NS(key=3, name="later.vmdk", size=999),
            ],
            disk=[
                NS(
                    key=2000,
                    chain=[
                        NS(fileKey=[1]),  # base: in the snapshot's chain
                        NS(fileKey=[2]),  # snapshot delta: in the chain
                        NS(fileKey=[3]),  # a later delta: not counted
                    ],
                )
            ],
        )
        backing = NS(fileName="snap.vmdk", parent=NS(fileName="base.vmdk"))
        device = disk_device(backing, key=2000, by_bytes=99999)
        self.assertEqual(estimate_copy_size(NS(layoutEx=layout), device), 120)

    def test_layout_without_matching_disk_uses_capacity(self) -> None:
        layout = NS(file=[], disk=[NS(key=9999, chain=[])])
        device = disk_device(NS(fileName="base.vmdk"), key=2000, by_bytes=42)
        self.assertEqual(estimate_copy_size(NS(layoutEx=layout), device), 42)


if __name__ == "__main__":
    unittest.main()
