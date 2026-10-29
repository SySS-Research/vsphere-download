"""Parsing and building vSphere datastore paths like "[ds] vm/vm.vmdk"."""

import posixpath
from typing import NamedTuple

from vsphere_download.errors import ExtractionError


class DatastorePath(NamedTuple):
    """A datastore path split into its datastore name and relative path."""

    datastore: str  # without the brackets, e.g. "FASTRAID1"
    rel_path: str  # e.g. "vm/vm.vmdk"

    def __str__(self) -> str:
        return f"[{self.datastore}] {self.rel_path}"

    def sibling(self, name: str) -> "DatastorePath":
        """A path to ``name`` in the same directory on the same datastore."""
        directory = posixpath.dirname(self.rel_path)
        return DatastorePath(self.datastore, posixpath.join(directory, name))


def parse_datastore_path(path: str) -> DatastorePath:
    """Split "[ds] rel/path" into its datastore name and relative path.

    Raises ExtractionError if ``path`` is not a datastore path.
    """
    datastore, sep, rel_path = path.partition("] ")
    if not sep or not datastore.startswith("["):
        raise ExtractionError(f"not a datastore path: {path}")
    return DatastorePath(datastore.removeprefix("["), rel_path)
