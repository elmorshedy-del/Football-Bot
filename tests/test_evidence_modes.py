"""Handoff section 5: capture mode is written, isolated, and never deleted."""
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from app import store

STUDY_TABLES = (
    "signals", "trades", "paper_fills", "latency", "bid_path_samples",
    "match_clock_observations", "provider_match_events",
    "goal_latency_observations", "feed_events",
)


class ModeFixtureMixin:
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def restart(self, mode="live"):
        """Close and reopen the database exactly as a redeploy would."""
        store._conn.close()
        store._conn = None
        store.init()
        store.set_mode(mode)
        if mode == "live":
            store.purge_non_live()

    def write_one_of_each(self, event="EV", fingerprint="fp-1"):
        """Write one fresh row into every study table. Returns the trade id."""
        signal_id = store.insert_signal({
            "ts_ms": 1000, "local_ts": 1000.0, "market": "T", "event": event,
            "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 200.0,
            "ref": 40.0, "ext": 60.0, "conf_lag_ms": 10.0, "late": True,
            "outcome": "confirmed", "detail": {},
        })
        trade_id = store.open_paper_trade(
            {
                "signal_id": signal_id, "market": "T", "event": event, "series": "S",
                "dir": 1, "side": "yes", "entry_ts": 1000.0, "entry_px": 40.0,
                "size": 10.0, "cap": 100.0, "notional": 4.0,
                "book_at_entry": {}, "strategy": "price_only_late_score",
            },
            {}, [(40.0, 10.0, 0.1)], 0.1, 12.0, order_arrival_ms=30.0,
        )
        store.add_latency("match_response_ms", 55.0)
        store.insert_bid_path([{
            "kind": "trade", "trade_id": trade_id, "signal_id": signal_id,
            "event": event, "market": "T", "side": "yes",
            "strategy": "price_only_late_score", "anchor_ts": 1000.0,
            "dt_ms": 0.0, "bid": 90.0, "bid_size": 100.0, "exec_px": 90.0,
            "qty": 10.0,
        }])
        store.insert_match_clock({
            "observed_ts": 1000.0, "poll_started_ts": 999.95,
            "previous_poll_ts": 999.75, "response_ms": 50.0, "event": event,
            "milestone_id": "m1", "provider_period": "2nd", "provider_minute": 90,
            "provider_stoppage": 5, "provider_clock": "90+5′",
            "provider_status": "live", "precision": "provider_minute_polled",
            "raw_context": {}, "confirmed_ts": 1000.0,
        })
        store.upsert_provider_event({
            "observed_ts": 1000.0, "poll_started_ts": 999.95,
            "previous_poll_ts": 999.75, "response_ms": 50.0, "event": event,
            "milestone_id": "m1", "fingerprint": fingerprint,
            "previous_fingerprint": None, "canonical_type": "goal.scored",
            "canonical_side": "home", "provider_period": "2nd",
            "provider_minute": 90, "provider_stoppage": 5,
            "provider_clock": "90+5′", "normalized_event": {"canonical_type": "goal.scored"},
            "raw_payload": {"occurence_ts": 1000.0},
        })
        store.insert_feed_event("connected", {"connection": 1, "event": event})
        store.insert_goal_latency({
            "observed_ts": 1000.0, "event": event, "milestone_id": "m1",
            "change_kind": "goal", "live_type": "soccer",
            "score_before": {"homeScore": 0.0}, "score_after": {"homeScore": 1.0},
            "previous_poll_ts": 999.75, "poll_started_ts": 999.95,
            "response_ms": 50.0, "detail": {},
        })
        return trade_id

    def modes_in(self, table):
        return [
            row["mode"] for row in store.q(f"SELECT mode FROM {table} ORDER BY rowid")
        ]

    def counts(self):
        return {
            table: store.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            for table in STUDY_TABLES
        }


