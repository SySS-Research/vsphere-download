"""Printing VM details and snapshot trees."""

from typing import Any

from pyVmomi import vim

# Used in snapshot and folder names, so it must be valid in file names.
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"


def print_vm(vm: dict[str, Any]) -> None:
    fields = [
        ("Name", "name"),
        ("Power state", "runtime.powerState"),
        ("Guest OS (config)", "config.guestFullName"),
        ("Guest ID", "config.guestId"),
    ]
    print("-" * 70)
    for label, key in fields:
        print(f"{label + ':':<20}{vm.get(key, '-')}")
    print_snapshots(vm.get("snapshot"))


def print_snapshots(info: vim.vm.SnapshotInfo | None) -> None:
    if info is None or not info.rootSnapshotList:
        print(f"{'Snapshots:':<20}-")
        return
    print("Snapshots:")
    current = info.currentSnapshot._moId if info.currentSnapshot else None
    print_snapshot_tree(info.rootSnapshotList, current, depth=1)


def print_snapshot_tree(
    snapshots: list[vim.vm.SnapshotTree], current: str | None, depth: int
) -> None:
    for snap in snapshots:
        marker = " (current)" if snap.snapshot._moId == current else ""
        # Local time, so it matches the timestamp in this tool's snapshot names.
        created = snap.createTime.astimezone().strftime(TIMESTAMP_FORMAT)
        print(f"{'  ' * depth}- {snap.name}{marker}  [{created}, {snap.state}]")
        # Skip descriptions that only repeat the name (e.g. "tmp-test").
        if snap.description and snap.description not in snap.name:
            print(f"{'  ' * (depth + 1)}{snap.description}")
        print_snapshot_disks(snap.snapshot, depth + 1)
        print_snapshot_tree(snap.childSnapshotList or [], current, depth + 1)


def print_snapshot_disks(snapshot: vim.vm.Snapshot, depth: int) -> None:
    # The snapshot's config records the disk files the VM was writing to when
    # it was taken; those files are frozen from then on. Each backing's parent
    # chain leads back to the base disk.
    for device in snapshot.config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        backing: Any = device.backing
        label = device.deviceInfo.label if device.deviceInfo else device.key
        print(f"{'  ' * depth}{label}: {backing.fileName}")
        parent = getattr(backing, "parent", None)
        while parent is not None:
            print(f"{'  ' * (depth + 1)}<- {parent.fileName}")
            parent = getattr(parent, "parent", None)
