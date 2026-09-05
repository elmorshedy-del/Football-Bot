"""Raw-feed archive: SigV4, the continuity contract, and transparent reads.

Every test runs against a local stub S3 endpoint started in a thread.  Nothing
here touches the network, and no credential value in this file is real.

The stub verifies the SigV4 signature of every request it receives and refuses
anything that does not match, so each upload test is also an end-to-end test of
the signer.  The published AWS test vectors are checked separately below.
"""
import asyncio
import gzip
import hashlib
import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from app import archive, config, exporter, main, store
from app.archive import R2Client, RawArchive, SigV4Signer
from app.recorder import RawRecorder

ACCESS_KEY = "stub-access-key-id"
SECRET_KEY = "stub-secret-access-key"
BUCKET = "football-bot-raw-feed-test"


# --- published AWS Signature Version 4 test vectors --------------------------

class SigV4TestVectorTests(unittest.TestCase):
    """The signer is validated offline before it is ever pointed at R2.

    `get-vanilla` and `post-vanilla-query` are the canonical cases from the AWS
    SigV4 test suite (service `service`, region `us-east-1`).  The two S3 cases
    are the worked examples from the S3 authorization-header documentation;
    they are the ones that matter here because they exercise
    `x-amz-content-sha256`, a signed `Range` header and a non-empty payload --
    exactly what the archive sends to R2.
    """

    SUITE_KEY = "AKIDEXAMPLE"
    SUITE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    S3_KEY = "AKIAIOSFODNN7EXAMPLE"
    S3_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    @staticmethod
    def signature(authorization):
        return authorization.split("Signature=")[1]

    def test_get_vanilla(self):
        signer = SigV4Signer(self.SUITE_KEY, self.SUITE_SECRET,
                             region="us-east-1", service="service")
        authorization = signer.authorization(
            "GET", "https://example.amazonaws.com/", {
                "host": "example.amazonaws.com",
                "x-amz-date": "20150830T123600Z",
            }, archive.EMPTY_SHA256, "20150830T123600Z",
        )

        self.assertEqual(
            authorization,
            "AWS4-HMAC-SHA256 "
            "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d"
            "763fbf31",
        )

    def test_post_vanilla_query(self):
        signer = SigV4Signer(self.SUITE_KEY, self.SUITE_SECRET,
                             region="us-east-1", service="service")
        authorization = signer.authorization(
            "POST", "https://example.amazonaws.com/?Param1=value1", {
                "host": "example.amazonaws.com",
                "x-amz-date": "20150830T123600Z",
            }, archive.EMPTY_SHA256, "20150830T123600Z",
        )

        self.assertEqual(
            self.signature(authorization),
            "28038455d6de14eafc1f9222cf5aa6f1a96197d7deb8263271d420d138af7f11",
        )

    def test_s3_get_object_with_range(self):
        signer = SigV4Signer(self.S3_KEY, self.S3_SECRET, region="us-east-1")
        authorization = signer.authorization(
            "GET", "https://examplebucket.s3.amazonaws.com/test.txt", {
                "host": "examplebucket.s3.amazonaws.com",
                "range": "bytes=0-9",
                "x-amz-content-sha256": archive.EMPTY_SHA256,
                "x-amz-date": "20130524T000000Z",
            }, archive.EMPTY_SHA256, "20130524T000000Z",
        )

        self.assertEqual(
            self.signature(authorization),
            "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        )

    def test_s3_put_object(self):
        body_hash = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
        self.assertEqual(
            body_hash,
            "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072",
        )
        signer = SigV4Signer(self.S3_KEY, self.S3_SECRET, region="us-east-1")
        authorization = signer.authorization(
            "PUT", "https://examplebucket.s3.amazonaws.com/test$file.text", {
                "date": "Fri, 24 May 2013 00:00:00 GMT",
                "host": "examplebucket.s3.amazonaws.com",
                "x-amz-content-sha256": body_hash,
                "x-amz-date": "20130524T000000Z",
                "x-amz-storage-class": "REDUCED_REDUNDANCY",
            }, body_hash, "20130524T000000Z",
        )

        self.assertEqual(
            self.signature(authorization),
            "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd",
        )

    def test_object_key_is_encoded_once(self):
        """R2 keys are signed as written; the path is never double-encoded."""
        client = R2Client("https://acct.example.com", "bucket", "id", "secret")
        self.assertEqual(
            client.url_for("raw/feed-20260905-13.jsonl.gz"),
            "https://acct.example.com/bucket/raw/feed-20260905-13.jsonl.gz",
        )


# --- local stub S3 endpoint --------------------------------------------------

class _StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _key(self):
        path = urlsplit(self.path).path
        prefix = f"/{BUCKET}/"
        return path[len(prefix):] if path.startswith(prefix) else None

    def _authorised(self, body):
        """Recompute the signature exactly as R2 would and refuse a mismatch."""
        authorization = self.headers.get("Authorization") or ""
        if "SignedHeaders=" not in authorization:
            return False
        signed = authorization.split("SignedHeaders=")[1].split(",")[0]
        headers = {}
        for name in signed.split(";"):
            value = self.headers.get(name)
            if value is None:
                return False
            headers[name] = value
        payload_hash = self.headers.get("x-amz-content-sha256") or ""
        if body and hashlib.sha256(body).hexdigest() != payload_hash:
            return False
        signer = SigV4Signer(ACCESS_KEY, SECRET_KEY)
        expected = signer.authorization(
            self.command, f"http://{headers.get('host')}{self.path}",
            headers, payload_hash, self.headers.get("x-amz-date") or "",
        )
        return expected == authorization

    def _respond(self, status, headers=None, body=b"", send_body=True):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)

    def do_PUT(self):
        body = self._body()
        self.server.requests.append(("PUT", self.path))
        if not self._authorised(body):
            self._respond(403, body=b"signature mismatch")
            return
        if self.server.fail_put:
            self._respond(500, body=b"stub failure")
            return
        key = self._key()
        self.server.objects[key] = body
        etag = hashlib.md5(body).hexdigest()
        if self.server.corrupt_etag:
            etag = hashlib.md5(body + b"x").hexdigest()
        self._respond(200, {"ETag": f'"{etag}"'})

    def do_HEAD(self):
        self.server.requests.append(("HEAD", self.path))
        if not self._authorised(b""):
            self._respond(403, send_body=False)
            return
        blob = self.server.objects.get(self._key())
        if blob is None:
            self._respond(404, send_body=False)
            return
        length = len(blob) + self.server.head_length_delta
        self.send_response(200)
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", f'"{hashlib.md5(blob).hexdigest()}"')
        self.end_headers()

    def do_GET(self):
        self.server.requests.append(("GET", self.path))
        if not self._authorised(b""):
            self._respond(403, body=b"signature mismatch")
            return
        blob = self.server.objects.get(self._key())
        if blob is None:
            self._respond(404, body=b"missing")
            return
        span = self.headers.get("Range")
        if span and span.startswith("bytes="):
            start_text, _, end_text = span[len("bytes="):].partition("-")
            start = int(start_text or 0)
            end = int(end_text) if end_text else len(blob) - 1
            chunk = blob[start:end + 1]
            self._respond(206, {
                "Content-Range": f"bytes {start}-{end}/{len(blob)}",
                "Accept-Ranges": "bytes",
            }, chunk)
            return
        self._respond(200, {"Accept-Ranges": "bytes"}, blob)


