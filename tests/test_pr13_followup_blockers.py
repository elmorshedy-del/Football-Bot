"""Regression coverage for the independent PR #13 follow-up review."""
import asyncio
import json
import tempfile
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from app import engine as engine_module
from app import main, store


def run(coro):
    return asyncio.run(coro)


class PR13FollowupBlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data_patch = patch("app.store.config.DATA_DIR", self.tmp.name)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        store._conn = None
        store.init()
        store.set_mode("live")
        self.engine_patch = patch.object(main, "engine", SimpleNamespace(
            desk=SimpleNamespace(positions={}, pos_dict=lambda *a: {}),
            clock_tracker=None, watched_events=set(), mode="live", meta={},
            event_markets={},
        ))
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def signal(self, event="GAME", started=True):
        return store.insert_signal({
            "ts_ms": 1, "local_ts": 1000.0, "market": f"{event}-YES",
            "event": event, "series": "SERIES", "dir": 1, "dl": 1.0,
            "levels": 5, "size": 10.0, "ref": 40.0, "ext": 60.0,
            "outcome": "confirmed", "detail": {},
            "forward_path_started_ts": 1000.0 if started else None,
        })

    def trade(self, signal_id, event="GAME", status="open"):
        tid = store.open_paper_trade({
            "signal_id": signal_id, "market": f"{event}-YES", "event": event,
            "series": "SERIES", "dir": 1, "side": "yes", "entry_ts": 1000.0,
            "entry_px": 50.0, "size": 10.0, "cap": 100.0, "notional": 5.0,
            "book_at_entry": {}, "strategy": "price_only_late_score",
        }, {}, [(50.0, 10.0, 0.1)], 0.1, 12.0, order_arrival_ms=20.0)
        if status == "closed":
            store.close_trade(tid, 60.0, "target", 1.0, 0.2, 0.8, 0.0, None)
        return tid

    def path_row(self, *, tid=None, sid=None, kind="position", seq=1, bid=55.0,
                 terminal=0, availability="quote"):
        return {
            "kind": kind, "trade_id": tid, "signal_id": sid, "event": "GAME",
            "market": "GAME-YES", "side": "yes",
            "strategy": "price_only_late_score", "anchor_ts": 1000.0,
            "dt_ms": float(seq * 10), "bid": bid,
            "bid_size": 20.0 if bid is not None else None,
            "exec_px": bid if bid is not None else None, "qty": 10.0,
            "sample_seq": seq, "availability": availability, "terminal": terminal,
        }

    def bare_engine(self):
        eng = engine_module.Engine.__new__(engine_module.Engine)
        eng._signal_paths = deque()
        eng._signal_path_failed_owners = set()
        eng.signal_path_fault = None
        eng.errors = deque(maxlen=50)
        eng._last_error_key = None
        eng._last_error_ts = 0.0
        eng._record_error = lambda *_args, **_kwargs: None
        return eng

    def test_schema_migration_is_idempotent_for_started_marker(self):
        store.init()
        store.init()
        columns = {row["name"] for row in store.q("PRAGMA table_info(signals)")}
        self.assertIn("forward_path_started_ts", columns)

    def test_identical_retry_is_idempotent_but_conflicting_sequence_raises(self):
        sid = self.signal()
        tid = self.trade(sid)
        row = self.path_row(tid=tid, sid=sid)
        self.assertEqual(store.insert_bid_path([row]), 1)
        self.assertEqual(store.insert_bid_path([dict(row)]), 1)
        self.assertEqual(store.q(
            "SELECT COUNT(*) AS n FROM bid_path_samples WHERE trade_id=?", (tid,),
        )[0]["n"], 1)

        conflict = dict(row, bid=99.0, exec_px=99.0)
        with self.assertRaisesRegex(store.PathSequenceConflict, "path_sequence_conflict"):
            store.insert_bid_path([conflict])
        durable = store.q(
            "SELECT bid FROM bid_path_samples WHERE trade_id=? AND sample_seq=1", (tid,),
        )
        self.assertEqual(durable[0]["bid"], 55.0)

    def test_conflicting_signal_sequence_keeps_watch_unfinalized_and_faulted(self):
        sid = self.signal()
        durable = self.path_row(sid=sid, kind="decline", seq=1, bid=55.0)
        durable["trade_id"] = None
        store.insert_bid_path([durable])

        conflict = dict(durable, bid=88.0, exec_px=88.0)
        watch = {
            "signal_id": sid, "market": "GAME-YES", "event": "GAME",
            "side": "yes", "strategy": "price_only_late_score",
            "anchor_ts": 1000.0, "expires_at": 0.0, "outcome": "confirmed",
            "last": None, "rows": [conflict], "dropped": 0, "total": 1,
        }
        eng = self.bare_engine()
        eng._signal_paths.append(watch)
        eng._expire_signal_paths(1.0)

        self.assertEqual(len(eng._signal_paths), 1)
        self.assertEqual(eng.signal_path_fault, "signal_path_persistence_failed")
        self.assertIn(sid, eng._signal_path_failed_owners)
        signal = store.q("SELECT forward_path_finalized FROM signals WHERE id=?", (sid,))[0]
        self.assertIsNone(signal["forward_path_finalized"])

    def test_zero_row_started_watch_is_recovered_after_restart(self):
        sid = self.signal(started=True)
        self.assertEqual([r["id"] for r in store.unfinalized_signal_paths()], [sid])
        eng = self.bare_engine()
        rebuilt = eng.rebuild_signal_paths()
        self.assertEqual(rebuilt, 1)
        self.assertEqual(len(eng._signal_paths), 0)
        signal = store.q(
            "SELECT forward_path_finalized,path_incomplete_reason,forward_path_summary "
            "FROM signals WHERE id=?", (sid,),
        )[0]
        self.assertIsNotNone(signal["forward_path_finalized"])
        self.assertEqual(signal["path_incomplete_reason"], "in_memory_tail_lost_on_restart")
        self.assertIsNone(signal["forward_path_summary"], "zero-row recovery invented quotes")

    def test_terminal_is_time_only_and_not_counted_as_quote_or_gap(self):
        rows = [
            self.path_row(seq=1, bid=90.0),
            self.path_row(seq=2, bid=None, availability="gap"),
            self.path_row(seq=3, bid=70.0),
            self.path_row(seq=4, bid=None, terminal=1, availability="terminal"),
        ]
        self.assertIsNone(rows[-1]["bid"])
        summary = store.bid_path_summary(rows)
        self.assertEqual(summary["samples_total"], 4)
        self.assertEqual(summary["samples_priced"], 2)
        self.assertEqual(summary["gap_count"], 1)
        self.assertEqual(summary["last_bid"], 70.0)
        self.assertEqual(summary["peak_bid"], 90.0)
        self.assertEqual(summary["trough_bid"], 70.0)

    def test_durable_path_never_exceeds_four_thousand_rows(self):
        sid = self.signal()
        tid = self.trade(sid)
        rows = [self.path_row(tid=tid, sid=sid, seq=i, bid=50.0) for i in range(1, 4001)]
        store.insert_bid_path(rows)
        extra = self.path_row(tid=tid, sid=sid, seq=4001, bid=None,
                              terminal=1, availability="terminal")
        with self.assertRaisesRegex(store.PathSampleCapExceeded, "path_sample_cap_exhausted"):
            store.insert_bid_path([extra])
        count = store.q(
            "SELECT COUNT(*) AS n FROM bid_path_samples WHERE trade_id=?", (tid,),
        )[0]["n"]
        self.assertEqual(count, store.BID_PATH_MAX_SAMPLES)

    def test_selected_mode_supplies_open_rows_and_path_urls_without_n_plus_one(self):
        store.set_mode("demo")
        demo_sid = self.signal(event="DEMO", started=False)
        demo_tid = self.trade(demo_sid, event="DEMO")
        store.set_mode("live")
        live_sid = self.signal(event="LIVE", started=False)
        self.trade(live_sid, event="LIVE")

        traced = []
        store._conn.set_trace_callback(traced.append)
        try:
            payload = run(main.trades(mode="demo"))
            signals = run(main.signals(mode="demo"))
        finally:
            store._conn.set_trace_callback(None)

        self.assertEqual([row["id"] for row in payload["open"]], [demo_tid])
        self.assertTrue(payload["open"][0]["bid_path_url"].endswith("?mode=demo"))
        self.assertTrue(signals[0]["forward_path_url"].endswith("?mode=demo"))
        path_selects = [sql for sql in traced if sql.lstrip().upper().startswith("SELECT")
                        and "BID_PATH_SAMPLES" in sql.upper()]
        self.assertEqual(path_selects, [],
                         f"list endpoints performed per-row path queries: {path_selects}")

    def test_archive_scope_is_explicitly_all_mode_and_frontend_exposes_it(self):
        job = main._new_export_job("archive", [], "all")
        public = main._public_export_job(job)
        self.assertEqual(public["mode_selector"], "all")
        self.assertTrue(public["all_mode_archival_export"])
        with open("static/index.html", encoding="utf-8") as handle:
            html = handle.read()
        with open("static/app.js", encoding="utf-8") as handle:
            js = handle.read()
        self.assertIn('data-export-scope="archive"', html)
        self.assertIn('"archive"', js)


if __name__ == "__main__":
    unittest.main()
