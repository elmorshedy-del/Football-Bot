import math
import tempfile
import time
import unittest
from unittest.mock import patch

from app.books import Book
from app.paper import PaperDesk, Position
from app import store


def held_book(yes_bid=None, no_bid=None, yes_ask=None):
    book = Book()
    if yes_bid is not None:
        book.yes_bids = {float(yes_bid): 100.0}
    if no_bid is not None:
        book.no_bids = {float(no_bid): 100.0}
    elif yes_ask is not None:
        book.no_bids = {100.0 - float(yes_ask): 100.0}
    book.ok = True
    return book


class TradeHighTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()
        store.set_mode("live")

    def tearDown(self):
        store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def open_position(self, side="yes", entry_px=50.0, tid=None):
        signal_id = store.insert_signal({
            "ts_ms": 1, "local_ts": 1.0, "market": "T", "event": "E",
            "series": "S", "dir": 1 if side == "yes" else -1, "dl": 1.0,
            "levels": 5, "size": 10, "ref": 40, "ext": 60, "late": True,
            "outcome": "filled",
        })
        trade_id = store.insert_trade({
            "signal_id": signal_id, "market": "T", "event": "E", "series": "S",
            "dir": 1 if side == "yes" else -1, "side": side, "entry_ts": 10.0,
            "entry_px": entry_px, "size": 10.0, "cap": 58.0, "notional": 5.0,
        })
        pos = Position(
            trade_id, signal_id, "T", "E", "S",
            1 if side == "yes" else -1, side, entry_px, 10.0, 40.0, 60.0,
        )
        pos.entry_ts = 10.0
        return pos

    def test_rising_bid_updates_high_falling_does_not(self):
        desk = PaperDesk(lambda *_: None, realistic=False)
        pos = self.open_position()
        desk.positions[pos.tid] = pos
        desk.on_book("T", held_book(yes_bid=52.0, yes_ask=54.0))
        desk.on_book("T", held_book(yes_bid=57.0, yes_ask=59.0))
        first_ts = pos.max_executable_bid_ts
        desk.on_book("T", held_book(yes_bid=55.0, yes_ask=57.0))
        self.assertEqual(pos.max_executable_bid, 57.0)
        self.assertEqual(pos.max_executable_bid_ts, first_ts)
        self.assertEqual(pos.mfe_c, 7.0)
        row = store.q("SELECT max_executable_bid, mfe_c FROM trades WHERE id=?", (pos.tid,))[0]
        self.assertEqual(row["max_executable_bid"], 57.0)
        self.assertEqual(row["mfe_c"], 7.0)

    def test_equal_high_keeps_first_timestamp(self):
        desk = PaperDesk(lambda *_: None, realistic=False)
        pos = self.open_position()
        desk.positions[pos.tid] = pos
        desk.on_book("T", held_book(yes_bid=60.0, yes_ask=62.0))
        first = pos.max_executable_bid_ts
        time.sleep(0.01)
        desk.on_book("T", held_book(yes_bid=60.0, yes_ask=62.0))
        self.assertEqual(pos.max_executable_bid_ts, first)

    def test_no_side_uses_no_bid_not_yes_ask(self):
        desk = PaperDesk(lambda *_: None, realistic=False)
        pos = self.open_position(side="no", entry_px=40.0)
        desk.positions[pos.tid] = pos
        book = held_book(yes_bid=58.0, no_bid=45.0)
        desk.on_book("T", book)
        self.assertEqual(pos.max_executable_bid, 45.0)
        self.assertEqual(pos.mfe_c, 5.0)

    def test_ask_mid_last_and_settlement_cannot_update_high(self):
        desk = PaperDesk(lambda *_: None, realistic=False)
        pos = self.open_position()
        desk.positions[pos.tid] = pos
        desk.on_book("T", held_book(yes_bid=51.0, yes_ask=80.0))
        self.assertEqual(pos.max_executable_bid, 51.0)
        desk.settle_market("T", "yes")
        self.assertEqual(pos.max_executable_bid, 51.0)

    def test_high_below_entry_is_visible_with_zero_mfe(self):
        desk = PaperDesk(lambda *_: None, realistic=False)
        pos = self.open_position(entry_px=50.0)
        desk.positions[pos.tid] = pos
        desk.on_book("T", held_book(yes_bid=47.0, yes_ask=49.0))
        self.assertEqual(pos.max_executable_bid, 47.0)
        self.assertEqual(pos.mfe_c, 0.0)
        row = store.q("SELECT max_executable_bid, mfe_c FROM trades WHERE id=?", (pos.tid,))[0]
        self.assertEqual(row["max_executable_bid"], 47.0)
        self.assertEqual(row["mfe_c"], 0.0)

    def test_no_quote_leaves_high_null(self):
        pos = self.open_position()
        row = store.q("SELECT max_executable_bid FROM trades WHERE id=?", (pos.tid,))[0]
        self.assertIsNone(row["max_executable_bid"])

    def test_restart_restores_stored_high_and_continues(self):
        desk = PaperDesk(lambda *_: None, realistic=True)
        pos = self.open_position()
        desk.positions[pos.tid] = pos
        desk.on_book("T", held_book(yes_bid=61.0, yes_ask=63.0))
        restored = PaperDesk(lambda *_: None, realistic=True)
        restored.restore_open_positions(store.load_open_paper_positions())
        loaded = restored.positions[pos.tid]
        self.assertEqual(loaded.max_executable_bid, 61.0)
        restored.on_book("T", held_book(yes_bid=66.0, yes_ask=68.0))
        self.assertEqual(loaded.max_executable_bid, 66.0)


class LatencyReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()

    def tearDown(self):
        store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def test_per_kind_sampling_is_not_crowded_out_by_feed_rows(self):
        for i in range(50):
            store.add_latency("feed_lag", 10.0 + i)
        store.add_latency("order_arrival", 180.0)
        store.add_latency("order_arrival", 190.0)
        summary = store.latency_kind_summary("order_arrival_ms", limit=10)
        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["p95"], 190.0)
        feed = store.latency_kind_summary("feed_ingress_ms", limit=10)
        self.assertEqual(feed["n"], 10)

    def test_negative_and_non_finite_values_are_quarantined(self):
        store.add_latency("decision_ms", -5)
        store.add_latency("decision_ms", float("nan"))
        store.add_latency("decision_ms", float("inf"))
        store.add_latency("decision_ms", 12.0)
        summary = store.latency_kind_summary("decision_ms")
        self.assertEqual(summary["n"], 1)
        self.assertEqual(summary["p50"], 12.0)
        self.assertGreaterEqual(summary["invalid"], 3)
        self.assertFalse(math.isnan(summary["p50"]))

    def test_collecting_stale_and_breach_states(self):
        now = time.time()
        for _ in range(5):
            store.add_latency("order_arrival_ms", 100.0)
        collecting = store.latency_kind_summary("order_arrival_ms", now=now)
        self.assertEqual(collecting["state"], "COLLECTING")
        with patch("app.store.LATENCY_MIN_SAMPLES", 2):
            for _ in range(20):
                store.add_latency("order_arrival_ms", 400.0)
            breach = store.latency_kind_summary("order_arrival_ms", now=now)
            self.assertEqual(breach["state"], "BREACH")
            stale = store.latency_kind_summary(
                "order_arrival_ms", now=now + store.LATENCY_STALE_AFTER_S + 10,
            )
            self.assertEqual(stale["state"], "STALE")


if __name__ == "__main__":
    unittest.main()