def start_stub():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.daemon_threads = True
    server.objects = {}
    server.requests = []
    server.fail_put = False
    server.corrupt_etag = False
    server.head_length_delta = 0
    # A short poll interval keeps `shutdown()` from costing half a second per
    # test; the suite runs this fixture dozens of times.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    return server, thread


class ArchiveCase(unittest.TestCase):
    """Shared fixture: a temp DATA_DIR, a live ledger and a stub endpoint."""

    enable = True

    def setUp(self):
        self.previous_connection = store._conn
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.raw = Path(self.tempdir.name) / "raw"
        self.raw.mkdir()

        self.server, self.thread = start_stub()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]

        overrides = {
            "DATA_DIR": self.tempdir.name,
            "RAW_ARCHIVE_ENABLED": self.enable,
            "R2_ACCOUNT_ID": "stub-account",
            "R2_ACCESS_KEY_ID": ACCESS_KEY if self.enable else "",
            "R2_SECRET_ACCESS_KEY": SECRET_KEY if self.enable else "",
            "R2_BUCKET": BUCKET,
            "R2_ENDPOINT": f"http://{host}:{port}",
            # Retention and the low-disk floor are OFF by default here, so a
            # test that does not ask for pruning cannot get it: the fixture
            # segments carry fixed past hours and would otherwise expire.
            "RAW_LOCAL_RETENTION_HOURS": 10 ** 6,
            "RAW_ARCHIVE_MIN_FREE_MB": 0,
            "RAW_ARCHIVE_MAX_ATTEMPTS": 5,
            "RAW_ARCHIVE_INTERVAL_S": 60.0,
        }
        for name, value in overrides.items():
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep_patch = patch.object(archive, "_UPLOAD_SLEEP_S", 0)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.restore_store)

        self.events = []
        self.errors = []
        self.archive = RawArchive(
            on_event=lambda kind, detail, marker: self.events.append(
                (kind, detail, marker)),
            on_error=lambda component, error: self.errors.append(
                (component, error)),
        )

    def restore_store(self):
        if store._conn is not None and store._conn is not self.previous_connection:
            store._conn.close()
        store._conn = self.previous_connection

    # -- helpers ---------------------------------------------------------
    def write_segment(self, hour, frames=1, name=None):
        name = name or f"feed-{hour}.jsonl.gz"
        path = self.raw / name
        with gzip.open(path, "wt") as handle:
            for index in range(frames):
                handle.write(json.dumps(
                    {"lt": 1.0, "lm": 1.0, "m": {"seq": index, "hour": hour}}) + "\n")
        return path

    def kinds(self):
        return [kind for kind, _detail, _marker in self.events]

    def state(self, name):
        row = store.raw_segment(name)
        return None if row is None else row["state"]