class ModeWriteTests(ModeFixtureMixin, unittest.TestCase):
    def test_every_fresh_live_insert_writes_live_mode(self):
        store.set_mode("live")
        self.write_one_of_each()

        for table in STUDY_TABLES:
            with self.subTest(table=table):
                modes = self.modes_in(table)
                self.assertTrue(modes, f"{table} recorded no row")
                self.assertTrue(
                    all(mode == "live" for mode in modes),
                    f"{table} wrote {modes!r} instead of live",
                )

    def test_live_latency_readiness_excludes_demo_and_legacy_samples(self):
        store.set_mode("demo")
        for _ in range(30):
            store.add_latency("order_arrival_ms", 5000.0)
        store.ex(
            "INSERT INTO latency(ts,kind,ms) VALUES(?,?,?)",
            (1000.0, "order_arrival_ms", 9000.0),
        )  # legacy null-mode sample
        store.set_mode("live")
        for _ in range(30):
            store.add_latency("order_arrival_ms", 10.0)

        summary = store.latency_readiness()["order_arrival_ms"]
        self.assertEqual(summary["n"], 30, "live readiness must see only live samples")
        self.assertLess(summary["p95"], 100.0)
        self.assertEqual(summary["state"], "PASS")


class ModePreservationTests(ModeFixtureMixin, unittest.TestCase):
    def test_demo_then_live_restart_preserves_both_but_live_apis_exclude_demo(self):
        store.set_mode("demo")
        self.write_one_of_each(event="DEMO", fingerprint="fp-demo")
        demo_counts = self.counts()

        self.restart("live")
        self.write_one_of_each(event="LIVE", fingerprint="fp-live")

        for table in STUDY_TABLES:
            with self.subTest(table=table):
                modes = self.modes_in(table)
                self.assertIn("demo", modes, f"{table} lost its demo history")
                self.assertIn("live", modes, f"{table} did not record live rows")
                self.assertGreaterEqual(
                    len(modes), demo_counts[table],
                    f"{table} rows were deleted across the restart",
                )

        stats = store.stats()
        self.assertNotIn(
            "DEMO", str(stats), "demo evidence leaked into live statistics",
        )

    def test_legacy_null_rows_survive_init_startup_and_two_restarts(self):
        store.set_mode("live")
        for table, columns, values in (
            ("signals",
             "(ts_ms,local_ts,market,event,series,dir,dl,levels,size,ref,ext,outcome,detail)",
             (1, 1.0, "T", "LEGACY", "S", 1, 1.0, 5, 200.0, 40.0, 60.0, "confirmed", "{}")),
            ("latency", "(ts,kind,ms)", (1.0, "order_arrival_ms", 42.0)),
            ("match_clock_observations",
             "(observed_ts,poll_started_ts,response_ms,event,milestone_id,precision,raw_context)",
             (1.0, 0.9, 50.0, "LEGACY", "m1", "provider_minute_polled", "{}")),
        ):
            marks = ",".join("?" for _ in values)
            store.ex(f"INSERT INTO {table}{columns} VALUES({marks})", values)

        before = self.counts()
        self.restart("live")
        self.restart("live")
        after = self.counts()

        for table in ("signals", "latency", "match_clock_observations"):
            with self.subTest(table=table):
                self.assertGreaterEqual(
                    after[table], before[table],
                    f"{table} lost legacy null-mode history across two restarts",
                )
                self.assertIn(
                    None, self.modes_in(table),
                    f"{table} no longer holds any legacy null-mode row",
                )

    def test_provider_and_goal_rows_survive_two_live_restarts(self):
        store.set_mode("live")
        self.write_one_of_each()
        before = self.counts()

        self.restart("live")
        self.restart("live")
        after = self.counts()

        for table in ("provider_match_events", "goal_latency_observations"):
            with self.subTest(table=table):
                self.assertEqual(
                    after[table], before[table],
                    f"{table} rows were deleted by a live restart",
                )

    def test_purge_non_live_never_deletes_study_observations(self):
        store.set_mode("demo")
        self.write_one_of_each(event="DEMO", fingerprint="fp-demo")
        before = self.counts()

        store.set_mode("live")
        store.purge_non_live()
        after = self.counts()

        for table in STUDY_TABLES:
            with self.subTest(table=table):
                self.assertEqual(
                    after[table], before[table],
                    f"purge_non_live() deleted rows from {table}",
                )


