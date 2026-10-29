"""Opening VMDK disks on a datastore as one stream."""

import re
from typing import BinaryIO, cast

from dissect.util.stream import MappingStream

from vsphere_download.datastore import (
    CHUNK_SIZE,
    DatastoreReader,
    datastore_url,
    open_datastore_file,
)
from vsphere_download.dspath import parse_datastore_path
from vsphere_download.errors import ExtractionError

SECTOR_SIZE = 512


# Descriptors are small text files; anything larger is not a descriptor.
MAX_DESCRIPTOR_SIZE = 64 * 1024


# Extent lines of a VMDK descriptor, e.g. RW 293601280 VMFS "vm-flat.vmdk".
EXTENT_RE = re.compile(
    r'^(RW|RDONLY|NOACCESS)\s+(\d+)\s+(\w+)\s+"([^"]+)"(?:\s+(\d+))?\s*$', re.M
)
# Disks encrypted with vSphere VM encryption carry their key in the descriptor.
ENCRYPTION_RE = re.compile(r"^\s*encryption\s*=", re.M)


def open_vmdk(reader: DatastoreReader, datacenter_path: str, path: str) -> BinaryIO:
    """Open a flat VMDK, given its descriptor, as one stream of the disk."""
    data, size = reader.fetch(
        datastore_url(datacenter_path, path), 0, MAX_DESCRIPTOR_SIZE
    )
    if size > MAX_DESCRIPTOR_SIZE or b"# Disk DescriptorFile" not in data:
        raise ExtractionError(f"{path}: not a VMDK descriptor")
    descriptor = data.decode("utf-8", errors="replace")
    if ENCRYPTION_RE.search(descriptor):
        raise ExtractionError(f"{path}: disk is encrypted")
    extents = EXTENT_RE.findall(descriptor)
    if not extents:
        raise ExtractionError(f"{path}: descriptor lists no extents")

    source = parse_datastore_path(path)
    disk = MappingStream(align=CHUNK_SIZE)
    disk_offset = 0
    for access, sectors, kind, filename, start in extents:
        # A copy of a delta disk is a standalone disk with the chain merged,
        # so its extents are plain flat files whose offsets match the disk's.
        if access == "NOACCESS" or kind not in ("VMFS", "FLAT"):
            raise ExtractionError(
                f"{path}: unsupported {access} {kind} extent {filename}"
            )
        if filename.startswith("/"):
            raise ExtractionError(f"{path}: unsupported absolute extent {filename}")
        extent_path = str(source.sibling(filename))
        fh = open_datastore_file(reader, datastore_url(datacenter_path, extent_path))
        length = int(sectors) * SECTOR_SIZE
        file_offset = int(start or 0) * SECTOR_SIZE
        assert fh.size is not None
        if fh.size < file_offset + length:
            raise ExtractionError(
                f"{extent_path}: {fh.size} bytes, extent needs {file_offset + length}"
            )
        disk.add(disk_offset, length, fh, file_offset)
        disk_offset += length
    return cast(BinaryIO, disk)