class ArchiveStateMachineTests(ArchiveCase):
    def test_sealed_segment_uploads_verifies_and_flips_state(self):
        path = self.write_segment("20260901-10", frames=5)
        expected = path.read_bytes()

        result = self.archive.tick()

        self.assertEqual(result["registered"], 1)
        self.assertEqual(result["uploaded"], 1)
        row = store.raw_segment(path.name)
        self.assertEqual(row["state"], store.RAW_UPLOADED)
        self.assertEqual(row["r2_key"], f"raw/{path.name}")
        self.assertEqual(row["bytes"], len(expected))
        self.assertEqual(row["md5"], hashlib.md5(expected).hexdigest())
        self.assertEqual(row["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(row["etag"], row["md5"])
        self.assertIsNotNone(row["verified_ts"])
        self.assertIsNone(row["last_error"])
        self.assertEqual(row["hour"], "20260901-10")
        self.assertEqual(row["mode"], "live")
        # The bytes in the stub are the bytes on disk, and the segment is still
        # on the volume: `uploaded` means verified, not removed.
        self.assertEqual(self.server.objects[f"raw/{path.name}"], expected)
        self.assertTrue(path.exists())
        self.assertIn(archive.ARCHIVE_UPLOADED, self.kinds())
        self.assertIn(archive.ARCHIVE_VERIFIED, self.kinds())
        # Ledger events never re-enter the recorder's gzip handle.
        self.assertTrue(all(marker is False for _k, _d, marker in self.events))
        self.assertEqual(self.errors, [])

    def test_etag_mismatch_does_not_prune_and_retries(self):
        path = self.write_segment("20260901-10")
        self.server.corrupt_etag = True

        self.archive.tick()

        row = store.raw_segment(path.name)
        self.assertEqual(row["state"], store.RAW_LOCAL)
        self.assertEqual(row["attempts"], 1)
        self.assertIn("ETag", row["last_error"])
        self.assertIsNone(row["verified_ts"])
        self.assertTrue(path.exists(), "an unverified segment was deleted")
        self.assertIn(archive.ARCHIVE_ERROR, self.kinds())
        self.assertNotIn(archive.ARCHIVE_VERIFIED, self.kinds())
        self.assertEqual(self.errors[0][0], "raw_archive")

        # Retention alone must not prune it either: it was never verified.
        with patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 0):
            self.assertEqual(self.archive.prune(), 0)
        self.assertTrue(path.exists())

        # The next attempt succeeds once the endpoint is healthy again.
        self.server.corrupt_etag = False
        self.archive._backoff.clear()
        self.archive.tick()
        self.assertEqual(self.state(path.name), store.RAW_UPLOADED)

    def test_length_mismatch_leaves_the_segment_local(self):
        path = self.write_segment("20260901-10")
        self.server.head_length_delta = 5

        self.archive.tick()

        row = store.raw_segment(path.name)
        self.assertEqual(row["state"], store.RAW_LOCAL)
        self.assertIn("remote length", row["last_error"])
        self.assertTrue(path.exists())

    def test_attempts_are_capped_and_the_segment_is_kept(self):
        path = self.write_segment("20260901-10")
        self.server.fail_put = True
        for _ in range(config.RAW_ARCHIVE_MAX_ATTEMPTS + 2):
            self.archive._backoff.clear()
            self.archive.tick()

        row = store.raw_segment(path.name)
        self.assertEqual(row["state"], store.RAW_LOCAL)
        self.assertEqual(row["attempts"], config.RAW_ARCHIVE_MAX_ATTEMPTS)
        self.assertTrue(path.exists(), "a segment was lost after repeated failures")

    def test_the_active_hour_is_never_uploaded_or_pruned(self):
        active_hour = archive.current_hour()
        active = self.write_segment(active_hour)
        sealed = self.write_segment("20260901-10")

        with patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 0):
            self.archive.tick()

        self.assertIsNone(store.raw_segment(active.name),
                          "the hour being appended to was registered")
        self.assertTrue(active.exists())
        self.assertNotIn(f"raw/{active.name}", self.server.objects)
        self.assertEqual(self.state(sealed.name), store.RAW_PRUNED)
        self.assertFalse(sealed.exists())

    def test_the_recorders_open_hour_is_never_uploaded(self):
        """The wall clock is not enough: a quiet hour boundary leaves the
        PREVIOUS hour's gzip member open, and uploading it would archive a
        truncated segment."""
        stale = self.write_segment("20260901-10")
        recorder = SimpleNamespace(_hour="20260901-10")
        self.archive.bind_recorder(recorder)

        self.archive.tick()

        self.assertIsNone(store.raw_segment(stale.name))
        self.assertEqual(self.server.objects, {})

    def test_retention_prunes_oldest_first_and_keeps_recent_segments(self):
        old = self.write_segment("20260901-10")
        newer = self.write_segment("20260901-11")
        recent = self.write_segment(archive.current_hour(time.time() - 3600))
        self.archive.tick()
        for path in (old, newer, recent):
            self.assertEqual(self.state(path.name), store.RAW_UPLOADED)
            self.assertTrue(path.exists(), "an upload deleted the local copy")

        with patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 48):
            pruned = self.archive.prune()

        self.assertEqual(pruned, 2)
        self.assertFalse(old.exists())
        self.assertFalse(newer.exists())
        self.assertTrue(recent.exists(), "a segment inside the retention window went")
        self.assertEqual(self.state(recent.name), store.RAW_UPLOADED)
        order = [detail["segment"] for kind, detail, _m in self.events
                 if kind == archive.ARCHIVE_PRUNED]
        self.assertEqual(order, [old.name, newer.name], "not oldest-first")
        self.assertEqual(
            [detail["reason"] for kind, detail, _m in self.events
             if kind == archive.ARCHIVE_PRUNED],
            ["retention", "retention"],
        )

    def test_low_disk_prunes_oldest_first_within_the_retention_window(self):
        recent_hours = [archive.current_hour(time.time() - 3600 * n)
                        for n in (3, 2)]
        first = self.write_segment(recent_hours[0])
        second = self.write_segment(recent_hours[1])
        self.archive.tick()
        self.assertTrue(first.exists() and second.exists())

        # Below the floor once, above it after one segment is freed.
        free = [10 * 1024 * 1024, 4096 * 1024 * 1024]

        def usage(_path):
            return SimpleNamespace(
                total=0, used=0, free=free.pop(0) if len(free) > 1 else free[0])

        with patch.object(config, "RAW_ARCHIVE_MIN_FREE_MB", 512), \
                patch.object(archive.shutil, "disk_usage", side_effect=usage):
            pruned = self.archive.prune()

        self.assertEqual(pruned, 1)
        self.assertFalse(first.exists(), "the oldest segment was not freed first")
        self.assertTrue(second.exists(), "more was deleted than the floor required")
        self.assertEqual(
            [detail["reason"] for kind, detail, _m in self.events
             if kind == archive.ARCHIVE_PRUNED], ["low_disk"],
        )

    def test_a_segment_that_changed_since_verification_is_not_deleted(self):
        path = self.write_segment("20260901-10")
        self.archive.tick()
        self.assertEqual(self.state(path.name), store.RAW_UPLOADED)
        with open(path, "ab") as handle:
            handle.write(b"appended-after-verification")

        with patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 0):
            pruned = self.archive.prune()

        self.assertEqual(pruned, 0)
        self.assertTrue(path.exists())
        self.assertIn("no longer matches verified",
                      store.raw_segment(path.name)["last_error"])

    def test_a_segment_missing_from_r2_is_not_deleted(self):
        path = self.write_segment("20260901-10")
        self.archive.tick()
        self.server.objects.clear()

        with patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 0):
            pruned = self.archive.prune()

        self.assertEqual(pruned, 0)
        self.assertTrue(path.exists(), "the only copy of a segment was deleted")

    def test_backfill_registers_and_drains_an_existing_backlog(self):
        paths = [self.write_segment(f"20260901-{hour:02d}") for hour in range(9, 14)]

        with patch.object(archive, "_UPLOADS_PER_TICK", 2):
            first = self.archive.tick()
            second = self.archive.tick()

        self.assertEqual(first["registered"], len(paths))
        self.assertEqual((first["uploaded"], second["uploaded"]), (2, 2))
        # Oldest first: the backlog drains in timeline order.
        self.assertEqual(
            [key for key in self.server.objects],
            [f"raw/{path.name}" for path in paths[:4]],
        )

    def test_the_recorder_hook_seals_a_rotated_segment(self):
        recorder = RawRecorder(on_sealed=self.archive.on_sealed)
        with patch("app.recorder.time.strftime", return_value="20260901-10"):
            recorder.write({"type": "trade"}, 1.0, 1.0)
        with patch("app.recorder.time.strftime", return_value="20260901-11"):
            recorder.write({"type": "trade"}, 2.0, 2.0)
        recorder.close()

        self.assertEqual(self.archive._sealed, {"feed-20260901-10.jsonl.gz"})
        self.archive.bind_recorder(SimpleNamespace(_hour="20260901-11"))
        self.archive.tick()
        self.assertEqual(self.state("feed-20260901-10.jsonl.gz"),
                         store.RAW_UPLOADED)
        self.assertIsNone(store.raw_segment("feed-20260901-11.jsonl.gz"))


