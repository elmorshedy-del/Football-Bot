import unittest
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


if __name__ == "__main__":
    unittest.main()
