"""PR 13 review §1.4: live APIs and exports must not mix evidence modes."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app import main, store


def run(coro):
    return asyncio.run(coro)


class ModeScopedApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("app.store.config.DATA_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store._conn = None
        store.init()

        engine_patch = patch.object(main, "engine", SimpleNamespace(
            desk=SimpleNamespace(positions={}, pos_dict=lambda *a: {}),
            clock_tracker=None, watched_events=set(), mode="live",
            meta={}, event_markets={},
        ))
        engine_patch.start()
        self.addCleanup(engine_patch.stop)

        self.ids = {}
        for mode, event in (("demo", "DEMO"), ("live", "LIVE")):
            store.set_mode(mode)
            self.ids[mode] = self.seed(event)
        # A legacy row with no provenance at all.
        store.ex(
            "INSERT INTO signals(ts_ms,local_ts,market,event,series,dir,dl,levels,"
            "size,ref,ext,outcome,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (3, 3.0, "T", "LEGACY", "S", 1, 1.0, 5, 200.0, 40.0, 60.0, "confirmed", "{}"),
        )
        store.set_mode("live")

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def seed(self, event):
        signal_id = store.insert_signal({
            "ts_ms": 1, "local_ts": 1000.0, "market": "T", "event": event,
            "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 10.0,
            "ref": 40.0, "ext": 60.0, "outcome": "filled", "detail": {},
        })
        trade_id = store.open_paper_trade(
            {
                "signal_id": signal_id, "market": "T", "event": event, "series": "S",
                "dir": 1, "side": "yes", "entry_ts": 1000.0, "entry_px": 40.0,
                "size": 10.0, "cap": 100.0, "notional": 4.0, "book_at_entry": {},
                "strategy": "price_only_late_score",
            },
            {}, [(40.0, 10.0, 0.1)], 0.1, 12.0, order_arrival_ms=30.0,
        )
        store.close_trade(trade_id, 60.0, "target", 2.0, 0.2, 1.8, 0.0, None)
        store.insert_bid_path([{
            "kind": "position", "trade_id": trade_id, "signal_id": signal_id,
            "event": event, "market": "T", "side": "yes",
            "strategy": "price_only_late_score", "anchor_ts": 1000.0, "dt_ms": 0.0,
            "bid": 90.0, "bid_size": 10.0, "exec_px": 90.0, "qty": 10.0,
            "sample_seq": 1, "availability": "quote", "terminal": 0,
        }])
        store.insert_match_clock({
            "observed_ts": 1000.0, "poll_started_ts": 999.9, "response_ms": 50.0,
            "event": event, "milestone_id": "m1", "provider_minute": 90,
            "precision": "provider_minute_polled", "raw_context": {},
        })
        store.upsert_provider_event({
            "observed_ts": 1000.0, "poll_started_ts": 999.9, "response_ms": 50.0,
            "event": event, "milestone_id": "m1", "fingerprint": f"fp-{event}",
            "canonical_type": "goal.observed", "normalized_event": {},
            "raw_payload": {"occurence_ts": 998.0},
        })
        store.insert_goal_latency({
            "observed_ts": 1000.0, "event": event, "milestone_id": "m1",
            "change_kind": "goal", "score_before": {}, "score_after": {},
            "poll_started_ts": 999.9, "response_ms": 50.0, "detail": {},
        })
        store.add_latency("order_arrival_ms", 20.0)
        store.insert_feed_event("connected", {"event": event}, 1000.0, 5.0)
        return {"signal_id": signal_id, "trade_id": trade_id}

    # ---------------------------------------------------------------- default

    def test_every_study_endpoint_defaults_to_the_active_mode(self):
        signals = run(main.signals())
        self.assertEqual({row["event"] for row in signals}, {"LIVE"},
                         "signals leaked another mode")

        trades = run(main.trades())
        self.assertEqual({row["event"] for row in trades["closed"]}, {"LIVE"})

        clocks = run(main.match_clocks())
        self.assertEqual({row["event"] for row in clocks["observations"]}, {"LIVE"})

        events = run(main.provider_events())
        self.assertEqual({row["event"] for row in events}, {"LIVE"})

        goals = run(main.goal_latency())
        self.assertEqual({row["event"] for row in goals}, {"LIVE"})

        feed = run(main.feed_events())["events"]
        self.assertEqual({row["detail"]["event"] for row in feed}, {"LIVE"},
                         "the feed-health ledger leaked another mode")
        self.assertEqual({row["kind"] for row in feed}, {"connected"})

    def test_every_endpoint_accepts_the_four_safe_selectors(self):
        for selector in ("live", "demo", "legacy_unknown", "all"):
            with self.subTest(selector=selector):
                self.assertIsNotNone(run(main.signals(mode=selector)))
                self.assertIsNotNone(run(main.trades(mode=selector)))
                self.assertIsNotNone(run(main.match_clocks(mode=selector)))
                self.assertIsNotNone(run(main.provider_events(mode=selector)))
                self.assertIsNotNone(run(main.goal_latency(mode=selector)))
                self.assertIsNotNone(run(main.feed_events(mode=selector)))
                self.assertIsNotNone(run(main.equity(mode=selector)))
                self.assertIsNotNone(run(main.latency(mode=selector)))
                self.assertIsNotNone(run(main.stats(mode=selector)))

    def test_an_unknown_selector_is_rejected(self):
        for bad in ("production", "LIVE; DROP TABLE signals", "", "None"):
            with self.subTest(selector=bad):
                with self.assertRaises(HTTPException) as caught:
                    run(main.signals(mode=bad))
                self.assertEqual(caught.exception.status_code, 400)

    def test_demo_selector_returns_only_demo(self):
        signals = run(main.signals(mode="demo"))
        self.assertEqual({row["event"] for row in signals}, {"DEMO"})

    def test_all_selector_returns_every_mode(self):
        events = {row["event"] for row in run(main.signals(mode="all"))}
        self.assertEqual(events, {"LIVE", "DEMO", "LEGACY"})

    def test_legacy_rows_are_presented_as_legacy_unknown(self):
        rows = run(main.signals(mode="legacy_unknown"))
        self.assertEqual({row["event"] for row in rows}, {"LEGACY"})
        self.assertEqual({row["mode"] for row in rows}, {"legacy_unknown"},
                         "null provenance must be labelled, not left null")

    # ----------------------------------------------------------------- nested

    def test_nested_decoration_cannot_cross_modes(self):
        """A live trade must not be decorated by a demo signal or event."""
        trades = run(main.trades(mode="live"))
        for row in trades["closed"]:
            self.assertNotEqual(row.get("event"), "DEMO")
            association = row.get("match_event") or {}
            self.assertNotEqual(association.get("event"), "DEMO")

    def test_path_access_is_scoped_through_its_parent(self):
        demo_trade = self.ids["demo"]["trade_id"]
        with self.assertRaises(HTTPException) as caught:
            run(main.trade_bid_path(demo_trade, mode="live"))
        self.assertEqual(caught.exception.status_code, 404,
                         "a live caller fetched another mode's path by id")

        allowed = run(main.trade_bid_path(demo_trade, mode="demo"))
        self.assertTrue(allowed["samples"])

    def test_signal_path_access_is_scoped_through_its_parent(self):
        demo_signal = self.ids["demo"]["signal_id"]
        with self.assertRaises(HTTPException) as caught:
            run(main.signal_forward_path(demo_signal, mode="live"))
        self.assertEqual(caught.exception.status_code, 404)

    def test_equity_and_latency_exclude_other_modes(self):
        live_points = run(main.equity(mode="live"))["combined"]
        all_points = run(main.equity(mode="all"))["combined"]
        self.assertLess(len(live_points), len(all_points),
                        "equity ignored the mode selector")

        live_latency = run(main.latency(mode="live"))["order_arrival_ms"]
        all_latency = run(main.latency(mode="all"))["order_arrival_ms"]
        self.assertLess(len(live_latency["hist"]), len(all_latency["hist"]),
                        "the latency histogram ignored the mode selector")


if __name__ == "__main__":
    unittest.main()


class ModeScopedExportTests(ModeScopedApiTests):
    """PR 13 review §1.4 items 5-7: the bundle must match its own label."""

    def build(self, mode="live", all_modes=False):
        import zipfile
        from app import exporter
        snapshot = exporter.prepare_database_snapshot()
        path, manifest = exporter.build_study_bundle(
            None, mode, [], snapshot, False, None, None, "audit",
            all_modes=all_modes,
        )
        self.addCleanup(lambda: __import__("os").unlink(path))
        with zipfile.ZipFile(path) as archive:
            jsonl = archive.read("tables/signals.jsonl").decode()
            csv_text = archive.read("tables/signals.csv").decode()
            archive.extract("database/footballbot-snapshot.db", self.dir.name)
        rows = [__import__("json").loads(line) for line in jsonl.splitlines() if line]
        return manifest, rows, csv_text

    def snapshot_events(self):
        import os
        import sqlite3
        path = os.path.join(self.dir.name, "database", "footballbot-snapshot.db")
        connection = sqlite3.connect(path)
        try:
            return {row[0] for row in connection.execute("SELECT event FROM signals")}
        finally:
            connection.close()

    def test_live_export_excludes_demo_and_legacy_everywhere(self):
        manifest, rows, csv_text = self.build("live")

        self.assertEqual({row["event"] for row in rows}, {"LIVE"},
                         "JSONL carried another mode")
        self.assertNotIn("DEMO", csv_text, "CSV carried another mode")
        self.assertNotIn("LEGACY", csv_text)
        self.assertEqual(self.snapshot_events(), {"LIVE"},
                         "the SQLite snapshot carried another mode")

    def test_manifest_records_requested_modes_and_per_mode_counts(self):
        manifest, _rows, _csv = self.build("live")

        self.assertEqual(manifest["mode_selector"], "live")
        self.assertEqual(manifest["requested_modes"], ["live"])
        for table, block in manifest["tables"].items():
            with self.subTest(table=table):
                self.assertIn("counts_by_mode", block)
                self.assertEqual(
                    sum(block["counts_by_mode"].values()), block["rows"],
                    f"{table} per-mode counts do not reconcile with its row count",
                )
                self.assertNotIn("demo", block["counts_by_mode"])

    def test_reconciliation_reports_no_orphan_fill(self):
        manifest, _rows, _csv = self.build("live")
        block = manifest["reconciliation"]

        self.assertTrue(block["reconciled"], f"unreconciled export: {block}")
        self.assertEqual(block["orphan_fills"], [])
        self.assertEqual(block["orphan_trades"], [])

    def test_an_explicit_archival_export_may_include_every_mode(self):
        manifest, rows, _csv = self.build("live", all_modes=True)

        self.assertTrue(manifest["all_mode_archival_export"])
        self.assertEqual({row["event"] for row in rows}, {"LIVE", "DEMO", "LEGACY"})
        self.assertEqual(self.snapshot_events(), {"LIVE", "DEMO", "LEGACY"})
