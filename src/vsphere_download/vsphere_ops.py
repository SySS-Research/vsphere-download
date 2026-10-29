"""Snapshots and disk copies, done through the vSphere API."""

from collections.abc import Callable
from contextlib import suppress
from typing import Any, cast

from pyVim.task import WaitForTask
from pyVmomi import vim, vmodl

from vsphere_download.dspath import parse_datastore_path
from vsphere_download.inventory import find_datacenter

# Share of a datastore's capacity that must stay free after copying a disk.
FREE_SPACE_MARGIN = 0.15

# Embedded in every snapshot and disk copy this tool creates, so leftovers
# from an interrupted run can be found again and can't be mistaken for an
# admin's own snapshots or disks.
ARTIFACT_MARKER = "vsphere-download"


def snapshot_name(timestamp: str) -> str:
    """Name for a snapshot this tool takes, carrying ARTIFACT_MARKER."""
    return f"{timestamp}_{ARTIFACT_MARKER}"


def copy_file_pattern() -> str:
    """Datastore-browser glob matching this tool's disk copies, any run."""
    return f"*{ARTIFACT_MARKER}*-copy-*.vmdk"


class TaskFailed(Exception):
    """A vSphere task could not be started or failed; the message says why."""

    def __init__(self, fault: vmodl.MethodFault) -> None:
        super().__init__(fault_message(fault))
        self.fault = fault


class TaskInterrupted(KeyboardInterrupt):
    """Ctrl-C while waiting for a task, which may still run on the server.

    ``task`` is None if Ctrl-C came before the task was started, or before
    the server said that it was.
    """

    def __init__(self, task: vim.Task | None) -> None:
        super().__init__()
        self.task = task


def fault_message(fault: vmodl.MethodFault) -> str:
    return str(getattr(fault, "msg", fault))


def run_task(label: str, start: Callable[..., vim.Task], **kwargs: Any) -> Any:
    """Call ``start(**kwargs)`` and wait for the task it starts; return its result.

    Progress is printed under ``label``.

    Raises TaskFailed if the task cannot be started or fails, and
    TaskInterrupted on Ctrl-C.
    """
    print(f"{label} ... ", end="", flush=True)
    task = None
    try:
        task = start(**kwargs)
        WaitForTask(task)
    except vmodl.MethodFault as e:
        print(f"failed: {fault_message(e)}")
        raise TaskFailed(e) from e
    except KeyboardInterrupt as e:
        print("interrupted")
        raise TaskInterrupted(task) from e
    print("done")
    return task.info.result


def wait_after_interrupt(label: str, task: vim.Task) -> bool:
    """Wait for an interrupted task to end; return whether it succeeded.

    Ctrl-C again stops waiting and raises KeyboardInterrupt.
    """
    print(
        f"{label}: waiting for the task to end (Ctrl-C again to stop waiting) ... ",
        end="",
        flush=True,
    )
    try:
        WaitForTask(task)
    except vmodl.MethodFault as e:
        print(f"failed: {fault_message(e)}")
        return False
    except KeyboardInterrupt:
        print("interrupted")
        raise
    print("done")
    return True


def gib(size: int) -> str:
    return f"{size / 2**30:.1f} GiB"


def estimate_copy_size(
    vm: vim.VirtualMachine, device: vim.vm.device.VirtualDisk
) -> int:
    """Upper bound for a merged copy of the disk: its chain's files combined."""
    chain = set()
    backing: Any = device.backing
    while backing is not None:
        chain.add(backing.fileName)
        backing = getattr(backing, "parent", None)
    layout = vm.layoutEx
    if layout is not None:
        files = {f.key: f for f in layout.file}
        for disk in layout.disk:
            if disk.key != device.key:
                continue
            # The VM's current chain extends past the snapshot; count only the
            # units (descriptor plus extents) that belong to the snapshot's.
            return sum(
                files[key].size
                for unit in disk.chain or []
                if any(files[key].name in chain for key in unit.fileKey)
                for key in unit.fileKey
            )
    # Without layout information, assume the copy is as large as the disk.
    capacity: int = device.capacityInBytes or device.capacityInKB * 1024
    return capacity


def check_free_space(datastore: vim.Datastore, needed: int) -> str | None:
    """Return why the copy must not run, or None if there is enough space."""
    try:
        # The summary's free space is only updated periodically.
        datastore.RefreshDatastore()
    except vmodl.MethodFault as e:
        return f"cannot refresh free space: {fault_message(e)}"
    summary = datastore.summary
    # A full datastore pauses every VM on it that needs to allocate space,
    # including the running VM whose snapshot delta keeps growing.
    margin = int(summary.capacity * FREE_SPACE_MARGIN)
    if summary.freeSpace - needed < margin:
        return (
            f"needs up to {gib(needed)}, {gib(summary.freeSpace)} free, "
            f"{gib(margin)} must stay free"
        )
    return None


# A disk copied from a snapshot: VM name, datacenter and datastore path.
DiskCopy = tuple[str, vim.Datacenter, str]


