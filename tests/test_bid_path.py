"""Execution-path collection: the change-log a scalar high cannot replace."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import store
from app.books import Book

ROOT = Path(__file__).resolve().parents[1]


def _book(yes_levels=(), no_levels=()):
    book = Book()
    book.yes_bids = {float(p): float(s) for p, s in yes_levels}
    book.no_bids = {float(p): float(s) for p, s in no_levels}
    book.ok = True
    return book


def _position(tid=1, signal_id=2, side="yes", remaining=10.0, entry_px=50.0):
    """A minimal stand-in carrying every field the recorder touches."""
    class Pos:
        pass
    pos = Pos()
    pos.tid, pos.signal_id = tid, signal_id
    pos.event, pos.market = "KXGAME", "KXGAME-YES"
    pos.side, pos.strategy = side, "gate_a"
    pos.entry_ts, pos.entry_px, pos.remaining = 0.0, entry_px, remaining
    pos.exec_path, pos.exec_path_last, pos.exec_path_dropped = [], None, 0
    pos.exec_path_total, pos.exec_path_flush_failed = 0, False
    pos.max_executable_bid = pos.max_executable_bid_ts = pos.mfe_c = None
    pos.high_dirty = False
    return pos


def _desk():
    from app import paper
    desk = paper.PaperDesk.__new__(paper.PaperDesk)
    desk._report_error = lambda *a, **k: None
    desk._safe_log = lambda *a, **k: None
    return desk


class BidPathStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("app.store.config.DATA_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store._conn = None
        store.init()

    def sample(self, dt_ms, bid, size=10.0, exec_px=None, trade_id=1):
        return {
            "kind": "position", "trade_id": trade_id, "signal_id": 7,
            "event": "KXGAME", "market": "KXGAME-YES", "side": "yes",
            "strategy": "price_only_late_score", "anchor_ts": 1000.0,
            "dt_ms": dt_ms, "bid": bid, "bid_size": size,
            "exec_px": exec_px, "qty": 10.0,
        }

    def test_batched_insert_writes_every_sample_once(self):
        rows = [self.sample(i * 100.0, 50.0 + i) for i in range(5)]
        self.assertEqual(store.insert_bid_path(rows), 5)
        stored = store.bid_path_for_trade(1)
        self.assertEqual(len(stored), 5)
        self.assertEqual([row["bid"] for row in stored], [50.0, 51.0, 52.0, 53.0, 54.0])
        # dt_ms ordering is what lets partial flushes reassemble after restart.
        self.assertEqual([row["dt_ms"] for row in stored], sorted(row["dt_ms"] for row in stored))

    def test_empty_flush_is_a_noop(self):
        self.assertEqual(store.insert_bid_path([]), 0)

    def test_summary_distinguishes_a_spike_from_a_plateau(self):
        """Identical peak, opposite conclusion — the whole reason for the path."""
        spike = [self.sample(0.0, 50.0), self.sample(100.0, 90.0), self.sample(200.0, 50.0)]
        plateau = [self.sample(0.0, 50.0), self.sample(100.0, 90.0),
                   self.sample(12_000.0, 90.0), self.sample(12_100.0, 50.0)]
        spike_summary = store.bid_path_summary(spike)
        plateau_summary = store.bid_path_summary(plateau)
        self.assertEqual(spike_summary["peak_bid"], plateau_summary["peak_bid"])
        self.assertLess(spike_summary["ms_at_peak"], 200.0)
        self.assertGreaterEqual(plateau_summary["ms_at_peak"], 11_000.0)

    def test_summary_reports_path_efficiency_and_excursions(self):
        chop = [self.sample(0.0, 50.0), self.sample(100.0, 60.0),
                self.sample(200.0, 50.0), self.sample(300.0, 60.0),
                self.sample(400.0, 50.0)]
        summary = store.bid_path_summary(chop)
        self.assertEqual(summary["peak_bid"], 60.0)
        self.assertEqual(summary["trough_bid"], 50.0)
        self.assertEqual(summary["displacement_c"], 0.0)
        self.assertEqual(summary["path_travelled_c"], 40.0)
        self.assertEqual(summary["path_efficiency"], 0.0)

    def test_summary_of_empty_or_unpriced_path_is_none(self):
        self.assertIsNone(store.bid_path_summary([]))
        self.assertIsNone(store.bid_path_summary(None))
        self.assertIsNone(store.bid_path_summary([{"dt_ms": 0.0, "bid": None}]))

    def test_decline_paths_are_queryable_by_signal(self):
        rows = [dict(self.sample(i * 100.0, 40.0 + i), kind="decline", trade_id=None)
                for i in range(3)]
        store.insert_bid_path(rows)
        self.assertEqual(len(store.bid_path_for_signal(7)), 3)
        # A position path must not leak into the decline query.
        store.insert_bid_path([self.sample(0.0, 99.0)])
        self.assertEqual(len(store.bid_path_for_signal(7)), 3)

    def test_migration_is_idempotent(self):
        store.init()
        store.init()
        store.insert_bid_path([self.sample(0.0, 50.0)])
        self.assertEqual(len(store.bid_path_for_trade(1)), 1)


class ExecPathRecordingTests(unittest.TestCase):
    """The recorder must capture depth, not just price."""

    def test_vwap_is_only_reported_when_the_held_size_is_fillable(self):
        from app import paper
        desk, pos = _desk(), _position(remaining=10.0)

        # Enough depth across two levels to fill 10.
        desk._record_exec_path(pos, _book(yes_levels=((90, 4), (89, 8))), 90.0, 1.0)
        self.assertEqual(len(pos.exec_path), 1)
        sample = pos.exec_path[0]
        self.assertEqual(sample["bid"], 90.0)
        self.assertEqual(sample["bid_size"], 4.0)
        # 4@90 + 6@89 = 89.4, not the 90 a scalar high would claim.
        self.assertAlmostEqual(sample["exec_px"], 89.4, places=4)

        # Thin book: the peak is not fillable in size, so no VWAP is invented.
        pos.exec_path, pos.exec_path_last = [], None
        desk._record_exec_path(pos, _book(yes_levels=((90, 1),)), 90.0, 1.0)
        self.assertIsNone(pos.exec_path[0]["exec_px"])
        self.assertEqual(pos.exec_path[0]["bid_size"], 1.0)

    def test_unchanged_quotes_do_not_grow_the_path(self):
        from app import paper
        desk, pos = _desk(), _position(remaining=10.0)
        book = _book(yes_levels=((90, 40),))
        for _ in range(10):
            desk._record_exec_path(pos, book, 90.0, 1.0)
        self.assertEqual(len(pos.exec_path), 1)
        # A real move appends.
        desk._record_exec_path(pos, _book(yes_levels=((91, 40),)), 91.0, 2.0)
        self.assertEqual(len(pos.exec_path), 2)


class HotPathWriteCostTests(unittest.TestCase):
    """Collection must not add synchronous commits to the asyncio hot path.

    store.ex() commits per statement on the event loop, so a naive per-quote
    write would add an fsync to every book update and make the K4 order-arrival
    latency worse.  Samples buffer in memory and flush in batches instead.
    """

    def test_recording_quotes_performs_no_database_writes(self):
        from app import paper
        desk, pos = _desk(), _position(remaining=10.0)
        with patch("app.store.insert_bid_path") as writer:
            for tick in range(paper.BID_PATH_FLUSH_EVERY - 1):
                desk._record_exec_path(
                    pos, _book(yes_levels=((50 + tick % 30, 40),)),
                    float(50 + tick % 30), float(tick),
                )
            writer.assert_not_called()
        self.assertGreater(len(pos.exec_path), 0)

    def test_buffer_flushes_in_batches_not_per_quote(self):
        from app import paper
        desk, pos = _desk(), _position(remaining=10.0)
        quotes = paper.BID_PATH_FLUSH_EVERY * 3
        with patch("app.store.insert_bid_path") as writer:
            for tick in range(quotes):
                desk._record_exec_path(
                    pos, _book(yes_levels=((40 + tick % 50, 40),)),
                    float(40 + tick % 50), float(tick),
                )
            # Batched: a handful of writes for hundreds of quotes, never one each.
            self.assertLessEqual(writer.call_count, quotes // paper.BID_PATH_FLUSH_EVERY + 1)
            self.assertLess(writer.call_count, quotes / 10)


class ArchitectBlockerFourTests(unittest.TestCase):
    """Merge blocker 4: the path dataset must be trustworthy."""

    def test_sample_cap_survives_incremental_flushes(self):
        """Reproduced: 4,250 rows persisted against a 4,000 cap, 0 drops.

        The cap was checked against len(exec_path), but the buffer is emptied on
        every incremental flush, so the check reset each time.
        """
        from app import paper
        desk, pos = _desk(), _position(remaining=1.0)
        written = []
        with patch("app.store.insert_bid_path", side_effect=written.extend):
            for tick in range(store.BID_PATH_MAX_SAMPLES + 250):
                desk._record_exec_path(
                    pos, _book(yes_levels=((1 + tick % 99, 10),)),
                    float(1 + tick % 99), float(tick),
                )
        self.assertLessEqual(len(written), store.BID_PATH_MAX_SAMPLES)
        self.assertGreater(pos.exec_path_dropped, 0, "truncation must be reported")

    def test_a_failed_write_does_not_lose_buffered_rows(self):
        desk, pos = _desk(), _position(remaining=1.0)
        for tick in range(10):
            desk._record_exec_path(
                pos, _book(yes_levels=((50 + tick, 10),)), float(50 + tick), float(tick),
            )
        buffered = len(pos.exec_path)
        with patch("app.store.insert_bid_path", side_effect=RuntimeError("disk full")):
            desk._flush_exec_path(pos)
        self.assertEqual(len(pos.exec_path), buffered, "rows were dropped on a failed write")
        self.assertTrue(pos.exec_path_flush_failed)
        # A later successful flush drains them.
        with patch("app.store.insert_bid_path", return_value=buffered):
            desk._flush_exec_path(pos)
        self.assertEqual(pos.exec_path, [])

    def test_a_failed_high_write_is_retried(self):
        """Memory must not claim a high the database never accepted."""
        desk, pos = _desk(), _position()
        with patch("app.store.update_trade_high", side_effect=RuntimeError("disk full")):
            desk._observe_executable_high(pos, 90.0, now=1.0)
        self.assertIsNone(pos.max_executable_bid)
        self.assertTrue(pos.high_dirty)
        calls = []
        with patch("app.store.update_trade_high", side_effect=lambda *a: calls.append(a)):
            desk._observe_executable_high(pos, 88.0, now=2.0)
        self.assertTrue(calls, "a lower bid after a failed write must still be attempted")
        self.assertEqual(pos.max_executable_bid, 88.0)
        self.assertFalse(pos.high_dirty)

    def test_missing_bids_and_the_exit_are_recorded(self):
        """Gaps and a terminal sample keep time-at-peak honest."""
        desk, pos = _desk(), _position(remaining=1.0)
        desk._record_exec_path(pos, _book(yes_levels=((90, 10),)), 90.0, 1.0)
        desk._record_exec_gap(pos, 2.0)
        desk._record_exec_terminal(pos, 70.0, 3.0)
        bids = [row["bid"] for row in pos.exec_path]
        self.assertEqual(bids, [90.0, None, 70.0])
        self.assertEqual(pos.exec_path[-1]["dt_ms"], 3000.0)

    def test_list_endpoints_do_not_embed_full_paths(self):
        """/api/trades and /api/signals must not run a query per row."""
        source = (ROOT / "app" / "main.py").read_text()
        trades = source.split("async def trades(", 1)[1].split("@app.get", 1)[0]
        self.assertNotIn("bid_path_for_trade", trades)
        self.assertIn("bid_path_url", trades)
        signals = source.split("async def signals(", 1)[1].split("@app.get", 1)[0]
        self.assertNotIn("bid_path_for_signal", signals)
        self.assertIn("forward_path_url", signals)
        # The full path is available on demand instead.
        self.assertIn('"/api/trades/{trade_id}/path"', source)
        self.assertIn('"/api/signals/{signal_id}/path"', source)


if __name__ == "__main__":
    unittest.main()
