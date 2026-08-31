import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import exporter, store


class StudyExportTests(unittest.TestCase):
    def _seed(self, directory, raw_bytes=None):
        store.init()
        store.set_mode("live")
        store.upsert_market(
            "KXTESTGAME-TEAM", "KXTESTGAME", "KXTESTGAME",
            "Alpha FC — Beta FC — Alpha FC", "2026-08-30T20:00:00Z", "open",
        )
        signal_id = store.insert_signal({
            "ts_ms": 1_777_777_777_000,
            "local_ts": 1_777_777_777.125,
            "market": "KXTESTGAME-TEAM",
            "event": "KXTESTGAME",
            "series": "KXTESTGAME",
            "dir": 1,
            "dl": 1.2,
            "levels": 6,
            "size": 250.0,
            "ref": 40.0,
            "ext": 55.0,
            "conf_lag_ms": 42.0,
            "late": True,
            "outcome": "filled",
            "detail": {"strategy": "price_only_late_score"},
        })
        trade_id = store.insert_trade({
            "signal_id": signal_id,
            "market": "KXTESTGAME-TEAM",
            "event": "KXTESTGAME",
            "series": "KXTESTGAME",
            "dir": 1,
            "side": "yes",
            "entry_ts": 1_777_777_777.25,
            "entry_px": 55.0,
            "size": 1.0,
            "cap": 58.0,
            "notional": 0.55,
            "book_at_entry": {"yes": [[55, 20]]},
            "strategy": "price_only_late_score",
        })
        store.ex(
            """INSERT INTO paper_fills(
                   trade_id,signal_id,ts,leg,side,price,quantity,notional,
                   fee,reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, 1_777_777_777.25, "entry", "yes", 55.0,
             1.0, 0.55, 0.01, "entry", "live"),
        )
        store.add_latency("order_arrival", 150.0)
        store.insert_goal_latency({
            "observed_ts": 1_777_777_778.0,
            "event": "KXTESTGAME",
            "milestone_id": "milestone-1",
            "change_kind": "score_change",
            "live_type": "in_progress",
            "score_before": {"home": 0, "away": 0},
            "score_after": {"home": 1, "away": 0},
            "previous_poll_ts": 1_777_777_777.5,
            "poll_started_ts": 1_777_777_777.9,
            "response_ms": 100.0,
            "detail": {"live_data": {"period": "second_half", "minute": 89}},
        })
        store.insert_match_clock({
            "observed_ts": 1_777_777_778.0,
            "poll_started_ts": 1_777_777_777.9,
            "previous_poll_ts": 1_777_777_777.5,
            "response_ms": 100.0,
            "event": "KXTESTGAME",
            "milestone_id": "milestone-1",
            "provider_period": "2nd",
            "provider_minute": 90,
            "provider_stoppage": 5,
            "provider_clock": "90+5′",
            "provider_status": "live",
            "precision": "provider_minute_polled",
            "raw_context": {"time": "90+5'"},
        })
        store.upsert_provider_event({
            "observed_ts": 1_777_777_778.0,
            "poll_started_ts": 1_777_777_777.9,
            "previous_poll_ts": 1_777_777_777.5,
            "response_ms": 100.0,
            "event": "KXTESTGAME",
            "milestone_id": "milestone-1",
            "fingerprint": "abc123",
            "canonical_type": "penalty.scored",
            "canonical_side": "home",
            "provider_period": "2nd",
            "provider_minute": 90,
            "provider_stoppage": 5,
            "provider_clock": "90+5′",
            "normalized_event": {"canonical_type": "penalty.scored"},
            "raw_payload": {"event_type": "score_change"},
        })
        store.log_event("test", "export fixture")
        raw_dir = Path(directory) / "raw"
        raw_dir.mkdir()
        raw_path = raw_dir / "feed-20260830-20-part-1.jsonl.gz"
        payload = raw_bytes if raw_bytes is not None else (
            json.dumps({
                "lt": 1_777_777_777.0,
                "lm": 123.0,
                "m": {"type": "orderbook_snapshot", "seq": 1},
            }) + "\n"
        )
        if isinstance(payload, bytes):
            raw_path.write_bytes(payload)
        else:
            with gzip.open(raw_path, "wt") as target:
                target.write(payload)
        return raw_path

    def _bundle_context(self, secret_markers):
        directory = tempfile.TemporaryDirectory()
        patches = (
            patch("app.store.config.DATA_DIR", directory.name),
            patch("app.exporter.config.DATA_DIR", directory.name),
            patch("app.exporter.config.ADMIN_TOKEN", secret_markers[0].decode()),
            patch("app.exporter.config.KALSHI_PRIVATE_KEY", secret_markers[1].decode()),
        )
        for item in patches:
            item.start()
        return directory, patches

    def test_bundle_is_parseable_reconciled_hashed_and_non_secret(self):
        old_connection = store._conn
        secret_markers = (b"ADMIN-SECRET-MARKER", b"PRIVATE-KEY-MARKER")
        directory, patches = self._bundle_context(secret_markers)
        try:
            raw_path = self._seed(directory.name)
            output = Path(directory.name) / "study.zip"
            path, returned_manifest = exporter.build_study_bundle(
                output, mode="live", raw_paths=[raw_path],
            )

            self.assertEqual(Path(path), output)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest, returned_manifest)
                self.assertEqual(manifest["schema"], exporter.EXPORT_SCHEMA)
                self.assertTrue(manifest["paper_only"])
                self.assertEqual(manifest["scope"], "full")
                self.assertTrue(manifest["include_raw"])
                self.assertEqual(manifest["guarantee"], "none")
                self.assertEqual(
                    manifest["audit_semantics"]["match_event_role"],
                    "diagnostic_only",
                )

                for table in exporter.TABLES:
                    metadata = manifest["tables"][table]
                    self.assertEqual(metadata["rows"], 1)
                    csv_rows = list(csv.DictReader(io.StringIO(
                        archive.read(metadata["csv"]).decode("utf-8"),
                    )))
                    jsonl_rows = [json.loads(line) for line in
                                  archive.read(metadata["jsonl"]).decode("utf-8").splitlines()]
                    self.assertEqual(len(csv_rows), metadata["rows"])
                    self.assertEqual(len(jsonl_rows), metadata["rows"])

                for name, metadata in manifest["artifacts"].items():
                    content = archive.read(name)
                    self.assertEqual(len(content), metadata["bytes"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), metadata["sha256"])

                raw_content = archive.read(manifest["raw_feed"][0]["file"])
                self.assertEqual(
                    archive.getinfo(manifest["raw_feed"][0]["file"]).compress_type,
                    zipfile.ZIP_STORED,
                )
                with gzip.GzipFile(fileobj=io.BytesIO(raw_content)) as source:
                    raw_rows = [json.loads(line) for line in source.read().decode().splitlines()]
                self.assertEqual(raw_rows[0]["m"]["seq"], 1)

                snapshot_path = Path(directory.name) / "verify-snapshot.db"
                snapshot_path.write_bytes(archive.read("database/footballbot-snapshot.db"))
                connection = sqlite3.connect(snapshot_path)
                try:
                    for table in exporter.TABLES:
                        count = connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0]
                        self.assertEqual(count, manifest["tables"][table]["rows"])
                finally:
                    connection.close()

                all_content = b"".join(archive.read(name) for name in archive.namelist())
                for marker in secret_markers:
                    self.assertNotIn(marker, all_content)
                self.assertIn("docs/PRICE_ONLY_BACKTEST_HANDOFF.md", archive.namelist())
        finally:
            for item in patches:
                item.stop()
            directory.cleanup()
            if store._conn is not None and store._conn is not old_connection:
                store._conn.close()
            store._conn = old_connection

    def test_audit_bundle_lists_raw_inventory_without_bodies(self):
        old_connection = store._conn
        secret_markers = (b"ADMIN-SECRET-MARKER", b"PRIVATE-KEY-MARKER")
        directory, patches = self._bundle_context(secret_markers)
        try:
            raw_path = self._seed(directory.name)
            output = Path(directory.name) / "audit.zip"
            updates = []
            path, manifest = exporter.build_study_bundle(
                output, mode="live", raw_paths=[raw_path],
                include_raw=False, scope="audit",
                progress=updates.append,
            )
            self.assertEqual(Path(path), output)
            self.assertEqual(manifest["scope"], "audit")
            self.assertFalse(manifest["include_raw"])
            self.assertEqual(manifest["raw_feed"][0]["name"], raw_path.name)
            self.assertFalse(manifest["raw_feed"][0]["included"])
            self.assertNotIn("sha256", manifest["raw_feed"][0])
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertNotIn(f"raw/{raw_path.name}", names)
                self.assertIn("manifest.json", names)
                self.assertIn("tables/match_clock_observations.csv", names)
            self.assertTrue(updates)
            self.assertEqual(updates[0]["total_segments"], 0)
        finally:
            for item in patches:
                item.stop()
            directory.cleanup()
            if store._conn is not None and store._conn is not old_connection:
                store._conn.close()
            store._conn = old_connection

    def test_full_bundle_reports_progress_and_honors_cancel(self):
        old_connection = store._conn
        secret_markers = (b"ADMIN-SECRET-MARKER", b"PRIVATE-KEY-MARKER")
        directory, patches = self._bundle_context(secret_markers)
        try:
            raw_path = self._seed(directory.name)
            output = Path(directory.name) / "full.zip"
            updates = []
            path, manifest = exporter.build_study_bundle(
                output, mode="live", raw_paths=[raw_path],
                scope="full", progress=updates.append,
            )
            self.assertTrue(manifest["raw_feed"][0]["included"])
            self.assertGreaterEqual(updates[-1]["processed_segments"], 1)
            self.assertEqual(updates[-1]["total_segments"], 1)
            cancelled = Path(directory.name) / "cancelled.zip"
            with self.assertRaises(exporter.ExportCancelled):
                exporter.build_study_bundle(
                    cancelled, mode="live", raw_paths=[raw_path],
                    scope="full", cancel_check=lambda: True,
                )
            self.assertFalse(cancelled.exists())
        finally:
            for item in patches:
                item.stop()
            directory.cleanup()
            if store._conn is not None and store._conn is not old_connection:
                store._conn.close()
            store._conn = old_connection

    def test_zip64_stored_raw_member_and_segment_path_safety(self):
        old_connection = store._conn
        secret_markers = (b"ADMIN-SECRET-MARKER", b"PRIVATE-KEY-MARKER")
        directory, patches = self._bundle_context(secret_markers)
        try:
            payload = b"x" * 200
            raw_path = self._seed(directory.name, raw_bytes=payload)
            output = Path(directory.name) / "zip64.zip"
            with patch.object(exporter.zipfile, "ZIP64_LIMIT", 64):
                path, manifest = exporter.build_study_bundle(
                    output, mode="live", raw_paths=[raw_path], scope="full",
                )
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo(manifest["raw_feed"][0]["file"])
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(info.file_size, 200)
                self.assertEqual(archive.read(info.filename), payload)
            self.assertIsNone(exporter.safe_raw_segment_path("../etc/passwd"))
            self.assertIsNone(exporter.safe_raw_segment_path("feed-20260830-20.jsonl.gz/../../x"))
            self.assertIsNone(exporter.safe_raw_segment_path("not-a-feed.txt"))
            self.assertEqual(
                exporter.safe_raw_segment_path(raw_path.name),
                raw_path.resolve(),
            )
        finally:
            for item in patches:
                item.stop()
            directory.cleanup()
            if store._conn is not None and store._conn is not old_connection:
                store._conn.close()
            store._conn = old_connection


if __name__ == "__main__":
    unittest.main()
