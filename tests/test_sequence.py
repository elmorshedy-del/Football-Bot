import unittest
from unittest.mock import AsyncMock, patch

from app.books import Book
from app.kalshi import KalshiWS
from app.sequence import SubscriptionSequenceTracker


class SubscriptionSequenceTrackerTests(unittest.TestCase):
    def test_interleaved_markets_share_one_subscription_sequence(self):
        tracker = SubscriptionSequenceTracker()

        self.assertEqual(tracker.track(7, 100), "ok")  # snapshot: market A
        self.assertEqual(tracker.track(7, 101), "ok")  # snapshot: market B
        self.assertEqual(tracker.track(7, 102), "ok")  # delta: market A

    def test_duplicate_is_dropped_without_advancing(self):
        tracker = SubscriptionSequenceTracker()

        self.assertEqual(tracker.track(3, 10), "ok")
        self.assertEqual(tracker.track(3, 10), "duplicate")
        self.assertEqual(tracker.track(3, 11), "ok")

    def test_forward_gap_and_reset_both_require_recovery(self):
        tracker = SubscriptionSequenceTracker()

        self.assertEqual(tracker.track(4, 20), "ok")
        self.assertEqual(tracker.track(4, 23), "gap")
        self.assertEqual(tracker.track(4, 1), "gap")


class BookSequenceBoundaryTests(unittest.TestCase):
    def test_live_router_can_apply_interleaved_subscription_sequences(self):
        book = Book()
        book.apply_snapshot({"yes_dollars_fp": [["0.40", "10"]]}, 100)

        applied = book.apply_delta(
            {"side": "yes", "price_dollars": "0.40", "delta_fp": "2"},
            102,
            sequence_validated=True,
        )

        self.assertTrue(applied)
        self.assertTrue(book.ok)
        self.assertEqual(book.yes_bids[40.0], 12.0)

    def test_legacy_isolated_book_check_remains_available(self):
        book = Book()
        book.apply_snapshot({"yes_dollars_fp": [["0.40", "10"]]}, 100)

        applied = book.apply_delta(
            {"side": "yes", "price_dollars": "0.40", "delta_fp": "2"}, 102
        )

        self.assertFalse(applied)
        self.assertFalse(book.ok)


class KalshiWSRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.forwarded = []

    def make_ws(self):
        with patch("app.kalshi._load_private_key", return_value=None):
            ws = KalshiWS(lambda *args: self.forwarded.append(args[0]))
        ws.connected = True
        ws._subscribed = {"A", "B"}
        ws._orderbook_sid = 7
        ws._send = AsyncMock(return_value=1)
        return ws

    async def test_gap_uses_non_mutating_snapshot_recovery(self):
        ws = self.make_ws()
        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 100,
            "msg": {"market_ticker": "A"},
        }))

        accepted = await ws._accept_orderbook_frame({
            "type": "orderbook_delta", "sid": 7, "seq": 103,
            "msg": {"market_ticker": "A"},
        })

        self.assertFalse(accepted)
        self.assertEqual(self.forwarded[0]["type"], "orderbook_gap")
        ws._send.assert_awaited_once_with("update_subscription", {
            "sid": 7,
            "action": "get_snapshot",
            "market_tickers": ["A", "B"],
        })
        self.assertNotIn("unsubscribe", [call.args[0] for call in ws._send.await_args_list])
        self.assertEqual(ws._orderbook_sid, 7)

    async def test_each_market_resumes_only_after_its_fresh_snapshot(self):
        ws = self.make_ws()
        await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 10,
            "msg": {"market_ticker": "A"},
        })
        await ws._accept_orderbook_frame({
            "type": "orderbook_delta", "sid": 7, "seq": 12,
            "msg": {"market_ticker": "A"},
        })

        self.assertFalse(await ws._accept_orderbook_frame({
            "type": "orderbook_delta", "sid": 7, "seq": 13,
            "msg": {"market_ticker": "A"},
        }))
        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 14,
            "msg": {"market_ticker": "A"},
        }))
        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_delta", "sid": 7, "seq": 15,
            "msg": {"market_ticker": "A"},
        }))
        self.assertFalse(await ws._accept_orderbook_frame({
            "type": "orderbook_delta", "sid": 7, "seq": 16,
            "msg": {"market_ticker": "B"},
        }))
        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 17,
            "msg": {"market_ticker": "B"},
        }))
        self.assertNotIn(7, ws._recovering_orderbooks)

    async def test_market_reconciliation_removes_deleted_recovery_targets(self):
        ws = self.make_ws()
        await ws._recover_orderbook(7, 11, 12)
        ws._send.reset_mock()

        await ws.set_markets({"A"})

        self.assertEqual(ws._recovering_orderbooks[7], {"A"})
        ws._send.assert_awaited_once_with("update_subscription", {
            "sid": 7,
            "action": "delete_markets",
            "market_tickers": ["B"],
        })

    async def test_stale_sid_is_ignored(self):
        ws = self.make_ws()

        accepted = await ws._accept_orderbook_frame({
            "type": "orderbook_delta", "sid": 6, "seq": 1,
            "msg": {"market_ticker": "A"},
        })

        self.assertFalse(accepted)
        ws._send.assert_not_awaited()

    async def test_snapshot_before_subscription_ack_is_accepted(self):
        ws = self.make_ws()
        ws._orderbook_sid = None

        accepted = await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 9, "seq": 1,
            "msg": {"market_ticker": "A"},
        })

        self.assertTrue(accepted)

    async def test_unsubscribe_clears_sequence_for_reused_sid(self):
        ws = self.make_ws()
        ws._sequences.track(7, 500)

        await ws.set_markets(set())
        ws._subscribed = {"A"}
        ws._orderbook_sid = 7

        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 1,
            "msg": {"market_ticker": "A"},
        }))


if __name__ == "__main__":
    unittest.main()
