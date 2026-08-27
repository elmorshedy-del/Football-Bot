import tempfile
import unittest
from unittest.mock import patch

from app.recorder import RawRecorder


class RawRecorderTests(unittest.TestCase):
    def test_write_failure_is_visible_and_alerted(self):
        alerts = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.recorder.config.DATA_DIR", directory):
            recorder = RawRecorder(alerts.append)
            with patch("app.recorder.gzip.open", side_effect=OSError("disk full")):
                recorder.write({"type": "trade"}, 100.0, 50.0)

        self.assertFalse(recorder.healthy)
        self.assertEqual(recorder.failures, 1)
        self.assertIn("disk full", recorder.last_error)
        self.assertEqual(alerts, [recorder.last_error])

    def test_successful_write_reports_health_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.recorder.config.DATA_DIR", directory):
            recorder = RawRecorder()
            recorder.write({"type": "trade"}, 100.0, 50.0)
            recorder.close()

        self.assertTrue(recorder.status()["healthy"])
        self.assertEqual(recorder.last_write_ts, 100.0)
        self.assertEqual(recorder.total, 1)


if __name__ == "__main__":
    unittest.main()