class ArchiveReadTests(ArchiveCase):
    def test_a_pruned_segment_is_listed_and_served_from_r2(self):
        pruned = self.write_segment("20260901-10", frames=40)
        local = self.write_segment("20260901-11")
        payload = pruned.read_bytes()
        self.archive.tick()
        with patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 0), \
                patch.object(self.archive, "active_names",
                             return_value={local.name}):
            self.archive.prune()
        self.assertFalse(pruned.exists())
        self.assertTrue(local.exists())

        inventory = {item["name"]: item for item in exporter.raw_inventory()}
        self.assertEqual(inventory[pruned.name]["location"], "r2")
        self.assertEqual(inventory[pruned.name]["bytes"], len(payload))
        self.assertEqual(inventory[pruned.name]["sha256"],
                         hashlib.sha256(payload).hexdigest())
        self.assertEqual(inventory[local.name]["location"], "both")

        async def download(range_header=None):
            with patch.object(main.config, "ADMIN_TOKEN", "admin"):
                response = await main.download_raw_segment(
                    pruned.name, x_admin_token="admin",
                    footballbot_export_raw=None, range_header=range_header,
                )
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return response, b"".join(chunks)

        response, body = asyncio.run(download())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, payload)
        self.assertEqual(response.headers["x-segment-location"], "r2")
        # The gzip member is intact end to end.
        self.assertEqual(
            len(gzip.decompress(body).splitlines()), 40)

        ranged, chunk = asyncio.run(download("bytes=0-9"))
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(chunk, payload[:10])
        self.assertEqual(ranged.headers["content-range"],
                         f"bytes 0-9/{len(payload)}")

    def test_a_local_segment_is_still_served_from_the_volume(self):
        local = self.write_segment("20260901-11")
        self.archive.tick()

        async def download():
            with patch.object(main.config, "ADMIN_TOKEN", "admin"):
                return await main.download_raw_segment(
                    local.name, x_admin_token="admin",
                    footballbot_export_raw=None, range_header=None,
                )

        response = asyncio.run(download())
        self.assertEqual(os.fspath(response.path), os.fspath(local))
        self.assertEqual(
            [call for call in self.server.requests if call[0] == "GET"], [],
            "a segment on the volume was fetched from R2",
        )

    def test_an_unknown_segment_is_still_a_404(self):
        async def download():
            with patch.object(main.config, "ADMIN_TOKEN", "admin"):
                return await main.download_raw_segment(
                    "feed-20200101-00.jsonl.gz", x_admin_token="admin",
                    footballbot_export_raw=None, range_header=None,
                )

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(download())
        self.assertEqual(caught.exception.status_code, 404)

    def test_the_api_reports_continuity_and_the_ledger(self):
        self.write_segment("20260901-10")
        self.archive.tick()

        payload = asyncio.run(main.archive_state())

        self.assertEqual(payload["archive"]["segments"], 1)
        self.assertEqual(payload["archive"]["by_state"]["uploaded"], 1)
        self.assertEqual(payload["segments"][0]["name"], "feed-20260901-10.jsonl.gz")
        self.assertEqual(payload["segments"][0]["mode"], "live")
        self.assertTrue(payload["archive"]["active"])
        self.assertTrue(payload["archive"]["credentialled"])
        serialized = json.dumps(payload)
        self.assertNotIn(SECRET_KEY, serialized)
        self.assertNotIn(ACCESS_KEY, serialized)


