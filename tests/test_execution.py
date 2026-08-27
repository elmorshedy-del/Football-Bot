import unittest
from unittest.mock import patch

from app.books import Book
from app.execution import ShadowBook, ShadowBooks
from app.paper import PaperDesk, PendingExit, Position, level_fees


def live_book(yes=None, no=None):
    book = Book()
    book.yes_bids = dict(yes or {})
    book.no_bids = dict(no or {})
    book.ok = True
    return book


class ShadowBookTests(unittest.TestCase):
    def test_buy_walks_multiple_levels_and_computes_vwap(self):
        shadow = ShadowBook.from_live(live_book(no={55.0: 2.0, 50.0: 3.0}))

        fill = shadow.buy("yes", notional_usd=2.40, price_cap=50.0)

        self.assertAlmostEqual(fill.quantity, 5.0)
        self.assertAlmostEqual(fill.notional, 2.40)
        self.assertAlmostEqual(fill.vwap, 48.0)
        self.assertTrue(fill.complete)
        self.assertEqual(fill.levels, [(45.0, 2.0), (50.0, 3.0)])

    def test_second_order_cannot_reuse_consumed_depth(self):
        shadow = ShadowBook.from_live(live_book(no={55.0: 2.0}))

        first = shadow.buy("yes", notional_usd=0.90, price_cap=50.0)
        second = shadow.buy("yes", notional_usd=0.90, price_cap=50.0)

        self.assertAlmostEqual(first.quantity, 2.0)
        self.assertEqual(second.quantity, 0.0)

    def test_exchange_delta_advances_counterfactual_shadow(self):
        shadows = ShadowBooks()
        book = live_book(no={55.0: 2.0})
        shadow = shadows.ensure("TICKER", book)
        shadow.buy("yes", notional_usd=0.90, price_cap=50.0)

        book.no_bids[55.0] = 6.0
        book.last_seq = 12
        shadows.apply_delta("TICKER", {
            "side": "no", "price_dollars": "0.55", "delta_fp": "4",
        }, 12)
        refill = shadow.buy("yes", notional_usd=1.80, price_cap=50.0)

        self.assertAlmostEqual(refill.quantity, 4.0)
        self.assertEqual(shadow.seq, 12)

    def test_sell_walks_bids_and_reports_partial_fill(self):
        shadow = ShadowBook.from_live(live_book(yes={60.0: 2.0, 55.0: 1.0}))

        fill = shadow.sell("yes", quantity=4.0)

        self.assertAlmostEqual(fill.quantity, 3.0)
        self.assertAlmostEqual(fill.vwap, (60.0 * 2.0 + 55.0) / 3.0)
        self.assertFalse(fill.complete)
        self.assertEqual(fill.levels, [(60.0, 2.0), (55.0, 1.0)])

    def test_live_book_is_never_mutated(self):
        book = live_book(no={55.0: 2.0})
        shadow = ShadowBook.from_live(book)

        shadow.buy("yes", notional_usd=0.90, price_cap=50.0)

        self.assertEqual(book.no_bids, {55.0: 2.0})

    def test_remove_and_readd_between_polls_restores_new_depth(self):
        shadows = ShadowBooks()
        shadow = shadows.ensure("TICKER", live_book(no={55.0: 2.0}))
        shadow.buy("yes", notional_usd=0.90, price_cap=50.0)

        shadows.apply_delta("TICKER", {
            "side": "no", "price_dollars": "0.55", "delta_fp": "-2",
        }, 2)
        shadows.apply_delta("TICKER", {
            "side": "no", "price_dollars": "0.55", "delta_fp": "2",
        }, 3)

        refill = shadow.buy("yes", notional_usd=0.90, price_cap=50.0)
        self.assertAlmostEqual(refill.quantity, 2.0)

    def test_recovery_snapshot_preserves_counterfactual_depletion(self):
        shadows = ShadowBooks()
        book = live_book(no={55.0: 2.0})
        shadow = shadows.ensure("TICKER", book)
        shadow.buy("yes", notional_usd=0.90, price_cap=50.0)
        shadows.invalidate(["TICKER"])

        shadows.reset("TICKER", book)

        self.assertTrue(shadow.ok)
        self.assertEqual(shadow.no_bids, {})

    def test_fee_is_calculated_at_each_fill_price(self):
        fees = level_fees([(45.0, 2.0), (50.0, 3.0)])

        self.assertEqual(fees, [(45.0, 2.0, 0.04), (50.0, 3.0, 0.06)])
        self.assertEqual(
            level_fees([(45.0, 2.0), (50.0, 3.0)], "quadratic", 2.0),
            [(45.0, 2.0, 0.07), (50.0, 3.0, 0.11)],
        )


