"""Reading datastore files over HTTP."""

import http.client
import queue
import re
import ssl
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor

from dissect.util.stream import AlignedStream

from vsphere_download.dspath import parse_datastore_path
from vsphere_download.errors import HttpError

# Disk data is fetched in chunks of this size; recently read chunks are kept.
# A large chunk means far fewer round-trips on a big sequential read (a large
# file is read start to end), at the cost of over-reading for the small
# scattered reads of NTFS metadata -- but those cluster within a chunk, so the
# cache turns neighbouring reads into hits.
CHUNK_SIZE = 4 * 1024 * 1024

# The /folder interface answers each range request only after a full round
# trip, so a single connection is latency-bound. Several connections fetch
# chunks ahead in parallel. Against a running DC over a tunnel this was ~2.2x
# faster at 16 connections; 32 was no better and 64 slower (the server starts
# to thrash).
CONNECTIONS = 16


HTTP_TIMEOUT = 60
HTTP_ATTEMPTS = 3


CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class _ServerError(Exception):
    """A server-side error that might go away when retried."""


class DatastoreHttp:
    """Range requests on datastore files through the /folder HTTP interface.

    Requests are authenticated with the vSphere API session's cookie, so no
    second login is needed. One connection is kept open and reused. An
    instance is not thread-safe; a pool holds one per worker thread.
    """

    def __init__(
        self, host: str, port: int, cookie: str, context: ssl.SSLContext
    ) -> None:
        self.host = host
        self.port = port
        # The stub's cookie carries attributes (Path, HttpOnly, ...); only
        # the name=value pair is sent back.
        self.cookie = cookie.split(";", 1)[0].strip()
        self.context = context
        self._conn: http.client.HTTPSConnection | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def fetch(self, url: str, offset: int, length: int) -> tuple[bytes, int]:
        """Return up to ``length`` bytes at ``offset`` and the file's size.

        Fewer bytes are returned only at the end of the file.
        """
        error = ""
        for attempt in range(HTTP_ATTEMPTS):
            if attempt:
                time.sleep(attempt)
            try:
                return self._fetch(url, offset, length)
            except (OSError, http.client.HTTPException) as e:
                # Connection problems; a fresh connection may succeed.
                self.close()
                error = str(e) or type(e).__name__
            except _ServerError as e:
                error = str(e)
        raise HttpError(f"reading {url} failed after {HTTP_ATTEMPTS} attempts: {error}")

    def _fetch(self, url: str, offset: int, length: int) -> tuple[bytes, int]:
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=HTTP_TIMEOUT, context=self.context
            )
        self._conn.request(
            "GET",
            url,
            headers={
                "Cookie": self.cookie,
                "Range": f"bytes={offset}-{offset + length - 1}",
            },
        )
        resp = self._conn.getresponse()
        try:
            return self._handle(resp, url, offset, length)
        finally:
            # A response that was not read completely leaves the connection
            # unusable; never read the rest, it may be a whole disk.
            if resp.will_close or not resp.isclosed():
                self.close()

    def _handle(
        self, resp: http.client.HTTPResponse, url: str, offset: int, length: int
    ) -> tuple[bytes, int]:
        status = f"HTTP {resp.status} {resp.reason}"
        if resp.status >= 500:
            # The datastore cannot open files locked by a running VM.
            raise _ServerError(f"{status} (file locked by a running VM?)")
        if resp.status in (401, 403):
            raise HttpError(f"reading {url}: {status}: access denied")
        if resp.status == 404:
            raise HttpError(f"reading {url}: {status}: file not found")
        if resp.status == 416:
            raise HttpError(f"reading {url}: {status}: offset {offset} past the end")
        content_length = int(resp.getheader("Content-Length") or -1)
        if resp.status == 200:
            # The whole file instead of the range. The server does that when
            # the range covers the whole file, so this is only safe for small
            # files; don't download a disk by accident.
            if not 0 <= content_length <= offset + length:
                raise HttpError(f"reading {url}: server ignored the byte range")
            data = resp.read()
            if len(data) != content_length:
                raise HttpError(f"reading {url}: truncated response")
            return data[offset : offset + length], len(data)
        if resp.status != 206:
            raise HttpError(f"reading {url}: unexpected {status}")
        match = CONTENT_RANGE_RE.fullmatch(resp.getheader("Content-Range") or "")
        if match is None:
            raise HttpError(f"reading {url}: missing or invalid Content-Range")
        start, end, size = map(int, match.groups())
        expected = min(length, size - offset)
        if start != offset or end - start + 1 != expected:
            raise HttpError(
                f"reading {url}: asked for {expected} bytes at {offset}, "
                f"got bytes {start}-{end}"
            )
        data = resp.read()
        if len(data) != expected:
            raise HttpError(f"reading {url}: truncated response")
        return data, size


def datastore_url(datacenter_path: str, path: str) -> str:
    """URL of a file like "[datastore] vm/vm.vmdk" in the /folder interface."""
    ds = parse_datastore_path(path)
    query = urllib.parse.urlencode({"dcPath": datacenter_path, "dsName": ds.datastore})
    return f"/folder/{urllib.parse.quote(ds.rel_path)}?{query}"