class ProviderModeScopeTests(ModeFixtureMixin, unittest.TestCase):
    def provider_row(self, event="EV", fingerprint="fp-1", canonical="goal.scored",
                     payload=None):
        return {
            "observed_ts": 1000.0, "poll_started_ts": 999.95,
            "previous_poll_ts": 999.75, "response_ms": 50.0, "event": event,
            "milestone_id": "m1", "fingerprint": fingerprint,
            "previous_fingerprint": None, "canonical_type": canonical,
            "canonical_side": "home", "provider_period": "2nd",
            "provider_minute": 90, "provider_stoppage": 5,
            "provider_clock": "90+5′",
            "normalized_event": {"canonical_type": canonical},
            "raw_payload": payload if payload is not None else {"occurence_ts": 1000.0},
        }

    def test_same_provider_fingerprint_can_exist_once_per_mode_without_overwrite(self):
        store.set_mode("demo")
        demo_id, demo_new = store.upsert_provider_event(
            self.provider_row(payload={"occurence_ts": 1.0}))
        self.assertTrue(demo_new)

        store.set_mode("live")
        live_id, live_new = store.upsert_provider_event(
            self.provider_row(payload={"occurence_ts": 2.0}))
        self.assertTrue(live_new, "the same fingerprint must be insertable per mode")
        self.assertNotEqual(demo_id, live_id)

        rows = {
            row["id"]: row for row in
            store.q("SELECT id, mode, raw_payload FROM provider_match_events")
        }
        self.assertEqual(rows[demo_id]["mode"], "demo")
        self.assertEqual(rows[live_id]["mode"], "live")
        self.assertIn("1.0", rows[demo_id]["raw_payload"])
        self.assertIn("2.0", rows[live_id]["raw_payload"])

    def test_duplicate_refresh_preserves_original_mode_and_raw_payload(self):
        store.set_mode("live")
        row_id, is_new = store.upsert_provider_event(
            self.provider_row(payload={"occurence_ts": 1.0}))
        self.assertTrue(is_new)

        refreshed = self.provider_row(payload={"occurence_ts": 999.0})
        refreshed["observed_ts"] = 2000.0
        same_id, is_new_again = store.upsert_provider_event(refreshed)

        self.assertEqual(same_id, row_id)
        self.assertFalse(is_new_again)
        stored = store.q(
            "SELECT mode, raw_payload, first_observed_ts, last_observed_ts"
            " FROM provider_match_events WHERE id=?", (row_id,),
        )[0]
        self.assertEqual(stored["mode"], "live")
        self.assertIn("1.0", stored["raw_payload"], "raw payload was overwritten")
        self.assertNotIn("999.0", stored["raw_payload"])
        self.assertEqual(stored["first_observed_ts"], 1000.0)
        self.assertEqual(stored["last_observed_ts"], 2000.0)


class ModeMigrationTests(ModeFixtureMixin, unittest.TestCase):
    def value_hash(self):
        """Hash the historical value columns, excluding new metadata/indexes."""
        digest = hashlib.sha256()
        for table, columns in (
            ("signals", "id,ts_ms,local_ts,market,event,outcome"),
            ("latency", "rowid,ts,kind,ms"),
            ("match_clock_observations", "id,observed_ts,event,provider_minute"),
            ("provider_match_events", "id,observed_ts,event,fingerprint,raw_payload"),
            ("goal_latency_observations", "id,observed_ts,event,change_kind"),
        ):
            for row in store.q(f"SELECT {columns} FROM {table} ORDER BY rowid"):
                digest.update(repr(sorted(dict(row).items())).encode())
        return digest.hexdigest()

    def test_mode_migration_is_idempotent_and_preserves_row_hashes(self):
        store.set_mode("live")
        self.write_one_of_each()
        store.ex(
            "INSERT INTO latency(ts,kind,ms) VALUES(?,?,?)",
            (1.0, "order_arrival_ms", 42.0),
        )
        before_hash = self.value_hash()
        before_counts = self.counts()

        self.restart("live")
        self.restart("live")

        self.assertEqual(self.value_hash(), before_hash,
                         "two migrations changed historical values")
        self.assertEqual(self.counts(), before_counts,
                         "two migrations changed row counts")

    def test_migration_adds_no_duplicate_columns_or_indexes(self):
        self.restart("live")
        self.restart("live")

        for table in STUDY_TABLES:
            with self.subTest(table=table):
                names = [
                    row["name"] for row in
                    store.q(f"PRAGMA table_info({table})")
                ]
                self.assertEqual(len(names), len(set(names)),
                                 f"{table} has duplicate columns")
        index_names = [
            row["name"] for row in
            store.q("SELECT name FROM sqlite_master WHERE type='index'")
        ]
        self.assertEqual(len(index_names), len(set(index_names)),
                         "duplicate index definitions")


if __name__ == "__main__":
    unittest.main()
