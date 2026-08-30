import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.main import require_admin
from app import main


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
        main._export_job = None

    async def asyncTearDown(self):
        for task in tuple(main._export_tasks):
            task.cancel()
        main._export_tasks.clear()
        main._export_job = self.old_job

    async def test_prepare_poll_and_cookie_authorized_download(self):
        recorder = Mock()
        record_error = Mock()
        fake_engine = SimpleNamespace(
            recorder=recorder, mode="live", _record_error=record_error,
        )
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
                cookie = response.headers["set-cookie"]
                self.assertIn("HttpOnly", cookie)
                self.assertIn("Secure", cookie)
                self.assertIn("SameSite=strict", cookie)
                self.assertIn("Path=/api/export/jobs", cookie)
                await asyncio.sleep(0.05)
                job_id = main._export_job["job_id"]
                status = await main.study_export_status(job_id)
                self.assertEqual(status["status"], "ready")
                self.assertEqual(status["bytes"], bundle.stat().st_size)
                download = await main.download_study_export(
                    job_id, x_admin_token=None,
                    footballbot_export_job=main._export_job["download_token"],
                )
                self.assertEqual(os.fspath(download.path), os.fspath(bundle))
                self.assertIn("footballbot_export_job", download.headers["set-cookie"])
        recorder.checkpoint_for_export.assert_called_once_with()
        record_error.assert_not_called()

    async def test_download_fails_closed_without_header_or_job_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "study.zip"
            bundle.write_bytes(b"PK-test-bundle")
            main._export_job = {
                "job_id": "job", "download_token": "secret", "status": "ready",
                "created_at": 1.0, "path": str(bundle), "bytes": bundle.stat().st_size,
                "error": None,
            }
            with patch("app.main.config.ADMIN_TOKEN", "admin"):
                with self.assertRaises(HTTPException) as caught:
                    await main.download_study_export(
                        "job", x_admin_token=None, footballbot_export_job=None,
                    )
        self.assertEqual(caught.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
