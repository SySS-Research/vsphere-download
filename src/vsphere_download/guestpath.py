"""Parsing Windows paths in the guest, like "C:\\Windows\\win.ini"."""

import re
from typing import NamedTuple

# The volume holding the Windows installation is the one found on the disks;
# other drive letters would need the guest's drive letter assignments.
SYSTEM_DRIVE = "C"

DRIVE_RE = re.compile(r"^([A-Za-z]):")


class GuestPath(NamedTuple):
    """A path on the guest's system volume, split into its components."""

    parts: tuple[str, ...]  # e.g. ("Windows", "System32", "drivers", "etc", "hosts")

    def __str__(self) -> str:
        return f"{SYSTEM_DRIVE}:\\" + "\\".join(self.parts)

    @property
    def name(self) -> str:
        """The last component; the copy is named after it."""
        return self.parts[-1]

    @property
    def ntfs_path(self) -> str:
        """The path relative to the volume's root, as dissect looks it up."""
        return "/".join(self.parts)

    def child(self, name: str) -> "GuestPath":
        return GuestPath((*self.parts, name))


def parse_guest_path(path: str) -> GuestPath:
    """Parse a path on the guest's system drive.

    Accepts backslashes or forward slashes, with or without the "C:" drive.
    Raises ValueError for another drive, ".." components, or the root itself.
    """
    rest = path
    match = DRIVE_RE.match(path)
    if match is not None:
        if match.group(1).upper() != SYSTEM_DRIVE:
            raise ValueError(
                f"{path}: only the system drive ({SYSTEM_DRIVE}:) is supported"
            )
        rest = path[match.end() :]
    parts = tuple(p for p in re.split(r"[\\/]", rest) if p not in ("", "."))
    if ".." in parts:
        raise ValueError(f"{path}: '..' is not supported")
    if not parts:
        raise ValueError(f"{path}: name a file or directory, not the whole drive")
    return GuestPath(parts)
