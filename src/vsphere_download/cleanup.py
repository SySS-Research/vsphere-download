"""Finding and removing leftover snapshots and disk copies from past runs.

A normal run deletes its snapshot and disk copies in ``finally`` blocks, but a
crash, a lost connection, or a double Ctrl-C can still leave one behind. This
sweep finds those leftovers on the server by the ARTIFACT_MARKER this tool
embeds in every name it creates, so it can list them for a manual check and,
when asked, delete them.
"""

import posixpath
from collections.abc import Iterator
from typing import Any

from pyVim.task import WaitForTask
from pyVmomi import vim, vmodl

from vsphere_download.dspath import DatastorePath, parse_datastore_path
from vsphere_download.inventory import find_datacenter
from vsphere_download.report import TIMESTAMP_FORMAT
from vsphere_download.vsphere_ops import (
    ARTIFACT_MARKER,
    DiskCopy,
    copy_file_pattern,
    delete_disk_copies,
    delete_snapshot,
)

# A leftover snapshot: owning VM name, the snapshot MO, its name, create time.
LeftoverSnapshot = tuple[str, vim.vm.Snapshot, str, str]


def iter_snapshot_trees(
    trees: list[vim.vm.SnapshotTree] | None,
) -> Iterator[vim.vm.SnapshotTree]:
    """Yield every snapshot in the tree, depth first."""
    for tree in trees or []:
        yield tree
        yield from iter_snapshot_trees(tree.childSnapshotList)


def find_leftover_snapshots(vms: list[dict[str, Any]]) -> list[LeftoverSnapshot]:
    """Snapshots on the matched VMs whose name carries ARTIFACT_MARKER."""
    found: list[LeftoverSnapshot] = []
    for vm in vms:
        vm_name: str = vm.get("name", "?")
        info: vim.vm.SnapshotInfo | None = vm.get("snapshot")
        if info is None:
            continue
        for tree in iter_snapshot_trees(info.rootSnapshotList):
            if ARTIFACT_MARKER not in tree.name:
                continue
            created = tree.createTime.astimezone().strftime(TIMESTAMP_FORMAT)
            found.append((vm_name, tree.snapshot, tree.name, created))
    return found


def unique_datastores(vms: list[dict[str, Any]]) -> list[vim.Datastore]:
    """The datastores backing the matched VMs, each listed once."""
    by_id: dict[str, vim.Datastore] = {}
    for vm in vms:
        for ds in vm["obj"].datastore:
            by_id.setdefault(ds._moId, ds)
    return list(by_id.values())


def find_leftover_copies(datastores: list[vim.Datastore]) -> list[DiskCopy]:
    """Disk copies this tool made, found by browsing the datastores.

    Only the disk descriptors are returned (the browser collapses each disk's
    extents), so each one can be handed straight to DeleteVirtualDisk, which
    removes the descriptor and its extents together.
    """
    spec = vim.host.DatastoreBrowser.SearchSpec(
        matchPattern=[copy_file_pattern()],
        query=[vim.host.DatastoreBrowser.VmDiskQuery()],
    )
    found: list[DiskCopy] = []
    for ds in datastores:
        try:
            datacenter = find_datacenter(ds)
            task = ds.browser.SearchDatastoreSubFolders_Task(
                datastorePath=f"[{ds.name}]", searchSpec=spec
            )
            WaitForTask(task)
        except vmodl.MethodFault as e:
            msg = getattr(e, "msg", e)
            print(f"[{ds.name}]: cannot search for copies: {msg}")
            continue
        for result in task.info.result or []:
            for file in result.file or []:
                path = copy_path(result.folderPath, file.path)
                found.append((ds.name, datacenter, path))
    return found


def copy_path(folder_path: str, file_name: str) -> str:
    """Join a browser result's folder path and file name into a datastore path.

    ``folderPath`` has the form "[ds] dir" and may or may not end with a slash,
    so the two parts are joined deliberately rather than concatenated.
    """
    folder = parse_datastore_path(folder_path)
    rel = posixpath.join(folder.rel_path, file_name)
    return str(DatastorePath(folder.datastore, rel))


def sweep(si: vim.ServiceInstance, vms: list[dict[str, Any]], delete: bool) -> None:
    """List this tool's leftover artifacts on ``vms``; delete them if asked."""
    snapshots = find_leftover_snapshots(vms)
    copies = find_leftover_copies(unique_datastores(vms))

    print(f"\nLeftover snapshots: {len(snapshots)}")
    for vm_name, _, name, created in snapshots:
        print(f"  - {vm_name}: {name}  [{created}]")
    print(f"Leftover disk copies: {len(copies)}")
    for _, _, path in copies:
        print(f"  - {path}")

    if not snapshots and not copies:
        print("\nNothing left behind.")
        return
    if not delete:
        print("\nThis was a dry run; re-run with --delete to remove the above.")
        return

    print()
    for vm_name, snapshot, name, _ in snapshots:
        if confirm(f"Delete snapshot {name} on {vm_name}?"):
            delete_snapshot(vm_name, snapshot, name)
    for copy in copies:
        if confirm(f"Delete disk copy {copy[2]}?"):
            delete_disk_copies(si, [copy])


def confirm(question: str) -> bool:
    """Ask ``question`` and return whether the answer was yes (default no)."""
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")
