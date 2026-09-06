"""The trades API must say how the parent market RESOLVED, not only what was paid.

Trades 112, 113 and 114 of 2026-09-05 are all correct: the match finished a
draw, 112 held YES on the Draw leg and 113/114 held NO on "Miami wins", and a
draw resolves "Miami wins" as NO -- so all three were paid 100.  Nothing about
the settlement logic is wrong, and none of it is touched here.

What was wrong is that the card rendered "Miami wins" next to "100" with no
statement of the side held or of how the market resolved, and the operator who
designed the bot read his own ledger as Miami having won.  The API could not
have supported a correct card either: it never exposed `markets.result`, so the
dashboard had nothing to explain the payout with.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import main, store


ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


class TradeResolutionApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("app.store.config.DATA_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

        engine_patch = patch.object(main, "engine", SimpleNamespace(
            desk=SimpleNamespace(positions={}, pos_dict=lambda *a: {}),
            clock_tracker=None, watched_events=set(), mode="live",
            meta={}, event_markets={},
        ))
        engine_patch.start()
        self.addCleanup(engine_patch.stop)

        # The real shape: a draw, a YES holder on the Draw leg and a NO holder
        # on the team leg, both paid 100, plus a legacy row with no result.
        for ticker, leg in (("KXMIA-TIE", "Draw"), ("KXMIA-MIA", "Inter Miami"),
                            ("KXOLD-OLD", "Legacy Home")):
            store.upsert_market(ticker, "KXMIA", "KXMLSGAME", f"{leg} wins",
                                "2026-09-05T22:00:00Z", "settled",
                                "Inter Miami vs Seattle", leg)
        store.record_market_result("KXMIA-TIE", "yes", 1_772_325_700.0)
        store.record_market_result("KXMIA-MIA", "no", 1_772_325_700.0)

        self.ids = {}
        for ticker, side in (("KXMIA-TIE", "yes"), ("KXMIA-MIA", "no"),
                             ("KXOLD-OLD", "no")):
            self.ids[ticker] = self.seed(ticker, side)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def seed(self, ticker, side):
        signal_id = store.insert_signal({
            "ts_ms": 1, "local_ts": 1000.0, "market": ticker, "event": "KXMIA",
            "series": "KXMLSGAME", "dir": 1 if side == "yes" else -1, "dl": 1.0,
            "levels": 5, "size": 10.0, "ref": 40.0, "ext": 60.0,
            "outcome": "filled", "detail": {},
        })
        trade_id = store.insert_trade({
            "signal_id": signal_id, "market": ticker, "event": "KXMIA",
            "series": "KXMLSGAME", "dir": 1 if side == "yes" else -1,
            "side": side, "entry_ts": 1000.0, "entry_px": 30.0, "size": 100.0,
            "cap": 58.0, "notional": 100.0,
        })
        store.close_trade(trade_id, 100.0, "settle", 70.0, 1.0, 69.0, 0.0, None)
        return trade_id

    def trades(self):
        payload = run(main.trades(mode="live"))
        return {row["market"]: row for row in payload["closed"]}

    def test_the_trades_api_exposes_the_parent_markets_resolution(self):
        rows = self.trades()

        self.assertEqual(rows["KXMIA-MIA"]["side"], "no")
        self.assertEqual(rows["KXMIA-MIA"]["market_result"], "no",
                         "a draw resolves 'Miami wins' as NO")
        self.assertEqual(rows["KXMIA-MIA"]["market_settled_ts"], 1_772_325_700.0)

        self.assertEqual(rows["KXMIA-TIE"]["side"], "yes")
        self.assertEqual(rows["KXMIA-TIE"]["market_result"], "yes")

        # Both were paid 100, which is exactly why the side and the resolution
        # have to be stated: the payout alone does not say which way round it was.
        for ticker in ("KXMIA-MIA", "KXMIA-TIE"):
            self.assertEqual(rows[ticker]["exit_px"], 100.0)
            self.assertEqual(rows[ticker]["exit_reason"], "settle")

    def test_an_unrecorded_resolution_is_reported_as_null_not_inferred(self):
        row = self.trades()["KXOLD-OLD"]

        self.assertIn("market_result", row, "the field must exist to be reported")
        self.assertIsNone(row["market_result"])
        self.assertIsNone(row["market_settled_ts"])

    def test_a_market_row_that_does_not_exist_does_not_break_the_listing(self):
        store.ex("DELETE FROM markets WHERE ticker=?", ("KXOLD-OLD",))
        row = self.trades()["KXOLD-OLD"]

        self.assertIsNone(row["market_result"])
        self.assertIsNone(row["market_status"])

    def test_the_resolution_lookup_is_one_query_for_the_whole_page(self):
        """Per-row lookups over 500 trades is the shape that already cost this
        dashboard a quadratic refresh once; see idx_trades_signal_id."""
        real = store.q
        seen = []

        def counting(sql, args=()):
            seen.append(sql)
            return real(sql, args)

        with patch.object(store, "q", counting):
            self.trades()

        market_queries = [sql for sql in seen if "FROM markets" in sql]
        self.assertEqual(len(market_queries), 1, market_queries)


class SettlementWordingSourceTests(unittest.TestCase):
    """The wording itself, asserted against the shipped file."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "app.js").read_text()

    def test_the_side_is_stated_in_words_that_cannot_be_read_backwards(self):
        for token in ("Betting ON: ", "Betting AGAINST: ",
                      "positionWording", "positionCallout", "resolutionMeaning"):
            self.assertIn(token, self.js)

    def test_a_settled_card_explains_the_payout_from_the_stored_resolution(self):
        self.assertIn("trade.market_result", self.js)
        self.assertIn("Market resolved ${upper}", self.js)
        self.assertIn("${upper} pays 100", self.js)
        self.assertIn("did not win", self.js)
        self.assertIn("the match was a draw", self.js)

    def test_an_unknown_resolution_is_never_inferred_from_the_payout(self):
        self.assertIn("Market resolution not recorded", self.js)
        block = self.js.split("function settlementBlock", 1)[1].split("\nfunction ", 1)[0]
        unknown = block.split("Market resolution not recorded", 1)[1].split("</div>", 1)[0]
        self.assertIn("rather than inferred", unknown)


if __name__ == "__main__":
    unittest.main()
