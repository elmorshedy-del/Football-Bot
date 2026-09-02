import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.main import require_admin
from app import exporter, main


class AdminControlTests(unittest.TestCase):
    def test_controls_fail_closed_when_token_is_missing(self):
        with patch("app.main.config.ADMIN_TOKEN", ""):
            with self.assertRaises(HTTPException) as caught:
                require_admin(None)

        self.assertEqual(caught.exception.status_code, 503)

    def test_controls_reject_wrong_token(self):
        with patch("app.main.config.ADMIN_TOKEN", "correct"):
            with self.assertRaises(HTTPException) as caught:
                require_admin("wrong")

        self.assertEqual(caught.exception.status_code, 401)

    def test_controls_accept_matching_token(self):
        with patch("app.main.config.ADMIN_TOKEN", "correct"):
            self.assertIsNone(require_admin("correct"))


class ExportFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_failure_is_visible_and_non_fatal(self):
        # The legacy endpoint now shares the single-full-job rule with
        # /api/export/prepare, so an active full job left by another test would
        # short-circuit this one before preparation is ever attempted.
        saved_jobs = dict(main._export_jobs)

        def _restore_jobs():
            main._export_jobs.clear()
            main._export_jobs.update(saved_jobs)

        self.addCleanup(_restore_jobs)
        main._export_jobs.clear()
        self.enterContext(patch.object(main, "_export_job", None))

        recorder = Mock()
        record_error = Mock()
        fake_engine = SimpleNamespace(
            recorder=recorder, mode="live", _record_error=record_error,
        )
        with patch.object(main, "engine", fake_engine), \
                patch.object(main.exporter, "raw_feed_paths", return_value=[]), \
                patch.object(
                    main.exporter, "prepare_database_snapshot",
                    side_effect=OSError("disk unavailable"),
                ):
            with self.assertRaises(HTTPException) as caught:
                await main.export_study_data()

        self.assertEqual(caught.exception.status_code, 500)
        self.assertIn("System status", caught.exception.detail)
        recorder.checkpoint_for_export.assert_called_once_with()
        record_error.assert_called_once()
        self.assertEqual(record_error.call_args.args[0], "study_export")


class AsyncExportJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_job = main._export_job
        self.old_jobs = dict(main._export_jobs)
        main._export_job = None
        main._export_jobs = {}

    async def asyncTearDown(self):
        for task in tuple(main._export_tasks):
            task.cancel()
        main._export_tasks.clear()
        main._export_job = self.old_job
        main._export_jobs = self.old_jobs

    def _engine(self):
        return SimpleNamespace(
            recorder=Mock(), mode="live", _record_error=Mock(),
            status=lambda: {"ok": True, "mode": "live"},
        )

    async def _await_flag(self, flag, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if flag.is_set():
                return True
            await asyncio.sleep(0.01)
        return flag.is_set()

    async def test_prepare_poll_and_cookie_authorized_download(self):
        fake_engine = self._engine()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.db"
            snapshot.write_bytes(b"snapshot")
            bundle = Path(directory) / "study.zip"
            bundle.write_bytes(b"PK-test-bundle")
            with patch.object(main, "engine", fake_engine), \
                    patch.object(main.exporter, "raw_feed_paths", return_value=[]), \
                    patch.object(
                        main.exporter, "prepare_database_snapshot", return_value=str(snapshot),
                    ), patch.object(
                        main.exporter, "build_study_bundle",
                        return_value=(str(bundle), {"schema": "test"}),
                    ):
                response = await main.prepare_study_export()
                payload = bytes(response.body)
                self.assertNotIn(b"download_token", payload)
                self.assertIn(b'"scope":"audit"', payload)
                cookie = response.headers["set-cookie"]
                self.assertIn("HttpOnly", cookie)
                self.assertIn("Secure", cookie)
                self.assertIn("SameSite=strict", cookie)
                self.assertIn("Path=/api/export/jobs", cookie)
                await asyncio.sleep(0.05)
                job_id = main._export_job["job_id"]
                status = await main.study_export_status(job_id)
                self.assertEqual(status["status"], "ready")
                self.assertEqual(status["scope"], "audit")
                self.assertEqual(status["bytes"], bundle.stat().st_size)
                download = await main.download_study_export(
                    job_id, x_admin_token=None,
                    footballbot_export_job=main._export_job["download_token"],
                )
                self.assertEqual(os.fspath(download.path), os.fspath(bundle))
                self.assertIn("footballbot_export_job", download.headers["set-cookie"])
                self.assertEqual(main._export_job["leases"], 1)
        fake_engine.recorder.checkpoint_for_export.assert_called_once_with()
        fake_engine._record_error.assert_not_called()

    async def test_download_fails_closed_without_header_or_job_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "study.zip"
            bundle.write_bytes(b"PK-test-bundle")
            main._export_job = {
                "job_id": "job", "download_token": "secret", "status": "ready",
                "created_at": 1.0, "path": str(bundle), "bytes": bundle.stat().st_size,
                "error": None, "scope": "audit",
            }
            with patch("app.main.config.ADMIN_TOKEN", "admin"):
                with self.assertRaises(HTTPException) as caught:
                    await main.download_study_export(
                        "job", x_admin_token=None, footballbot_export_job=None,
                    )
        self.assertEqual(caught.exception.status_code, 401)

    async def test_wrong_cookie_and_missing_job_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "study.zip"
            bundle.write_bytes(b"PK-test-bundle")
            job = {
                "job_id": "job", "download_token": "secret", "status": "ready",
                "created_at": 1.0, "path": str(bundle), "bytes": 4,
                "error": None, "scope": "audit", "leases": 0,
            }
            main._export_job = job
            main._export_jobs["job"] = job
            with patch("app.main.config.ADMIN_TOKEN", "admin"):
                with self.assertRaises(HTTPException) as caught:
                    await main.download_study_export(
                        "job", x_admin_token=None, footballbot_export_job="other",
                    )
                self.assertEqual(caught.exception.status_code, 401)
                with self.assertRaises(HTTPException) as missing:
                    await main.study_export_status("missing")
                self.assertEqual(missing.exception.status_code, 404)

    async def test_concurrent_audit_while_full_prepares(self):
        fake_engine = self._engine()
        started = threading.Event()
        release = threading.Event()

        def slow_bundle(*args, **kwargs):
            scope = kwargs.get("scope")
            if scope is None and len(args) >= 8:
                scope = args[7]
            include_raw = kwargs.get("include_raw")
            if include_raw is None and len(args) >= 5:
                include_raw = args[4]
            if scope == "full" or include_raw:
                started.set()
                release.wait(timeout=2)
            output = args[0] if args else None
            path = output or str(Path(tempfile.gettempdir()) / f"{scope or 'x'}.zip")
            Path(path).write_bytes(b"PK")
            return path, {"schema": "test", "scope": scope}

        with tempfile.TemporaryDirectory() as directory:
            def make_snapshot():
                path = Path(directory) / f"snap-{time.time_ns()}.db"
                path.write_bytes(b"db")
                return str(path)

            with patch.object(main, "engine", fake_engine), \
                    patch.object(main.exporter, "raw_feed_paths", return_value=[]), \
                    patch.object(
                        main.exporter, "prepare_database_snapshot",
                        side_effect=make_snapshot,
                    ), patch.object(
                        main.exporter, "build_study_bundle", side_effect=slow_bundle,
                    ):
                full = await main.prepare_study_export(scope="full")
                self.assertTrue(await self._await_flag(started))
                second_full = await main.prepare_study_export(scope="full")
                full_id = __import__("json").loads(full.body)["job_id"]
                self.assertEqual(
                    __import__("json").loads(second_full.body)["job_id"], full_id,
                )
                audit = await main.prepare_study_export(scope="audit")
                audit_job = main._lookup_job(
                    __import__("json").loads(audit.body)["job_id"]
                )
                for _ in range(40):
                    if audit_job["status"] == "ready":
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(audit_job["status"], "ready")
                full_id = __import__("json").loads(full.body)["job_id"]
                self.assertEqual(main._lookup_job(full_id)["status"], "preparing")
                release.set()
                for _ in range(40):
                    if main._lookup_job(full_id)["status"] == "ready":
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(main._lookup_job(full_id)["status"], "ready")

    async def test_cancel_full_job_and_lease_blocks_delete(self):
        fake_engine = self._engine()
        started = threading.Event()
        release = threading.Event()

        def slow_bundle(*args, **kwargs):
            check = kwargs.get("cancel_check")
            if check is None and len(args) >= 7:
                check = args[6]
            started.set()
            for _ in range(200):
                if check and check():
                    raise exporter.ExportCancelled()
                if release.wait(timeout=0.01):
                    break
            output = Path(tempfile.gettempdir()) / "cancelled-export.zip"
            output.write_bytes(b"PK")
            return str(output), {"schema": "test"}

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(main, "engine", fake_engine), \
                    patch.object(main.exporter, "raw_feed_paths", return_value=[]), \
                    patch.object(
                        main.exporter, "prepare_database_snapshot",
                        side_effect=lambda: (
                            Path(directory) / f"snap-{time.time_ns()}.db"
                        ).write_bytes(b"db") or str(
                            next(reversed(sorted(Path(directory).glob("snap-*.db"))))
                        ),
                    ), patch.object(
                        main.exporter, "build_study_bundle", side_effect=slow_bundle,
                    ):
                response = await main.prepare_study_export(scope="full")
                job_id = __import__("json").loads(response.body)["job_id"]
                self.assertTrue(await self._await_flag(started))
                cancelled = await main.cancel_study_export(job_id)
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertEqual(cancelled["error_code"], "CANCELLED")
                release.set()
                await asyncio.sleep(0.05)
                with patch("app.main.config.ADMIN_TOKEN", "admin"):
                    with self.assertRaises(HTTPException) as caught:
                        await main.download_study_export(
                            job_id, x_admin_token="admin", footballbot_export_job=None,
                        )
                    self.assertEqual(caught.exception.status_code, 410)

        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "leased.zip"
            bundle.write_bytes(b"PK-lease")
            job = {
                "job_id": "leased", "download_token": "secret", "status": "ready",
                "created_at": 1.0, "ready_at": 1.0, "path": str(bundle),
                "bytes": 8, "error": None, "scope": "full", "leases": 1,
            }
            main._export_job = job
            main._export_jobs = {"leased": job}
            await main.cancel_study_export("leased")
            self.assertTrue(bundle.exists())
            self.assertEqual(job["leases"], 1)
            job["status"] = "expired"
            main._release_export_lease("leased")
            self.assertFalse(bundle.exists())

    async def test_status_stays_responsive_during_full_prepare(self):
        fake_engine = self._engine()
        started = threading.Event()
        release = threading.Event()

        def blocking(*args, **kwargs):
            started.set()
            release.wait(timeout=2)
            path = Path(tempfile.gettempdir()) / "blocked.zip"
            path.write_bytes(b"PK")
            return str(path), {"schema": "test"}

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(main, "engine", fake_engine), \
                    patch.object(main.exporter, "raw_feed_paths", return_value=[]), \
                    patch.object(
                        main.exporter, "prepare_database_snapshot",
                        side_effect=lambda: (
                            (Path(directory) / "snap.db").write_bytes(b"db")
                            or str(Path(directory) / "snap.db")
                        ),
                    ), patch.object(
                        main.exporter, "build_study_bundle", side_effect=blocking,
                    ):
                await main.prepare_study_export(scope="full")
                self.assertTrue(await self._await_flag(started))
                began = time.perf_counter()
                payload = await main.status()
                elapsed = time.perf_counter() - began
                self.assertLess(elapsed, 0.25)
                self.assertTrue(payload["ok"])
                release.set()

    async def test_prepare_does_not_stall_the_loop_with_a_slow_snapshot(self):
        """B3: the previous test mocked the snapshot as instantaneous.

        Recorder rotation, path enumeration and the SQLite backup all run before
        the 202 is returned.  With a realistic 350 ms snapshot the event loop
        recorded zero heartbeats instead of roughly 35, so live collection
        paused for the whole preparation.
        """
        fake_engine = self._engine()
        beats = 0
        stop = asyncio.Event()

        async def heartbeat():
            nonlocal beats
            while not stop.is_set():
                await asyncio.sleep(0.01)
                beats += 1

        def slow_snapshot():
            time.sleep(0.35)          # blocking, as a real sqlite backup is
            path = Path(tempfile.gettempdir()) / "slow-snap.db"
            path.write_bytes(b"db")
            return str(path)

        def slow_rotate(*args, **kwargs):
            time.sleep(0.05)

        fake_engine.recorder.checkpoint_for_export = slow_rotate
        ticker = asyncio.create_task(heartbeat())
        try:
            with patch.object(main, "engine", fake_engine), \
                    patch.object(main.exporter, "raw_feed_paths", return_value=[]), \
                    patch.object(
                        main.exporter, "prepare_database_snapshot",
                        side_effect=slow_snapshot,
                    ), patch.object(
                        main.exporter, "build_study_bundle",
                        side_effect=lambda *a, **k: (str(
                            Path(tempfile.gettempdir()) / "b.zip"
                        ), {}),
                    ):
                (Path(tempfile.gettempdir()) / "b.zip").write_bytes(b"PK")
                await main.prepare_study_export(scope="full")
        finally:
            stop.set()
            await ticker
        # ~400 ms of blocking work at a 10 ms tick should yield tens of beats.
        self.assertGreater(
            beats, 20,
            f"event loop stalled during export preparation ({beats} heartbeats)",
        )

    async def test_raw_segment_range_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            raw_dir.mkdir()
            name = "feed-20260830-20.jsonl.gz"
            path = raw_dir / name
            path.write_bytes(b"abcdefghij")
            with patch("app.exporter.config.DATA_DIR", directory), \
                    patch("app.main.config.ADMIN_TOKEN", "admin"):
                listing = await main.list_raw_segments()
                payload = __import__("json").loads(listing.body)
                self.assertEqual(payload["segments"][0]["name"], name)
                self.assertIn("HttpOnly", listing.headers["set-cookie"])
                response = await main.download_raw_segment(
                    name, x_admin_token="admin", footballbot_export_raw=None,
                    range_header="bytes=2-5",
                )
                self.assertEqual(response.status_code, 206)
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                self.assertEqual(b"".join(chunks), b"cdef")
                self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
                with self.assertRaises(HTTPException) as missing:
                    await main.download_raw_segment(
                        "../etc/passwd", x_admin_token="admin",
                        footballbot_export_raw=None, range_header=None,
                    )
                self.assertEqual(missing.exception.status_code, 404)
                with self.assertRaises(HTTPException) as auth:
                    await main.download_raw_segment(
                        name, x_admin_token=None, footballbot_export_raw=None,
                        range_header=None,
                    )
                self.assertEqual(auth.exception.status_code, 401)
                cookie_download = await main.download_raw_segment(
                    name, x_admin_token=None,
                    footballbot_export_raw=main._raw_download_token,
                    range_header=None,
                )
                self.assertEqual(os.fspath(cookie_download.path), os.fspath(path))

    async def test_ready_file_lease_survives_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "keep.zip"
            bundle.write_bytes(b"PK-keep")
            job = {
                "job_id": "keep", "download_token": "secret", "status": "ready",
                "created_at": 1.0, "ready_at": 1.0, "path": str(bundle),
                "bytes": 7, "error": None, "scope": "audit", "leases": 1,
            }
            main._export_job = job
            main._export_jobs = {"keep": job}
            main._expire_export_jobs(now=1.0 + main.EXPORT_JOB_TTL_S + 10)
            self.assertEqual(job["status"], "expired")
            self.assertTrue(bundle.exists())
            main._release_export_lease("keep")
            self.assertFalse(bundle.exists())


if __name__ == "__main__":
    unittest.main()
