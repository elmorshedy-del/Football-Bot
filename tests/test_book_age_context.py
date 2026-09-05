"""B4: a paper fill must state how old the book it consumed was.

A raw L2 replay of 2026-09-04 20:00-22:00 (1.9 M frames, 78 Gate-A candidates)
found the reconstructed book's best ask worse than a real same-side executed
price for 29 of 29 candidates, median +12c, while this process ran 5.6 s median
behind the exchange across 18 sequence gaps and 8 reconnects.  The same 29
trades priced at -$862.96, -$228.72 or +$550.33 depending only on how entry was
modelled.  None of that is decidable unless every fill records the age of the
depth it walked, so these tests pin the age fields, the exit trigger values, and
the opt-in `PAPER_MAX_BOOK_AGE_MS` bound (default 0 = record only).
"""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import config, store
from app.books import Book
from app.execution import ShadowBook
from app.paper import PaperDesk, PendingEntry, Position


def snapshot_book(yes_bids=(), no_bids=(), ts_ms=None, arrival_wall=None):
    book = Book()
    msg = {
        "yes_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in yes_bids],
        "no_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in no_bids],
    }
    if ts_ms is not None:
        msg["ts_ms"] = ts_ms
    book.apply_snapshot(msg, 1, arrival_wall=arrival_wall)
    return book


class BookStampTests(unittest.TestCase):
    def test_a_delta_carries_the_exchange_stamp_and_the_arrival_wall(self):
        book = snapshot_book(yes_bids=[(40, 10)], ts_ms=1_000, arrival_wall=100.0)
        self.assertEqual(book.last_exchange_ts_ms, 1_000.0)
        self.assertEqual(book.last_arrival_wall, 100.0)

        book.apply_delta(
            {"price_dollars": "0.41", "delta_fp": "5", "side": "yes",
             "ts_ms": 2_000},
            2, sequence_validated=True, arrival_wall=105.0,
        )

        self.assertEqual(book.last_exchange_ts_ms, 2_000.0)
        self.assertEqual(book.last_arrival_wall, 105.0)

    def test_a_frame_without_an_exchange_stamp_leaves_the_stamp_alone(self):
        """A missing provider timestamp stays missing; it is never invented."""
        book = snapshot_book(yes_bids=[(40, 10)], arrival_wall=100.0)
        self.assertIsNone(book.last_exchange_ts_ms)

        book.apply_delta(
            {"price_dollars": "0.41", "delta_fp": "5", "side": "yes"},
            2, sequence_validated=True, arrival_wall=101.0,
        )

        self.assertIsNone(book.last_exchange_ts_ms)
        self.assertEqual(book.last_arrival_wall, 101.0)

    def test_the_shadow_mirrors_the_age_through_reset_and_delta(self):
        book = snapshot_book(yes_bids=[(40, 10)], ts_ms=1_000, arrival_wall=100.0)
        shadow = ShadowBook.from_live(book)

        self.assertEqual(shadow.last_exchange_ts_ms, 1_000.0)
        self.assertEqual(shadow.last_arrival_wall, 100.0)

        shadow.apply_delta(
            {"price_dollars": "0.41", "delta_fp": "5", "side": "yes",
             "ts_ms": 3_000},
            2, arrival_wall=110.0,
        )

        self.assertEqual(shadow.last_exchange_ts_ms, 3_000.0)
        self.assertEqual(shadow.last_arrival_wall, 110.0)


class EntryContextTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(config, "DATA_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        if store._conn is not None:
            store._conn.close()
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def signal(self, sid=1):
        return store.insert_signal({
            "ts_ms": 1_000, "local_ts": 1.0, "market": "T", "event": "E",
            "series": "S", "dir": 1, "dl": 1.0, "levels": 6, "size": 300.0,
            "ref": 40.0, "ext": 55.0, "outcome": "queued", "detail": {},
        })

    def desk(self, realistic, feed_state=None):
        return PaperDesk(Mock(), Mock(), realistic=realistic,
                         error_result=Mock(), feed_state=feed_state)

    def sig(self):
        return {"ticker": "T", "ts_ms": 1_000, "dir": 1, "ref": 40.0,
                "ext": 55.0, "strategy": "gate_a"}

    def meta(self):
        return {"event": "E", "series": "S", "fee_type": "quadratic",
                "fee_multiplier": 1.0}

    def enter_v2(self, book, now_wall, feed_state=None):
        desk = self.desk(True, feed_state=feed_state)
        sid = self.signal()
        pending = PendingEntry(signal_id=sid, sig=self.sig(), meta=self.meta(),
                               queued_wall=now_wall - 0.15,
                               due_mono=0.0)
        desk.pending_entries.append(pending)
        desk.apply_book_snapshot("T", book)
        desk.process_pending({"T": book}, now_mono=1.0, now_wall=now_wall)
        return desk, sid

    def entry_context(self):
        row = store.q("SELECT entry_context FROM trades ORDER BY id DESC")[0]
        return json.loads(row["entry_context"])

    def test_the_realistic_entry_records_the_age_of_the_book_it_filled_from(self):
        now = 1_000.0
        book = snapshot_book(
            yes_bids=[(40, 500)], no_bids=[(55, 500)],
            ts_ms=(now - 5.6) * 1000.0, arrival_wall=now - 2.0,
        )
        desk, _sid = self.enter_v2(book, now, feed_state=lambda: {
            "feed_lag_ms": 5600.0, "backlog": 42,
        })

        self.assertEqual(len(desk.positions), 1)
        context = self.entry_context()
        self.assertAlmostEqual(context["book_age_ms"], 2000.0, places=1)
        self.assertAlmostEqual(context["book_exchange_lag_ms"], 5600.0, places=1)
        self.assertAlmostEqual(context["book_exchange_ts_ms"],
                               (now - 5.6) * 1000.0, places=1)
        self.assertEqual(context["feed_lag_ms"], 5600.0)
        self.assertEqual(context["backlog"], 42)
        self.assertEqual(context["max_book_age_ms"], 0.0)

    def test_the_original_paper_path_records_the_same_fields(self):
        desk = self.desk(False)
        sid = self.signal()
        book = snapshot_book(yes_bids=[(40, 500)], no_bids=[(55, 500)],
                             ts_ms=1_000.0, arrival_wall=None)

        with patch("app.paper.store.log_event"):
            outcome = desk.try_enter(sid, self.sig(), self.meta(), book)

        self.assertEqual(outcome, "filled")
        context = self.entry_context()
        self.assertIn("book_age_ms", context)
        self.assertIn("book_exchange_lag_ms", context)
        self.assertEqual(context["book_exchange_ts_ms"], 1_000.0)

    def test_an_unstamped_book_reports_the_age_as_unknown_not_as_zero(self):
        book = snapshot_book(yes_bids=[(40, 500)], no_bids=[(55, 500)])
        book.last_arrival_wall = None
        desk = self.desk(False)
        sid = self.signal()

        with patch("app.paper.store.log_event"):
            desk.try_enter(sid, self.sig(), self.meta(), book)

        context = self.entry_context()
        self.assertIsNone(context["book_age_ms"])
        self.assertEqual(context["book_age_unknown"], "no_arrival_stamp")

    # ---- PAPER_MAX_BOOK_AGE_MS ---------------------------------------

    def test_zero_disables_the_bound_entirely(self):
        now = 1_000.0
        book = snapshot_book(yes_bids=[(40, 500)], no_bids=[(55, 500)],
                             ts_ms=1.0, arrival_wall=now - 600.0)

        with patch.object(config, "PAPER_MAX_BOOK_AGE_MS", 0.0):
            desk, _sid = self.enter_v2(book, now)

        self.assertEqual(len(desk.positions), 1, "a 600 s old book still filled")
        self.assertEqual(
            store.q("SELECT outcome FROM signals ORDER BY id DESC")[0]["outcome"],
            "filled")

    def test_above_the_bound_the_entry_is_refused_as_stale_book(self):
        now = 1_000.0
        book = snapshot_book(yes_bids=[(40, 500)], no_bids=[(55, 500)],
                             ts_ms=1.0, arrival_wall=now - 16.0)

        with patch.object(config, "PAPER_MAX_BOOK_AGE_MS", 5_000.0):
            desk, _sid = self.enter_v2(book, now)

        self.assertEqual(desk.positions, {})
        row = store.q("SELECT outcome, detail FROM signals ORDER BY id DESC")[0]
        self.assertEqual(row["outcome"], "stale_book")
        detail = json.loads(row["detail"])
        self.assertAlmostEqual(
            detail["entry_context"]["book_age_ms"], 16_000.0, places=1)

    def test_below_the_bound_the_entry_still_fills(self):
        now = 1_000.0
        book = snapshot_book(yes_bids=[(40, 500)], no_bids=[(55, 500)],
                             ts_ms=1.0, arrival_wall=now - 0.5)

        with patch.object(config, "PAPER_MAX_BOOK_AGE_MS", 5_000.0):
            desk, _sid = self.enter_v2(book, now)

        self.assertEqual(len(desk.positions), 1)

    def test_an_unknown_age_never_refuses(self):
        """Unknown is not evidence of staleness; it must not change admission."""
        book = snapshot_book(yes_bids=[(40, 500)], no_bids=[(55, 500)])
        book.last_arrival_wall = None
        desk = self.desk(False)
        sid = self.signal()

        with patch.object(config, "PAPER_MAX_BOOK_AGE_MS", 1.0), \
                patch("app.paper.store.log_event"):
            outcome = desk.try_enter(sid, self.sig(), self.meta(), book)

        self.assertEqual(outcome, "filled")

    def test_both_paper_paths_agree_on_the_bound(self):
        book = snapshot_book(yes_bids=[(40, 500)], no_bids=[(55, 500)],
                             ts_ms=1.0, arrival_wall=1.0)
        desk = self.desk(False)
        sid = self.signal()

        with patch.object(config, "PAPER_MAX_BOOK_AGE_MS", 1.0), \
                patch("app.paper.store.log_event"):
            outcome = desk.try_enter(sid, self.sig(), self.meta(), book)

        self.assertEqual(outcome, "stale_book")

    def test_stale_book_is_counted_as_a_confirmed_outcome(self):
        """It must not silently shrink the K2 denominator, exactly like no_book."""
        for outcome in ("filled", "no_book", "stale_book", "unconfirmed"):
            store.insert_signal({
                "ts_ms": 1, "local_ts": 1.0, "market": "T", "event": "E",
                "series": "S", "dir": 1, "dl": 1.0, "levels": 6, "size": 300.0,
                "ref": 40.0, "ext": 55.0, "outcome": outcome,
                "detail": {"strategy": "gate_a"},
            })

        summary = store.stats()["sleeves"]["gate_a"]
        self.assertEqual(summary["signals"]["stale_book"], 1)
        # filled + no_book + stale_book, never `unconfirmed`.
        self.assertEqual(summary["evidence"]["k2_ci"]["n_signals"], 3)

    def test_the_knob_is_part_of_the_strategy_identity(self):
        self.assertIn("PAPER_MAX_BOOK_AGE_MS", config.STRATEGY_PARAM_NAMES)
        before = config.config_id()
        with patch.object(config, "PAPER_MAX_BOOK_AGE_MS", 5_000.0):
            self.assertNotEqual(config.config_id(), before)


class ExitContextTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(config, "DATA_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        if store._conn is not None:
            store._conn.close()
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def open_position(self, desk, sleeve=None):
        sid = store.insert_signal({
            "ts_ms": 1_000, "local_ts": 1.0, "market": "T", "event": "E",
            "series": "S", "dir": 1, "dl": 1.0, "levels": 6, "size": 300.0,
            "ref": 40.0, "ext": 55.0, "outcome": "filled", "detail": {},
        })
        tid = store.insert_trade({
            "signal_id": sid, "market": "T", "event": "E", "series": "S",
            "dir": 1, "side": "yes", "entry_ts": 1.0, "entry_px": 45.0,
            "size": 100.0, "cap": 58.0, "notional": 100.0,
        })
        pos = Position(tid, sid, "T", "E", "S", 1, "yes", 45.0, 100.0, 40.0,
                       55.0, sleeve=sleeve)
        pos.entry_ts = 1_000.0
        pos.entry_fees = 1.0
        desk.positions[tid] = pos
        return pos

    def exit_context(self, tid):
        row = store.q("SELECT exit_context FROM trades WHERE id=?", (tid,))[0]
        return json.loads(row["exit_context"])

    def test_a_close_records_the_trigger_values_behind_the_label(self):
        desk = PaperDesk(Mock(), realistic=False, error_result=Mock())
        pos = self.open_position(desk)
        pos.peak_bid = 61.0
        book = snapshot_book(yes_bids=[(50, 20), (49, 30)],
                             no_bids=[(46, 40)], ts_ms=900_000.0,
                             arrival_wall=1_179.0)

        with patch("app.paper.store.log_event"):
            desk.close(pos, 50.0, "timeout", book=book, bid=50.0, now=1_180.0)

        context = self.exit_context(pos.tid)
        trigger = context["exit_trigger"]
        self.assertEqual(trigger["reason"], "timeout")
        self.assertEqual(trigger["observed_bid"], 50.0)
        self.assertEqual(trigger["elapsed_s"], 180.0)
        self.assertEqual(trigger["peak_bid"], 61.0)
        self.assertEqual(context["book_source"], "fill")
        self.assertAlmostEqual(context["book_age_ms"], 1_000.0, places=1)

    def test_the_exit_records_the_held_and_opposite_side_top_eight(self):
        desk = PaperDesk(Mock(), realistic=False, error_result=Mock())
        pos = self.open_position(desk)
        book = snapshot_book(
            yes_bids=[(50 - i, 10 + i) for i in range(10)],
            no_bids=[(46 - i, 20 + i) for i in range(10)],
            arrival_wall=1_180.0,
        )

        with patch("app.paper.store.log_event"):
            desk.close(pos, 50.0, "timeout", book=book, bid=50.0, now=1_180.0)

        depth = self.exit_context(pos.tid)["book"]
        self.assertEqual(depth["held_side"], "yes")
        self.assertEqual(len(depth["held"]), 8)
        self.assertEqual(len(depth["opposite"]), 8)
        self.assertEqual(depth["held"][0][0], 50.0)
        self.assertEqual(depth["opposite"][0][0], 46.0)

    def test_a_sleeve_exit_records_the_computed_scratch_level(self):
        desk = PaperDesk(Mock(), realistic=True, error_result=Mock())
        pos = self.open_position(desk, sleeve={"strategy": "price_only"})
        pos.sleeve_anchor_bid = 52.0
        pos.peak_bid = 58.0

        desk._queue_exit(pos, "sleeve_scratch", 0.0, bid=47.0, now=1_010.0)

        trigger = desk.pending_exits[pos.tid].trigger
        self.assertEqual(trigger["reason"], "sleeve_scratch")
        self.assertEqual(trigger["observed_bid"], 47.0)
        self.assertEqual(trigger["elapsed_s"], 10.0)
        self.assertEqual(trigger["anchor_bid"], 52.0)
        self.assertIsInstance(trigger["scratch_c"], float)
        self.assertGreater(trigger["scratch_c"], pos.entry_px)

    def test_a_timeout_with_no_book_in_hand_falls_back_and_says_so(self):
        """The dominant exit reason must not be the one with no book evidence."""
        desk = PaperDesk(Mock(), realistic=False, error_result=Mock())
        pos = self.open_position(desk)
        pos.best_bid = 44.0
        book = snapshot_book(yes_bids=[(44, 10)], no_bids=[(50, 10)],
                             arrival_wall=1_179.5)
        desk.on_book("T", book)

        with patch("app.paper.store.log_event"), \
                patch("app.paper.time.time", return_value=1_500.0):
            desk.check_timeouts()

        context = self.exit_context(pos.tid)
        self.assertEqual(context["book_source"], "last_seen")
        self.assertEqual(context["exit_trigger"]["reason"], "timeout")
        self.assertEqual(context["exit_trigger"]["observed_bid"], 44.0)
        self.assertIsNotNone(context["book"])

    def test_a_settlement_records_its_own_exit_context(self):
        desk = PaperDesk(Mock(), realistic=False, error_result=Mock())
        pos = self.open_position(desk)

        with patch("app.paper.store.log_event"):
            desk.settle_market("T", "yes")

        context = self.exit_context(pos.tid)
        self.assertEqual(context["exit_trigger"]["reason"], "settle")
        self.assertIn("book_age_ms", context)

    def test_the_position_bid_path_shape_is_untouched(self):
        """H1: four exit decisions read it; new state lives elsewhere."""
        pos = Position(1, 1, "T", "E", "S", 1, "yes", 45.0, 10.0, 40.0, 55.0)
        self.assertEqual(pos.bid_path.maxlen, 240)
        self.assertEqual(list(pos.bid_path), [])


if __name__ == "__main__":
    unittest.main()