class PaperDeskV2Tests(unittest.TestCase):
    def test_restart_restores_partial_position_state(self):
        desk = PaperDesk(lambda msg: None, realistic=True)
        restored = desk.restore_open_positions([{
            "id": 7, "signal_id": 42, "market": "T", "event": "E", "series": "S",
            "dir": 1, "side": "yes", "entry_px": 45.0, "size": 4.0,
            "entry_ts": 100.0, "remaining": 1.5, "realized_gross": 0.3,
            "entry_fees": 0.08, "accrued_fees": 0.12, "exit_qty": 2.5,
            "exit_vwap_num": 150.0, "mae": 5.0, "shadow_stop_px": 35.0,
            "ref": 40.0, "ext": 60.0,
        }])

        pos = desk.positions[7]
        self.assertEqual(restored, 1)
        self.assertEqual(pos.remaining, 1.5)
        self.assertEqual(pos.entry_fees, 0.08)
        self.assertAlmostEqual(pos.exit_fees, 0.04)
        self.assertEqual(pos.shadow_stop_hit_px, 35.0)

    @patch("app.paper.store.log_event")
    @patch("app.paper.store.open_paper_trade", return_value=9)
    def test_entry_uses_arrival_book_and_real_signal_id(self, open_trade, _log):
        results = []
        desk = PaperDesk(lambda msg: None, lambda *args: results.append(args), realistic=True)
        book = live_book(no={55.0: 2.0, 50.0: 3.0})
        sig = {"ticker": "T", "dir": 1, "ref": 40.0, "ext": 60.0, "ts_ms": 99900}
        meta = {"event": "E", "series": "S"}

        with patch("app.paper.config.PAPER_ENTRY_LATENCY_MS", 100.0), \
                patch("app.paper.config.NOTIONAL_USD", 2.40), \
                patch("app.paper.config.PRICE_CAP", 50.0):
            desk.queue_enter(42, sig, meta, now_mono=10.0, now_wall=100.0)
            desk.process_pending({"T": book}, now_mono=10.05, now_wall=100.05)
            self.assertFalse(desk.positions)
            desk.process_pending({"T": book}, now_mono=10.10, now_wall=100.10)

        self.assertEqual(results[0][0], 42)
        self.assertEqual(results[0][2], "filled")
        self.assertEqual(desk.positions[9].signal_id, 42)
        self.assertEqual(open_trade.call_args.args[0]["signal_id"], 42)
        self.assertAlmostEqual(open_trade.call_args.args[-1], 200.0)
        self.assertEqual(book.no_bids, {55.0: 2.0, 50.0: 3.0})

    @patch("app.paper.store.log_event")
    @patch("app.paper.store.record_paper_exit")
    def test_partial_exit_retries_until_new_depth_arrives(self, record_exit, _log):
        desk = PaperDesk(lambda msg: None, realistic=True)
        pos = Position(7, 42, "T", "E", "S", 1, "yes", 45.0, 4.0, 40.0, 60.0)
        pos.entry_fees = 0.08
        desk.positions[pos.tid] = pos
        book = live_book(yes={60.0: 2.0, 55.0: 1.0})
        desk.pending_exits[pos.tid] = PendingExit(0.0, 100.0, "timeout", 0.0)

        with patch("app.paper.config.PAPER_EXIT_LATENCY_MS", 100.0):
            desk.process_pending({"T": book}, now_mono=1.0, now_wall=100.1)

        self.assertAlmostEqual(pos.remaining, 1.0)
        self.assertIn(pos.tid, desk.pending_exits)
        self.assertEqual(book.yes_bids, {60.0: 2.0, 55.0: 1.0})

        book.yes_bids[50.0] = 1.0
        desk.apply_book_delta("T", {
            "side": "yes", "price_dollars": "0.50", "delta_fp": "1",
        }, 2)
        desk.process_pending({"T": book}, now_mono=2.0, now_wall=100.2)

        self.assertNotIn(pos.tid, desk.positions)
        self.assertNotIn(pos.tid, desk.pending_exits)
        self.assertEqual(record_exit.call_count, 2)
        final = record_exit.call_args.args[-1]
        self.assertAlmostEqual(final["exit_px"], 56.25)

    @patch("app.paper.store.log_event")
    @patch("app.paper.store.open_paper_trade", side_effect=RuntimeError("db unavailable"))
    def test_entry_db_failure_keeps_pending_and_restores_depth(self, _open, _log):
        desk = PaperDesk(lambda msg: None, realistic=True)
        book = live_book(no={55.0: 2.0})
        sig = {"ticker": "T", "dir": 1, "ref": 40.0, "ext": 60.0}
        desk.queue_enter(42, sig, {"event": "E", "series": "S"}, 0.0, 100.0)

        with patch("app.paper.config.PAPER_ENTRY_LATENCY_MS", 0.0), \
                patch("app.paper.config.NOTIONAL_USD", 0.90), \
                patch("app.paper.config.PRICE_CAP", 50.0):
            desk.process_pending({"T": book}, now_mono=1.0, now_wall=100.1)

        self.assertEqual(len(desk.pending_entries), 1)
        self.assertFalse(desk.positions)
        self.assertEqual(desk.shadows.ensure("T", book).no_bids, {55.0: 2.0})

    @patch("app.paper.store.finish_paper_signal")
    def test_unknown_live_fee_schedule_rejects_instead_of_guessing(self, finish_signal):
        results = []
        desk = PaperDesk(lambda msg: None, lambda *args: results.append(args), realistic=True)
        book = live_book(no={55.0: 2.0})
        sig = {"ticker": "T", "dir": 1, "ref": 40.0, "ext": 60.0}
        meta = {"event": "E", "series": "S", "fee_type": None, "fee_multiplier": None}
        desk.queue_enter(42, sig, meta, 0.0, 100.0)

        with patch("app.paper.config.NOTIONAL_USD", 0.90), \
                patch("app.paper.config.PRICE_CAP", 50.0):
            desk.process_pending({"T": book}, now_mono=1.0, now_wall=100.1)

        self.assertEqual(results[0][2], "unsupported_fee")
        self.assertFalse(desk.pending_entries)
        self.assertEqual(desk.shadows.ensure("T", book).no_bids, {55.0: 2.0})
        self.assertEqual(finish_signal.call_args.args[1], "unsupported_fee")

    @patch("app.paper.store.log_event")
    @patch("app.paper.store.record_paper_exit", side_effect=RuntimeError("db unavailable"))
    def test_exit_db_failure_keeps_position_order_and_depth(self, _record, _log):
        desk = PaperDesk(lambda msg: None, realistic=True)
        pos = Position(7, 42, "T", "E", "S", 1, "yes", 45.0, 2.0, 40.0, 60.0)
        desk.positions[pos.tid] = pos
        book = live_book(yes={60.0: 2.0})
        desk.pending_exits[pos.tid] = PendingExit(0.0, 100.0, "flatten", 0.0)

        desk.process_pending({"T": book}, now_mono=1.0, now_wall=100.1)

        self.assertEqual(pos.remaining, 2.0)
        self.assertIn(pos.tid, desk.pending_exits)
        self.assertEqual(desk.shadows.ensure("T", book).yes_bids, {60.0: 2.0})

    @patch("app.paper.store.record_paper_exit")
    def test_flatten_with_no_depth_remains_queued(self, record_exit):
        desk = PaperDesk(lambda msg: None, realistic=True)
        pos = Position(7, 42, "T", "E", "S", 1, "yes", 45.0, 2.0, 40.0, 60.0)
        desk.positions[pos.tid] = pos
        book = live_book()
        desk.pending_exits[pos.tid] = PendingExit(0.0, 100.0, "flatten", 0.0)

        desk.process_pending({"T": book}, now_mono=1.0, now_wall=100.1)

        self.assertIn(pos.tid, desk.pending_exits)
        self.assertEqual(desk.pending_exits[pos.tid].attempts, 1)
        record_exit.assert_not_called()

    def test_stop_requeues_even_after_first_trigger_was_recorded(self):
        desk = PaperDesk(lambda msg: None, realistic=True)
        pos = Position(7, 42, "T", "E", "S", 1, "yes", 45.0, 2.0, 40.0, 60.0)
        pos.shadow_stop_hit_px = 50.0
        desk.positions[pos.tid] = pos
        book = live_book(yes={50.0: 1.0})

        with patch("app.paper.config.USE_STOP", True):
            desk.on_book("T", book)

        self.assertEqual(desk.pending_exits[pos.tid].reason, "stop")


if __name__ == "__main__":
    unittest.main()
