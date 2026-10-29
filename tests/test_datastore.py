"""Tests for datastore URL building and HTTP response handling."""

import unittest

from vsphere_download.datastore import (
    DatastoreHttp,
    _ServerError,
    datastore_url,
)
from vsphere_download.errors import ExtractionError, HttpError


class DatastoreUrlTest(unittest.TestCase):
    def test_builds_folder_url(self) -> None:
        self.assertEqual(
            datastore_url("DC", "[FASTRAID1] vm/vm.vmdk"),
            "/folder/vm/vm.vmdk?dcPath=DC&dsName=FASTRAID1",
        )

    def test_quotes_datacenter_and_spaces(self) -> None:
        self.assertEqual(
            datastore_url("Folder/DC", "[ds] My VM/disk.vmdk"),
            "/folder/My%20VM/disk.vmdk?dcPath=Folder%2FDC&dsName=ds",
        )

    def test_rejects_path_without_datastore(self) -> None:
        with self.assertRaises(ExtractionError):
            datastore_url("DC", "vm/vm.vmdk")

    def test_rejects_path_without_bracket(self) -> None:
        with self.assertRaises(ExtractionError):
            datastore_url("DC", "ds] vm/vm.vmdk")


class FakeResponse:
    """The subset of http.client.HTTPResponse that _handle reads."""

    def __init__(
        self,
        status: int,
        reason: str = "",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.reason = reason
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)

    def read(self) -> bytes:
        return self._body


class HandleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.http = DatastoreHttp("host", 443, "sess=abc; Path=/", None)  # type: ignore[arg-type]

    def handle(self, resp: FakeResponse, offset: int, length: int) -> object:
        return self.http._handle(resp, "/folder/x", offset, length)  # type: ignore[arg-type]

    def test_partial_content_returns_data_and_size(self) -> None:
        resp = FakeResponse(
            206, headers={"Content-Range": "bytes 0-3/1000"}, body=b"\x00\x01\x02\x03"
        )
        self.assertEqual(self.handle(resp, 0, 4), (b"\x00\x01\x02\x03", 1000))

    def test_partial_content_short_range_at_end(self) -> None:
        # Fewer bytes than asked for are fine at the end of the file.
        resp = FakeResponse(
            206, headers={"Content-Range": "bytes 998-999/1000"}, body=b"\xaa\xbb"
        )
        self.assertEqual(self.handle(resp, 998, 4), (b"\xaa\xbb", 1000))

    def test_partial_content_truncated_body(self) -> None:
        resp = FakeResponse(
            206, headers={"Content-Range": "bytes 0-3/1000"}, body=b"\x00\x01"
        )
        with self.assertRaises(HttpError):
            self.handle(resp, 0, 4)

    def test_partial_content_wrong_start(self) -> None:
        resp = FakeResponse(
            206, headers={"Content-Range": "bytes 8-11/1000"}, body=b"\x00\x01\x02\x03"
        )
        with self.assertRaises(HttpError):
            self.handle(resp, 0, 4)

    def test_partial_content_missing_content_range(self) -> None:
        resp = FakeResponse(206, body=b"\x00\x01\x02\x03")
        with self.assertRaises(HttpError):
            self.handle(resp, 0, 4)

    def test_ok_whole_small_file(self) -> None:
        # A 200 for the whole file is only accepted when the range covered it.
        resp = FakeResponse(200, headers={"Content-Length": "6"}, body=b"abcdef")
        self.assertEqual(self.handle(resp, 0, 100), (b"abcdef", 6))

    def test_ok_larger_than_range_is_rejected(self) -> None:
        # The server ignored the range and would hand back a whole disk.
        resp = FakeResponse(200, headers={"Content-Length": "6"}, body=b"abcdef")
        with self.assertRaises(HttpError):
            self.handle(resp, 2, 3)

    def test_ok_truncated_body(self) -> None:
        resp = FakeResponse(200, headers={"Content-Length": "10"}, body=b"abcde")
        with self.assertRaises(HttpError):
            self.handle(resp, 0, 100)

    def test_access_denied(self) -> None:
        for status in (401, 403):
            with self.assertRaises(HttpError):
                self.handle(FakeResponse(status), 0, 4)

    def test_not_found(self) -> None:
        with self.assertRaises(HttpError):
            self.handle(FakeResponse(404), 0, 4)

    def test_range_not_satisfiable(self) -> None:
        with self.assertRaises(HttpError):
            self.handle(FakeResponse(416), 0, 4)

    def test_server_error_is_retryable(self) -> None:
        # 500 is the locked-file case: retryable, not a hard HttpError.
        with self.assertRaises(_ServerError):
            self.handle(FakeResponse(500), 0, 4)


if __name__ == "__main__":
    unittest.main()
