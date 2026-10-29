"""Errors raised while reading files from a VM's disks."""


class ExtractionError(Exception):
    """Reading a file from a VM's disks failed; the message says why."""


class HttpError(ExtractionError):
    """A datastore file could not be read over HTTP."""
