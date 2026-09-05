"""B5: the path budget must not be spent in arrival order.

A flat 4,000-row cap is a budget consumed fastest by the busiest markets, so
the first live study recorded samples=3999 for every La Liga trade and signal
and the intended 300 s forward window collapsed to 60-130 s exactly where
activity was highest.  Time-based thinning spends the budget on time instead:
every change for the first `PATH_THIN_AFTER_S`, then one row per
`PATH_THIN_INTERVAL_MS`, with every new peak and trough always recorded.

These tests pin that the extremes survive, that the hard backstop still holds,
that terminal semantics are unchanged, and that `store.bid_path_summary` still
answers correctly on a thinned path.
"""
import unittest
from unittest.mock import patch

from app import config, store
from app.books import Book
from app.engine import Engine
from app.paper import PaperDesk, path_thins


def book_at(bid, size=10.0):
    book = Book()
    book.yes_bids = {float(bid): float(size)}
    book.ok = True
    return book


def position(entry_ts=0.0):
    class Pos:
        pass
    pos = Pos()
    pos.tid, pos.signal_id = 1, 2
    pos.event, pos.market, pos.side, pos.strategy = "EV", "T", "yes", "gate_a"
    pos.entry_ts, pos.entry_px, pos.remaining = entry_ts, 50.0, 1.0
    pos.exec_path, pos.exec_path_last, pos.exec_path_dropped = [], None, 0
    pos.exec_path_total, pos.exec_path_flush_failed = 0, False
    pos.max_executable_bid = pos.max_executable_bid_ts = pos.mfe_c = None
    pos.high_dirty = False
    pos.path_peak = pos.path_trough = None
    pos.path_last_written, pos.exec_path_thinned = 0.0, 0
    return pos


def desk():
    instance = PaperDesk.__new__(PaperDesk)
    instance._report_error = lambda *a, **k: None
    instance._safe_log = lambda *a, **k: None
    return instance


class ThinningRuleTests(unittest.TestCase):
    def test_nothing_is_thinned_inside_the_early_window(self):
        self.assertFalse(path_thins(1.0, 50.0, 40.0, 30.0, 100.0, 99.99))

    def test_a_repeat_inside_the_interval_is_thinned_after_the_window(self):
        self.assertTrue(path_thins(30.0, 35.0, 40.0, 30.0, 100.0, 99.99))

    def test_a_new_peak_or_trough_is_never_thinned(self):
        self.assertFalse(path_thins(30.0, 41.0, 40.0, 30.0, 100.0, 99.99))
        self.assertFalse(path_thins(30.0, 29.0, 40.0, 30.0, 100.0, 99.99))

    def test_an_unpriced_observation_is_never_thinned(self):
        self.assertTrue(path_thins(30.0, 35.0, 40.0, 30.0, 100.0, 99.99))
        self.assertFalse(path_thins(30.0, None, 40.0, 30.0, 100.0, 99.99))

    def test_past_the_interval_the_row_is_written(self):
        self.assertFalse(path_thins(30.0, 35.0, 40.0, 30.0, 100.0, 99.0))

    def test_the_knobs_are_not_strategy_parameters(self):
        """Collection cannot change a trading decision."""
        self.assertNotIn("PATH_THIN_AFTER_S", config.STRATEGY_PARAM_NAMES)
        self.assertNotIn("PATH_THIN_INTERVAL_MS", config.STRATEGY_PARAM_NAMES)
        before = config.config_id()
        with patch.object(config, "PATH_THIN_INTERVAL_MS", 999.0):
            self.assertEqual(config.config_id(), before)


