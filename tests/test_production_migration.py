"""BR-04: a production-schema copy migrates twice without loss.

The fixture is the database shape deployed at the reviewed head (PR 12 base
`8b6a8a8` plus the migrations that already ran in production): no
`latency.mode`, no clock `source`/`confirmed_ts`, no provider occurrence
columns, no path `sample_seq`/`availability`/`terminal`, and the old
two-column unique fingerprint index.
"""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import store

# Exactly what production had before this pass.  `mode` is present on the five
# tables the earlier migration reached, and absent from `latency` and
# `paper_fills`, which is the state that produced the reviewed defects.
LEGACY_SCHEMA = """
CREATE TABLE markets(
  ticker TEXT PRIMARY KEY, event TEXT, series TEXT, title TEXT,
  close_time TEXT, status TEXT, added_ts REAL, display_game TEXT, display_leg TEXT);
CREATE TABLE signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, local_ts REAL,
  market TEXT, event TEXT, series TEXT, dir INTEGER, dl REAL, levels INTEGER,
  size REAL, ref REAL, ext REAL, conf_lag_ms REAL, late INTEGER,
  outcome TEXT, detail TEXT, mode TEXT,
  match_clock_snapshot TEXT, forward_path_summary TEXT);
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, market TEXT,
  event TEXT, series TEXT, dir INTEGER, side TEXT,
  entry_ts REAL, entry_px REAL, size REAL, cap REAL, notional REAL,
  exit_ts REAL, exit_px REAL, exit_reason TEXT,
  gross REAL, fees REAL, net REAL, mae REAL, shadow_stop_px REAL,
  book_at_entry TEXT, status TEXT DEFAULT 'open', mode TEXT,
  remaining REAL, realized_gross REAL DEFAULT 0, accrued_fees REAL DEFAULT 0,
  exit_qty REAL DEFAULT 0, exit_vwap_num REAL DEFAULT 0,
  fee_type TEXT, fee_multiplier REAL, strategy TEXT,
  max_executable_bid REAL, max_executable_bid_ts REAL,
  bid_path_summary TEXT, mfe_c REAL);
CREATE TABLE paper_fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, signal_id INTEGER,
  ts REAL, leg TEXT, side TEXT, price REAL, quantity REAL, notional REAL,
  fee REAL, reason TEXT, mode TEXT);
CREATE TABLE latency(ts REAL, kind TEXT, ms REAL);
CREATE TABLE eventlog(ts REAL, kind TEXT, text TEXT);
CREATE TABLE goal_latency_observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, observed_ts REAL NOT NULL,
  event TEXT NOT NULL, milestone_id TEXT NOT NULL, change_kind TEXT NOT NULL,
  live_type TEXT, score_before TEXT NOT NULL, score_after TEXT NOT NULL,
  previous_poll_ts REAL, poll_started_ts REAL NOT NULL, response_ms REAL NOT NULL,
  last_book_change_ts REAL, last_book_lead_ms REAL, last_trade_ts REAL,
  last_trade_lead_ms REAL, first_book_after_ts REAL, first_book_after_ms REAL,
  first_trade_after_ts REAL, first_trade_after_ms REAL,
  canonical_type TEXT, canonical_side TEXT, normalized_event TEXT,
  detail TEXT NOT NULL, mode TEXT);
CREATE TABLE match_clock_observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, observed_ts REAL NOT NULL,
  poll_started_ts REAL NOT NULL, previous_poll_ts REAL, response_ms REAL NOT NULL,
  event TEXT NOT NULL, milestone_id TEXT NOT NULL, provider_period TEXT,
  provider_minute INTEGER, provider_stoppage INTEGER, provider_clock TEXT,
  provider_status TEXT, precision TEXT NOT NULL, raw_context TEXT NOT NULL,
  mode TEXT);
CREATE INDEX idx_match_clock_event_ts ON match_clock_observations(event, observed_ts);
CREATE TABLE provider_match_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, observed_ts REAL NOT NULL,
  first_observed_ts REAL NOT NULL, last_observed_ts REAL NOT NULL,
  poll_started_ts REAL NOT NULL, previous_poll_ts REAL, response_ms REAL NOT NULL,
  event TEXT NOT NULL, milestone_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
  previous_fingerprint TEXT, canonical_type TEXT NOT NULL, canonical_side TEXT,
  provider_period TEXT, provider_minute INTEGER, provider_stoppage INTEGER,
  provider_clock TEXT, normalized_event TEXT NOT NULL, raw_payload TEXT NOT NULL,
  mode TEXT);
CREATE INDEX idx_provider_events_event_ts ON provider_match_events(event, observed_ts);
CREATE UNIQUE INDEX idx_provider_events_fingerprint
  ON provider_match_events(event, fingerprint);
CREATE TABLE bid_path_samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, trade_id INTEGER,
  signal_id INTEGER, event TEXT, market TEXT, side TEXT, strategy TEXT,
  anchor_ts REAL NOT NULL, dt_ms REAL NOT NULL, bid REAL, bid_size REAL,
  exec_px REAL, qty REAL, mode TEXT);
CREATE INDEX idx_bid_path_trade ON bid_path_samples(trade_id, dt_ms);
CREATE INDEX idx_bid_path_signal ON bid_path_samples(signal_id, dt_ms);
CREATE INDEX idx_bid_path_kind ON bid_path_samples(kind, anchor_ts);
"""

