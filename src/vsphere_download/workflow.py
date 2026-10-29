"""Copying the requested files out of one VM."""

import re
from pathlib import Path
from typing import Any

from pyVmomi import vim

from vsphere_download.datastore import DatastoreReader
from vsphere_download.errors import ExtractionError
from vsphere_download.guestpath import GuestPath
from vsphere_download.inventory import inventory_path
from vsphere_download.vmdk import open_vmdk
from vsphere_download.vsphere_ops import (
    DiskCopy,
    copy_snapshot_disks,
    create_snapshot,
    delete_disk_copies,
    delete_snapshot,
    snapshot_name,
)
from vsphere_download.windows import CopyResult, find_files


def extract_files(
    reader: DatastoreReader,
    copies: list[DiskCopy],
    skipped: list[str],
    sources: list[GuestPath],
    directory: Path,
) -> CopyResult:
    """Copy ``sources`` from the disk copies' Windows volume to ``directory``.

    Raises ExtractionError if no readable Windows installation is found.
    ``skipped`` are the reasons why disks were not copied; they may explain
    why no Windows installation was found.
    """
    notes = list(skipped)
    for _, datacenter, path in copies:
        # The reads below are silent and can be large (a big file or a whole
        # directory); report progress so a long read is not mistaken for a hang.
        reader.start_progress(f"{path}: reading")
        try:
            disk = open_vmdk(reader, inventory_path(datacenter), path)
            result = find_files(disk, path, notes, sources, directory, reader.message)
        finally:
            reader.finish_progress()
        if result is not None:
            return result
    if not copies and not notes:
        raise ExtractionError("VM has no disks")
    raise ExtractionError(
        "no readable Windows installation found"
        + "".join(f"\n  - {note}" for note in notes)
    )


def output_dir(target: Path, vm_name: str, timestamp: str) -> Path:
    """Folder below ``target`` for one VM's files of this run."""
    # VM names may contain characters that are not valid in file names.
    return target / (re.sub(r"[^\w.-]", "_", vm_name) + "_" + timestamp)


def keep_partial(vm_name: str, directory: Path) -> None:
    """After a failed copy, remove ``directory`` if empty, else point at it."""
    if not directory.is_dir():
        return
    if any(directory.iterdir()):
        print(f"{vm_name}: what was copied so far is in {directory}/")
    else:
        directory.rmdir()


def process_vm(
    si: vim.ServiceInstance,
    reader: DatastoreReader,
    vm: dict[str, Any],
    sources: list[GuestPath],
    target: Path,
    timestamp: str,
) -> bool:
    """Copy ``sources`` out of a VM; return whether anything was copied.

    The snapshot and the disk copies are deleted however this ends.
    """
    vm_name: str = vm.get("name", "?")
    name = snapshot_name(timestamp)
    snapshot = create_snapshot(vm, name)
    if snapshot is None:
        return False
    copies: list[DiskCopy] = []
    try:
        try:
            skipped = copy_snapshot_disks(si, snapshot, name, copies)
        finally:
            # The copies hold the disks as of the snapshot, so the snapshot
            # can go before the copies are worked on. This keeps the time the
            # VM runs on a snapshot short.
            delete_snapshot(vm_name, snapshot, name)
        directory = output_dir(target, vm_name, timestamp)
        try:
            result = extract_files(reader, copies, skipped, sources, directory)
        except BaseException:
            # Includes Ctrl-C; partly written files are removed already.
            keep_partial(vm_name, directory)
            raise
        if result.copied:
            print(f"{vm_name}: wrote to {directory}/: {', '.join(result.copied)}")
        if result.notes:
            print(f"{vm_name}: not copied:")
            for note in result.notes:
                print(f"  - {note}")
        if not result.copied:
            keep_partial(vm_name, directory)
        return bool(result.copied)
    except ExtractionError as e:
        print(f"{vm_name}: cannot copy out the files: {e}")
        return False
    except OSError as e:
        # Network errors are HttpErrors by now, so this is an output file.
        print(f"{vm_name}: cannot write the files: {e}")
        return False
    finally:
        # Clean up even if copying or working on the copies failed.
        delete_disk_copies(si, copies)