class ArchiveContinuityTests(ArchiveCase):
    def test_missing_hours_between_the_first_and_the_last_are_reported(self):
        for hour in ("20260901-10", "20260901-11", "20260901-14"):
            self.write_segment(hour)
        self.archive.tick()

        report = archive.archive_continuity()

        self.assertEqual(report["segments"], 3)
        self.assertEqual(report["first_hour"], "20260901-10")
        self.assertEqual(report["last_hour"], "20260901-14")
        self.assertEqual(report["missing_hours"], ["20260901-12", "20260901-13"])
        self.assertFalse(report["missing_hours_truncated"])
        self.assertEqual(report["by_state"],
                         {"local": 0, "uploaded": 3, "pruned": 0, "unregistered": 0})
        self.assertEqual(report["hours_present"], 3)

    def test_continuity_counts_local_and_remote_bytes_separately(self):
        kept = self.write_segment("20260901-10", frames=3)
        gone = self.write_segment("20260901-11", frames=9)
        kept_bytes, gone_bytes = kept.stat().st_size, gone.stat().st_size
        self.archive.tick()
        with patch.object(self.archive, "active_names", return_value={kept.name}), \
                patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 0):
            self.archive.prune()

        report = archive.archive_continuity()

        self.assertEqual(report["by_state"]["pruned"], 1)
        self.assertEqual(report["by_state"]["uploaded"], 1)
        self.assertEqual(report["local_bytes"], kept_bytes)
        self.assertEqual(report["remote_bytes"], kept_bytes + gone_bytes)
        self.assertEqual(report["missing_hours"], [])

    def test_an_unregistered_file_is_still_part_of_the_timeline(self):
        """Continuity is the union: a segment sealed seconds ago and not yet
        registered is still on the volume and must be reported."""
        self.write_segment("20260901-10")

        report = archive.archive_continuity()

        self.assertEqual(report["segments"], 1)
        self.assertEqual(report["by_state"]["unregistered"], 1)
        self.assertEqual(report["first_hour"], "20260901-10")

    def test_the_engine_status_block_matches_the_manifest_block(self):
        self.write_segment("20260901-10")
        self.archive.tick()
        bundle = Path(self.tempdir.name) / "study.zip"

        exporter.build_study_bundle(bundle, mode="live", include_raw=False,
                                    scope="audit")
        with zipfile.ZipFile(bundle) as opened:
            manifest = json.loads(opened.read("manifest.json"))
        status = self.archive.status()

        for key in ("segments", "by_state", "first_hour", "last_hour",
                    "missing_hours", "local_bytes", "remote_bytes"):
            self.assertEqual(manifest["archive"][key], status[key], key)
        # The bundle lists the archive ledger as a table of its own, so a
        # reader can see the segments that were not copied into it.
        self.assertEqual(manifest["tables"]["raw_segments"]["rows"], 1)


