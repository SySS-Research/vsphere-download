"""Finding the Windows volume on a disk and copying files from it."""

import functools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from dissect.ntfs import NTFS
from dissect.ntfs.exceptions import FileNotFoundError as NtfsFileNotFoundError
from dissect.ntfs.exceptions import NotADirectoryError as NtfsNotADirectoryError
from dissect.volume.disk import Disk
from dissect.volume.exceptions import DiskError

from vsphere_download.errors import ExtractionError
from vsphere_download.guestpath import GuestPath
from vsphere_download.vmdk import SECTOR_SIZE

# OEM IDs in the volume boot record.
NTFS_OEM_ID = b"NTFS    "
BITLOCKER_OEM_ID = b"-FVE-FS-"

WINDOWS_DIR = "Windows/System32"

# Files are copied to disk in blocks of this size rather than read whole into
# memory, so a multi-gigabyte file never has to fit in RAM. It matches the
# datastore chunk so the reads line up with cached chunks.
STREAM_BUFFER = 4 * 2**20


class UnreadableError(Exception):
    """One file or directory in the guest could not be read; others may be.

    Unlike an ExtractionError, which affects the whole disk, this only skips
    the one entry.
    """


@dataclass
class Tally:
    """What copying one source wrote."""

    files: int = 0
    dirs: int = 0
    size: int = 0


@dataclass
class CopyResult:
    """What was copied out of a VM, and what was not and why."""

    copied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def iter_volumes(
    disk: BinaryIO, name: str, notes: list[str]
) -> Iterator[tuple[str, BinaryIO]]:
    """Yield a name and stream for each partition, or the whole disk.

    Partitions that cannot hold a readable volume are skipped; why is added
    to ``notes``, where it explains a missing Windows installation.
    """
    disk.seek(0)
    try:
        partitions = Disk(disk, sector_size=SECTOR_SIZE).partitions
    except DiskError:
        # Not partitioned; the volume may span the whole disk.
        yield "whole disk", disk
        return
    for part in partitions:
        type_name = part.type_name.lower()
        if "dynamic" in type_name or "logical disk manager" in type_name:
            notes.append(f"{name}, partition {part.number}: dynamic disk unsupported")
        elif "storage spaces" in type_name:
            notes.append(f"{name}, partition {part.number}: Storage Spaces unsupported")
        else:
            yield f"partition {part.number}", part.open()


def guest_read[T](guest: GuestPath, what: str, read: Callable[[], T]) -> T:
    """Call ``read``, turning dissect's errors into an UnreadableError.

    dissect raises all kinds of errors on damaged or unsupported structures;
    they only make this one entry unreadable. An ExtractionError (a failed
    datastore read) affects everything else as well, so it is passed on.
    """
    try:
        return read()
    except ExtractionError:
        raise
    except Exception as e:
        raise UnreadableError(f"{guest}: cannot {what}: {e!r}") from e


def is_windows_volume(fs: NTFS) -> bool:
    assert fs.mft is not None
    try:
        return bool(fs.mft.get(WINDOWS_DIR).is_dir())
    except (NtfsFileNotFoundError, NtfsNotADirectoryError):
        return False


def stream_file(record: Any, guest: GuestPath, dest: Path, tally: Tally) -> None:
    """Copy an NTFS file to ``dest`` in blocks, without buffering it in memory.

    ``dest`` must not exist yet; a partly written file is removed again.
    """
    size: int = guest_read(guest, "read the file size", record.size)
    src = guest_read(guest, "open the file", record.open)
    read = functools.partial(src.read, STREAM_BUFFER)
    written = 0
    out = open(dest, "xb")  # noqa: SIM115 -- closed below; never overwrite
    try:
        with out:
            while block := guest_read(guest, "read the file", read):
                out.write(block)
                written += len(block)
        if written != size:
            raise UnreadableError(f"{guest}: truncated ({written} of {size} bytes)")
    except BaseException:
        # Includes Ctrl-C: never leave a file behind that looks complete.
        dest.unlink(missing_ok=True)
        raise
    tally.files += 1
    tally.size += written