def copy_snapshot_disks(
    si: vim.ServiceInstance,
    snapshot: vim.vm.Snapshot,
    copy_name: str,
    copies: list[DiskCopy],
) -> list[str]:
    """Copy each of the snapshot's disks on the datastore.

    Copying a delta disk produces a standalone disk with the whole chain
    merged in, so the copy holds the disk exactly as of the snapshot. Unlike
    the snapshot's own files, the copy is not locked by the running VM, and
    it outlives the snapshot. Each copy is appended to ``copies`` before it
    is started, so the caller deletes it even if it fails or is interrupted
    and leaves a partial file behind.

    Returns why each disk that was not copied was skipped.
    """
    vdm = si.RetrieveContent().virtualDiskManager
    assert vdm is not None
    vm = snapshot.vm
    datacenter = find_datacenter(vm)
    skipped: list[str] = []
    for device in snapshot.config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        backing: Any = device.backing
        source: str = backing.fileName
        # A copy of an encrypted disk is encrypted as well, so the data could
        # not be read; don't spend the time and space on it.
        if getattr(backing, "keyId", None) is not None:
            print(f"{vm.name}: not copying {source}: disk is encrypted")
            skipped.append(f"{source}: disk is encrypted")
            continue
        # Next to the source disk, so it stays on the same datastore.
        src_path = parse_datastore_path(source)
        name = f"{copy_name}-copy-{device.key}.vmdk"
        dest = str(src_path.sibling(name))
        ds_name = src_path.datastore
        ds = next((ds for ds in vm.datastore if ds.name == ds_name), None)
        problem: str | None
        if ds is None:
            problem = f"datastore {ds_name} not found"
        else:
            problem = check_free_space(ds, estimate_copy_size(vm, device))
        if problem is not None:
            print(f"{vm.name}: not copying {source}: {problem}")
            skipped.append(f"{source}: {problem}")
            continue
        copies.append((vm.name, datacenter, dest))
        try:
            run_task(
                f"{vm.name}: copying {source} to {dest}",
                vdm.CopyVirtualDisk_Task,
                sourceName=source,
                sourceDatacenter=datacenter,
                destName=dest,
                destDatacenter=datacenter,
                # Without a spec the copy would be fully preallocated.
                destSpec=vim.VirtualDiskManager.VirtualDiskSpec(
                    diskType="thin", adapterType="lsiLogic"
                ),
                force=False,
            )
        except TaskFailed as e:
            skipped.append(f"{source}: copy failed: {e}")
        except TaskInterrupted as e:
            # The copy cannot be deleted while the task still writes it.
            if e.task is not None:
                with suppress(vmodl.MethodFault):
                    e.task.CancelTask()
                with suppress(KeyboardInterrupt):
                    wait_after_interrupt(f"{vm.name}: copy to {dest}", e.task)
            raise
    return skipped


def delete_disk_copies(si: vim.ServiceInstance, copies: list[DiskCopy]) -> None:
    vdm = si.RetrieveContent().virtualDiskManager
    assert vdm is not None
    for vm_name, datacenter, path in copies:
        try:
            run_task(
                f"{vm_name}: deleting {path}",
                vdm.DeleteVirtualDisk_Task,
                name=path,
                datacenter=datacenter,
            )
        except TaskFailed as e:
            # A copy that failed early may never have been created.
            if not isinstance(e.fault, vim.fault.FileNotFound):
                print(f"{vm_name}: delete {path} manually")


def create_snapshot(vm: dict[str, Any], name: str) -> vim.vm.Snapshot | None:
    """Create a snapshot; return None if that failed.

    On Ctrl-C the snapshot may still be created; it is deleted once it is.
    """
    vm_name: str = vm.get("name", "?")
    try:
        snapshot: vim.vm.Snapshot = run_task(
            f"{vm_name}: creating snapshot {name}",
            vm["obj"].CreateSnapshot_Task,
            name=name,
            description="Temporary, taken by vsphere-download to copy the disks",
            memory=False,
            quiesce=False,
        )
    except TaskFailed:
        return None
    except TaskInterrupted as e:
        if e.task is None:
            print(f"{vm_name}: look for snapshot {name} and delete it manually")
            raise
        try:
            if wait_after_interrupt(f"{vm_name}: snapshot {name}", e.task):
                created = cast(vim.vm.Snapshot, e.task.info.result)
                delete_snapshot(vm_name, created, name)
        except KeyboardInterrupt:
            print(f"{vm_name}: delete snapshot {name} manually")
        raise
    return snapshot


def delete_snapshot(vm_name: str, snapshot: vim.vm.Snapshot, name: str) -> None:
    try:
        run_task(
            f"{vm_name}: deleting snapshot {name}",
            snapshot.RemoveSnapshot_Task,
            # Keep any child snapshots; only this one is removed and consolidated.
            removeChildren=False,
        )
    except TaskFailed:
        # The VM keeps running on the snapshot's delta disks until then.
        print(f"{vm_name}: delete snapshot {name} manually")
