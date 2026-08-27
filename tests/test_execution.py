import unittest
from unittest.mock import patch

from app.books import Book
from app.execution import ShadowBook, ShadowBooks
from app.paper import PaperDesk, Position


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
        shadows.ensure("TICKER", book)
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


class PaperDeskV2Tests(unittest.TestCase):
    @patch("app.paper.store.log_event")
    @patch("app.paper.store.add_latency")
    @patch("app.paper.store.insert_trade", return_value=9)
    def test_entry_uses_arrival_book_and_real_signal_id(self, insert_trade, _lat, _log):
        results = []
        desk = PaperDesk(lambda msg: None, lambda *args: results.append(args), realistic=True)
        book = live_book(no={55.0: 2.0, 50.0: 3.0})
        sig = {"ticker": "T", "dir": 1, "ref": 40.0, "ext": 60.0}
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
        self.assertEqual(insert_trade.call_args.args[0]["signal_id"], 42)
        self.assertEqual(book.no_bids, {55.0: 2.0, 50.0: 3.0})

    @patch("app.paper.store.log_event")
    @patch("app.paper.store.add_latency")
    @patch("app.paper.store.close_trade")
    def test_partial_exit_keeps_remainder_until_new_depth_arrives(self, close_trade, _lat, _log):
        desk = PaperDesk(lambda msg: None, realistic=True)
        pos = Position(7, 42, "T", "E", "S", 1, "yes", 45.0, 4.0, 40.0, 60.0)
        desk.positions[pos.tid] = pos
        book = live_book(yes={60.0: 2.0, 55.0: 1.0})
        desk.pending_exits[pos.tid] = (0.0, 100.0, "timeout", 0.0)

        desk.process_pending({"T": book}, now_mono=1.0, now_wall=100.1)

        self.assertAlmostEqual(pos.remaining, 1.0)
        close_trade.assert_not_called()
        self.assertEqual(book.yes_bids, {60.0: 2.0, 55.0: 1.0})

        book.yes_bids[50.0] = 1.0
        desk.pending_exits[pos.tid] = (0.0, 100.1, "timeout", 0.0)
        desk.process_pending({"T": book}, now_mono=2.0, now_wall=100.2)

        self.assertNotIn(pos.tid, desk.positions)
        self.assertAlmostEqual(close_trade.call_args.args[1], 56.25)


if __name__ == "__main__":
    unittest.main()