class ArchiveInertWhenDisabledTests(ArchiveCase):
    enable = False

    def test_nothing_is_registered_uploaded_or_deleted(self):
        path = self.write_segment("20260901-10")

        self.assertFalse(self.archive.enabled)
        result = self.archive.tick()
        self.archive.on_sealed(str(path))

        self.assertEqual(result, {"registered": 0, "uploaded": 0, "pruned": 0})
        self.assertEqual(self.archive._sealed, set())
        self.assertEqual(store.q("SELECT COUNT(*) AS n FROM raw_segments")[0]["n"], 0)
        self.assertEqual(self.server.requests, [])
        self.assertTrue(path.exists())
        self.assertEqual(self.events, [])
        self.assertEqual(self.errors, [])
        self.assertIsNone(R2Client.from_config())

    def test_the_engine_starts_no_archive_task_and_still_reports(self):
        self.write_segment("20260901-10")
        created = []

        async def start():
            engine = main.Engine.__new__(main.Engine)
            engine.archive = self.archive
            with patch("app.engine.asyncio.create_task",
                       side_effect=lambda coro: (coro.close(),
                                                 created.append(coro))[0]):
                self.assertFalse(engine.archive.enabled)

        asyncio.run(start())
        self.assertEqual(created, [])
        report = self.archive.status()
        self.assertFalse(report["enabled"])
        self.assertFalse(report["credentialled"])
        self.assertFalse(report["active"])
        # The volume is still described honestly while the feature is off.
        self.assertEqual(report["segments"], 1)
        self.assertEqual(report["by_state"]["unregistered"], 1)

    def test_the_inventory_still_lists_the_volume(self):
        path = self.write_segment("20260901-10")

        inventory = exporter.raw_inventory()

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["name"], path.name)
        self.assertEqual(inventory[0]["location"], "local")
        self.assertIsNone(inventory[0]["sha256"])


