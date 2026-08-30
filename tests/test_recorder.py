import gzip
import json
import os
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

    def test_export_checkpoint_rotates_valid_immutable_gzip(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.recorder.config.DATA_DIR", directory), \
                patch("app.recorder.time.strftime", return_value="20260830-12"):
            recorder = RawRecorder()
            recorder.write({"type": "first"}, 100.0, 50.0)
            finalized = recorder.checkpoint_for_export()

            self.assertTrue(os.path.isfile(finalized))
            with gzip.open(finalized, "rt") as source:
                first_rows = [json.loads(line) for line in source]
            self.assertEqual([row["m"]["type"] for row in first_rows], ["first"])

            recorder.write({"type": "second"}, 101.0, 51.0)
            recorder.close()
            active = os.path.join(directory, "raw", "feed-20260830-12.jsonl.gz")
            with gzip.open(active, "rt") as source:
                second_rows = [json.loads(line) for line in source]
            self.assertEqual([row["m"]["type"] for row in second_rows], ["second"])

            with gzip.open(finalized, "rt") as source:
                unchanged = [json.loads(line) for line in source]
            self.assertEqual(unchanged, first_rows)


if __name__ == "__main__":
    unittest.main()