def copy_entry(
    record: Any, guest: GuestPath, dest: Path, tally: Tally, notes: list[str]
) -> None:
    """Copy a file, or a directory with everything below it, to ``dest``.

    Entries below a directory that cannot be read are skipped and added to
    ``notes``; links and junctions are not followed, so nothing is copied
    twice and a junction loop cannot recurse forever.
    """
    if not guest_read(guest, "read the MFT record", record.is_dir):
        stream_file(record, guest, dest, tally)
        return
    entries = guest_read(
        guest, "list the directory", lambda: record.listdir(ignore_dos=True)
    )
    dest.mkdir()
    tally.dirs += 1
    for name in sorted(entries, key=str.lower):
        if name in (".", ".."):
            continue
        child_guest = guest.child(name)
        try:
            child = guest_read(
                child_guest, "read the MFT record", entries[name].dereference
            )
            if guest_read(child_guest, "read the MFT record", child.is_reparse_point):
                notes.append(f"{child_guest}: link or junction, not followed")
                continue
            copy_entry(child, child_guest, dest / name, tally, notes)
        except UnreadableError as e:
            notes.append(str(e))


def describe(guest: GuestPath, tally: Tally) -> str:
    if tally.dirs:
        return f"{guest.name}\\ ({tally.files} files, {tally.size} bytes)"
    return f"{guest.name} ({tally.size} bytes)"


def copy_sources(
    fs: NTFS, volume: str, sources: list[GuestPath], directory: Path
) -> CopyResult:
    """Copy each source from the Windows volume into ``directory``.

    Every source is looked up (metadata only) before anything is written, and
    the directory is created only if at least one of them exists, so a VM
    where none is found leaves no output behind.
    """
    assert fs.mft is not None
    result = CopyResult()
    found: list[tuple[GuestPath, Any]] = []
    for guest in sources:
        try:
            look_up = functools.partial(fs.mft.get, guest.ntfs_path)
            record = guest_read(guest, "look up the path", look_up)
            if guest_read(guest, "read the MFT record", record.is_reparse_point):
                result.notes.append(f"{guest}: link or junction, not followed")
                continue
        except UnreadableError as e:
            if isinstance(e.__cause__, NtfsFileNotFoundError | NtfsNotADirectoryError):
                result.notes.append(f"{guest}: not found")
            else:
                result.notes.append(str(e))
            continue
        found.append((guest, record))
    if not found:
        raise ExtractionError(
            f"{volume}: Windows found, but none of the paths could be copied"
            + "".join(f"\n  - {note}" for note in result.notes)
        )

    directory.mkdir()
    for guest, record in found:
        tally = Tally()
        try:
            copy_entry(record, guest, directory / guest.name, tally, result.notes)
        except UnreadableError as e:
            result.notes.append(str(e))
            continue
        result.copied.append(describe(guest, tally))
    return result


def find_files(
    disk: BinaryIO,
    name: str,
    notes: list[str],
    sources: list[GuestPath],
    directory: Path,
    message: Callable[[str], None],
) -> CopyResult | None:
    """Copy ``sources`` from the disk's Windows volume to ``directory``.

    Returns what was copied, or None if the disk holds no Windows volume.
    ``message`` prints a line of output.
    """
    noted = len(notes)
    for volume, fh in iter_volumes(disk, name, notes):
        label = f"{name}, {volume}"
        fh.seek(0)
        oem_id = fh.read(SECTOR_SIZE)[3:11]
        if oem_id == BITLOCKER_OEM_ID:
            notes.append(f"{label}: BitLocker-encrypted")
            continue
        if oem_id != NTFS_OEM_ID:
            continue
        try:
            fs = NTFS(fh)
            is_windows = is_windows_volume(fs)
        except ExtractionError:
            # Includes failed reads; the next volume would fail the same way.
            raise
        except Exception as e:
            # dissect raises all kinds of errors on damaged structures.
            notes.append(f"{label}: NTFS not readable: {e!r}")
            continue
        if is_windows:
            message(f"{name}: Windows found on {volume}")
            return copy_sources(fs, label, sources, directory)
        notes.append(f"{label}: NTFS, but no Windows installation")
    if len(notes) == noted:
        notes.append(f"{name}: no NTFS volume")
    return None