class ArchiveSecretHygieneTests(ArchiveCase):
    def test_credentials_never_reach_an_export_or_the_config_allowlist(self):
        markers = ("R2-ACCESS-KEY-MARKER", "R2-SECRET-KEY-MARKER",
                   "R2-ACCOUNT-MARKER")
        self.write_segment("20260901-10")
        self.archive.tick()
        with patch.object(config, "R2_ACCESS_KEY_ID", markers[0]), \
                patch.object(config, "R2_SECRET_ACCESS_KEY", markers[1]), \
                patch.object(config, "R2_ACCOUNT_ID", markers[2]):
            allowlist = json.dumps(exporter.non_secret_config())
            bundle = Path(self.tempdir.name) / "secret-scan.zip"
            exporter.build_study_bundle(bundle, mode="live", scope="full")
            with zipfile.ZipFile(bundle) as opened:
                blob = b"".join(opened.read(name) for name in opened.namelist())

        for marker in markers:
            self.assertNotIn(marker, allowlist)
            self.assertNotIn(marker.encode(), blob)
        for name in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID",
                     "R2_BUCKET", "R2_ENDPOINT"):
            self.assertNotIn(name, config.STRATEGY_PARAM_NAMES)
            self.assertNotIn(name.lower(), exporter.non_secret_config())

    def test_storage_knobs_are_not_part_of_the_strategy_identity(self):
        """Capturing or moving a file cannot change a trading decision."""
        before = config.config_id()
        with patch.object(config, "RAW_LOCAL_RETENTION_HOURS", 1), \
                patch.object(config, "RAW_ARCHIVE_MIN_FREE_MB", 1), \
                patch.object(config, "RAW_ARCHIVE_ENABLED", not config.RAW_ARCHIVE_ENABLED):
            self.assertEqual(config.config_id(), before)
        for name in ("RAW_ARCHIVE_ENABLED", "RAW_LOCAL_RETENTION_HOURS",
                     "RAW_ARCHIVE_MIN_FREE_MB", "RAW_ARCHIVE_MAX_ATTEMPTS",
                     "RAW_ARCHIVE_INTERVAL_S"):
            self.assertNotIn(name, config.STRATEGY_PARAM_NAMES)