# A mix of live, demo and legacy (null-mode) history, as a real volume holds.
LEGACY_ROWS = (
    ("signals",
     "(ts_ms,local_ts,market,event,series,dir,dl,levels,size,ref,ext,outcome,detail,mode)",
     [(1, 1.0, "T", "EV", "S", 1, 1.0, 5, 200.0, 40.0, 60.0, "filled", "{}", "live"),
      (2, 2.0, "T", "EV", "S", 1, 1.0, 5, 200.0, 40.0, 60.0, "filled", "{}", "demo"),
      (3, 3.0, "T", "EV", "S", 1, 1.0, 5, 200.0, 40.0, 60.0, "filled", "{}", None)]),
    ("latency", "(ts,kind,ms)",
     [(1.0, "order_arrival_ms", 42.0), (2.0, "order_arrival_ms", 3642.0)]),
    ("match_clock_observations",
     "(observed_ts,poll_started_ts,response_ms,event,milestone_id,provider_minute,"
     "precision,raw_context,mode)",
     [(1.0, 0.9, 50.0, "EV", "m1", 90, "provider_minute_polled", "{}", "live"),
      (2.0, 1.9, 50.0, "EV", "m1", 91, "provider_minute_polled", "{}", None)]),
    ("provider_match_events",
     "(observed_ts,first_observed_ts,last_observed_ts,poll_started_ts,response_ms,"
     "event,milestone_id,fingerprint,canonical_type,normalized_event,raw_payload,mode)",
     [(1.0, 1.0, 1.0, 0.9, 50.0, "EV", "m1", "fp-1", "goal.observed", "{}",
       '{"occurence_ts": 998.5}', "live"),
      (2.0, 2.0, 2.0, 1.9, 50.0, "EV", "m1", "fp-2", "score.correction", "{}",
       '{"occurence_ts": 999.5}', None)]),
    ("goal_latency_observations",
     "(observed_ts,event,milestone_id,change_kind,score_before,score_after,"
     "poll_started_ts,response_ms,detail,mode)",
     [(1.0, "EV", "m1", "goal", "{}", "{}", 0.9, 50.0, "{}", "live"),
      (2.0, "EV", "m1", "goal", "{}", "{}", 1.9, 50.0, "{}", None)]),
    ("bid_path_samples",
     "(kind,trade_id,signal_id,event,anchor_ts,dt_ms,bid,bid_size,exec_px,qty,mode)",
     [("position", 1, 1, "EV", 1.0, 0.0, 90.0, 100.0, 90.0, 10.0, "live"),
      ("position", 1, 1, "EV", 1.0, 100.0, None, None, None, 10.0, "live"),
      ("decline", None, 2, "EV", 1.0, 0.0, 80.0, 50.0, 80.0, 5.0, None)]),
)

TABLES = (
    "signals", "trades", "paper_fills", "latency", "bid_path_samples",
    "match_clock_observations", "provider_match_events",
    "goal_latency_observations",
)


class ProductionSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "footballbot.db"

        connection = sqlite3.connect(self.path)
        connection.executescript(LEGACY_SCHEMA)
        for table, columns, rows in LEGACY_ROWS:
            marks = ",".join("?" for _ in rows[0])
            connection.executemany(
                f"INSERT INTO {table}{columns} VALUES({marks})", rows)
        connection.commit()
        connection.close()

        patcher = patch("app.store.config.DATA_DIR", self.tempdir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store._conn = None

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def snapshot(self):
        """Historical value columns only; new metadata is excluded by design."""
        digest = hashlib.sha256()
        counts = {}
        for table, columns in (
            ("signals", "id,ts_ms,local_ts,market,event,outcome,detail,mode"),
            ("latency", "rowid,ts,kind,ms"),
            ("match_clock_observations",
             "id,observed_ts,event,milestone_id,provider_minute,raw_context,mode"),
            ("provider_match_events",
             "id,observed_ts,event,fingerprint,canonical_type,raw_payload,mode"),
            ("goal_latency_observations", "id,observed_ts,event,change_kind,detail,mode"),
            ("bid_path_samples", "id,kind,trade_id,signal_id,dt_ms,bid,qty,mode"),
        ):
            rows = store.q(f"SELECT {columns} FROM {table} ORDER BY rowid")
            counts[table] = len(rows)
            for row in rows:
                digest.update(repr(sorted(row.items())).encode())
        return digest.hexdigest(), counts

    def migrate(self):
        if store._conn is not None:
            store._conn.close()
            store._conn = None
        store.init()
        store.set_mode("live")

    def test_production_copy_migrates_twice_without_loss_or_change(self):
        self.migrate()
        first_hash, first_counts = self.snapshot()

        self.migrate()
        second_hash, second_counts = self.snapshot()

        self.assertEqual(first_counts, second_counts, "row counts changed on remigration")
        self.assertEqual(first_hash, second_hash, "historical values changed on remigration")
        for table, count in first_counts.items():
            self.assertGreater(count, 0, f"{table} lost all of its history")

    def test_migration_adds_the_new_columns_without_duplicating_any(self):
        self.migrate()
        self.migrate()

        expected = {
            "latency": {"mode"},
            "match_clock_observations": {
                "source", "confirmed_ts", "confirmation_previous_poll_ts"},
            "provider_match_events": {
                "provider_occurrence_ts", "provider_occurrence_source",
                "provider_occurrence_unavailable_reason"},
            "bid_path_samples": {"sample_seq", "availability", "terminal"},
        }
        for table in TABLES:
            names = [row["name"] for row in store.q(f"PRAGMA table_info({table})")]
            with self.subTest(table=table):
                self.assertEqual(len(names), len(set(names)),
                                 f"{table} has duplicate columns")
                self.assertIn("mode", names, f"{table} has no mode column")
                for column in expected.get(table, set()):
                    self.assertIn(column, names, f"{table} is missing {column}")

    def test_the_fingerprint_index_is_replaced_not_duplicated(self):
        self.migrate()
        self.migrate()

        indexes = [row["name"] for row in store.q(
            "SELECT name FROM sqlite_master WHERE type='index'")]
        self.assertEqual(len(indexes), len(set(indexes)), "duplicate index definitions")
        self.assertIn("idx_provider_events_fingerprint_mode", indexes)
        self.assertNotIn(
            "idx_provider_events_fingerprint", indexes,
            "the mode-blind unique index still constrains inserts",
        )

    def test_historical_provenance_is_not_rewritten(self):
        self.migrate()
        self.migrate()

        modes = [row["mode"] for row in store.q(
            "SELECT mode FROM signals ORDER BY id")]
        self.assertEqual(modes, ["live", "demo", None],
                         "stored capture modes were rewritten by the migration")
        self.assertIsNone(
            store.q("SELECT source FROM match_clock_observations ORDER BY id")[0]["source"],
            "a legacy clock row was relabeled with the current provider source",
        )
        self.assertEqual(store.present_mode(None), "legacy_unknown")

    def test_legacy_rows_survive_a_live_boot(self):
        self.migrate()
        store.purge_non_live()
        _hash, counts = self.snapshot()

        self.assertEqual(counts["signals"], 3, "a live boot deleted signal history")
        self.assertEqual(counts["provider_match_events"], 2)
        self.assertEqual(counts["goal_latency_observations"], 2)
        self.assertEqual(counts["bid_path_samples"], 3)


if __name__ == "__main__":
    unittest.main()