class ExecutionPathThinningTests(unittest.TestCase):
    def record(self, ticks):
        instance, pos = desk(), position()
        for now, bid in ticks:
            instance._record_exec_path(pos, book_at(bid), float(bid), now)
        return pos

    def test_every_change_is_kept_inside_the_first_ten_seconds(self):
        ticks = [(i * 0.01, 50 + (i % 5)) for i in range(1, 400)]
        pos = self.record(ticks)

        self.assertEqual(pos.exec_path_thinned, 0)
        # Only consecutive-duplicate quotes are dropped, as before.
        expected = sum(1 for i in range(1, len(ticks))
                       if ticks[i][1] != ticks[i - 1][1]) + 1
        self.assertEqual(len(pos.exec_path), expected)

    def test_after_the_window_the_rate_is_bounded_but_extremes_survive(self):
        ticks = [(10.0, 50.0), (10.01, 51.0), (10.02, 50.5)]
        # A long chop strictly inside the range already seen, at 10 ms.
        ticks += [(10.03 + i * 0.01, 50.0 + (i % 2) * 0.5) for i in range(200)]
        # Then a genuine new peak and a genuine new trough, also at 10 ms.
        ticks.append((12.2, 99.0))
        ticks.append((12.21, 1.0))
        pos = self.record(ticks)

        bids = [row["bid"] for row in pos.exec_path]
        self.assertIn(99.0, bids, "a new peak must survive thinning")
        self.assertIn(1.0, bids, "a new trough must survive thinning")
        self.assertGreater(pos.exec_path_thinned, 100)
        # ~2.2 s of chop at one row per 250 ms, plus the opening rows and the
        # two extremes: far fewer than the 200 observations offered.
        self.assertLess(len(pos.exec_path), 30)

    def test_thinning_never_counts_as_truncation(self):
        """`dropped_samples`/`truncated` keep meaning 'the cap bit'."""
        ticks = [(10.0, 50.0), (10.01, 51.0)]
        ticks += [(10.02 + i * 0.01, 50.5) for i in range(50)]
        pos = self.record(ticks)

        self.assertGreater(pos.exec_path_thinned, 0)
        self.assertEqual(pos.exec_path_dropped, 0)

    def test_the_hard_backstop_still_bounds_the_path(self):
        instance, pos = desk(), position()
        # One second apart, so thinning never applies and only the cap can bite.
        for tick in range(store.BID_PATH_MAX_SAMPLES + 250):
            bid = float(1 + tick % 99)
            instance._record_exec_path(pos, book_at(bid), bid, float(tick))
        instance._record_exec_terminal(pos, 55.0, 99_999.0)

        self.assertLessEqual(pos.exec_path_total, store.BID_PATH_MAX_SAMPLES)
        self.assertGreater(pos.exec_path_dropped, 0)
        self.assertEqual(sum(1 for row in pos.exec_path if row["terminal"]), 1)

    def test_the_reserved_terminal_slot_survives_thinning(self):
        instance, pos = desk(), position()
        for tick in range(2000):
            instance._record_exec_path(pos, book_at(50.0 + tick % 3),
                                       50.0 + tick % 3, 10.0 + tick * 0.001)
        instance._record_exec_terminal(pos, 55.0, 100.0)

        terminal = pos.exec_path[-1]
        self.assertEqual(terminal["availability"], "terminal")
        self.assertEqual(terminal["terminal"], 1)
        self.assertIsNone(terminal["bid"])
        self.assertLessEqual(pos.exec_path_total, store.BID_PATH_MAX_SAMPLES)

    def test_a_quote_resuming_after_an_outage_is_always_recorded(self):
        instance, pos = desk(), position()
        instance._record_exec_path(pos, book_at(50.0), 50.0, 1.0)
        instance._record_exec_path(pos, book_at(52.0), 52.0, 2.0)
        instance._record_exec_path(pos, book_at(48.0), 48.0, 3.0)
        instance._record_exec_gap(pos, 30.0)
        # Well inside the thinning interval, and strictly inside the range.
        instance._record_exec_path(pos, book_at(50.0), 50.0, 30.01)

        self.assertEqual(pos.exec_path[-1]["bid"], 50.0)
        self.assertEqual(pos.exec_path[-2]["availability"], "gap")

    def test_the_summary_still_reads_correctly_on_a_thinned_path(self):
        ticks = [(0.5, 50.0), (1.0, 60.0)]
        ticks += [(10.5 + i * 0.01, 55.0 + (i % 2)) for i in range(100)]
        ticks.append((12.0, 70.0))
        ticks.append((12.5, 40.0))
        pos = self.record(ticks)

        summary = store.bid_path_summary(
            pos.exec_path, truncated=bool(pos.exec_path_dropped),
            dropped_samples=pos.exec_path_dropped,
        )

        self.assertEqual(summary["peak_bid"], 70.0)
        self.assertEqual(summary["trough_bid"], 40.0)
        self.assertEqual(summary["first_bid"], 50.0)
        self.assertEqual(summary["last_bid"], 40.0)
        self.assertFalse(summary["truncated"])
        self.assertEqual(summary["dropped_samples"], 0)
        self.assertEqual(summary["gap_count"], 0)
        self.assertGreater(summary["path_travelled_c"], 0)


class SignalPathThinningTests(unittest.TestCase):
    def engine(self):
        engine = Engine.__new__(Engine)
        engine._signal_paths = []
        return engine

    def watch(self, anchor=0.0):
        return {
            "signal_id": 1, "market": "T", "event": "EV", "side": "yes",
            "strategy": "gate_a", "anchor_ts": anchor, "expires_at": 1e18,
            "outcome": "unconfirmed", "last": None, "rows": [], "dropped": 0,
            "total": 0, "peak": None, "trough": None, "last_written": 0.0,
            "thinned": 0,
        }

    def test_a_forward_watch_keeps_every_peak_and_trough(self):
        engine = self.engine()
        watch = self.watch()
        engine._signal_paths.append(watch)

        engine._record_signal_paths("T", book_at(50.0), 0.5)
        engine._record_signal_paths("T", book_at(55.0), 1.0)
        for index in range(200):
            bid = 51.0 + (index % 2)
            engine._record_signal_paths("T", book_at(bid), 11.0 + index * 0.01)
        engine._record_signal_paths("T", book_at(90.0), 13.5)
        engine._record_signal_paths("T", book_at(10.0), 13.6)

        bids = [row["bid"] for row in watch["rows"]]
        self.assertIn(90.0, bids)
        self.assertIn(10.0, bids)
        # The 200 chop observations reach at most one row per 250 ms; the rest
        # are split between the durable-signature dedupe that already existed
        # and the new thinning.
        self.assertGreater(watch["thinned"], 50)
        self.assertLess(len(watch["rows"]), 30)
        self.assertEqual(watch["dropped"], 0)
        self.assertEqual(watch["total"], len(watch["rows"]))

    def test_the_backstop_and_terminal_slot_are_unchanged(self):
        engine = self.engine()
        watch = self.watch()
        watch["total"] = store.BID_PATH_MAX_SAMPLES - 1
        engine._signal_paths.append(watch)

        engine._record_signal_paths("T", book_at(50.0), 0.5)

        self.assertEqual(watch["rows"], [], "the reserved slot must stay free")
        self.assertEqual(watch["dropped"], 1)


if __name__ == "__main__":
    unittest.main()