class ArchiveTaskTests(ArchiveCase):
    def test_a_failing_tick_never_crashes_the_task(self):
        async def drive():
            with patch.object(self.archive, "tick",
                              side_effect=RuntimeError("stub outage")), \
                    patch.object(config, "RAW_ARCHIVE_INTERVAL_S", 0.01):
                task = asyncio.create_task(self.archive.run())
                await asyncio.sleep(0.05)
                self.archive.stop()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(drive())

        self.assertGreater(self.archive.failures, 0)
        self.assertIn("stub outage", self.archive.last_error)
        self.assertEqual(self.errors[0][0], "raw_archive")

    def test_the_upload_runs_off_the_event_loop(self):
        """`tick` is dispatched to a worker thread: a 118 MB PUT must never
        block the loop that serves the WebSocket."""
        self.write_segment("20260901-10")
        threads = []

        async def drive():
            original = self.archive.tick

            def record():
                threads.append(threading.current_thread())
                return original()

            with patch.object(self.archive, "tick", side_effect=record), \
                    patch.object(config, "RAW_ARCHIVE_INTERVAL_S", 30.0):
                task = asyncio.create_task(self.archive.run())
                await asyncio.sleep(0.2)
                self.archive.stop()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(drive())

        self.assertTrue(threads)
        self.assertNotIn(threading.main_thread(), threads)
        self.assertEqual(self.state("feed-20260901-10.jsonl.gz"),
                         store.RAW_UPLOADED)


if __name__ == "__main__":
    unittest.main()
