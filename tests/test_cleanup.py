"""Tests for finding leftover snapshots and the datastores to sweep."""

import unittest
from datetime import datetime
from types import SimpleNamespace as NS

from vsphere_download.cleanup import (
    copy_path,
    find_leftover_snapshots,
    iter_snapshot_trees,
    unique_datastores,
)
from vsphere_download.vsphere_ops import ARTIFACT_MARKER, snapshot_name


def tree(name: str, children: list[object] | None = None) -> NS:
    return NS(
        name=name,
        snapshot=NS(_moId=name),
        createTime=datetime(2026, 10, 7, 11, 0, 0).astimezone(),
        childSnapshotList=children or [],
    )


class IterSnapshotTreesTest(unittest.TestCase):
    def test_walks_children_depth_first(self) -> None:
        root = tree("a", [tree("b", [tree("c")]), tree("d")])
        names = [t.name for t in iter_snapshot_trees([root])]
        self.assertEqual(names, ["a", "b", "c", "d"])

    def test_handles_none(self) -> None:
        self.assertEqual(list(iter_snapshot_trees(None)), [])


class FindLeftoverSnapshotsTest(unittest.TestCase):
    def test_matches_only_this_tools_snapshots(self) -> None:
        ours = snapshot_name("2026-10-07_11-00-13")
        vm = {
            "name": "dc01",
            "snapshot": NS(
                rootSnapshotList=[
                    tree("admin backup", [tree(ours)]),
                ]
            ),
        }
        found = find_leftover_snapshots([vm])
        self.assertEqual([(n, name) for n, _, name, _ in found], [("dc01", ours)])

    def test_skips_vms_without_snapshots(self) -> None:
        self.assertEqual(find_leftover_snapshots([{"name": "x", "snapshot": None}]), [])

    def test_marker_substring_is_enough(self) -> None:
        vm = {"name": "dc01", "snapshot": NS(rootSnapshotList=[tree(ARTIFACT_MARKER)])}
        self.assertEqual(len(find_leftover_snapshots([vm])), 1)


class CopyPathTest(unittest.TestCase):
    def test_folder_without_trailing_slash_is_joined_with_one(self) -> None:
        # The browser reports folderPath without a trailing slash; the file
        # name must not be concatenated straight onto the folder name.
        self.assertEqual(
            copy_path("[FASTRAID1] winvm01", "copy-2000.vmdk"),
            "[FASTRAID1] winvm01/copy-2000.vmdk",
        )

    def test_folder_with_trailing_slash(self) -> None:
        self.assertEqual(
            copy_path("[FASTRAID1] winvm01/", "copy-2000.vmdk"),
            "[FASTRAID1] winvm01/copy-2000.vmdk",
        )

    def test_datastore_root(self) -> None:
        self.assertEqual(
            copy_path("[FASTRAID1] ", "copy-2000.vmdk"),
            "[FASTRAID1] copy-2000.vmdk",
        )


class UniqueDatastoresTest(unittest.TestCase):
    def test_dedups_shared_datastores_by_moid(self) -> None:
        ds1, ds2 = NS(_moId="ds-1"), NS(_moId="ds-2")
        vms = [
            {"obj": NS(datastore=[ds1, ds2])},
            {"obj": NS(datastore=[ds1])},  # shares ds1 with the first VM
        ]
        result = unique_datastores(vms)
        self.assertEqual(sorted(d._moId for d in result), ["ds-1", "ds-2"])


if __name__ == "__main__":
    unittest.main()
