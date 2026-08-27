import unittest
from unittest.mock import AsyncMock, patch

from app.kalshi import KalshiWS


class LifecycleSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def make_ws(self):
        with patch("app.kalshi._load_private_key", return_value=None):
            ws = KalshiWS(lambda *args: None)
        ws.connected = True
        ws._subscribed = {"A"}
        ws._orderbook_sid = 1
        ws._trade_sid = 2
        ws._lifecycle_sid = 3
        ws._send = AsyncMock(return_value=1)
        return ws

    async def test_dynamic_market_add_updates_lifecycle_stream(self):
        ws = self.make_ws()

        await ws.set_markets({"A", "B"})

        calls = [call.args for call in ws._send.await_args_list]
        self.assertEqual(calls, [
            ("update_subscription", {
                "sid": 1, "action": "add_markets", "market_tickers": ["B"],
            }),
            ("update_subscription", {
                "sid": 2, "action": "add_markets", "market_tickers": ["B"],
            }),
            ("update_subscription", {
                "sid": 3, "action": "add_markets", "market_tickers": ["B"],
            }),
        ])

    async def test_empty_market_set_unsubscribes_lifecycle_stream(self):
        ws = self.make_ws()

        await ws.set_markets(set())

        calls = [call.args for call in ws._send.await_args_list]
        self.assertEqual(calls, [
            ("unsubscribe", {"sids": [1]}),
            ("unsubscribe", {"sids": [2]}),
            ("unsubscribe", {"sids": [3]}),
        ])
        self.assertIsNone(ws._lifecycle_sid)

    async def test_lifecycle_ack_is_recorded(self):
        ws = self.make_ws()
        ws._lifecycle_sid = None

        ws._record_subscription("market_lifecycle_v2", 17)

        self.assertEqual(ws._lifecycle_sid, 17)


if __name__ == "__main__":
    unittest.main()