class DatastoreReader:
    """Reads datastore files over several /folder connections in parallel.

    A pool of connections (one usable by a thread at a time) is driven by a
    thread pool, so chunks are fetched ahead while the consumer works through
    earlier ones. Progress is reported on a single rewritten line.
    """

    def __init__(
        self,
        host: str,
        port: int,
        cookie: str,
        context: ssl.SSLContext,
    ) -> None:
        self._free: queue.Queue[DatastoreHttp] = queue.Queue()
        self._all = [
            DatastoreHttp(host, port, cookie, context) for _ in range(CONNECTIONS)
        ]
        for conn in self._all:
            self._free.put(conn)
        self._executor = ThreadPoolExecutor(
            max_workers=CONNECTIONS, thread_name_prefix="datastore"
        )
        # Progress, shared across worker threads.
        self._lock = threading.Lock()
        self.progress_label: str | None = None
        self._read_bytes = 0
        self._requests = 0
        self._last_report = 0.0
        # Whether the progress line is on screen without its newline yet.
        self._line_open = False

    def fetch(self, url: str, offset: int, length: int) -> tuple[bytes, int]:
        """A single range request, borrowing one connection from the pool."""
        conn = self._free.get()
        try:
            data, size = conn.fetch(url, offset, length)
        finally:
            self._free.put(conn)
        self._count(len(data))
        return data, size

    def submit(self, url: str, offset: int, length: int) -> "Future[tuple[bytes, int]]":
        return self._executor.submit(self.fetch, url, offset, length)

    def open(self, url: str, size: int) -> "PrefetchRangeStream":
        return PrefetchRangeStream(self, url, size)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        for conn in self._all:
            conn.close()

    # -- progress -----------------------------------------------------------
    # Reads happen deep inside dissect with no output of their own, so a large
    # read (a large file or directory especially) looks like a hang without this.

    def start_progress(self, label: str) -> None:
        with self._lock:
            self.progress_label = label
            self._read_bytes = 0
            self._requests = 0
            self._last_report = 0.0

    def finish_progress(self) -> None:
        with self._lock:
            if self.progress_label is not None and self._requests:
                self._report(end="\n")
            self.progress_label = None

    def message(self, text: str) -> None:
        """Print a line of output without running it into the progress line.

        The progress line is ended first; the next report starts a new one.
        """
        with self._lock:
            if self._line_open:
                sys.stdout.write("\n")
                self._line_open = False
                # Show the progress again on the next read, below this line.
                self._last_report = 0.0
            print(text, flush=True)

    def _count(self, read: int) -> None:
        with self._lock:
            if self.progress_label is None:
                return
            self._read_bytes += read
            self._requests += 1
            # Rewrite the line at most a few times a second, not once per read.
            now = time.monotonic()
            if now - self._last_report >= 0.25:
                self._last_report = now
                self._report()

    def _report(self, *, end: str = "") -> None:
        mib = self._read_bytes / 2**20
        sys.stdout.write(
            f"\r{self.progress_label}: read {mib:.1f} MiB "
            f"in {self._requests} request(s){end}"
        )
        sys.stdout.flush()
        self._line_open = not end


class PrefetchRangeStream(AlignedStream):  # type: ignore[misc]
    """A datastore file read on demand, with chunks prefetched in parallel.

    When a chunk is needed, it and the next few chunks are requested at once;
    the extra requests run on the reader's other connections, so by the time
    the consumer asks for them they are already in flight or done. This turns
    a serial, latency-bound read into a parallel, throughput-bound one.

    Only the consumer thread touches the in-flight and cache bookkeeping; the
    pool's worker threads just run the fetches. The lock guards nothing else,
    so there is no callback racing the consumer for it.
    """

    def __init__(self, reader: DatastoreReader, url: str, size: int) -> None:
        super().__init__(size, align=CHUNK_SIZE)
        self._reader = reader
        self._url = url
        # Look this many chunks ahead; enough to keep every connection busy.
        self._readahead = CONNECTIONS
        # Bound the work in flight and the chunks kept, so neither the prefetch
        # nor the cache grows without limit on a scattered read.
        self._max_inflight = 2 * CONNECTIONS
        self._max_cached = CONNECTIONS + 4
        self._inflight: dict[int, Future[tuple[bytes, int]]] = {}
        self._cache: OrderedDict[int, bytes] = OrderedDict()

    def _submit(self, offset: int, force: bool = False) -> None:
        assert self.size is not None
        if offset >= self.size or offset in self._cache or offset in self._inflight:
            return
        # The needed chunk is always submitted; readahead stops at the limit.
        if not force and len(self._inflight) >= self._max_inflight:
            return
        expected = min(CHUNK_SIZE, self.size - offset)
        self._inflight[offset] = self._reader.submit(self._url, offset, expected)

    def _chunk(self, offset: int) -> bytes:
        assert self.size is not None
        data = self._cache.get(offset)
        if data is not None:
            self._cache.move_to_end(offset)
            return data
        self._submit(offset, force=True)
        for i in range(1, self._readahead + 1):
            self._submit(offset + i * CHUNK_SIZE)
        data, _ = self._inflight[offset].result()  # Raises HttpError on failure.
        expected = min(CHUNK_SIZE, self.size - offset)
        if len(data) != expected:
            raise HttpError(f"reading {self._url}: short read at {offset}")
        del self._inflight[offset]
        self._cache[offset] = data
        while len(self._cache) > self._max_cached:
            self._cache.popitem(last=False)
        return data

    def _read(self, offset: int, length: int) -> bytes:
        assert self.size is not None
        end = min(offset + length, self.size)
        return b"".join(self._chunk(chunk) for chunk in range(offset, end, CHUNK_SIZE))[
            : end - offset
        ]


def open_datastore_file(reader: DatastoreReader, url: str) -> PrefetchRangeStream:
    # Reading the first byte checks access and returns the file's size.
    _, size = reader.fetch(url, 0, 1)
    return reader.open(url, size)
